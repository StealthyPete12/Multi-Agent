"""RabbitMQ delayed-retry ladder: ``q.retry.5s`` -> ``q.retry.30s`` ->
``q.retry.5m`` -> ``q.dlq``.

RabbitMQ has no native "retry in N seconds" primitive without the
(non-default) delayed-message-exchange plugin, so this implements the
standard TTL+dead-letter-exchange "parking lot" pattern by hand: each
rung is a durable queue with a fixed ``x-message-ttl`` and
``x-dead-letter-exchange`` pointing back at the *main* event exchange —
when a message's TTL expires, RabbitMQ automatically redelivers it.

The one subtlety this module depends on (verified empirically against a
live broker before writing this): a message dead-lettered by TTL expiry
is redelivered using the routing key it was *originally published with
when it entered the expiring queue* — not the queue's name — as long as
the queue does not set ``x-dead-letter-routing-key`` itself. So each rung
gets its own small topic exchange (bound catch-all, ``#``, to exactly one
queue) purely to act as a named "entry door" for that delay tier;
publishing a retry means picking the exchange for the desired delay and
publishing with the message's *original* business routing key
(``EventType.value``, e.g. ``commit.detected``) — the main exchange's own
topic bindings then route it back to the correct origin queue with zero
extra relay code.

Retry metadata (attempt count, reason, first-failure time, originating
queue) travels as AMQP message headers, not inside the envelope body —
the envelope contract stays untouched (Phase 0-3's contracts are
strict/closed; adding retry bookkeeping fields to a payload would be a
contract change for something that's purely transport-layer concern).

Usage::

    retry = RetryLadder(broker)
    await retry.declare_topology()

    # a consumer's failure handler:
    attempt = retry.attempt_from_headers(message.headers) + 1
    if attempt > MAX_RETRY_ATTEMPTS:
        await retry.send_to_dlq(envelope, routing_key=..., reason=str(exc), attempt=attempt)
    else:
        await retry.schedule_retry(envelope, routing_key=..., reason=str(exc), attempt=attempt)
    await message.ack()
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import aio_pika
from aio_pika import ExchangeType

from shared.logging import configure_logging

if TYPE_CHECKING:
    from shared.broker import Broker
    from shared.contracts import Envelope

__all__ = [
    "RetryRung",
    "RetryLadder",
    "RETRY_LADDER",
    "MAX_RETRY_ATTEMPTS",
    "DLQ_QUEUE_NAME",
    "HEADER_ATTEMPT",
    "HEADER_REASON",
    "HEADER_ORIGINAL_QUEUE",
    "HEADER_FIRST_FAILED_AT",
]

log = configure_logging(service_name="retry")

DLQ_QUEUE_NAME = os.environ.get("QUEUE_DLQ", "q.dlq")

HEADER_ATTEMPT = "x-retry-attempt"
HEADER_REASON = "x-retry-reason"
HEADER_ORIGINAL_QUEUE = "x-retry-original-queue"
HEADER_FIRST_FAILED_AT = "x-retry-first-failed-at"


@dataclass(frozen=True)
class RetryRung:
    """One step of the retry ladder: a fixed delay, its own dedicated
    "entry door" exchange, and the queue that parks messages for
    ``delay_ms`` before they fall back to the main exchange."""

    name: str
    delay_ms: int
    exchange_name: str
    queue_name: str


def _build_ladder(exchange_prefix: str) -> tuple[RetryRung, ...]:
    return (
        RetryRung(
            name="5s",
            delay_ms=int(os.environ.get("RETRY_DELAY_5S_MS", 5_000)),
            exchange_name=f"{exchange_prefix}.retry.5s",
            queue_name="q.retry.5s",
        ),
        RetryRung(
            name="30s",
            delay_ms=int(os.environ.get("RETRY_DELAY_30S_MS", 30_000)),
            exchange_name=f"{exchange_prefix}.retry.30s",
            queue_name="q.retry.30s",
        ),
        RetryRung(
            name="5m",
            delay_ms=int(os.environ.get("RETRY_DELAY_5M_MS", 300_000)),
            exchange_name=f"{exchange_prefix}.retry.5m",
            queue_name="q.retry.5m",
        ),
    )


RETRY_LADDER: tuple[RetryRung, ...] = _build_ladder(os.environ.get("RABBITMQ_EXCHANGE", "swarm.events"))

# attempt 1 -> 5s, attempt 2 -> 30s, attempt 3 -> 5m, attempt 4 -> DLQ.
MAX_RETRY_ATTEMPTS = len(RETRY_LADDER)


class RetryLadder:
    """Owns the retry-ladder topology and the publish operations that move
    a message through it, on top of an already-connected :class:`Broker`.
    """

    def __init__(self, broker: "Broker", *, ladder: tuple[RetryRung, ...] = RETRY_LADDER) -> None:
        self.broker = broker
        self.ladder = ladder
        self._dlq_declared = False

    async def declare_topology(self) -> None:
        """Declare every rung's exchange+queue and the final ``q.dlq``.

        Idempotent: safe to call from every consumer process at startup
        (mirrors ``Broker.declare_queue``'s own idempotent declare).
        """
        channel = self.broker.channel
        for rung in self.ladder:
            exchange = await channel.declare_exchange(
                rung.exchange_name, ExchangeType.TOPIC, durable=True
            )
            queue = await channel.declare_queue(
                rung.queue_name,
                durable=True,
                arguments={
                    "x-message-ttl": rung.delay_ms,
                    "x-dead-letter-exchange": self.broker.exchange_name,
                    # No x-dead-letter-routing-key: on TTL expiry the
                    # message is redelivered using the routing key it
                    # entered this queue with (its original business
                    # routing key), which is exactly how it lands back on
                    # the correct origin queue via the main exchange's
                    # own topic bindings.
                },
            )
            await queue.bind(exchange, routing_key="#")

        if not self._dlq_declared:
            await channel.declare_queue(DLQ_QUEUE_NAME, durable=True)
            self._dlq_declared = True

    def rung_for_attempt(self, attempt: int) -> RetryRung | None:
        """The ladder rung for a 1-indexed ``attempt`` number, or ``None``
        once ``attempt`` exceeds the ladder (caller should DLQ instead)."""
        if 1 <= attempt <= len(self.ladder):
            return self.ladder[attempt - 1]
        return None

    @staticmethod
    def attempt_from_headers(headers: dict | None) -> int:
        """Current attempt count already recorded on a redelivered
        message, or 0 for a message that has never been retried."""
        if not headers:
            return 0
        try:
            return int(headers.get(HEADER_ATTEMPT, 0))
        except (TypeError, ValueError):
            return 0

    async def schedule_retry(
        self,
        envelope: "Envelope",
        *,
        routing_key: str,
        reason: str,
        attempt: int,
        original_queue: str,
        first_failed_at: str | None = None,
    ) -> RetryRung:
        """Publish ``envelope`` into the retry rung for ``attempt``.

        Raises ``ValueError`` if ``attempt`` exceeds the ladder — callers
        must check ``rung_for_attempt`` (or catch this) and call
        :meth:`send_to_dlq` instead once attempts are exhausted.
        """
        rung = self.rung_for_attempt(attempt)
        if rung is None:
            raise ValueError(f"attempt {attempt} exceeds retry ladder ({len(self.ladder)} rungs)")

        exchange = await self.broker.channel.get_exchange(rung.exchange_name)
        message = aio_pika.Message(
            body=envelope.to_bytes(),
            content_type="application/json",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            message_id=envelope.event_id,
            correlation_id=envelope.correlation_id,
            type=envelope.event_type.value,
            headers={
                "trace_id": envelope.trace_id,
                HEADER_ATTEMPT: attempt,
                HEADER_REASON: reason[:500],
                HEADER_ORIGINAL_QUEUE: original_queue,
                HEADER_FIRST_FAILED_AT: first_failed_at or datetime.now(timezone.utc).isoformat(),
            },
        )
        await exchange.publish(message, routing_key=routing_key)
        log.warning(
            "message routed to retry ladder",
            extra={
                "event_id": envelope.event_id,
                "trace_id": envelope.trace_id,
                "retry_queue": rung.queue_name,
                "retry_count": attempt,
                "reason": reason,
                "original_queue": original_queue,
            },
        )
        return rung

    async def send_to_dlq(
        self,
        envelope: "Envelope",
        *,
        reason: str,
        attempt: int,
        original_queue: str,
        first_failed_at: str | None = None,
    ) -> None:
        """Publish ``envelope`` directly to the terminal ``q.dlq``.

        No further redelivery happens automatically from here — this is
        the end of the line, inspected/replayed only via
        ``tools/replay_dlq.py``. Published via the default exchange
        (routing key == queue name) since nothing downstream needs the
        original routing key preserved anymore.
        """
        message = aio_pika.Message(
            body=envelope.to_bytes(),
            content_type="application/json",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            message_id=envelope.event_id,
            correlation_id=envelope.correlation_id,
            type=envelope.event_type.value,
            headers={
                "trace_id": envelope.trace_id,
                HEADER_ATTEMPT: attempt,
                HEADER_REASON: reason[:500],
                HEADER_ORIGINAL_QUEUE: original_queue,
                HEADER_FIRST_FAILED_AT: first_failed_at or datetime.now(timezone.utc).isoformat(),
            },
        )
        await self.broker.channel.default_exchange.publish(message, routing_key=DLQ_QUEUE_NAME)
        log.error(
            "message routed to DLQ",
            extra={
                "event_id": envelope.event_id,
                "trace_id": envelope.trace_id,
                "dlq_routing": DLQ_QUEUE_NAME,
                "retry_count": attempt,
                "reason": reason,
                "original_queue": original_queue,
            },
        )
