from datetime import datetime, timezone

import pytest

from agents.reviewer.main import classify_reviewer_failure, handle_message, process_findings
from agents.reviewer.storage import SavedReport
from shared.contracts import BlastRadius, EventType, FindingsReady, make_envelope
from shared.errors import FatalError, RetryableError
from shared.llm import LLMError, LLMResponse
from shared.retry import HEADER_ATTEMPT, MAX_RETRY_ATTEMPTS
from shared.slack import SlackError, SlackNotifier


class FakeMessage:
    """Minimal stand-in for aio_pika's IncomingMessage — manual-ack
    surface matching handle_message's Phase 4 contract."""

    def __init__(self, body: bytes, headers: dict | None = None) -> None:
        self.body = body
        self.headers = headers or {}
        self.acked = False
        self.nacked: list[bool] = []

    async def ack(self) -> None:
        self.acked = True

    async def nack(self, requeue: bool = False) -> None:
        self.nacked.append(requeue)


class FakeStorage:
    def __init__(self, *, already_processed: bool = False, raises: Exception | None = None) -> None:
        self.already_processed = already_processed
        self.saved: list[dict] = []
        self._raises = raises

    async def is_processed(self, event_id: str) -> bool:
        return self.already_processed

    async def save_report(self, findings, *, event_id, trace_id, breakdown, status, narrative):
        if self._raises is not None:
            raise self._raises
        self.saved.append(
            {
                "event_id": event_id,
                "trace_id": trace_id,
                "findings": findings,
                "breakdown": breakdown,
                "status": status,
                "narrative": narrative,
            }
        )
        return SavedReport(report_id=f"report-{len(self.saved)}", status=status)


class FakeLLMClient:
    provider = "fake"

    def __init__(self, *, text: str | None = "A narrative.", raises: bool = False) -> None:
        self.text = text
        self.raises = raises

    async def complete(self, *, system, prompt, max_tokens, temperature=0.2):
        if self.raises:
            raise LLMError("boom")
        return LLMResponse(
            text=self.text or "",
            model="fake-model",
            provider="fake",
            input_tokens=1,
            output_tokens=1,
            latency_ms=1.0,
        )


class FakeBroker:
    def __init__(self) -> None:
        self.published: list[tuple[object, str]] = []

    async def publish(self, envelope, *, routing_key: str) -> None:
        self.published.append((envelope, routing_key))


class RecordingSlackNotifier(SlackNotifier):
    def __init__(self, *, raises: Exception | None = None) -> None:
        super().__init__(webhook_url="")
        self.sent: list[dict] = []
        self._raises = raises

    async def send(self, payload: dict) -> bool:
        if self._raises is not None:
            raise self._raises
        self.sent.append(payload)
        return True


class FakeRetryLadder:
    def __init__(self) -> None:
        self.scheduled: list[dict] = []
        self.dlq: list[dict] = []
        self.raw_dlq: list[dict] = []

    async def schedule_retry(self, envelope, *, routing_key, reason, attempt, original_queue):
        self.scheduled.append(
            {
                "envelope": envelope,
                "routing_key": routing_key,
                "reason": reason,
                "attempt": attempt,
                "original_queue": original_queue,
            }
        )

    async def send_to_dlq(self, envelope, *, reason, attempt, original_queue):
        self.dlq.append(
            {"envelope": envelope, "reason": reason, "attempt": attempt, "original_queue": original_queue}
        )

    async def send_raw_to_dlq(self, raw_body, *, reason, original_queue):
        self.raw_dlq.append({"raw_body": raw_body, "reason": reason, "original_queue": original_queue})


class FakeIdempotency:
    def __init__(self, *, already_claimed: bool = False) -> None:
        self.claimed: list[str] = []
        self.released: list[str] = []
        self.completed: list[str] = []
        self._already_claimed = already_claimed

    async def claim(self, *, event_id, event_type, trace_id):
        self.claimed.append(event_id)
        return not self._already_claimed

    async def release(self, event_id):
        self.released.append(event_id)

    async def mark_complete(self, event_id):
        self.completed.append(event_id)


def _findings(**overrides) -> FindingsReady:
    now = datetime.now(timezone.utc)
    defaults = dict(
        commit_sha="a" * 40,
        agent_name="researcher",
        findings=[],
        started_at=now,
        completed_at=now,
        repo="acme/widgets",
        changed_files=["tests/test_widget.py"],
        blast_radius=BlastRadius(impacted_modules=[], impact_count=0, max_depth=0),
        sensitive_hits=[],
        semantic_summary="A small change.",
    )
    defaults.update(overrides)
    return FindingsReady(**defaults)


async def test_process_findings_low_risk_returns_review_completed():
    storage = FakeStorage()
    llm_client = FakeLLMClient(text="Low risk commit.")
    slack = RecordingSlackNotifier()

    review = await process_findings(
        _findings(),
        event_id="evt-1",
        trace_id="trace-1",
        storage=storage,
        llm_client=llm_client,
        slack=slack,
    )

    assert review is not None
    assert review.severity == "low"
    assert review.status == "passed"
    assert review.score == 0
    assert review.repo == "acme/widgets"
    assert len(storage.saved) == 1
    assert len(slack.sent) == 1


async def test_process_findings_skips_already_processed_event():
    storage = FakeStorage(already_processed=True)
    llm_client = FakeLLMClient()
    slack = RecordingSlackNotifier()

    review = await process_findings(
        _findings(),
        event_id="evt-dup",
        trace_id="trace-1",
        storage=storage,
        llm_client=llm_client,
        slack=slack,
    )

    assert review is None
    assert storage.saved == []
    assert slack.sent == []


async def test_process_findings_falls_back_to_deterministic_narrative_on_llm_error():
    storage = FakeStorage()
    llm_client = FakeLLMClient(raises=True)
    slack = RecordingSlackNotifier()

    review = await process_findings(
        _findings(sensitive_hits=["auth/login.py"]),
        event_id="evt-2",
        trace_id="trace-1",
        storage=storage,
        llm_client=llm_client,
        slack=slack,
    )

    assert review is not None
    saved_narrative = storage.saved[0]["narrative"]
    assert "no LLM narrative" in saved_narrative or "Generated without an LLM" in saved_narrative


async def test_process_findings_sensitive_hit_escalates_status():
    storage = FakeStorage()
    llm_client = FakeLLMClient()
    slack = RecordingSlackNotifier()

    review = await process_findings(
        _findings(changed_files=["auth/login.py"], sensitive_hits=["auth/login.py"]),
        event_id="evt-3",
        trace_id="trace-1",
        storage=storage,
        llm_client=llm_client,
        slack=slack,
    )

    assert review.severity in ("moderate", "high", "critical")
    assert review.status == "needs_review"


def _handle_kwargs(broker, storage, llm_client, slack, retry_ladder=None, idempotency=None):
    return dict(
        broker=broker,
        storage=storage,
        llm_client=llm_client,
        slack=slack,
        retry_ladder=retry_ladder or FakeRetryLadder(),
        idempotency=idempotency or FakeIdempotency(),
    )


async def test_handle_message_valid_publishes_review_completed():
    envelope = make_envelope(_findings(), event_type=EventType.FINDINGS_READY, source="test")
    broker = FakeBroker()
    storage = FakeStorage()
    llm_client = FakeLLMClient()
    slack = RecordingSlackNotifier()
    idempotency = FakeIdempotency()
    message = FakeMessage(envelope.to_bytes())

    await handle_message(
        message, **_handle_kwargs(broker, storage, llm_client, slack, idempotency=idempotency)
    )

    assert len(broker.published) == 1
    review_envelope, routing_key = broker.published[0]
    assert routing_key == EventType.REVIEW_COMPLETED.value
    assert review_envelope.trace_id == envelope.trace_id
    assert review_envelope.payload.commit_sha == envelope.payload.commit_sha
    assert message.acked is True
    assert idempotency.claimed == [envelope.event_id]
    assert idempotency.completed == [envelope.event_id]


async def test_handle_message_already_processed_does_not_publish():
    envelope = make_envelope(_findings(), event_type=EventType.FINDINGS_READY, source="test")
    broker = FakeBroker()
    storage = FakeStorage(already_processed=True)
    llm_client = FakeLLMClient()
    slack = RecordingSlackNotifier()
    message = FakeMessage(envelope.to_bytes())

    await handle_message(message, **_handle_kwargs(broker, storage, llm_client, slack))

    assert broker.published == []
    assert message.acked is True


async def test_handle_message_duplicate_delivery_skips_without_doing_any_work():
    envelope = make_envelope(_findings(), event_type=EventType.FINDINGS_READY, source="test")
    broker = FakeBroker()
    storage = FakeStorage()
    llm_client = FakeLLMClient()
    slack = RecordingSlackNotifier()
    idempotency = FakeIdempotency(already_claimed=True)
    message = FakeMessage(envelope.to_bytes())

    await handle_message(
        message, **_handle_kwargs(broker, storage, llm_client, slack, idempotency=idempotency)
    )

    assert broker.published == []
    assert storage.saved == []
    assert slack.sent == []
    assert message.acked is True


async def test_handle_message_rejects_invalid_contract_routes_to_dlq():
    bad_body = b'{"event_type": "findings.ready", "source": "x", "payload": {}}'
    broker = FakeBroker()
    storage = FakeStorage()
    llm_client = FakeLLMClient()
    slack = RecordingSlackNotifier()
    retry_ladder = FakeRetryLadder()
    message = FakeMessage(bad_body)

    await handle_message(
        message, **_handle_kwargs(broker, storage, llm_client, slack, retry_ladder=retry_ladder)
    )

    assert broker.published == []
    assert message.acked is True
    assert len(retry_ladder.raw_dlq) == 1


async def test_handle_message_retryable_failure_schedules_retry():
    envelope = make_envelope(_findings(), event_type=EventType.FINDINGS_READY, source="test")
    broker = FakeBroker()
    storage = FakeStorage(raises=RetryableError("db connection reset"))
    llm_client = FakeLLMClient()
    slack = RecordingSlackNotifier()
    retry_ladder = FakeRetryLadder()
    idempotency = FakeIdempotency()
    message = FakeMessage(envelope.to_bytes())

    await handle_message(
        message,
        **_handle_kwargs(broker, storage, llm_client, slack, retry_ladder, idempotency),
    )

    assert message.acked is True
    assert len(retry_ladder.scheduled) == 1
    assert retry_ladder.scheduled[0]["attempt"] == 1
    assert idempotency.released == [envelope.event_id]


async def test_handle_message_retry_exhausted_goes_to_dlq():
    envelope = make_envelope(_findings(), event_type=EventType.FINDINGS_READY, source="test")
    broker = FakeBroker()
    storage = FakeStorage(raises=RetryableError("still failing"))
    llm_client = FakeLLMClient()
    slack = RecordingSlackNotifier()
    retry_ladder = FakeRetryLadder()
    idempotency = FakeIdempotency()
    message = FakeMessage(envelope.to_bytes(), headers={HEADER_ATTEMPT: MAX_RETRY_ATTEMPTS})

    await handle_message(
        message,
        **_handle_kwargs(broker, storage, llm_client, slack, retry_ladder, idempotency),
    )

    assert retry_ladder.scheduled == []
    assert len(retry_ladder.dlq) == 1
    assert retry_ladder.dlq[0]["attempt"] == MAX_RETRY_ATTEMPTS + 1


async def test_handle_message_slack_failure_is_retryable():
    envelope = make_envelope(_findings(), event_type=EventType.FINDINGS_READY, source="test")
    broker = FakeBroker()
    storage = FakeStorage()
    llm_client = FakeLLMClient()
    slack = RecordingSlackNotifier(raises=SlackError("slack delivery failed: 503"))
    retry_ladder = FakeRetryLadder()
    message = FakeMessage(envelope.to_bytes())

    await handle_message(
        message, **_handle_kwargs(broker, storage, llm_client, slack, retry_ladder=retry_ladder)
    )

    assert len(retry_ladder.scheduled) == 1
    assert broker.published == []


async def test_handle_message_fatal_failure_nacks_with_requeue_and_raises():
    envelope = make_envelope(_findings(), event_type=EventType.FINDINGS_READY, source="test")
    broker = FakeBroker()
    storage = FakeStorage(raises=ValueError("unexpected bug"))
    llm_client = FakeLLMClient()
    slack = RecordingSlackNotifier()
    message = FakeMessage(envelope.to_bytes())

    with pytest.raises(FatalError):
        await handle_message(message, **_handle_kwargs(broker, storage, llm_client, slack))

    assert message.acked is False
    assert message.nacked == [True]


def test_classify_reviewer_failure_connection_error_is_retryable():
    assert classify_reviewer_failure(ConnectionError("x")) is RetryableError


def test_classify_reviewer_failure_unknown_is_fatal():
    assert classify_reviewer_failure(ValueError("x")) is FatalError
