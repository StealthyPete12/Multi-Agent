"""Redis-backed distributed token bucket rate limiter.

Scoped per ``provider/model`` (e.g. ``anthropic:claude-haiku-4-5-20251001``)
so the Researcher's cheap "summary" calls and the Reviewer's "narrative"
calls to the same provider don't starve each other unless they actually
share a model, and so multiple process instances of the same agent share
one limit instead of each enforcing its own in-memory bucket.

Token bucket, not a fixed window: refills continuously
(``tokens_per_minute / 60000`` tokens per millisecond) rather than
resetting a counter at minute boundaries, so it doesn't allow a burst of
2x the limit right at a window edge.

Atomicity comes from a Lua script executed with ``EVAL`` — Redis runs the
whole script as one atomic operation, so concurrent callers across
multiple processes can't race on read-modify-write of the bucket state.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass

import redis.asyncio as redis

from shared import telemetry
from shared.logging import configure_logging

__all__ = ["RateLimiter", "RateLimitDecision", "get_rate_limiter", "get_redis_client"]

log = configure_logging(service_name="ratelimit")

_TOKEN_BUCKET_LUA = """
local bucket_key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill_rate_per_ms = tonumber(ARGV[2])
local now_ms = tonumber(ARGV[3])
local requested = tonumber(ARGV[4])

local bucket = redis.call('HMGET', bucket_key, 'tokens', 'ts')
local tokens = tonumber(bucket[1])
local ts = tonumber(bucket[2])

if tokens == nil then
    tokens = capacity
    ts = now_ms
end

local elapsed = math.max(0, now_ms - ts)
tokens = math.min(capacity, tokens + elapsed * refill_rate_per_ms)

local allowed = 0
local wait_ms = 0
if tokens >= requested then
    tokens = tokens - requested
    allowed = 1
else
    local deficit = requested - tokens
    wait_ms = math.ceil(deficit / refill_rate_per_ms)
end

redis.call('HMSET', bucket_key, 'tokens', tostring(tokens), 'ts', tostring(now_ms))
redis.call('PEXPIRE', bucket_key, 3600000)

return {allowed, tostring(tokens), wait_ms}
"""

DEFAULT_TOKENS_PER_MINUTE = int(os.environ.get("RATE_LIMIT_TOKENS_PER_MINUTE", "60"))
DEFAULT_MAX_WAIT_SECONDS = float(os.environ.get("RATE_LIMIT_MAX_WAIT_SECONDS", "60"))

_redis_client: redis.Redis | None = None


def get_redis_client() -> redis.Redis:
    """Process-wide Redis client, built from ``REDIS_URL``. Reused across
    callers rather than opening a new connection per rate-limit check."""
    global _redis_client
    if _redis_client is None:
        url = os.environ.get("REDIS_URL", "redis://:swarm_dev_password@localhost:6379/0")
        _redis_client = redis.from_url(url, decode_responses=True)
    return _redis_client


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    remaining_tokens: float
    wait_seconds: float
    key: str


class RateLimiter:
    """Distributed token bucket scoped by an arbitrary string key
    (typically ``"<provider>:<model>"``)."""

    def __init__(
        self,
        client: redis.Redis,
        *,
        tokens_per_minute: int,
        capacity: int | None = None,
        max_wait_seconds: float = DEFAULT_MAX_WAIT_SECONDS,
    ) -> None:
        self.client = client
        self.tokens_per_minute = tokens_per_minute
        self.capacity = capacity if capacity is not None else tokens_per_minute
        self.refill_rate_per_ms = tokens_per_minute / 60_000.0
        self.max_wait_seconds = max_wait_seconds
        self._script = client.register_script(_TOKEN_BUCKET_LUA)

    async def acquire(self, key: str, *, tokens: int = 1) -> RateLimitDecision:
        """Single, non-blocking attempt to take ``tokens`` from the bucket
        named ``key``. Never sleeps — see :meth:`wait_and_acquire` for the
        blocking variant callers actually want before an LLM call."""
        now_ms = int(time.time() * 1000)
        bucket_key = f"ratelimit:{key}"
        allowed, remaining, wait_ms = await self._script(
            keys=[bucket_key],
            args=[self.capacity, self.refill_rate_per_ms, now_ms, tokens],
        )
        decision = RateLimitDecision(
            allowed=bool(int(allowed)),
            remaining_tokens=float(remaining),
            wait_seconds=float(wait_ms) / 1000.0,
            key=key,
        )
        return decision

    async def wait_and_acquire(self, key: str, *, tokens: int = 1) -> RateLimitDecision:
        """Block (async-sleep) until ``tokens`` are available, up to
        ``max_wait_seconds``, then acquire. Raises ``TimeoutError`` if the
        bucket can't refill enough within that budget."""
        deadline = time.monotonic() + self.max_wait_seconds
        while True:
            decision = await self.acquire(key, tokens=tokens)
            if decision.allowed:
                return decision

            remaining_budget = deadline - time.monotonic()
            if remaining_budget <= 0:
                log.warning(
                    "rate limiter exhausted wait budget",
                    extra={"rate_limiter_delay": decision.wait_seconds, "key": key},
                )
                raise TimeoutError(f"rate limit wait budget exceeded for {key!r}")

            sleep_for = min(decision.wait_seconds, remaining_budget)
            log.info(
                "rate limiter delaying call",
                extra={"rate_limiter_delay": sleep_for, "key": key},
            )
            m = telemetry.get_metrics()
            m.rate_limit_delays.add(1, {"key": key})
            m.rate_limit_delay_seconds.record(sleep_for, {"key": key})
            await asyncio.sleep(sleep_for)


_limiters: dict[str, RateLimiter] = {}


def get_rate_limiter(provider: str, model: str) -> RateLimiter:
    """Process-wide, per-``provider:model`` :class:`RateLimiter`, built
    from ``RATE_LIMIT_TOKENS_PER_MINUTE`` (or a provider-specific
    ``RATE_LIMIT_<PROVIDER>_TPM`` override)."""
    scope = f"{provider}:{model}"
    if scope not in _limiters:
        override = os.environ.get(f"RATE_LIMIT_{provider.upper()}_TPM")
        tpm = int(override) if override else DEFAULT_TOKENS_PER_MINUTE
        _limiters[scope] = RateLimiter(get_redis_client(), tokens_per_minute=tpm)
    return _limiters[scope]
