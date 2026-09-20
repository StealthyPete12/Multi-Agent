"""Provider-agnostic LLM abstraction shared by every agent that needs a
model call (Researcher's semantic summary, Reviewer's narrative).

No caller outside this module knows which provider is in use — provider
selection happens entirely through environment variables
(``LLM_PROVIDER``, ``*_API_KEY``, ``OLLAMA_BASE_URL``, ``MODEL_SELECTIONS``)
and every provider implementation speaks the same :class:`LLMClient`
protocol and raises the same :class:`LLMError` on failure. Callers never
branch on provider identity.

The LLM only ever produces text (summaries, narratives). It never decides
risk scores — see ``agents/reviewer/scoring.py`` for the deterministic,
model-free scoring logic this is deliberately kept separate from.

Usage::

    from shared.llm import get_llm_client, LLMError

    client = get_llm_client(purpose="summary")
    try:
        response = await client.complete(
            system="You summarize commits in 2-3 sentences.",
            prompt="...",
            max_tokens=300,
        )
    except LLMError:
        response_text = ""  # degrade gracefully, never block the pipeline
"""

from __future__ import annotations

import asyncio
import os
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, TypeVar, runtime_checkable

import httpx
from opentelemetry.trace import SpanKind

from shared import telemetry
from shared.breaker import CircuitOpenError, get_circuit_breaker
from shared.errors import PoisonMessageError, RetryableError, classify_exception
from shared.logging import configure_logging
from shared.ratelimit import get_rate_limiter

__all__ = [
    "LLMClient",
    "LLMResponse",
    "LLMError",
    "AnthropicClient",
    "OpenAIClient",
    "OllamaClient",
    "NullLLMClient",
    "get_llm_client",
    "get_model_for",
    "DEFAULT_TIMEOUT_SECONDS",
]

log = configure_logging(service_name="llm")

DEFAULT_TIMEOUT_SECONDS = float(os.environ.get("LLM_TIMEOUT_SECONDS", "30"))
DEFAULT_MAX_RETRIES = int(os.environ.get("LLM_MAX_RETRIES", "3"))
DEFAULT_BACKOFF_BASE_SECONDS = float(os.environ.get("LLM_RETRY_BACKOFF_BASE_SECONDS", "1.0"))
DEFAULT_BACKOFF_MAX_SECONDS = float(os.environ.get("LLM_RETRY_BACKOFF_MAX_SECONDS", "20.0"))
DEFAULT_BREAKER_FAILURE_THRESHOLD = int(os.environ.get("LLM_BREAKER_FAILURE_THRESHOLD", "5"))
DEFAULT_BREAKER_OPEN_SECONDS = float(os.environ.get("LLM_BREAKER_OPEN_SECONDS", "60"))
RATE_LIMIT_ENABLED = os.environ.get("LLM_RATE_LIMIT_ENABLED", "true").strip().lower() not in (
    "false",
    "0",
    "",
)

_T = TypeVar("_T")

_DEFAULT_MODELS = {
    "anthropic": "claude-haiku-4-5-20251001",
    "openai": "gpt-4o-mini",
    "ollama": "llama3.1",
}


@dataclass(frozen=True)
class LLMResponse:
    """Standardized response shape, identical across every provider."""

    text: str
    model: str
    provider: str
    input_tokens: int
    output_tokens: int
    latency_ms: float
    finish_reason: str | None = None


class LLMError(RuntimeError):
    """Raised on any provider failure (timeout, HTTP error, malformed
    response, missing configuration). Callers catch this one type
    regardless of which provider is configured."""


@runtime_checkable
class LLMClient(Protocol):
    """Provider-agnostic completion interface. Every implementation in
    this module satisfies this protocol; nothing outside this module
    should implement it directly."""

    provider: str

    async def complete(
        self,
        *,
        system: str,
        prompt: str,
        max_tokens: int,
        temperature: float = 0.2,
    ) -> LLMResponse: ...


def get_model_for(purpose: str, provider: str) -> str:
    """Resolve the model to use for ``purpose`` (e.g. ``"summary"``,
    ``"narrative"``) on ``provider``.

    Reads ``MODEL_SELECTIONS``, a comma-separated ``purpose=model`` list
    (e.g. ``MODEL_SELECTIONS=summary=claude-haiku-4-5-20251001,narrative=claude-sonnet-5``),
    falling back to that provider's built-in default when ``purpose``
    isn't listed.
    """
    raw = os.environ.get("MODEL_SELECTIONS", "")
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        key, _, value = pair.partition("=")
        if key.strip() == purpose and value.strip():
            return value.strip()
    return _DEFAULT_MODELS.get(provider, _DEFAULT_MODELS["anthropic"])


class _BaseLLMClient:
    """Shared timeout/retry/rate-limit/circuit-breaker/logging plumbing
    for every real provider.

    Every provider's ``complete()`` builds one inner zero-arg async
    closure that performs the actual HTTP call and response parsing, then
    hands it to :meth:`_execute_with_resilience`, which is the single
    place backoff, jitter, error classification, rate limiting, and
    circuit breaking live — no per-provider duplication.
    """

    provider: str = "base"

    def __init__(
        self,
        *,
        model: str,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_backoff_seconds: float = DEFAULT_BACKOFF_BASE_SECONDS,
        retry_backoff_max_seconds: float = DEFAULT_BACKOFF_MAX_SECONDS,
        breaker_failure_threshold: int = DEFAULT_BREAKER_FAILURE_THRESHOLD,
        breaker_open_seconds: float = DEFAULT_BREAKER_OPEN_SECONDS,
        rate_limit_enabled: bool | None = None,
    ) -> None:
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.retry_backoff_seconds = retry_backoff_seconds
        self.retry_backoff_max_seconds = retry_backoff_max_seconds
        self.breaker_failure_threshold = breaker_failure_threshold
        self.breaker_open_seconds = breaker_open_seconds
        self.rate_limit_enabled = (
            rate_limit_enabled if rate_limit_enabled is not None else RATE_LIMIT_ENABLED
        )

    def _backoff_delay(self, attempt: int) -> float:
        """Exponential backoff with full jitter: a random delay in
        ``[0, min(base * 2**(attempt-1), max))``. Full jitter (rather than
        a fixed or half-jittered delay) avoids synchronized retry storms
        across multiple agent instances retrying the same provider outage
        at once."""
        ceiling = min(
            self.retry_backoff_seconds * (2 ** (attempt - 1)), self.retry_backoff_max_seconds
        )
        return random.uniform(0, ceiling)

    async def _acquire_rate_limit(self) -> None:
        if not self.rate_limit_enabled:
            return
        key = f"{self.provider}:{self.model}"
        try:
            limiter = get_rate_limiter(self.provider, self.model)
            await limiter.wait_and_acquire(key)
        except TimeoutError:
            raise
        except Exception as exc:  # Redis unreachable, etc. — degrade gracefully.
            log.warning(
                "rate limiter unavailable, proceeding without limiting",
                extra={"provider": self.provider, "model": self.model, "reason": str(exc)},
            )

    async def _execute_with_resilience(self, operation: Callable[[], Awaitable[_T]]) -> _T:
        """Run ``operation()`` (one full request+parse attempt) behind the
        rate limiter and circuit breaker, retrying with backoff+jitter on
        :class:`~shared.errors.RetryableError`-classified failures up to
        ``self.max_retries`` times. Raises :class:`LLMError` on the final
        failure (retryable-exhausted, poison, fatal, or an open circuit)
        so every caller keeps catching exactly one exception type.
        """
        breaker = get_circuit_breaker(
            self.provider,
            failure_threshold=self.breaker_failure_threshold,
            open_duration_seconds=self.breaker_open_seconds,
        )

        with telemetry.span(
            "llm.request",
            kind=SpanKind.CLIENT,
            tracer_name="shared.llm",
            attributes={"llm.provider": self.provider, "llm.model": self.model},
        ) as current_span:
            last_exc: Exception | None = None
            for attempt in range(1, self.max_retries + 2):
                try:
                    await self._acquire_rate_limit()
                except TimeoutError as exc:
                    telemetry.record_llm_failure(
                        provider=self.provider, model=self.model, reason="rate_limit_timeout"
                    )
                    raise LLMError(f"{self.provider} rate limit wait exceeded: {exc}") from exc

                try:
                    result = await breaker.call(operation)
                    current_span.set_attribute("llm.retry_count", attempt - 1)
                    current_span.set_attribute("llm.breaker_state", breaker.state.value)
                    telemetry.get_metrics().llm_calls.add(
                        1, {"provider": self.provider, "model": self.model, "outcome": "success"}
                    )
                    return result
                except CircuitOpenError as exc:
                    log.warning(
                        "llm call skipped: circuit open",
                        extra={
                            "provider": self.provider,
                            "model": self.model,
                            "circuit_state": breaker.state.value,
                        },
                    )
                    current_span.set_attribute("llm.breaker_state", breaker.state.value)
                    telemetry.get_metrics().llm_calls.add(
                        1,
                        {"provider": self.provider, "model": self.model, "outcome": "circuit_open"},
                    )
                    telemetry.record_llm_failure(
                        provider=self.provider, model=self.model, reason="circuit_open"
                    )
                    raise LLMError(f"{self.provider} circuit open: {exc}") from exc
                except Exception as exc:
                    last_exc = exc
                    category = classify_exception(exc)
                    retryable = category is RetryableError
                    if not retryable or attempt > self.max_retries:
                        log.error(
                            "llm call failed, not retrying",
                            extra={
                                "provider": self.provider,
                                "model": self.model,
                                "retry_count": attempt,
                                "error_category": category.__name__,
                                "reason": str(exc),
                            },
                        )
                        current_span.set_attribute("llm.retry_count", attempt)
                        telemetry.get_metrics().llm_calls.add(
                            1, {"provider": self.provider, "model": self.model, "outcome": "failed"}
                        )
                        telemetry.record_llm_failure(
                            provider=self.provider, model=self.model, reason=category.__name__
                        )
                        raise LLMError(f"{self.provider} request failed: {exc}") from exc

                    delay = self._backoff_delay(attempt)
                    log.warning(
                        "llm call failed, retrying",
                        extra={
                            "provider": self.provider,
                            "model": self.model,
                            "retry_count": attempt,
                            "retry_delay_seconds": delay,
                            "reason": str(exc),
                        },
                    )
                    telemetry.get_metrics().retry_count.add(
                        1, {"component": "llm", "provider": self.provider, "model": self.model}
                    )
                    await asyncio.sleep(delay)

            telemetry.get_metrics().llm_calls.add(
                1, {"provider": self.provider, "model": self.model, "outcome": "failed"}
            )
            raise LLMError(f"{self.provider} request failed after retries: {last_exc}")

    def _log_usage(self, response: LLMResponse) -> None:
        log.info(
            "llm completion",
            extra={
                "provider": response.provider,
                "model": response.model,
                "input_tokens": response.input_tokens,
                "output_tokens": response.output_tokens,
                "latency_ms": response.latency_ms,
                "duration_ms": response.latency_ms,
                "event_type": "llm.completion",
            },
        )
        telemetry.record_llm_success(
            provider=response.provider,
            model=response.model,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            latency_ms=response.latency_ms,
        )


class AnthropicClient(_BaseLLMClient):
    """Calls the Anthropic Messages API directly over HTTPS (no SDK
    dependency, to keep provider wiring contained to this module)."""

    provider = "anthropic"

    def __init__(self, *, api_key: str, model: str, **kwargs) -> None:
        super().__init__(model=model, **kwargs)
        self.api_key = api_key

    async def complete(
        self, *, system: str, prompt: str, max_tokens: int, temperature: float = 0.2
    ) -> LLMResponse:
        async def _call() -> LLMResponse:
            started = time.monotonic()
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                resp = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": self.api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "max_tokens": max_tokens,
                        "temperature": temperature,
                        "system": system,
                        "messages": [{"role": "user", "content": prompt}],
                    },
                )
                resp.raise_for_status()
                data = resp.json()

            latency_ms = (time.monotonic() - started) * 1000
            try:
                text = "".join(
                    block.get("text", "")
                    for block in data["content"]
                    if block.get("type") == "text"
                )
                usage = data.get("usage", {})
                response = LLMResponse(
                    text=text,
                    model=data.get("model", self.model),
                    provider=self.provider,
                    input_tokens=int(usage.get("input_tokens", 0)),
                    output_tokens=int(usage.get("output_tokens", 0)),
                    latency_ms=latency_ms,
                    finish_reason=data.get("stop_reason"),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise PoisonMessageError(f"anthropic response malformed: {exc}") from exc

            self._log_usage(response)
            return response

        return await self._execute_with_resilience(_call)


class OpenAIClient(_BaseLLMClient):
    """Calls the OpenAI Chat Completions API directly over HTTPS."""

    provider = "openai"

    def __init__(self, *, api_key: str, model: str, **kwargs) -> None:
        super().__init__(model=model, **kwargs)
        self.api_key = api_key

    async def complete(
        self, *, system: str, prompt: str, max_tokens: int, temperature: float = 0.2
    ) -> LLMResponse:
        async def _call() -> LLMResponse:
            started = time.monotonic()
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                resp = await client.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "content-type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "max_tokens": max_tokens,
                        "temperature": temperature,
                        "messages": [
                            {"role": "system", "content": system},
                            {"role": "user", "content": prompt},
                        ],
                    },
                )
                resp.raise_for_status()
                data = resp.json()

            latency_ms = (time.monotonic() - started) * 1000
            try:
                choice = data["choices"][0]
                usage = data.get("usage", {})
                response = LLMResponse(
                    text=choice["message"]["content"] or "",
                    model=data.get("model", self.model),
                    provider=self.provider,
                    input_tokens=int(usage.get("prompt_tokens", 0)),
                    output_tokens=int(usage.get("completion_tokens", 0)),
                    latency_ms=latency_ms,
                    finish_reason=choice.get("finish_reason"),
                )
            except (KeyError, TypeError, ValueError, IndexError) as exc:
                raise PoisonMessageError(f"openai response malformed: {exc}") from exc

            self._log_usage(response)
            return response

        return await self._execute_with_resilience(_call)


class OllamaClient(_BaseLLMClient):
    """Calls a local/self-hosted Ollama server's chat API. No API key."""

    provider = "ollama"

    def __init__(self, *, base_url: str, model: str, **kwargs) -> None:
        super().__init__(model=model, **kwargs)
        self.base_url = base_url.rstrip("/")

    async def complete(
        self, *, system: str, prompt: str, max_tokens: int, temperature: float = 0.2
    ) -> LLMResponse:
        async def _call() -> LLMResponse:
            started = time.monotonic()
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                resp = await client.post(
                    f"{self.base_url}/api/chat",
                    json={
                        "model": self.model,
                        "stream": False,
                        "options": {"temperature": temperature, "num_predict": max_tokens},
                        "messages": [
                            {"role": "system", "content": system},
                            {"role": "user", "content": prompt},
                        ],
                    },
                )
                resp.raise_for_status()
                data = resp.json()

            latency_ms = (time.monotonic() - started) * 1000
            try:
                response = LLMResponse(
                    text=data["message"]["content"] or "",
                    model=data.get("model", self.model),
                    provider=self.provider,
                    input_tokens=int(data.get("prompt_eval_count", 0)),
                    output_tokens=int(data.get("eval_count", 0)),
                    latency_ms=latency_ms,
                    finish_reason="stop" if data.get("done") else None,
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise PoisonMessageError(f"ollama response malformed: {exc}") from exc

            self._log_usage(response)
            return response

        return await self._execute_with_resilience(_call)


class NullLLMClient:
    """Used when no provider is configured (``LLM_PROVIDER`` unset/``none``,
    or a required API key is missing). Always raises :class:`LLMError` so
    callers exercise the same degrade-gracefully path they'd need for a
    real provider outage, rather than silently no-op'ing."""

    provider = "none"

    def __init__(self, *, reason: str) -> None:
        self.reason = reason

    async def complete(
        self, *, system: str, prompt: str, max_tokens: int, temperature: float = 0.2
    ) -> LLMResponse:
        raise LLMError(f"no LLM provider configured: {self.reason}")


def get_llm_client(purpose: str = "default") -> LLMClient:
    """Build the configured provider's client for a given ``purpose``
    (e.g. ``"summary"`` for Researcher's cheap/small model, ``"narrative"``
    for Reviewer's explanation model).

    Reads ``LLM_PROVIDER`` (``anthropic`` | ``openai`` | ``ollama`` |
    ``none``); returns a :class:`NullLLMClient` for ``none``, an unset
    value, or a configured provider missing its required credential —
    every case behaves identically to callers (an :class:`LLMError` on
    ``complete()``).
    """
    provider = os.environ.get("LLM_PROVIDER", "none").strip().lower()

    if provider == "anthropic":
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            return NullLLMClient(reason="ANTHROPIC_API_KEY not set")
        return AnthropicClient(api_key=api_key, model=get_model_for(purpose, provider))

    if provider == "openai":
        api_key = os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            return NullLLMClient(reason="OPENAI_API_KEY not set")
        return OpenAIClient(api_key=api_key, model=get_model_for(purpose, provider))

    if provider == "ollama":
        base_url = os.environ.get("OLLAMA_BASE_URL", "")
        if not base_url:
            return NullLLMClient(reason="OLLAMA_BASE_URL not set")
        return OllamaClient(base_url=base_url, model=get_model_for(purpose, provider))

    return NullLLMClient(reason=f"LLM_PROVIDER={provider!r}")
