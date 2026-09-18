-- 003_idempotency_claims.sql
-- Phase 4: turn processed_events into a real claim-before-processing
-- ledger (shared/idempotency.py), not just a completion marker.
--
-- Same auto-init caveat as 001/002 (see db/README.md): only applies to a
-- fresh Postgres volume; an existing stack needs this applied by hand.
--
-- Why this is needed: Phase 4's consumers claim an event_id (insert this
-- row) *before* doing any work, so a caught RetryableError can release()
-- the claim for a later retry-ladder attempt to reclaim. But a hard
-- process crash (SIGKILL, OOM-kill, node failure) never runs any
-- exception handler at all -- the claim row is left behind with no
-- record of whether the work it guarded ever finished. Without a way to
-- tell "claimed and completed" apart from "claimed, then the process
-- died before finishing", a crash-then-redeliver (RabbitMQ's normal
-- behavior for an unacked message) would find the event permanently
-- "already claimed" and skip it forever -- the message survives at the
-- broker level, but the pipeline silently never produces its
-- findings.ready/review.completed. `completed_at` (NULL until the
-- claiming process finishes) plus a staleness check in
-- shared/idempotency.py closes that gap: a claim that's neither
-- completed nor released within IDEMPOTENCY_STALE_CLAIM_SECONDS is
-- assumed abandoned and can be reclaimed.

ALTER TABLE processed_events
    ADD COLUMN IF NOT EXISTS claimed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS completed_at  TIMESTAMPTZ;

-- Rows written by Phase 3 code (a single insert-on-success, no separate
-- claim step) predate this column and represent already-finished work --
-- backfill them as completed so they aren't mistaken for an abandoned
-- claim.
UPDATE processed_events SET completed_at = processed_at WHERE completed_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_processed_events_incomplete_claims
    ON processed_events (claimed_at)
    WHERE completed_at IS NULL;
