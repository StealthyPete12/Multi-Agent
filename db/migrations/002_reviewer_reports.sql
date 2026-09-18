-- 002_reviewer_reports.sql
-- Phase 3: extend `reports` for the Reviewer agent's persisted output.
--
-- Same auto-init caveat as 001 (see db/README.md): this only applies to a
-- fresh Postgres volume. An existing running stack needs either
-- `docker compose down -v && docker compose up -d` (dev, data loss) or
-- this file applied by hand with `psql`/a migration runner (a real
-- migration tool is still a known gap carried over from Phase 0).
--
-- Why a migration is required (not just reusing 001's schema as-is):
-- `reports.commit_id` is NOT NULL and references `commits(id)`, but
-- nothing in Phases 1-2 ever inserts into `commits` -- the watcher only
-- has commit metadata, and `FindingsReady` (all the Reviewer consumes)
-- doesn't carry branch/author/message needed to backfill a `commits` row
-- itself. Rather than making the Reviewer responsible for populating a
-- table it has no full data for, `commit_id` becomes optional and the
-- report carries `repo`/`commit_sha` directly, which is everything
-- `FindingsReady` actually has.

ALTER TABLE reports
    ALTER COLUMN commit_id DROP NOT NULL;

ALTER TABLE reports
    ADD COLUMN IF NOT EXISTS repo             TEXT,
    ADD COLUMN IF NOT EXISTS commit_sha       TEXT,
    ADD COLUMN IF NOT EXISTS severity         TEXT
        CHECK (severity IN ('low', 'moderate', 'high', 'critical')),
    ADD COLUMN IF NOT EXISTS score            INTEGER,
    ADD COLUMN IF NOT EXISTS score_breakdown  JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS narrative        TEXT,
    ADD COLUMN IF NOT EXISTS blast_radius     JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS sensitive_hits   JSONB NOT NULL DEFAULT '[]'::jsonb,
    -- The findings.ready event_id this report was generated from. Not a
    -- hard UNIQUE constraint (a deliberate re-review of the same commit
    -- is a legitimate future scenario); the actual idempotency guard is
    -- `processed_events.event_id` (UNIQUE from 001), checked and written
    -- inside the same transaction as this insert.
    ADD COLUMN IF NOT EXISTS source_event_id  UUID;

CREATE INDEX IF NOT EXISTS idx_reports_repo_commit_sha ON reports(repo, commit_sha);
CREATE INDEX IF NOT EXISTS idx_reports_source_event_id ON reports(source_event_id);
