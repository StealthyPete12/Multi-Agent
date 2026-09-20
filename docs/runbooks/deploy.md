# Runbook: Deploy

## Which compose file

| File | Use when |
|---|---|
| [`docker-compose.yml`](../../docker-compose.yml) | Infra only (RabbitMQ/Postgres/Redis/Phoenix/Prometheus/Grafana/postgres_exporter); agents run as host processes (`python -m agents.researcher.main`, `uvicorn agents.watcher.main:app`). Uses `network_mode: host` on the observability services — see [`docs/architecture.md`](../architecture.md) and `PHASE_5_REPORT.md`'s "Known limitations" for why. This is what the repo has been developed and load-tested against. |
| [`docker-compose.dev.yml`](../../docker-compose.dev.yml) | Everything containerized (agents included), standard bridge networking. The "clone the repo, run one command" path for a new machine. |
| [`docker-compose.prod.yml`](../../docker-compose.prod.yml) | Same containers, resource limits, `restart: always`, bind-mounted configurable persistence, no default secrets (fails fast if unset). Still single-host — see [Known limitations](#known-limitations). |

## First-time setup

```bash
git clone <repo-url> && cd Multi-Agent
cp .env.example .env
# edit .env: at minimum set GITHUB_WEBHOOK_SECRET to a real secret
# (openssl rand -hex 32) before exposing the watcher publicly.
```

### Option A — infra only, agents on host (`docker-compose.yml`)

```bash
docker compose up -d
./scripts/validate_stack.sh          # waits for healthchecks, checks schema
pip install -e '.[dev]'
uvicorn agents.watcher.main:app --host 0.0.0.0 --port ${WATCHER_PORT:-8001} &
python -m agents.researcher.main &
python -m agents.reviewer.main &
python -m tools.seed_commit --random   # generate a sample event
```

### Option B — fully containerized (`docker-compose.dev.yml`)

```bash
docker compose -f docker-compose.dev.yml up -d --build
# wait for `docker compose -f docker-compose.dev.yml ps` to show every
# service healthy, then:
python -m tools.seed_commit --random   # from the host, against the
                                        # published RabbitMQ port
```

### Option C — production (`docker-compose.prod.yml`)

```bash
# Every credential below is required — the compose file refuses to start
# without it (no baked-in defaults, unlike .dev.yml/.yml).
cat >> .env <<'EOF'
RABBITMQ_PASSWORD=<generate>
POSTGRES_PASSWORD=<generate>
REDIS_PASSWORD=<generate>
GRAFANA_ADMIN_PASSWORD=<generate>
GITHUB_WEBHOOK_SECRET=<generate, openssl rand -hex 32>
EOF

# Optional: point persistence at real mounted disks instead of ./data/*
echo "POSTGRES_DATA_DIR=/mnt/disks/pgdata" >> .env

docker compose -f docker-compose.prod.yml up -d --build
```

Scale the two stateless consumer agents under load (safe — see
[`docs/architecture.md`](../architecture.md)'s idempotency section and
`README.md`'s "Consumer hygiene"):

```bash
docker compose -f docker-compose.prod.yml up -d --scale researcher=3 --scale reviewer=3
```

Do not scale `watcher` past 1 replica without a load balancer in front of
it — it's stateless, so that's a routing decision, not a code change.

## GitHub webhook setup

Point the GitHub repo's webhook (Settings -> Webhooks -> Add webhook) at
`https://<your-host>/webhook/github`, content type `application/json`,
secret = `GITHUB_WEBHOOK_SECRET`, event = "Just the push event". For local
development without a public URL, use Smee — see the README's "Smee.io
setup" section.

## Post-deploy verification

Follow [`docs/runbooks/operations.md`](operations.md)'s "How to verify
traces" and "How to inspect reports" sections, or run the same checks
`.github/workflows/eval-pr.yml` runs in CI: publish a synthetic commit,
confirm a `reports` row appears, confirm `q.dlq` stays empty, confirm
every `/metrics` endpoint responds.

## Known limitations

- **Single-host only.** Neither compose file schedules across multiple
  Docker hosts — RabbitMQ/Postgres/Redis remain single instances. A
  multi-node deployment needs an orchestrator (Kubernetes, Nomad) and is
  out of scope for this repo (see `ROADMAP.md`).
- **`network_mode: host` in `docker-compose.yml` is Linux-only** and was
  adopted specifically to work around this project's development sandbox
  blocking fresh container-to-container bridge traffic (see
  `PHASE_5_REPORT.md`). On Docker Desktop (Mac/Windows) or any host
  without that restriction, prefer `docker-compose.dev.yml`/`.prod.yml`
  instead, which use standard bridge networking throughout.
- **No real Postgres migration runner** — `db/migrations/*.sql` only
  auto-applies to a *fresh* volume via `docker-entrypoint-initdb.d`. An
  already-running volume needs each new migration applied by hand (see
  [`db/README.md`](../../db/README.md)) until a real migration tool
  (Alembic or similar) is adopted — tracked in `ROADMAP.md`.
