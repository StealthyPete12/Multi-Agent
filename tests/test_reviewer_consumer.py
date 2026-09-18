from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from agents.reviewer.main import handle_message, process_findings
from agents.reviewer.storage import SavedReport
from shared.contracts import BlastRadius, EventType, FindingsReady, make_envelope
from shared.llm import LLMError, LLMResponse
from shared.slack import SlackNotifier


class FakeMessage:
    def __init__(self, body: bytes) -> None:
        self.body = body

    @asynccontextmanager
    async def process(self, ignore_processed: bool = True, requeue: bool = False):
        yield


class FakeStorage:
    def __init__(self, *, already_processed: bool = False) -> None:
        self.already_processed = already_processed
        self.saved: list[dict] = []

    async def is_processed(self, event_id: str) -> bool:
        return self.already_processed

    async def save_report(self, findings, *, event_id, trace_id, breakdown, status, narrative):
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
    def __init__(self) -> None:
        super().__init__(webhook_url="")
        self.sent: list[dict] = []

    async def send(self, payload: dict) -> bool:
        self.sent.append(payload)
        return True


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


async def test_handle_message_valid_publishes_review_completed():
    envelope = make_envelope(_findings(), event_type=EventType.FINDINGS_READY, source="test")
    broker = FakeBroker()
    storage = FakeStorage()
    llm_client = FakeLLMClient()
    slack = RecordingSlackNotifier()

    await handle_message(
        FakeMessage(envelope.to_bytes()),
        broker=broker,
        storage=storage,
        llm_client=llm_client,
        slack=slack,
    )

    assert len(broker.published) == 1
    review_envelope, routing_key = broker.published[0]
    assert routing_key == EventType.REVIEW_COMPLETED.value
    assert review_envelope.trace_id == envelope.trace_id
    assert review_envelope.payload.commit_sha == envelope.payload.commit_sha


async def test_handle_message_already_processed_does_not_publish():
    envelope = make_envelope(_findings(), event_type=EventType.FINDINGS_READY, source="test")
    broker = FakeBroker()
    storage = FakeStorage(already_processed=True)
    llm_client = FakeLLMClient()
    slack = RecordingSlackNotifier()

    await handle_message(
        FakeMessage(envelope.to_bytes()),
        broker=broker,
        storage=storage,
        llm_client=llm_client,
        slack=slack,
    )

    assert broker.published == []


async def test_handle_message_rejects_invalid_contract():
    bad_body = b'{"event_type": "findings.ready", "source": "x", "payload": {}}'
    broker = FakeBroker()
    storage = FakeStorage()
    llm_client = FakeLLMClient()
    slack = RecordingSlackNotifier()

    with pytest.raises(ValidationError):
        await handle_message(
            FakeMessage(bad_body), broker=broker, storage=storage, llm_client=llm_client, slack=slack
        )

    assert broker.published == []
