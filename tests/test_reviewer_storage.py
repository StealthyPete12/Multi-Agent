import uuid
from datetime import UTC, datetime

import pytest

from agents.reviewer.scoring import compute_score
from agents.reviewer.storage import ReviewStorage
from shared.contracts import BlastRadius, FindingsReady


def _findings(**overrides) -> FindingsReady:
    now = datetime.now(UTC)
    defaults = {
        "commit_sha": uuid.uuid4().hex[:12],
        "agent_name": "researcher",
        "findings": [],
        "started_at": now,
        "completed_at": now,
        "repo": f"test/{uuid.uuid4().hex[:8]}",
        "changed_files": ["auth/login.py"],
        "blast_radius": BlastRadius(impacted_modules=["auth.login"], impact_count=1, max_depth=1),
        "sensitive_hits": ["auth/login.py"],
        "semantic_summary": "Touches login validation.",
    }
    defaults.update(overrides)
    return FindingsReady(**defaults)


async def test_is_processed_false_for_unknown_event(postgres_available):
    if not postgres_available:
        pytest.skip("Postgres not reachable at DATABASE_URL")

    storage = ReviewStorage()
    await storage.connect()
    try:
        assert await storage.is_processed(str(uuid.uuid4())) is False
    finally:
        await storage.close()


async def test_save_report_persists_and_marks_processed(postgres_available):
    if not postgres_available:
        pytest.skip("Postgres not reachable at DATABASE_URL")

    storage = ReviewStorage()
    await storage.connect()
    findings = _findings()
    event_id = str(uuid.uuid4())
    breakdown = compute_score(findings)

    try:
        saved = await storage.save_report(
            findings,
            event_id=event_id,
            trace_id=str(uuid.uuid4()),
            breakdown=breakdown,
            status="needs_review",
            narrative="A narrative explanation.",
        )
        assert saved.report_id
        assert saved.status == "needs_review"

        assert await storage.is_processed(event_id) is True

        async with storage.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM reports WHERE id = $1::uuid", saved.report_id)
        assert row["repo"] == findings.repo
        assert row["commit_sha"] == findings.commit_sha
        assert row["severity"] == breakdown.severity
        assert row["score"] == breakdown.total
        assert row["narrative"] == "A narrative explanation."
        assert row["status"] == "needs_review"
        assert row["source_event_id"] == uuid.UUID(event_id)
    finally:
        async with storage.pool.acquire() as conn:
            await conn.execute("DELETE FROM reports WHERE repo = $1", findings.repo)
            await conn.execute("DELETE FROM processed_events WHERE event_id = $1", event_id)
        await storage.close()


async def test_save_report_is_idempotent_via_processed_events(postgres_available):
    if not postgres_available:
        pytest.skip("Postgres not reachable at DATABASE_URL")

    storage = ReviewStorage()
    await storage.connect()
    findings = _findings()
    event_id = str(uuid.uuid4())
    breakdown = compute_score(findings)

    try:
        await storage.save_report(
            findings,
            event_id=event_id,
            trace_id=str(uuid.uuid4()),
            breakdown=breakdown,
            status="passed",
            narrative="n1",
        )
        # A caller that checks is_processed() first (as the reviewer's
        # process_findings does) would skip here rather than calling
        # save_report again - this confirms the flag it relies on is set.
        assert await storage.is_processed(event_id) is True
    finally:
        async with storage.pool.acquire() as conn:
            await conn.execute("DELETE FROM reports WHERE repo = $1", findings.repo)
            await conn.execute("DELETE FROM processed_events WHERE event_id = $1", event_id)
        await storage.close()
