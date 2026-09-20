from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from shared.contracts import (
    BlastRadius,
    CommitDetected,
    Envelope,
    EventType,
    Finding,
    FindingsReady,
    ReviewCompleted,
    make_envelope,
    parse_envelope,
)


def _now():
    return datetime.now(UTC)


def test_commit_detected_round_trip():
    payload = CommitDetected(
        repo="acme/widgets",
        commit_sha="a" * 40,
        branch="main",
        author="jane@acme.dev",
        message="fix: off by one",
        committed_at=_now(),
        changed_files=["widgets/core.py"],
    )
    envelope = make_envelope(payload, event_type=EventType.COMMIT_DETECTED, source="ingestion")

    wire = envelope.to_bytes()
    restored = Envelope[CommitDetected].from_json(wire)

    assert restored.payload == payload
    assert restored.event_type is EventType.COMMIT_DETECTED
    assert restored.trace_id == envelope.trace_id


def test_findings_ready_round_trip_via_parse_envelope():
    finding = Finding(
        file_path="widgets/core.py",
        line_number=42,
        severity="high",
        category="security",
        message="possible SQL injection",
        agent_name="static-analysis",
    )
    payload = FindingsReady(
        commit_sha="a" * 40,
        agent_name="static-analysis",
        findings=[finding],
        started_at=_now(),
        completed_at=_now(),
        repo="acme/widgets",
        changed_files=["widgets/core.py"],
        blast_radius=BlastRadius(
            impacted_modules=["widgets.core", "widgets.api"],
            impact_count=2,
            max_depth=1,
        ),
        sensitive_hits=[],
        semantic_summary="",
    )
    envelope = make_envelope(payload, event_type=EventType.FINDINGS_READY, source="analysis-agent")

    restored = parse_envelope(envelope.to_json())

    assert isinstance(restored.payload, FindingsReady)
    assert restored.payload.findings[0].severity == "high"
    assert restored.payload.blast_radius.impact_count == 2


def test_review_completed_round_trip():
    payload = ReviewCompleted(
        commit_sha="a" * 40,
        repo="acme/widgets",
        report_id="rep-1",
        status="needs_review",
        severity="high",
        score=7,
        summary="1 high severity finding",
        total_findings=1,
        completed_at=_now(),
    )
    envelope = make_envelope(payload, event_type=EventType.REVIEW_COMPLETED, source="aggregator")

    restored = parse_envelope(envelope.to_json())
    assert restored.payload == payload


def test_envelope_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        Envelope[CommitDetected].model_validate(
            {
                "event_type": "commit.detected",
                "source": "ingestion",
                "payload": {
                    "repo": "acme/widgets",
                    "commit_sha": "a" * 40,
                    "branch": "main",
                    "author": "jane@acme.dev",
                    "message": "x",
                    "committed_at": _now().isoformat(),
                },
                "unexpected_field": "nope",
            }
        )


def test_payload_rejects_invalid_severity():
    with pytest.raises(ValidationError):
        Finding(
            file_path="f.py",
            severity="catastrophic",  # not a valid literal
            category="bug",
            message="oops",
            agent_name="agent",
        )


def test_schema_version_defaults_and_is_present_on_wire():
    payload = CommitDetected(
        repo="acme/widgets",
        commit_sha="a" * 40,
        branch="main",
        author="jane@acme.dev",
        message="x",
        committed_at=_now(),
    )
    assert payload.schema_version == "1.0"
    envelope = make_envelope(payload, event_type=EventType.COMMIT_DETECTED, source="ingestion")
    assert '"schema_version":"1.0"' in envelope.to_json()


def test_parse_envelope_rejects_unknown_event_type():
    with pytest.raises(ValueError):
        parse_envelope('{"event_type": "not.a.real.event", "source": "x", "payload": {}}')
