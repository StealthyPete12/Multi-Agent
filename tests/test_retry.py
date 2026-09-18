import asyncio
import uuid
from datetime import datetime, timezone

import pytest

from shared.broker import Broker
from shared.contracts import CommitDetected, EventType, make_envelope
from shared.retry import HEADER_ATTEMPT, HEADER_REASON, MAX_RETRY_ATTEMPTS, RetryLadder, RetryRung


def _commit_envelope():
    payload = CommitDetected(
        repo="acme/retry-test",
        commit_sha=uuid.uuid4().hex,
        branch="main",
        author="tester",
        message="retry test",
        committed_at=datetime.now(timezone.utc),
        changed_files=["a.py"],
    )
    return make_envelope(payload, event_type=EventType.COMMIT_DETECTED, source="test")


async def test_max_retry_attempts_matches_ladder_length():
    assert MAX_RETRY_ATTEMPTS == 3


async def test_rung_for_attempt_progression():
    broker = Broker()
    ladder = RetryLadder(broker)
    assert ladder.rung_for_attempt(1).name == "5s"
    assert ladder.rung_for_attempt(2).name == "30s"
    assert ladder.rung_for_attempt(3).name == "5m"
    assert ladder.rung_for_attempt(4) is None


async def test_attempt_from_headers():
    assert RetryLadder.attempt_from_headers(None) == 0
    assert RetryLadder.attempt_from_headers({}) == 0
    assert RetryLadder.attempt_from_headers({HEADER_ATTEMPT: 2}) == 2
    assert RetryLadder.attempt_from_headers({HEADER_ATTEMPT: "3"}) == 3


async def test_schedule_retry_and_redelivery_preserves_routing_key(rabbitmq_available):
    if not rabbitmq_available:
        pytest.skip("rabbitmq not reachable")

    # Fully isolated exchange/queue names (uuid-suffixed) so this test
    # never touches the real "swarm.events" exchange/q.commits (which a
    # live researcher instance could be consuming from) and never trips
    # over a stale leftover message from a previous test run.
    suffix = uuid.uuid4().hex[:8]
    broker = Broker(exchange_name=f"test.retry.exchange.{suffix}")
    await broker.connect()
    ladder = RetryLadder(
        broker,
        ladder=(
            # A tiny, test-local delay so this test doesn't take 5 real
            # seconds; everything else about the topology (dedicated
            # exchange, TTL, dead-letter-back-to-main) matches production.
            RetryRung(
                name="test-fast",
                delay_ms=200,
                exchange_name=f"test.retry.fast.{suffix}",
                queue_name=f"test.q.retry.fast.{suffix}",
            ),
        ),
    )
    await ladder.declare_topology()

    origin_queue_name = f"test.q.retry.origin.{suffix}"
    origin_queue = await broker.declare_queue(
        origin_queue_name, routing_keys=[EventType.COMMIT_DETECTED.value]
    )

    envelope = _commit_envelope()
    await ladder.schedule_retry(
        envelope,
        routing_key=EventType.COMMIT_DETECTED.value,
        reason="simulated transient failure",
        attempt=1,
        original_queue=origin_queue_name,
    )

    received = None

    async def consume():
        nonlocal received
        async with origin_queue.iterator() as it:
            async for message in it:
                async with message.process():
                    if message.body == envelope.to_bytes():
                        received = message
                        return

    try:
        await asyncio.wait_for(consume(), timeout=5)
    finally:
        await broker.channel.queue_delete(origin_queue_name)
        await broker.channel.queue_delete(f"{origin_queue_name}.dlq")
        await broker.channel.queue_delete(f"test.q.retry.fast.{suffix}")
        await broker.channel.exchange_delete(f"test.retry.fast.{suffix}")
        await broker.channel.exchange_delete(broker.exchange_name)
        await broker.channel.exchange_delete(broker.dlx_name)
        await broker.close()

    assert received is not None
    assert received.routing_key == EventType.COMMIT_DETECTED.value
    assert received.headers.get(HEADER_ATTEMPT) == 1
    assert received.headers.get(HEADER_REASON) == "simulated transient failure"


async def test_send_to_dlq(rabbitmq_available):
    if not rabbitmq_available:
        pytest.skip("rabbitmq not reachable")

    from shared.retry import DLQ_QUEUE_NAME

    # send_to_dlq() only ever touches the default exchange + the single
    # shared q.dlq — it never publishes to the main topic exchange or a
    # retry rung, so (unlike the rung test above) there's no need for a
    # dedicated exchange here, and using one would fight the real
    # q.retry.* queues' already-declared (mismatched) arguments.
    broker = Broker()
    await broker.connect()
    ladder = RetryLadder(broker)

    dlq = await broker.channel.declare_queue(DLQ_QUEUE_NAME, durable=True)

    envelope = _commit_envelope()
    await ladder.send_to_dlq(
        envelope,
        reason="retries exhausted",
        attempt=4,
        original_queue="q.commits",
    )

    # q.dlq is real, shared production infrastructure — a live agent may
    # have parked genuine poison messages there. Drain everything into
    # memory first (get() until empty) rather than iterating+auto-acking
    # (which would silently destroy anything already in it, and which a
    # requeue-while-iterating loop could also livelock on); then ack only
    # our own message and nack-with-requeue every other one back in,
    # unchanged — the same drain/restore pattern tools/replay_dlq.py uses.
    received = None
    others = []
    try:
        while True:
            message = await dlq.get(no_ack=False, fail=False)
            if message is None:
                break
            if received is None and message.body == envelope.to_bytes():
                received = message
            else:
                others.append(message)

        if received is not None:
            await received.ack()
        for message in others:
            await message.nack(requeue=True)
    finally:
        await broker.close()

    assert received is not None
    assert received.headers.get(HEADER_ATTEMPT) == 4
