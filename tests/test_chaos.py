"""Repeatable chaos scenarios for the Phase 4 fault-tolerant pipeline.

Each test is a direct, automated analogue of one PHASE_4 chaos scenario.
Where the real production timing would make a test impractically slow
(the 5s/30s/5m retry ladder, the 60s circuit-breaker open window), the
same *mechanism* is exercised through real infrastructure (RabbitMQ TTL
expiry, real Postgres claims, the real CircuitBreaker state machine) with
short, explicitly-labeled substitute timings — never simulated by
skipping the mechanism itself. All tests skip gracefully if their
required infrastructure isn't reachable, matching the rest of the suite.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import asyncpg
import pytest

from agents.researcher.main import handle_message as researcher_handle_message
from agents.reviewer.main import handle_message as reviewer_handle_message
from shared.breaker import CircuitState, get_circuit_breaker
from shared.broker import Broker
from shared.contracts import CommitDetected, EventType, make_envelope
from shared.idempotency import IdempotencyStore
from shared.llm import AnthropicClient, LLMError
from shared.retry import DLQ_QUEUE_NAME, HEADER_ATTEMPT, RetryLadder, RetryRung
from tests.test_researcher_consumer import (
    FakeBroker as ResearcherFakeBroker,
)
from tests.test_researcher_consumer import (
    FakeDatabase,
    FakeRepositoryCache,
    _sample_commit,
    _write_sample_repo,
)
from tests.test_researcher_consumer import FakeIdempotency as ResearcherFakeIdempotency
from tests.test_researcher_consumer import (
    FakeMessage as ResearcherFakeMessage,
)
from tests.test_researcher_consumer import (
    FakeRetryLadder as ResearcherFakeRetryLadder,
)
from tests.test_reviewer_consumer import (
    FakeLLMClient,
    FakeStorage,
    RecordingSlackNotifier,
    _findings,
)
from tests.test_reviewer_consumer import (
    FakeMessage as ReviewerFakeMessage,
)
from tests.test_reviewer_consumer import (
    FakeRetryLadder as ReviewerFakeRetryLadder,
)

DATABASE_URL = "postgresql://swarm:swarm_dev_password@localhost:5432/code_review_swarm"


# ---------------------------------------------------------------------------
# Scenario A: Researcher crashes while processing.
# Verify: no message loss, exactly one findings.ready produced.
# ---------------------------------------------------------------------------


async def test_scenario_a_researcher_crash_produces_exactly_one_findings_ready(
    postgres_available, tmp_path
):
    if not postgres_available:
        pytest.skip("postgres not reachable")

    _write_sample_repo(tmp_path)
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=2)
    try:
        event_id = str(uuid.uuid4())
        envelope = make_envelope(
            _sample_commit(commit_sha=uuid.uuid4().hex),
            event_type=EventType.COMMIT_DETECTED,
            source="chaos-a",
        )
        envelope.event_id = event_id

        crashed_idempotency = IdempotencyStore(pool, stale_after_seconds=0.2)
        # Simulate: the first researcher instance claimed the event and
        # started work, then was SIGKILLed before finishing — no
        # mark_complete(), no release(), exactly what a real crash leaves
        # behind.
        claimed = await crashed_idempotency.claim(
            event_id=event_id, event_type="commit.detected", trace_id=envelope.trace_id
        )
        assert claimed is True

        # RabbitMQ would redeliver the still-unacked message immediately;
        # at this point the claim isn't stale yet, so a second consumer
        # picking it up right away must NOT double-process concurrently.
        broker = ResearcherFakeBroker()
        retry_ladder = ResearcherFakeRetryLadder()
        too_soon_idempotency = IdempotencyStore(pool, stale_after_seconds=0.2)
        message = ResearcherFakeMessage(envelope.to_bytes())
        await researcher_handle_message(
            message,
            broker=broker,
            repo_cache=FakeRepositoryCache(tmp_path),
            database=FakeDatabase(),
            retry_ladder=retry_ladder,
            idempotency=too_soon_idempotency,
        )
        assert broker.published == []  # correctly skipped: claim still active
        assert message.acked is True

        # Wait past the staleness window (simulating "the restarted
        # researcher instance eventually picks the redelivered message
        # back up"), then process for real.
        await asyncio.sleep(0.3)
        broker2 = ResearcherFakeBroker()
        restarted_idempotency = IdempotencyStore(pool, stale_after_seconds=0.2)
        message2 = ResearcherFakeMessage(envelope.to_bytes())
        await researcher_handle_message(
            message2,
            broker=broker2,
            repo_cache=FakeRepositoryCache(tmp_path),
            database=FakeDatabase(),
            retry_ladder=ResearcherFakeRetryLadder(),
            idempotency=restarted_idempotency,
        )
        assert len(broker2.published) == 1  # no message loss: it got processed
        assert message2.acked is True

        # A third redelivery (e.g. an operator's own retry, or a slow
        # network causing RabbitMQ to think the ack didn't land) must
        # find the work already completed and not reprocess.
        broker3 = ResearcherFakeBroker()
        message3 = ResearcherFakeMessage(envelope.to_bytes())
        await researcher_handle_message(
            message3,
            broker=broker3,
            repo_cache=FakeRepositoryCache(tmp_path),
            database=FakeDatabase(),
            retry_ladder=ResearcherFakeRetryLadder(),
            idempotency=IdempotencyStore(pool, stale_after_seconds=0.2),
        )
        assert broker3.published == []  # exactly one findings.ready total, not two
        assert message3.acked is True
    finally:
        await pool.execute("DELETE FROM processed_events WHERE event_id = $1", event_id)
        await pool.close()


# ---------------------------------------------------------------------------
# Scenario B: Reviewer crashes while processing.
# Verify: exactly one report, exactly one Slack message.
# ---------------------------------------------------------------------------


async def test_scenario_b_reviewer_crash_produces_exactly_one_report_and_slack_message(
    postgres_available,
):
    if not postgres_available:
        pytest.skip("postgres not reachable")

    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=2)
    try:
        event_id = str(uuid.uuid4())
        envelope = make_envelope(_findings(), event_type=EventType.FINDINGS_READY, source="chaos-b")
        envelope.event_id = event_id

        crashed_idempotency = IdempotencyStore(pool, stale_after_seconds=0.2)
        claimed = await crashed_idempotency.claim(
            event_id=event_id, event_type="findings.ready", trace_id=envelope.trace_id
        )
        assert claimed is True
        # ... crash: no mark_complete(), no release() ...

        storage = FakeStorage()
        slack = RecordingSlackNotifier()
        broker = ResearcherFakeBroker()  # generic — just records publish() calls

        await asyncio.sleep(0.3)
        message = ReviewerFakeMessage(envelope.to_bytes())
        await reviewer_handle_message(
            message,
            broker=broker,
            storage=storage,
            llm_client=FakeLLMClient(),
            slack=slack,
            retry_ladder=ReviewerFakeRetryLadder(),
            idempotency=IdempotencyStore(pool, stale_after_seconds=0.2),
        )
        assert len(storage.saved) == 1
        assert len(slack.sent) == 1
        assert len(broker.published) == 1

        # Redelivery after completion must not duplicate either side effect.
        message2 = ReviewerFakeMessage(envelope.to_bytes())
        broker2 = ResearcherFakeBroker()
        await reviewer_handle_message(
            message2,
            broker=broker2,
            storage=storage,
            llm_client=FakeLLMClient(),
            slack=slack,
            retry_ladder=ReviewerFakeRetryLadder(),
            idempotency=IdempotencyStore(pool, stale_after_seconds=0.2),
        )
        assert len(storage.saved) == 1  # still exactly one report
        assert len(slack.sent) == 1  # still exactly one Slack message
        assert broker2.published == []
    finally:
        await pool.execute("DELETE FROM processed_events WHERE event_id = $1", event_id)
        await pool.close()


# ---------------------------------------------------------------------------
# Scenario C: simulated persistent failure (e.g. provider 429).
# Verify: 5s -> 30s -> 5m -> DLQ progression, via real RabbitMQ TTL expiry
# with short substitute delays (the mechanism is identical; only the
# clock is sped up so this test runs in under a second instead of ~6 min).
# ---------------------------------------------------------------------------


async def test_scenario_c_retry_ladder_progression_to_dlq(rabbitmq_available):
    if not rabbitmq_available:
        pytest.skip("rabbitmq not reachable")

    suffix = uuid.uuid4().hex[:8]
    broker = Broker(exchange_name=f"chaos.c.exchange.{suffix}")
    await broker.connect()
    fast_ladder = (
        RetryRung(
            name="rung1",
            delay_ms=150,
            exchange_name=f"chaos.c.r1.{suffix}",
            queue_name=f"chaos.c.q1.{suffix}",
        ),
        RetryRung(
            name="rung2",
            delay_ms=150,
            exchange_name=f"chaos.c.r2.{suffix}",
            queue_name=f"chaos.c.q2.{suffix}",
        ),
        RetryRung(
            name="rung3",
            delay_ms=150,
            exchange_name=f"chaos.c.r3.{suffix}",
            queue_name=f"chaos.c.q3.{suffix}",
        ),
    )
    ladder = RetryLadder(broker, ladder=fast_ladder)
    await ladder.declare_topology()

    origin_queue_name = f"chaos.c.q.origin.{suffix}"
    origin_queue = await broker.declare_queue(
        origin_queue_name, routing_keys=[EventType.COMMIT_DETECTED.value]
    )

    payload = CommitDetected(
        repo="acme/chaos-c",
        commit_sha=uuid.uuid4().hex,
        branch="main",
        author="chaos",
        message="simulated persistent 429",
        committed_at=datetime.now(UTC),
        changed_files=[],
    )
    envelope = make_envelope(payload, event_type=EventType.COMMIT_DETECTED, source="chaos-c")

    observed_attempts: list[int] = []
    dlq = await broker.channel.declare_queue(DLQ_QUEUE_NAME, durable=True)

    try:
        # Kick off attempt 1 directly (as if a consumer just failed once).
        await ladder.schedule_retry(
            envelope,
            routing_key=EventType.COMMIT_DETECTED.value,
            reason="simulated 429",
            attempt=1,
            original_queue=origin_queue_name,
        )

        # Each redelivery: bump the attempt, retry again or (once the
        # 3-rung ladder is exhausted) send to the DLQ instead.
        for _ in range(len(fast_ladder)):
            message = await asyncio.wait_for(_get_one(origin_queue), timeout=5)
            attempt = RetryLadder.attempt_from_headers(message.headers)
            observed_attempts.append(attempt)
            await message.ack()
            next_attempt = attempt + 1
            if ladder.rung_for_attempt(next_attempt) is not None:
                await ladder.schedule_retry(
                    envelope,
                    routing_key=EventType.COMMIT_DETECTED.value,
                    reason="simulated 429",
                    attempt=next_attempt,
                    original_queue=origin_queue_name,
                )
            else:
                await ladder.send_to_dlq(
                    envelope,
                    reason="simulated 429, retries exhausted",
                    attempt=next_attempt,
                    original_queue=origin_queue_name,
                )

        assert observed_attempts == [1, 2, 3]

        dlq_message = await asyncio.wait_for(_get_one_matching(dlq, envelope.to_bytes()), timeout=5)
        assert dlq_message is not None
        assert dlq_message.headers.get(HEADER_ATTEMPT) == 4
        await dlq_message.ack()
    finally:
        await broker.channel.queue_delete(origin_queue_name)
        await broker.channel.queue_delete(f"{origin_queue_name}.dlq")
        for rung in fast_ladder:
            await broker.channel.queue_delete(rung.queue_name)
            await broker.channel.exchange_delete(rung.exchange_name)
        await broker.channel.exchange_delete(broker.exchange_name)
        await broker.channel.exchange_delete(broker.dlx_name)
        await broker.close()


async def _get_one(queue):
    while True:
        message = await queue.get(no_ack=False, fail=False)
        if message is not None:
            return message
        await asyncio.sleep(0.05)


async def _get_one_matching(queue, body: bytes):
    """Drain q.dlq for our own message, requeuing anything else — same
    safe pattern as tools/replay_dlq.py, since q.dlq is shared."""
    others = []
    found = None
    try:
        while True:
            message = await queue.get(no_ack=False, fail=False)
            if message is None:
                break
            if found is None and message.body == body:
                found = message
            else:
                others.append(message)
    finally:
        for m in others:
            await m.nack(requeue=True)
    return found


# ---------------------------------------------------------------------------
# Scenario D: malformed contract. Verify immediate DLQ, no retries.
# ---------------------------------------------------------------------------


async def test_scenario_d_malformed_contract_immediate_dlq_no_retries():
    broker = ResearcherFakeBroker()
    retry_ladder = ResearcherFakeRetryLadder()
    bad_body = (
        b'{"event_type": "commit.detected", "source": "chaos-d", "payload": {"not": "valid"}}'
    )
    message = ResearcherFakeMessage(bad_body)

    await researcher_handle_message(
        message,
        broker=broker,
        repo_cache=FakeRepositoryCache(None),
        database=FakeDatabase(),
        retry_ladder=retry_ladder,
        idempotency=ResearcherFakeIdempotency(),
    )

    assert broker.published == []
    assert message.acked is True
    assert retry_ladder.scheduled == []  # zero retry attempts
    assert len(retry_ladder.raw_dlq) == 1  # straight to DLQ


# ---------------------------------------------------------------------------
# Scenario E: duplicate event delivery. Verify a single report persisted.
# ---------------------------------------------------------------------------


async def test_scenario_e_duplicate_delivery_single_report_persisted(postgres_available):
    if not postgres_available:
        pytest.skip("postgres not reachable")

    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=2)
    try:
        envelope = make_envelope(_findings(), event_type=EventType.FINDINGS_READY, source="chaos-e")
        storage = FakeStorage()
        slack = RecordingSlackNotifier()
        idempotency = IdempotencyStore(pool)

        for _ in range(3):  # three exact-duplicate deliveries of the same event
            broker = ResearcherFakeBroker()
            message = ReviewerFakeMessage(envelope.to_bytes())
            await reviewer_handle_message(
                message,
                broker=broker,
                storage=storage,
                llm_client=FakeLLMClient(),
                slack=slack,
                retry_ladder=ReviewerFakeRetryLadder(),
                idempotency=idempotency,
            )
            assert message.acked is True

        assert len(storage.saved) == 1
        assert len(slack.sent) == 1
    finally:
        await pool.execute("DELETE FROM processed_events WHERE event_id = $1", envelope.event_id)
        await pool.close()


# ---------------------------------------------------------------------------
# Scenario F: circuit breaker. Verify 5 failures -> OPEN, then HALF_OPEN
# recovery (open_duration shortened so this runs in well under a second
# instead of the real 60s).
# ---------------------------------------------------------------------------


async def test_scenario_f_circuit_breaker_full_cycle(httpx_mock):
    provider_name = f"chaos-f-{uuid.uuid4().hex[:8]}"

    class _ChaosClient(AnthropicClient):
        provider = provider_name

    for _ in range(5):
        httpx_mock.add_response(url="https://api.anthropic.com/v1/messages", status_code=503)

    client = _ChaosClient(
        api_key="sk-test",
        model="chaos-model",
        rate_limit_enabled=False,
        max_retries=0,
        breaker_failure_threshold=5,
        breaker_open_seconds=0.2,
    )

    for _ in range(5):
        with pytest.raises(LLMError):
            await client.complete(system="s", prompt="p", max_tokens=10)

    breaker = get_circuit_breaker(provider_name)
    assert breaker.state is CircuitState.OPEN
    assert breaker.metrics.failure_count == 5
    assert breaker.metrics.open_count == 1

    # While OPEN, no network call is even attempted.
    with pytest.raises(LLMError):
        await client.complete(system="s", prompt="p", max_tokens=10)
    assert len(httpx_mock.get_requests()) == 5

    await asyncio.sleep(0.25)  # past open_seconds
    assert breaker.state is CircuitState.HALF_OPEN

    httpx_mock.add_response(
        url="https://api.anthropic.com/v1/messages",
        json={
            "model": "chaos-model",
            "content": [{"type": "text", "text": "recovered"}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "stop_reason": "end_turn",
        },
    )
    response = await client.complete(system="s", prompt="p", max_tokens=10)
    assert response.text == "recovered"
    assert breaker.state is CircuitState.CLOSED
    assert breaker.metrics.recovery_count == 1
