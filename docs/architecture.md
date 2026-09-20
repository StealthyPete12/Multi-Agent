# Architecture

This document is the single diagram-first reference for how the swarm
fits together. For the prose version of any of this — why a decision was
made, what was tried, what broke — the six `PHASE_*_REPORT.md` files at
the repo root are the primary source; this page summarizes and diagrams
what they describe.

## System overview

```mermaid
flowchart LR
    GH[GitHub push] -->|"HTTPS POST /webhook/github\n(HMAC-signed)"| Watcher
    Watcher["Watcher\n(FastAPI)"] -->|commit.detected| MQ1[(RabbitMQ)]
    MQ1 --> Researcher["Researcher\n(clone, AST graph,\nblast radius, LLM summary)"]
    Researcher -->|findings.ready| MQ2[(RabbitMQ)]
    MQ2 --> Reviewer["Reviewer\n(risk score, LLM narrative,\nPostgres, Slack)"]
    Reviewer -->|review.completed| MQ3[(RabbitMQ)]
    Reviewer -->|Block Kit message| Slack[Slack]

    classDef svc fill:#2563eb,color:#fff,stroke:none;
    class Watcher,Researcher,Reviewer svc;
```

Every hop is an `Envelope[Payload]` (see [`shared/contracts.py`](../shared/contracts.py))
carrying a `trace_id` generated once per webhook delivery and threaded
through every log line and OTel span — see [Data flow](#data-flow) below
for what actually rides in that envelope at each stage, and
[`docs/demo/`](demo/) for real, schema-validated example payloads.

## Data flow

Each stage adds fields to what it knows; nothing already on the wire is
ever removed (see `shared/contracts.py`'s "additive only" evolution
described in `PHASE_2_REPORT.md`/`PHASE_3_REPORT.md`).

```mermaid
sequenceDiagram
    participant GH as GitHub
    participant W as Watcher
    participant MQ as RabbitMQ
    participant R as Researcher
    participant Rv as Reviewer
    participant PG as Postgres
    participant S as Slack

    GH->>W: push webhook (HMAC-signed)
    W->>W: verify signature, extract commits
    W->>MQ: publish commit.detected (per commit)
    MQ->>R: deliver commit.detected
    R->>R: claim event_id (idempotency)
    R->>R: clone/refresh repo, build AST import graph
    R->>PG: upsert modules/imports
    R->>PG: recursive-CTE blast radius query
    R->>R: sensitive-path check
    R->>R: LLM semantic summary (best-effort, "" on failure)
    R->>MQ: publish findings.ready
    MQ->>Rv: deliver findings.ready
    Rv->>Rv: claim event_id (idempotency)
    Rv->>Rv: deterministic risk score (no LLM)
    Rv->>Rv: LLM narrative (explains score, template fallback)
    Rv->>PG: persist report + mark processed_events complete
    Rv->>S: POST Block Kit message (no-op if unconfigured)
    Rv->>MQ: publish review.completed
```

See [`docs/demo/`](demo/) for one real, internally-consistent set of
payloads for every arrow above (`sample_commit.json` ->
`sample_findings.json` -> `sample_review.json` ->
`sample_slack_message.json`), sharing one `trace_id` and a real
correlation-id chain.

## Failure recovery: retry and DLQ flow

RabbitMQ has no native delayed-retry primitive, so this is a hand-built
TTL + dead-letter-exchange "parking lot" ladder — see
`shared/retry.py` and `PHASE_4_REPORT.md`'s "Retry-ladder flow" for the
routing-key-preservation subtlety this depends on.

```mermaid
flowchart TD
    Msg[Message delivered] --> Handle{Consumer\nhandle_message}
    Handle -->|success| Ack[ack]
    Handle -->|exception| Classify["classify_exception()\nshared/errors.py"]

    Classify -->|RetryableError| Ladder
    Classify -->|PoisonMessageError| DLQ["q.dlq\n(immediate, zero retries)"]
    Classify -->|FatalError| Fatal["nack(requeue=True)\n+ service exits\n(SystemExit 1)"]

    subgraph Ladder[Retry ladder]
        direction TB
        R1["q.retry.5s\n(TTL 5000ms)"] -->|expires| Back1[redelivered to\norigin queue]
        R2["q.retry.30s\n(TTL 30000ms)"] -->|expires| Back2[redelivered to\norigin queue]
        R3["q.retry.5m\n(TTL 300000ms)"] -->|expires| Back3[redelivered to\norigin queue]
    end

    Back1 --> Handle
    Back2 --> Handle
    Back3 --> Handle
    Ladder -->|attempt 1 fails| R1
    Ladder -->|attempt 2 fails| R2
    Ladder -->|attempt 3 fails| R3
    Ladder -->|attempt 4 fails| DLQ

    DLQ -->|"tools/replay_dlq.py\n(inspect / dry-run / replay)"| Handle

    classDef term fill:#dc2626,color:#fff,stroke:none;
    class DLQ,Fatal term;
```

Idempotency (`shared/idempotency.py`) sits underneath every path here: a
claim is taken before any work starts and released on a retryable
failure, so a redelivery from any rung of the ladder — or a crash with no
release at all — can never produce a duplicate report or Slack message.
See `PHASE_4_REPORT.md`'s "Idempotency design" and "Chaos test evidence"
(scenarios A/B/E) for the exact crash/duplicate scenarios this was
validated against.

## Observability stack

```mermaid
flowchart TB
    subgraph Services["Watcher / Researcher / Reviewer"]
        direction LR
        Spans[OTel spans] 
        Metrics["Prometheus\n/metrics endpoints\n(:8001, :9102, :9103)"]
        Logs["JSON logs\n(trace_id + otel_trace_id)"]
    end

    Spans -->|OTLP HTTP| Phoenix["Arize Phoenix\n(trace UI, :6006)"]
    Metrics -->|scrape| Prometheus["Prometheus\n(:9090)"]
    RabbitMQ["RabbitMQ\nrabbitmq_prometheus\n(:15692)"] -->|scrape| Prometheus
    PGExporter["postgres_exporter\n(:9187)"] -->|scrape| Prometheus
    Prometheus -->|datasource| Grafana["Grafana\n(:3000)\n3 provisioned dashboards"]
    Logs -.->|"grep by trace_id /\notel_trace_id"| Operator((Operator))
    Phoenix -.-> Operator
    Grafana -.-> Operator
```

Every metric name a dashboard panel queries is statically cross-checked
against `shared/telemetry.py`'s real registered Prometheus instrument
names by `tests/test_observability_config.py` — see `PHASE_5_REPORT.md`
for the catalog and the one real bug this caught (`_ms` vs
`_milliseconds` suffix mismatch) before it ever reached a running
Prometheus.

## Persistence layer

```mermaid
erDiagram
    MODULES ||--o{ IMPORTS : "importer/imported_name"
    REPORTS {
        uuid id PK
        text repo
        text commit_sha
        text severity
        int score
        jsonb score_breakdown
        text narrative
        jsonb blast_radius
        jsonb sensitive_hits
        uuid source_event_id
        timestamptz generated_at
    }
    MODULES {
        uuid id PK
        text repo
        text path
        text name
        text language
    }
    IMPORTS {
        uuid id PK
        uuid module_id FK
        text imported_name
        text import_type
    }
    PROCESSED_EVENTS {
        text event_id PK
        text event_type
        timestamptz claimed_at
        timestamptz completed_at
    }
    COMMITS {
        uuid id PK
        text repo
        text sha
    }
    FINDINGS {
        uuid id PK
        uuid commit_id FK
    }
    AUDIT_LOG {
        uuid id PK
        text action
        jsonb detail
    }
    REPORTS }o--o| COMMITS : "commit_id (nullable, see 002_reviewer_reports.sql)"
```

- **`modules`/`imports`** — the researcher's persisted AST dependency
  graph (`agents/researcher/db.py`), one row per file / per import edge,
  upserted idempotently per commit. The recursive-CTE blast-radius query
  (`PHASE_2_REPORT.md`) runs directly over these two tables.
- **`reports`** — the reviewer's output (`db/migrations/002_reviewer_reports.sql`),
  keyed by `(repo, commit_sha)` rather than a hard FK to `commits` (nothing
  populates `commits` yet — see that migration's own header comment for
  why, and `PHASE_3_REPORT.md`'s "Known limitations").
- **`processed_events`** — the idempotency ledger (`shared/idempotency.py`),
  extended in `db/migrations/003_idempotency_claims.sql` with
  `claimed_at`/`completed_at` so a claim and its completion are
  distinguishable — this is what makes a hard crash mid-message safely
  reclaimable instead of a silent permanent skip.
- **`commits`/`findings`/`audit_log`** — declared in
  `db/migrations/001_init_schema.sql` for a future phase; not written to
  by any agent today (see `PHASE_3_REPORT.md`'s known limitations).

Redis holds no durable state relevant to this diagram — it's the
rate-limiter's token-bucket counters (`shared/ratelimit.py`), ephemeral by
design.
