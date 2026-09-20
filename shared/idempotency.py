"""Generic claim-before-processing idempotency, shared by every consumer.

Reuses the ``processed_events`` table Phase 0 created for exactly this
purpose (see ``db/migrations/001_init_schema.sql``), extended by
``db/migrations/003_idempotency_claims.sql`` with ``claimed_at``/
``completed_at`` so a claim and its completion are two distinct, checkable
states rather than one insert meaning both.

**Why "claim first" instead of "check, do work, then record"**: the
Reviewer's Phase 3 storage layer checked ``is_processed()`` *before* doing
any work but only wrote the ``processed_events`` row at the very end,
atomically with the report insert. That closes the gap for a single
worker's crash-mid-processing, but not for two workers concurrently
handling a redelivered copy of the same message (RabbitMQ is
at-least-once, not exactly-once) — both can pass the early check, both do
the full (expensive, side-effecting: LLM call, Slack post, DB write)
pipeline, and only then race on the final insert. One of them succeeds;
the other's transaction already sent a duplicate Slack message. Claiming
*before* any work closes that race: ``INSERT ... ON CONFLICT (event_id)
DO NOTHING`` is atomic, so only one concurrent caller ever gets
``claimed=True`` — the other bails out before doing anything observable.

**Why completion is a separate step from the claim**: a caught
:class:`~shared.errors.RetryableError` can call :meth:`release` to free
the claim for a later retry. But a hard process crash (SIGKILL, OOM-kill,
a node dying) never runs any exception handler — the claim row is left
behind with no record of whether the work it guarded ever finished.
Without a way to tell "claimed and completed" apart from "claimed, then
the process died mid-work", a crash followed by RabbitMQ's normal
redeliver-the-unacked-message behavior would find the event permanently
"already claimed" and skip it forever: the message survives at the
broker, but the pipeline silently never produces its output. ``claim()``
treats a claim whose ``completed_at`` is still NULL after
``stale_after_seconds`` as abandoned and lets a new caller reclaim it.
"""

from __future__ import annotations

import os

import asyncpg

from shared.logging import configure_logging

__all__ = ["IdempotencyStore"]

log = configure_logging(service_name="idempotency")

DEFAULT_STALE_CLAIM_SECONDS = float(os.environ.get("IDEMPOTENCY_STALE_CLAIM_SECONDS", "300"))


class IdempotencyStore:
    """Wraps an already-connected ``asyncpg.Pool`` with claim/complete/
    release operations against ``processed_events``. Stateless beyond the
    pool reference — safe to construct fresh per call site, or share one
    instance across a consumer's lifetime."""

    def __init__(
        self, pool: asyncpg.Pool, *, stale_after_seconds: float = DEFAULT_STALE_CLAIM_SECONDS
    ) -> None:
        self._pool = pool
        self.stale_after_seconds = stale_after_seconds

    async def claim(self, *, event_id: str, event_type: str, trace_id: str | None) -> bool:
        """Atomically claim ``event_id``. Returns ``True`` if this call
        claimed it (caller should proceed), ``False`` if it's already
        claimed by someone else's still-active or completed work.

        A fresh insert wins the claim outright. Losing that insert (the
        row already exists) falls back to a conditional reclaim: only a
        row that is neither completed nor claimed within
        ``stale_after_seconds`` is treated as abandoned and reclaimed.
        """
        inserted = await self._pool.fetchrow(
            """
            INSERT INTO processed_events (event_id, event_type, trace_id, claimed_at, completed_at)
            VALUES ($1, $2, $3, now(), NULL)
            ON CONFLICT (event_id) DO NOTHING
            RETURNING id
            """,
            event_id,
            event_type,
            trace_id,
        )
        if inserted is not None:
            log.info(
                "idempotency claim",
                extra={
                    "event_id": event_id,
                    "event_type": event_type,
                    "claimed": True,
                    "reclaimed": False,
                },
            )
            return True

        reclaimed = await self._pool.fetchrow(
            """
            UPDATE processed_events
            SET claimed_at = now(), trace_id = $3
            WHERE event_id = $1
              AND completed_at IS NULL
              AND claimed_at < now() - ($2 * interval '1 second')
            RETURNING id
            """,
            event_id,
            self.stale_after_seconds,
            trace_id,
        )
        claimed = reclaimed is not None
        log.info(
            "idempotency claim",
            extra={
                "event_id": event_id,
                "event_type": event_type,
                "claimed": claimed,
                "reclaimed": claimed,
            },
        )
        return claimed

    async def mark_complete(self, event_id: str) -> None:
        """Record that the work a claim guarded finished successfully.
        Call this once, after every side effect (publish, persist, notify)
        has actually happened — never before."""
        await self._pool.execute(
            "UPDATE processed_events SET completed_at = now() WHERE event_id = $1", event_id
        )
        log.info("idempotency complete", extra={"event_id": event_id})

    async def release(self, event_id: str) -> None:
        """Undo a claim after a *caught* retryable failure, so an
        immediate retry-ladder redelivery (with the same ``event_id``)
        doesn't have to wait out ``stale_after_seconds`` to reclaim it.
        Never call this after a poison/fatal failure or after
        :meth:`mark_complete`."""
        await self._pool.execute("DELETE FROM processed_events WHERE event_id = $1", event_id)
        log.info("idempotency release", extra={"event_id": event_id})

    async def is_claimed(self, event_id: str) -> bool:
        row = await self._pool.fetchrow(
            "SELECT 1 FROM processed_events WHERE event_id = $1", event_id
        )
        return row is not None

    async def is_completed(self, event_id: str) -> bool:
        row = await self._pool.fetchrow(
            "SELECT 1 FROM processed_events WHERE event_id = $1 AND completed_at IS NOT NULL",
            event_id,
        )
        return row is not None
