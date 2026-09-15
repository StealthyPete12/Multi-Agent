# Phase 0 Report — Foundational Infrastructure

**Project:** Event-Driven Multi-Agent Code Review Swarm
**Phase:** 0 — infrastructure only, no agent logic
**Status:** Complete and validated

## Starting state

The repository contained a single-line `README.md` and nothing else — no
architecture roadmap document, no code, no config. There was nothing to
audit or reuse, so this phase built the full Phase 0 deliverable set from
scratch, using the task brief as the source of truth for scope. The repo
structure for future agent phases (`agents/*`) was inferred rather than
copied from a roadmap doc, since none existed in the repo; see **Known
gaps** below.

## Completed work

| # | Deliverable | Status | Notes |
|---|-------------|--------|-------|
| 1 | Docker Compose (RabbitMQ 3.13-management, Postgres 16, Redis, healthchecks, named volumes, env-var config) | ✅ Done | [`docker-compose.yml`](docker-compose.yml) |
| 2 | Shared contracts (`Envelope`, `CommitDetected`, `FindingsReady`, `ReviewCompleted`, Pydantic v2, versioned, strict, serialize/deserialize helpers) | ✅ Done | [`shared/contracts.py`](shared/contracts.py) |
| 3 | Shared structured logging (JSON, trace_id propagation, correlation IDs) | ✅ Done | [`shared/logging.py`](shared/logging.py) |
| 4 | Database foundation (migrations + schema for all 7 tables) | ✅ Done | [`db/migrations/001_init_schema.sql`](db/migrations/001_init_schema.sql) |
| 5 | `.env.example` documenting every variable | ✅ Done | [`.env.example`](.env.example) |
| 6 | Repo structure incl. placeholders for future phases | ✅ Done | `agents/{ingestion,analysis,aggregation,orchestrator}/` |
| 7 | Validation (`docker compose up`, management UI reachable) | ✅ Done | See **Validation results** below |

No item was partially completed — everything above was built new in this
phase.

## Architecture decisions

- **Envelope pattern for messages.** Every event is an `Envelope[Payload]`
  carrying routing/tracing metadata (`event_id`, `event_type`, `trace_id`,
  `correlation_id`, `source`, `occurred_at`) around a strongly-typed
  payload. Agents can log/correlate any message without knowing its
  payload shape.
- **Strict, closed contracts.** All contract models use
  `ConfigDict(strict=True, extra="forbid", frozen=True)`. A version
  mismatch or malformed producer fails validation loudly at the consumer
  instead of silently coercing or dropping fields. Each payload also
  carries an explicit `schema_version: Literal["1.0"]`, so a future v2
  payload is a new, distinct type rather than a mutation of v1's shape.
- **JSON-mode vs. python-mode validation.** Pydantic strict mode only
  relaxes datetime/enum coercion for JSON input (`model_validate_json`),
  not for dicts (`model_validate`). `Envelope.from_json` and
  `parse_envelope` both validate directly from the original JSON text for
  this reason — see the comment in `shared/contracts.py::parse_envelope`.
- **`parse_envelope` registry dispatch.** A consumer bound to a queue that
  can carry more than one event type doesn't need to know the payload
  type ahead of time — `parse_envelope` peeks at `event_type` and looks up
  the right model in `PAYLOAD_REGISTRY`.
- **JSON logs, contextvar-scoped trace IDs.** `shared/logging.py` uses
  `contextvars` (not thread-locals or globals) so trace/correlation IDs
  stay correct under `asyncio` concurrency, which the agent services will
  need. `trace_context` is a context manager scoped to one handled
  message, so concurrent handlers can't leak IDs into each other's logs.
- **Postgres schema bootstrap via `docker-entrypoint-initdb.d`.** For
  Phase 0, `db/migrations/*.sql` is bind-mounted straight into that
  directory rather than run through a migration tool — the official
  Postgres image applies every file there, in order, on first boot
  against an empty volume. This is enough to satisfy "docker compose up
  creates the schema" with zero extra tooling. See **Known gaps** for why
  this isn't sufficient long-term.
- **`processed_events` as an idempotency ledger.** RabbitMQ's
  at-least-once delivery means every consumer must be able to no-op on a
  redelivered `event_id`. The table exists now so that contract lands
  before any consumer logic is written against it.
- **Redis requires a password even in dev**, and the compose file passes
  it via `command: redis-server --requirepass ...` rather than
  `REDIS_PASSWORD` as a container env var, because the official Redis
  image doesn't read that env var itself.

## Validation results

Ran `scripts/validate_stack.sh` (also runnable manually):

```
==> docker compose up -d
==> waiting for healthchecks (up to 90s)
OK: rabbitmq is healthy
OK: postgres is healthy
OK: redis is healthy
==> checking RabbitMQ management UI
OK: RabbitMQ management UI reachable at http://localhost:15672
==> checking Postgres schema
OK: table 'modules' exists
OK: table 'imports' exists
OK: table 'commits' exists
OK: table 'findings' exists
OK: table 'reports' exists
OK: table 'processed_events' exists
OK: table 'audit_log' exists
==> all checks passed
```

Additionally verified directly:
- `redis-cli -a <password> ping` → `PONG`
- `curl -u swarm:swarm_dev_password http://localhost:15672/api/overview` →
  valid JSON, `rabbitmq_version: 3.13.7`
- `pytest tests/` → 7/7 passing, covering round-trip
  serialization/deserialization for all three payload types, strict
  rejection of unknown fields, strict rejection of an invalid enum value,
  presence of `schema_version` on the wire, and rejection of an unknown
  `event_type` in `parse_envelope`.

## Known gaps (explicitly out of scope for Phase 0, or open decisions)

- **No architecture roadmap document exists in the repo.** The directory
  layout under `agents/` (`ingestion`, `analysis`, `aggregation`,
  `orchestrator`) and the shape of the three contracts were inferred from
  this phase's task brief, not copied from a design doc. If a roadmap
  exists outside this repo, it should be checked in (e.g.
  `docs/ARCHITECTURE.md`) and this structure reconciled against it before
  Phase 1 starts.
- **No real migration tool.** `docker-entrypoint-initdb.d` only runs once,
  against an empty volume — it cannot apply schema changes to a database
  that already has data. A tool like Alembic (or `golang-migrate`,
  `sqlx`, etc., depending on Phase 1's language choice) is needed once any
  environment has real data to preserve across a schema change. See
  `db/README.md`.
- **No message broker topology yet.** No exchanges, queues, or bindings
  are declared — RabbitMQ starts with an empty vhost. Phase 1 (or an
  orchestrator bootstrap script) needs to declare the topology that
  `CommitDetected` → `FindingsReady` → `ReviewCompleted` actually flows
  through (direct/topic exchange choice, per-agent queues, dead-letter
  queues for poison messages).
- **No agent logic.** `agents/*` are empty placeholders (`__init__.py` +
  README) by design — Phase 0 explicitly excludes agent logic.
- **No CI configuration.** Tests run locally (`pytest tests/`) but there's
  no GitHub Actions/other CI wiring yet to run them automatically or to
  run `scripts/validate_stack.sh` against a fresh checkout.
- **Dev-only credentials.** `.env.example` ships plaintext default
  passwords suitable for local development only; secrets management for
  staging/production is not addressed in this phase.
- **No `DATABASE_URL`/`RABBITMQ_URL`/`REDIS_URL` consumer yet** — these
  composed connection strings are documented in `.env.example` for
  Phase 1's services to consume, but nothing in this repo reads them yet
  since there are no services.
