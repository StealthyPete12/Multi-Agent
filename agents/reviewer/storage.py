"""Postgres storage for the Reviewer: persisted reports plus the
idempotency check against ``processed_events``.

Uses the existing ``reports``/``processed_events`` tables from
``db/migrations/001_init_schema.sql``, extended by
``db/migrations/002_reviewer_reports.sql`` with the columns Phase 3 needs
(severity/score/narrative/etc.) — see that migration's header comment for
why an extension was required rather than reusing the Phase 0 columns
as-is.

Idempotency: ``processed_events`` was defined in Phase 0 for exactly this
purpose ("every consumer checks/writes event_id here before acting on a
message") but nothing used it yet. The Reviewer is the first consumer to
actually do so, closing that Phase 0 gap.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass

import asyncpg
from opentelemetry.trace import SpanKind

from agents.reviewer.scoring import ScoreBreakdown
from shared import telemetry
from shared.contracts import FindingsReady
from shared.logging import configure_logging

__all__ = ["ReviewStorage", "SavedReport", "DEFAULT_DATABASE_URL"]

log = configure_logging(service_name="reviewer")

DEFAULT_DATABASE_URL = "postgresql://swarm:swarm_dev_password@localhost:5432/code_review_swarm"


@dataclass(frozen=True)
class SavedReport:
    report_id: str
    status: str


class ReviewStorage:
    """Thin asyncpg pool wrapper, mirroring
    ``agents/researcher/db.py::Database``'s connect/close lifecycle."""

    def __init__(self, dsn: str | None = None) -> None:
        self.dsn = dsn or os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)
        self._pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        if self._pool is not None:
            return
        self._pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=5)
        telemetry.register_pool_gauges("reviewer", lambda: self._pool)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("ReviewStorage.connect() must be awaited before use")
        return self._pool

    async def is_processed(self, event_id: str) -> bool:
        """True if ``event_id`` (the findings.ready event) has already
        been fully processed into a report — an at-least-once redelivery
        of the same event should no-op rather than duplicate a report.

        Checks ``completed_at IS NOT NULL`` (not mere row existence):
        since Phase 4, ``shared/idempotency.py::IdempotencyStore.claim()``
        inserts a row *before* any work starts (see
        ``db/migrations/003_idempotency_claims.sql``), so a row can exist
        for an event that's claimed but not yet actually processed — that
        must not be mistaken for "done" here.
        """
        row = await self.pool.fetchrow(
            "SELECT 1 FROM processed_events WHERE event_id = $1 AND completed_at IS NOT NULL",
            event_id,
        )
        return row is not None

    async def save_report(
        self,
        findings: FindingsReady,
        *,
        event_id: str,
        trace_id: str,
        breakdown: ScoreBreakdown,
        status: str,
        narrative: str,
    ) -> SavedReport:
        """Persist one review report and mark ``event_id`` processed, in a
        single transaction so a crash between the two never leaves an
        unmarked report that would be reprocessed as a duplicate."""
        with telemetry.span(
            "database.write",
            kind=SpanKind.CLIENT,
            tracer_name="agents.reviewer",
            attributes={"swarm.repo": findings.repo, "db.operation": "save_report"},
        ) as current_span:
            started = time.monotonic()
            score_breakdown_json = json.dumps(
                {
                    "blast_radius_points": breakdown.blast_radius_points,
                    "impact_count_points": breakdown.impact_count_points,
                    "sensitive_hits_points": breakdown.sensitive_hits_points,
                    "changed_files_points": breakdown.changed_files_points,
                    "test_proximity_points": breakdown.test_proximity_points,
                }
            )
            blast_radius_json = json.dumps(
                {
                    "impacted_modules": findings.blast_radius.impacted_modules,
                    "impact_count": findings.blast_radius.impact_count,
                    "max_depth": findings.blast_radius.max_depth,
                }
            )

            try:
                async with self.pool.acquire() as conn:
                    async with conn.transaction():
                        row = await conn.fetchrow(
                            """
                            INSERT INTO reports (
                                commit_id, status, summary, findings_count, report_data,
                                repo, commit_sha, severity, score, score_breakdown,
                                narrative, blast_radius, sensitive_hits, source_event_id
                            )
                            VALUES (
                                NULL, $1, $2, $3, '{}'::jsonb,
                                $4, $5, $6, $7, $8::jsonb,
                                $9, $10::jsonb, $11::jsonb, $12
                            )
                            RETURNING id
                            """,
                            status,
                            findings.semantic_summary,
                            len(findings.findings),
                            findings.repo,
                            findings.commit_sha,
                            breakdown.severity,
                            breakdown.total,
                            score_breakdown_json,
                            narrative,
                            blast_radius_json,
                            json.dumps(findings.sensitive_hits),
                            event_id,
                        )
                        # ON CONFLICT DO UPDATE (not DO NOTHING): Phase 4's outer
                        # claim (shared/idempotency.py) already inserted this row
                        # with completed_at NULL before process_findings() ran —
                        # this is what actually marks it complete, atomically with
                        # the report row, in the same transaction.
                        await conn.execute(
                            """
                            INSERT INTO processed_events (event_id, event_type, trace_id, completed_at)
                            VALUES ($1, $2, $3, now())
                            ON CONFLICT (event_id) DO UPDATE SET completed_at = now()
                            """,
                            event_id,
                            "findings.ready",
                            trace_id,
                        )
            except Exception:
                telemetry.get_metrics().db_failures.add(1, {"operation": "save_report"})
                raise

            duration_ms = (time.monotonic() - started) * 1000
            current_span.set_attribute("swarm.duration_ms", duration_ms)
            current_span.set_attribute("swarm.report_id", str(row["id"]))
            telemetry.get_metrics().db_write_duration_ms.record(
                duration_ms, {"table": "reports", "operation": "save_report"}
            )
            log.info(
                "report persisted",
                extra={
                    "repo": findings.repo,
                    "commit_sha": findings.commit_sha,
                    "report_id": str(row["id"]),
                    "severity": breakdown.severity,
                    "score": breakdown.total,
                    "duration_ms": duration_ms,
                    "event_type": "database.write",
                },
            )
            return SavedReport(report_id=str(row["id"]), status=status)

    async def __aenter__(self) -> ReviewStorage:
        await self.connect()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()
