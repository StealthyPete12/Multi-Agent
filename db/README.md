# Database

PostgreSQL 16 schema for the code review swarm.

## How migrations are applied

`migrations/*.sql` is bind-mounted into the `postgres` container at
`/docker-entrypoint-initdb.d`. The official Postgres image runs every
`.sql`/`.sh` file in that directory, in alphabetical order, **once** — the
first time the container starts against an empty data volume. That's why
files are numbered (`001_`, `002_`, ...): the number controls apply order.

This means:

- Editing an already-applied migration file has no effect on an existing
  database — it only affects fresh volumes. Ship schema changes as a new
  `NNN_description.sql` file instead.
- To re-run migrations from scratch in local dev, drop the named volume
  (`docker compose down -v`) and start the stack again.
- Phase 0 relies on this auto-init behavior for bootstrapping. A real
  migration runner (Alembic or similar) that can apply incremental
  migrations to a live database is expected in a later phase — see
  `PHASE_0_REPORT.md` for details.

## Tables

| Table              | Purpose                                                              |
|--------------------|-----------------------------------------------------------------------|
| `modules`          | Source modules/files discovered per repo.                            |
| `imports`          | Import edges between modules (dependency graph).                     |
| `commits`          | Commits detected by ingestion and their review status.                |
| `findings`         | Individual issues raised by analysis agents against a commit.        |
| `reports`          | Aggregated review report generated per commit.                       |
| `processed_events` | Idempotency ledger — one row per successfully processed `event_id`.  |
| `audit_log`        | Append-only record of notable events across the system.              |
