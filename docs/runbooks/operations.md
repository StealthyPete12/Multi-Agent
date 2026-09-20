# Runbook: Operations

## How to start the system

See [`docs/runbooks/deploy.md`](deploy.md) for the three deployment
options (infra-only + host agents, fully containerized dev, production).
Quick reference for local development:

```bash
cp .env.example .env
docker compose -f docker-compose.dev.yml up -d --build
docker compose -f docker-compose.dev.yml ps      # wait for all "healthy"
```

## How to generate sample events

```bash
# a single explicit synthetic commit
python -m tools.seed_commit --repo acme/widgets --branch main \
    --author jane --message "fix: off by one"

# a random one, or a burst of N
python -m tools.seed_commit --random
python -m tools.seed_commit --random --count 5

# skip straight to the reviewer (bypasses the researcher entirely)
python -m tools.seed_findings --repo acme/widgets --sha low0001
python -m tools.seed_findings --repo acme/widgets --sha sens0001 \
    --changed-files auth/login.py --sensitive-hits auth/login.py

# a real GitHub-style signed webhook, if the watcher is running:
curl -X POST http://localhost:8001/webhook/github \
    -H "X-GitHub-Event: push" -H "X-Hub-Signature-256: sha256=<hmac>" \
    -d '<push payload>'
```

See the README's "Local testing workflow" section for the fully offline
(no real GitHub repo needed) variant using a `file://` git remote, and
[`docs/demo/`](../demo/) for the exact payload shapes these produce.

## How to observe the entire pipeline

| What | Where |
|---|---|
| RabbitMQ queues/DLQ depth | http://localhost:15672 (management UI) |
| Structured logs, one JSON object per line | each service's stdout — `grep '"trace_id": "<id>"'` across watcher/researcher/reviewer to follow one commit |
| Distributed trace (13 spans across all 3 services) | http://localhost:6006 (Phoenix) |
| Metrics (throughput, retries, DLQ, LLM cost/latency, blast-radius/DB timings) | http://localhost:9090 (Prometheus) or the raw `/metrics` endpoints (`:8001`, `:9102`, `:9103`) |
| Dashboards (System Overview, LLM, Repository) | http://localhost:3000 (`admin`/`admin` by default) |
| Persisted reports | Postgres `reports` table — see below |

See [`docs/runbooks/recovery.md`](recovery.md) for the detailed
trace-verification and report-inspection procedures, and
[`docs/architecture.md`](../architecture.md) for what the observability
stack looks like end to end.

## How to investigate failures

See [`docs/runbooks/recovery.md`](recovery.md)'s "How to investigate a
failure" and "How to replay the DLQ" sections — this covers classifying a
failure from its log line, checking for a stuck idempotency claim, and
the DLQ inspect/dry-run/replay workflow.

## How to verify traces

See [`docs/runbooks/recovery.md`](recovery.md)'s "How to verify traces"
section.

## How to inspect reports

```bash
docker compose exec postgres psql -U swarm -d code_review_swarm \
    -c "SELECT repo, commit_sha, severity, score, status, generated_at
        FROM reports ORDER BY generated_at DESC LIMIT 20;"
```

See [`docs/runbooks/recovery.md`](recovery.md)'s "How to inspect reports"
for the full-detail query and [`docs/demo/sample_review.json`](../demo/sample_review.json)
for the exact row shape.

## How to troubleshoot LLM providers

See [`docs/runbooks/recovery.md`](recovery.md)'s "How to troubleshoot LLM
providers" section — covers missing credentials, circuit-breaker state,
rate-limit delays, and per-provider failure reasons.

## Routine maintenance

- **Check for a growing DLQ.** `swarm_dlq_total` (Prometheus) or
  `rabbitmqctl list_queues name messages | grep dlq` — a steadily growing
  DLQ usually means a systemic issue (bad credentials, a downstream outage)
  rather than a one-off; inspect before replaying (see recovery.md).
- **Check circuit-breaker opens.** `swarm_circuit_breaker_opens_total` —
  frequent opens against a live provider usually means the provider is
  degraded, not a bug here (see recovery.md's LLM section).
- **Rotate credentials** (`GITHUB_WEBHOOK_SECRET`, `RABBITMQ_PASSWORD`,
  `POSTGRES_PASSWORD`, `REDIS_PASSWORD`, LLM API keys) by updating `.env`
  and recreating the affected containers
  (`docker compose -f docker-compose.prod.yml up -d --force-recreate`).
  RabbitMQ/Postgres/Redis password rotation requires updating the
  running service's own credential too (these images set it at first
  boot from a fresh volume, not on every restart).
- **Apply a new database migration** manually against a running volume
  (no migration runner yet — see [`db/README.md`](../../db/README.md) and
  `ROADMAP.md`):
  ```bash
  docker compose exec -T postgres psql -U swarm -d code_review_swarm \
      < db/migrations/00N_new_migration.sql
  ```
