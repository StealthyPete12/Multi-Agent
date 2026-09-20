#!/usr/bin/env python3
"""Regenerate docs/demo/*.json from the real contract models and
shared/slack.py::build_review_message, so the samples are guaranteed to
match the actual wire schema rather than hand-typed guesses.

One coherent scenario throughout: a sensitive-path commit to acme/widgets
touching auth/login.py, the same worked example used across
PHASE_2_REPORT.md through PHASE_4_REPORT.md's validation sections.

Usage::

    python -m tools.generate_demo_assets
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from shared.contracts import (
    BlastRadius,
    CommitDetected,
    Envelope,
    EventType,
    Finding,
    FindingsReady,
    ReviewCompleted,
    make_envelope,
)
from shared.slack import build_review_message

TRACE_ID = "8b2a38d0-19bf-4b71-a3fe-e68eafe880b3"
COMMIT_SHA = "4f6a1c9e2b7d3a58f0c1e9d6b4a7c3f8e1d2a5b6"
REPO = "acme/widgets"

NARRATIVE = (
    "This commit touches auth/login.py and auth/session.py, both matched by "
    "the sensitive-path policy for authentication code, which is why the "
    "score includes sensitive-hit points even though the blast radius is "
    "moderate (4 impacted modules, max depth 3: auth.login -> auth.session -> "
    "payments.charge -> checkout). Review the session-expiry logic change "
    "carefully and confirm the payments path still receives a valid session "
    "before charge submission; no test file was touched in this commit, so "
    "test coverage for the new expiry check should be verified manually."
)
REPORT_ID = "922039c1-4b1a-4e3d-9c7a-6f2b8e1a5d40"


def _dump(obj: object) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"


def main() -> None:
    committed_at = datetime(2026, 3, 12, 14, 32, 5, tzinfo=UTC)

    commit = CommitDetected(
        repo=REPO,
        commit_sha=COMMIT_SHA,
        branch="main",
        author="jane@acme.dev",
        message="fix: tighten session expiry check in login flow",
        committed_at=committed_at,
        changed_files=["auth/login.py", "auth/session.py"],
    )
    commit_envelope = make_envelope(
        commit, event_type=EventType.COMMIT_DETECTED, source="watcher", trace_id=TRACE_ID
    ).model_copy(
        update={"occurred_at": committed_at, "event_id": "b7f52a50-0aeb-45c1-9ec2-a80acf44f814"}
    )

    findings = FindingsReady(
        commit_sha=COMMIT_SHA,
        agent_name="researcher",
        findings=[
            Finding(
                file_path="auth/login.py",
                line_number=42,
                severity="medium",
                category="sensitive-path",
                message="Change touches an authentication-critical module (auth/).",
                agent_name="researcher",
                details={"pattern_matched": "auth/"},
            )
        ],
        started_at=committed_at,
        completed_at=datetime(2026, 3, 12, 14, 32, 6, 210000, tzinfo=UTC),
        repo=REPO,
        changed_files=["auth/login.py", "auth/session.py"],
        blast_radius=BlastRadius(
            impacted_modules=["auth.login", "auth.session", "payments.charge", "checkout"],
            impact_count=4,
            max_depth=3,
        ),
        sensitive_hits=["auth/login.py"],
        semantic_summary=(
            "Tightens the session expiry check in the login flow so an expired "
            "session token is rejected before reaching the payments path."
        ),
    )
    findings_envelope = make_envelope(
        findings,
        event_type=EventType.FINDINGS_READY,
        source="researcher",
        trace_id=TRACE_ID,
        correlation_id=commit_envelope.event_id,
    ).model_copy(
        update={
            "occurred_at": findings.completed_at,
            "event_id": "c3b308a1-9afd-42ba-adf5-f465fcba4168",
        }
    )

    review = ReviewCompleted(
        commit_sha=COMMIT_SHA,
        repo=REPO,
        report_id=REPORT_ID,
        status="needs_review",
        severity="moderate",
        score=4,
        summary=NARRATIVE,
        total_findings=1,
        completed_at=datetime(2026, 3, 12, 14, 32, 8, 940000, tzinfo=UTC),
    )
    review_envelope = make_envelope(
        review,
        event_type=EventType.REVIEW_COMPLETED,
        source="reviewer",
        trace_id=TRACE_ID,
        correlation_id=findings_envelope.event_id,
    ).model_copy(
        update={
            "occurred_at": review.completed_at,
            "event_id": "fe27d89c-8e38-42e0-a603-457eb36707a8",
        }
    )

    # The persisted `reports` row shape (db/migrations/002_reviewer_reports.sql) —
    # not a contract model (it's a DB row, not a wire event), built to match
    # the same scenario for a coherent demo set.
    persisted_report = {
        "id": REPORT_ID,
        "commit_id": None,
        "repo": REPO,
        "commit_sha": COMMIT_SHA,
        "severity": "moderate",
        "score": 4,
        "score_breakdown": {
            "blast_radius_points": 0,
            "impact_count_points": 0,
            "sensitive_hits_points": 2,
            "changed_files_points": 0,
            "test_proximity_points": 2,
        },
        "narrative": NARRATIVE,
        "blast_radius": {
            "impacted_modules": ["auth.login", "auth.session", "payments.charge", "checkout"],
            "impact_count": 4,
            "max_depth": 3,
        },
        "sensitive_hits": ["auth/login.py"],
        "source_event_id": findings_envelope.event_id,
        "generated_at": "2026-03-12T14:32:08.940000+00:00",
    }

    slack_payload = build_review_message(
        repo=REPO,
        commit_sha=COMMIT_SHA,
        severity="moderate",
        score=4,
        blast_radius_impact_count=4,
        blast_radius_max_depth=3,
        sensitive_hits=["auth/login.py"],
        narrative=NARRATIVE,
    )

    out_dir = "docs/demo"
    with open(f"{out_dir}/sample_commit.json", "w") as f:
        f.write(_dump(json.loads(commit_envelope.to_json())))
    with open(f"{out_dir}/sample_findings.json", "w") as f:
        f.write(_dump(json.loads(findings_envelope.to_json())))
    with open(f"{out_dir}/sample_review.json", "w") as f:
        f.write(
            _dump(
                {
                    "review_completed_event": json.loads(review_envelope.to_json()),
                    "persisted_report_row": persisted_report,
                }
            )
        )
    with open(f"{out_dir}/sample_slack_message.json", "w") as f:
        f.write(_dump(slack_payload))

    # Sanity: round-trip every envelope back through the strict contract models.
    assert Envelope[CommitDetected].from_json(commit_envelope.to_json())
    assert Envelope[FindingsReady].from_json(findings_envelope.to_json())
    assert Envelope[ReviewCompleted].from_json(review_envelope.to_json())
    print(f"wrote {out_dir}/sample_*.json (round-trip validated)")


if __name__ == "__main__":
    main()
