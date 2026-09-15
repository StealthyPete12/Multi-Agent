-- 001_init_schema.sql
-- Phase 0 foundational schema for the code review swarm.
--
-- Applied automatically on first container start via Postgres's
-- /docker-entrypoint-initdb.d mechanism (see docker-compose.yml). Files in
-- that directory only run once, against an empty data directory — future
-- schema changes must ship as a new NNN_description.sql file rather than
-- editing this one, and be applied with an explicit migration runner.

-- gen_random_uuid() is built into Postgres 16 core (pgcrypto no longer
-- required as of PG13+).

CREATE TABLE IF NOT EXISTS modules (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    repo        TEXT NOT NULL,
    path        TEXT NOT NULL,
    name        TEXT NOT NULL,
    language    TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (repo, path)
);

CREATE TABLE IF NOT EXISTS imports (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    module_id      UUID NOT NULL REFERENCES modules(id) ON DELETE CASCADE,
    imported_name  TEXT NOT NULL,
    import_type    TEXT,
    line_number    INTEGER,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_imports_module_id ON imports(module_id);

CREATE TABLE IF NOT EXISTS commits (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    repo          TEXT NOT NULL,
    commit_sha    TEXT NOT NULL,
    branch        TEXT NOT NULL,
    author        TEXT NOT NULL,
    message       TEXT,
    committed_at  TIMESTAMPTZ NOT NULL,
    detected_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    status        TEXT NOT NULL DEFAULT 'pending',
    trace_id      UUID,
    UNIQUE (repo, commit_sha)
);
CREATE INDEX IF NOT EXISTS idx_commits_status ON commits(status);

CREATE TABLE IF NOT EXISTS findings (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    commit_id     UUID NOT NULL REFERENCES commits(id) ON DELETE CASCADE,
    agent_name    TEXT NOT NULL,
    severity      TEXT NOT NULL CHECK (severity IN ('info', 'low', 'medium', 'high', 'critical')),
    category      TEXT NOT NULL,
    file_path     TEXT NOT NULL,
    line_number   INTEGER,
    message       TEXT NOT NULL,
    details       JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_findings_commit_id ON findings(commit_id);
CREATE INDEX IF NOT EXISTS idx_findings_severity ON findings(severity);

CREATE TABLE IF NOT EXISTS reports (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    commit_id       UUID NOT NULL REFERENCES commits(id) ON DELETE CASCADE,
    status          TEXT NOT NULL CHECK (status IN ('passed', 'failed', 'needs_review')),
    summary         TEXT,
    findings_count  INTEGER NOT NULL DEFAULT 0,
    report_data     JSONB NOT NULL DEFAULT '{}'::jsonb,
    generated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_reports_commit_id ON reports(commit_id);

-- Idempotency ledger: every consumer checks/writes event_id here before
-- acting on a message, so an at-least-once redelivery from RabbitMQ is a
-- no-op instead of a duplicate side effect.
CREATE TABLE IF NOT EXISTS processed_events (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id      UUID NOT NULL UNIQUE,
    event_type    TEXT NOT NULL,
    trace_id      UUID,
    processed_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_processed_events_event_type ON processed_events(event_type);

CREATE TABLE IF NOT EXISTS audit_log (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_type   TEXT NOT NULL,
    entity_type  TEXT,
    entity_id    UUID,
    actor        TEXT,
    payload      JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_audit_log_entity ON audit_log(entity_type, entity_id);
