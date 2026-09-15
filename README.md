# Multi-Agent

Event-Driven Multi-Agent Code Review Swarm.

This repo currently implements **Phase 0: foundational infrastructure** —
the message broker, database, shared contracts, and shared logging that
every future agent will build on. No agent logic exists yet. See
[`PHASE_0_REPORT.md`](PHASE_0_REPORT.md) for what was built, the
architecture decisions behind it, and known gaps.

## Repo structure

```
.
├── docker-compose.yml       # RabbitMQ, Postgres, Redis
├── .env.example             # every environment variable, documented
├── shared/
│   ├── contracts.py         # Envelope + CommitDetected/FindingsReady/ReviewCompleted (Pydantic v2)
│   └── logging.py           # structured JSON logging, trace/correlation IDs
├── db/
│   ├── migrations/          # schema, applied on first Postgres boot
│   └── README.md
├── agents/                  # placeholders for future phases
│   ├── ingestion/
│   ├── analysis/
│   ├── aggregation/
│   └── orchestrator/
├── scripts/
│   └── validate_stack.sh    # brings the stack up and checks it end-to-end
├── tests/
│   └── test_contracts.py
└── PHASE_0_REPORT.md
```

## Prerequisites

- Docker + Docker Compose v2
- Python 3.11+ (for `shared/` and running tests)

## Setup

1. Copy the environment template and adjust if needed (defaults work for
   local development):

   ```bash
   cp .env.example .env
   ```

2. Start the infrastructure:

   ```bash
   docker compose up -d
   ```

   This starts RabbitMQ (broker + management UI), PostgreSQL (with the
   schema in [`db/migrations`](db/migrations) applied automatically on
   first boot), and Redis. All three have healthchecks; `docker compose
   ps` shows `healthy` once ready.

3. Verify everything end-to-end (healthchecks, management UI, schema):

   ```bash
   ./scripts/validate_stack.sh
   ```

4. Install the shared package and dev dependencies, then run tests:

   ```bash
   pip install -e '.[dev]'
   pytest tests/
   ```

## Using the stack

- **RabbitMQ management UI:** http://localhost:15672
  (user/password from `.env`, default `swarm` / `swarm_dev_password`)
- **Postgres:** `postgresql://swarm:swarm_dev_password@localhost:5432/code_review_swarm`
  (or read `DATABASE_URL` from `.env`)
- **Redis:** `redis://:swarm_dev_password@localhost:6379/0`
  (or read `REDIS_URL` from `.env`)

## Shared contracts

`shared/contracts.py` defines the versioned, strictly-typed messages that
flow through RabbitMQ:

```python
from shared.contracts import CommitDetected, EventType, make_envelope

payload = CommitDetected(
    repo="acme/widgets",
    commit_sha="...",
    branch="main",
    author="jane@acme.dev",
    message="fix: off by one",
    committed_at=...,
)
envelope = make_envelope(payload, event_type=EventType.COMMIT_DETECTED, source="ingestion")
body = envelope.to_bytes()  # publish this as the RabbitMQ message body
```

On the consumer side, decode with `Envelope[CommitDetected].from_json(body)`
if the payload type is known, or `parse_envelope(body)` if a queue can
carry more than one event type.

## Shared logging

```python
from shared.logging import configure_logging, trace_context

log = configure_logging(service_name="ingestion")

with trace_context(trace_id=envelope.trace_id, correlation_id=envelope.event_id):
    log.info("commit detected", extra={"commit_sha": payload.commit_sha})
```

Every log line is a single JSON object on stdout, suitable for container
log collectors.

## Tearing down

```bash
docker compose down        # stop containers, keep data
docker compose down -v     # stop containers and delete named volumes
```
