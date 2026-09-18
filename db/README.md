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
| `commits`          | Commits detected by ingestion and their review status. **Not yet written to by any agent** — see Known limitations in `PHASE_3_REPORT.md`. |
| `findings`         | Individual issues raised by analysis agents against a commit.        |
| `reports`          | Reviewer output per commit: score, severity, narrative, blast radius, sensitive hits (extended by `002_reviewer_reports.sql` — see that file's header for why `commit_id` is nullable and `repo`/`commit_sha` live directly on the row). |
| `processed_events` | Idempotency ledger — one row per successfully processed `event_id`. First used by `agents/reviewer` (Phase 3) to dedupe `findings.ready` redeliveries. |
| `audit_log`        | Append-only record of notable events across the system. Still unused. |

## Applying `002_reviewer_reports.sql` to an already-running stack

`docker-entrypoint-initdb.d` only runs against a fresh volume (see above),
so a Postgres container started before Phase 3 needs this migration
applied by hand once:

```bash
docker compose exec -T postgres psql -U "${POSTGRES_USER:-swarm}" -d "${POSTGRES_DB:-code_review_swarm}" \
    < db/migrations/002_reviewer_reports.sql
```

A fresh `docker compose up -d` (new volume, or after `docker compose down -v`)
picks it up automatically alongside `001_init_schema.sql`.
