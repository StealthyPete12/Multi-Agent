"""Generic claim-before-processing idempotency, shared by every consumer.

Reuses the ``processed_events`` table Phase 0 created for exactly this
purpose (see ``db/migrations/001_init_schema.sql``) — no schema change.

**Why "claim first" instead of "check, do work, then record"**: the
Reviewer's Phase 3 storage layer checked ``is_processed()`` *before* doing
any work but only wrote the ``processed_events`` row at the very end,
atomically with the report insert. That closes the gap for a single
worker's crash-mid-processing, but not for two workers concurrently
handling a redelivered copy of the same message (RabbitMQ is
at-least-once, not exactly-once) — both can pass the early check, both do
the full (expensive, side-effecting: LLM call, Slack post, DB write)
pipeline, and only then race on the final insert. One of them succeeds;
the other's transaction still already sent a duplicate Slack message.

Claiming *before* any work closes that race: ``INSERT ... ON CONFLICT
(event_id) DO NOTHING`` is atomic at the database level, so only one
concurrent caller ever gets ``claimed=True`` for a given ``event_id`` —
the other bails out immediately, before doing anything observable.

The three-step contract this module supports:

1. **claim** — ``claim()`` returns ``True`` (proceed) or ``False``
   (duplicate, skip entirely).
2. **perform work** — the caller's normal pipeline. If this raises a
   :class:`~shared.errors.RetryableError`, call :meth:`release` so a
   later retry-ladder redelivery can reclaim the same ``event_id``
   instead of being permanently skipped as "already processed" for work
   that never actually completed.
3. **mark complete** — nothing further to do: the claim row inserted in
   step 1 *is* the completion marker once work succeeds. (A poison or
   fatal failure also leaves the claim in place deliberately — that
   message is never going to be retried, so "seen but failed" is an
   accurate, harmless terminal state for it.)
"""

from __future__ import annotations

import asyncpg

from shared.logging import configure_logging

__all__ = ["IdempotencyStore"]

log = configure_logging(service_name="idempotency")


class IdempotencyStore:
    """Wraps an already-connected ``asyncpg.Pool`` with claim/release
    operations against ``processed_events``. Stateless beyond the pool
    reference — safe to construct fresh per call site, or share one
    instance across a consumer's lifetime."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def claim(self, *, event_id: str, event_type: str, trace_id: str | None) -> bool:
        """Atomically claim ``event_id``. Returns ``True`` if this call
        claimed it (caller should proceed), ``False`` if it was already
        claimed (caller should skip — a duplicate delivery)."""
        row = await self._pool.fetchrow(
            """
            INSERT INTO processed_events (event_id, event_type, trace_id)
            VALUES ($1, $2, $3)
            ON CONFLICT (event_id) DO NOTHING
            RETURNING id
            """,
            event_id,
            event_type,
            trace_id,
        )
        claimed = row is not None
        log.info(
            "idempotency claim",
            extra={"event_id": event_id, "event_type": event_type, "claimed": claimed},
        )
        return claimed

    async def release(self, event_id: str) -> None:
        """Undo a claim after a retryable failure, so a later attempt
        (redelivered by the retry ladder with the same ``event_id``) can
        reclaim and actually complete the work. Never call this after a
        poison/fatal failure or after successful completion."""
        await self._pool.execute("DELETE FROM processed_events WHERE event_id = $1", event_id)
        log.info("idempotency release", extra={"event_id": event_id})

    async def is_claimed(self, event_id: str) -> bool:
        row = await self._pool.fetchrow(
            "SELECT 1 FROM processed_events WHERE event_id = $1", event_id
        )
        return row is not None

    async def claim_and_process(self, *, event_id: str, event_type: str, trace_id: str | None, work):
        """Convenience wrapper: claim, run ``work()`` (a zero-arg async
        callable), release the claim on a retryable failure so the next
        redelivery can retry, and re-raise in every failure case.

        Returns the sentinel ``_DUPLICATE`` if the event was already
        claimed (skip), otherwise ``work()``'s return value.
        """
        from shared.errors import RetryableError  # local import: avoid a cycle at module load time

        if not await self.claim(event_id=event_id, event_type=event_type, trace_id=trace_id):
            return _DUPLICATE
        try:
            return await work()
        except RetryableError:
            await self.release(event_id)
            raise


class _Duplicate:
    def __repr__(self) -> str:
        return "<duplicate event, already processed>"


_DUPLICATE = _Duplicate()
