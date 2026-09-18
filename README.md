# Multi-Agent

Event-Driven Multi-Agent Code Review Swarm.

This repo implements:

- **Phase 0: foundational infrastructure** — the message broker,
  database, shared contracts, and shared logging that every agent builds
  on. See [`PHASE_0_REPORT.md`](PHASE_0_REPORT.md).
- **Phase 1: walking skeleton** — proves an event can move from webhook
  → RabbitMQ → consumer with durability and acknowledgements. No AI, no
  Postgres business logic, no blast-radius analysis yet. See
  [`PHASE_1_REPORT.md`](PHASE_1_REPORT.md).
- **Phase 2: repository-analysis engine** — the researcher agent now
  clones repositories, builds a Python import dependency graph via AST
  analysis, persists it to Postgres, computes blast radius with a
  recursive CTE, flags sensitive-path changes, and publishes
  `findings.ready`. No AI, no Slack, no reviewer logic yet. See
  [`PHASE_2_REPORT.md`](PHASE_2_REPORT.md).

## Architecture

```
GitHub push
    │  HTTPS POST /webhook/github (HMAC-signed)
    ▼
agents/watcher  ──────────────► RabbitMQ (swarm.events exchange)
(FastAPI)          commit.detected         │
                    routing key             │  q.commits (durable,
                                             │  dead-letters to q.commits.dlq)
                                             ▼
                                agents/researcher (consumer)
                                clone repo -> AST import graph -> Postgres
                                (modules/imports) -> blast radius (recursive
                                CTE) -> sensitive-path check
                                             │
                                             │  findings.ready
                                             ▼
                                          q.findings (durable,
                                          dead-letters to q.findings.dlq)
```

`tools/seed_commit.py` can publish synthetic `commit.detected` events
directly, bypassing the watcher/GitHub entirely — useful for local
testing without a real repo or webhook. See
[`agents/researcher/README.md`](agents/researcher/README.md) for the
researcher's internal architecture (repository cache, dependency graph,
blast radius).

Every event is an `Envelope[Payload]` (see [`shared/contracts.py`](shared/contracts.py))
carrying a `trace_id` that's generated once per webhook delivery (or
`seed_commit` invocation) and threaded through every log line via
[`shared/logging.py`](shared/logging.py)'s `trace_context`, so a single
commit's path through the system can be grepped out of JSON logs by
`trace_id`.

`agents/reviewer` and `agents/orchestrator` remain placeholders — see
their READMEs for what they'll own in later phases.

## Repo structure

```
.
├── docker-compose.yml       # RabbitMQ, Postgres, Redis
├── .env.example             # every environment variable, documented
├── shared/
│   ├── contracts.py         # Envelope + CommitDetected/FindingsReady/ReviewCompleted (Pydantic v2)
│   ├── logging.py           # structured JSON logging, trace/correlation IDs
│   └── broker.py            # aio-pika topology, publish, consume — used by every agent
├── db/
│   ├── migrations/          # schema, applied on first Postgres boot
│   └── README.md
├── agents/
│   ├── watcher/              # GitHub webhook -> commit.detected (FastAPI)
│   ├── researcher/           # commit.detected -> repo clone, AST graph, blast radius -> findings.ready
│   │   ├── repository.py     # local git clone/cache
│   │   ├── graph.py          # AST import analysis -> DependencyGraph
│   │   ├── impact.py         # in-memory blast-radius traversal
│   │   ├── db.py             # Postgres upserts + recursive-CTE blast radius
│   │   └── sensitive.py      # sensitive-path detection
│   ├── reviewer/              # placeholder — future FindingsReady aggregation
│   └── orchestrator/          # placeholder — future topology/retry ownership
├── tools/
│   └── seed_commit.py       # publish synthetic commit.detected events, no GitHub needed
├── scripts/
│   └── validate_stack.sh    # brings the stack up and checks it end-to-end
├── tests/
├── PHASE_0_REPORT.md
└── PHASE_1_REPORT.md
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

## Local testing workflow (Path A — no GitHub needed)

1. Bring up the stack (`docker compose up -d`) and copy `.env.example` to
   `.env`.
2. In one terminal, run the pipeline-verification consumer:

   ```bash
   python -m agents.researcher.main
   ```

3. In another terminal, publish a synthetic event:

   ```bash
   # explicit fields
   python -m tools.seed_commit --repo acme/widgets --branch main \
       --author jane --message "fix: off by one"

   # or a random commit
   python -m tools.seed_commit --random

   # or a burst of random commits
   python -m tools.seed_commit --random --count 5
   ```

4. The researcher's stdout logs a `commit.detected received` line with
   the same `trace_id`/`event_id` the seed script printed, then acks the
   message. Check http://localhost:15672 (RabbitMQ management UI) to see
   `q.commits` and its `q.commits.dlq` dead-letter queue.

### Testing the researcher's repository analysis (Phase 2)

`CommitDetected.repo` (e.g. `acme/widgets`) is resolved to a clone URL as
`${REPO_CLONE_BASE_URL}/${repo}.git` by default. To test against a real
GitHub repo, leave `REPO_CLONE_BASE_URL=https://github.com/` (the
default) and seed a commit with a real `--repo`/`--sha`. To test entirely
offline (no GitHub, no network), point it at a local git repository:

```bash
# 1. Create a local "remote" the researcher can clone from.
mkdir -p /tmp/sample-remotes/acme/widgets.git
cd /tmp/sample-remotes/acme/widgets.git
git init -q -b main
echo "def connect(): pass" > database.py
mkdir auth && echo "import database" > auth/login.py && touch auth/__init__.py
git add . && git commit -q -m "initial commit"
git rev-parse HEAD   # <- use this as --sha below

# 2. Point the researcher at it and run the pipeline as usual.
export REPO_CLONE_BASE_URL="file:///tmp/sample-remotes/"
python -m agents.researcher.main &
python -m tools.seed_commit --repo acme/widgets --branch main --author jane \
    --message "test change" --sha <sha-from-above> \
    --changed-files database.py,auth/login.py
```

The researcher logs each stage (repository cache hit/miss, graph build,
Postgres write, blast-radius query) and publishes `findings.ready` to
`q.findings` with the computed blast radius and any sensitive-path hits
(`auth/login.py` above matches the default `auth/` pattern). Running the
same commit again logs a cache hit instead of re-cloning, and re-storing
the same graph leaves `modules`/`imports` row counts unchanged
(idempotent upserts).

## GitHub webhook setup (Path B)

1. Start the watcher:

   ```bash
   uvicorn agents.watcher.main:app --host 0.0.0.0 --port ${WATCHER_PORT:-8001}
   ```

2. On the GitHub repo you want to watch: **Settings → Webhooks → Add
   webhook**.
   - Payload URL: your tunnel URL + `/webhook/github` (see Smee setup
     below for local dev).
   - Content type: `application/json`.
   - Secret: the same value as `GITHUB_WEBHOOK_SECRET` in your `.env`.
   - Events: "Just the push event".
3. Push a commit to a branch listed in `WATCHED_BRANCHES` (default
   `main`). The watcher verifies the `X-Hub-Signature-256` HMAC, extracts
   commit metadata, and publishes one `commit.detected` event per commit
   in the push.
4. With `agents/researcher/main.py` running, watch it log the received
   event.

### Smee.io setup (tunneling GitHub → localhost)

GitHub needs a public URL to deliver webhooks to; [Smee](https://smee.io)
forwards deliveries to your local watcher without exposing your machine
directly.

```bash
# get a channel URL at https://smee.io/new, then:
npx smee-client --url https://smee.io/<your-channel> \
    --target http://localhost:${WATCHER_PORT:-8001}/webhook/github
```

Use the `https://smee.io/<your-channel>` URL as the webhook's Payload URL
in GitHub. Smee replays each delivery (including headers) to your local
`/webhook/github` endpoint, so signature verification behaves exactly as
it would against GitHub directly. `SMEE_URL` in `.env.example` is a place
to note the channel for your own reference — no service reads it.

## Tearing down

```bash
docker compose down        # stop containers, keep data
docker compose down -v     # stop containers and delete named volumes
```
