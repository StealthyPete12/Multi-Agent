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
- **Phase 3: intelligence layer** — a provider-agnostic LLM abstraction
  (`shared/llm.py`), a semantic-summary step added to the researcher, and
  a new reviewer agent that deterministically scores each commit's risk,
  asks an LLM only to *explain* that score, persists a report, and
  notifies Slack. See [`PHASE_3_REPORT.md`](PHASE_3_REPORT.md).

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
                                CTE) -> sensitive-path check -> semantic
                                summary (small/cheap LLM, shared/llm.py)
                                             │
                                             │  findings.ready
                                             ▼
                                          q.findings (durable,
                                          dead-letters to q.findings.dlq)
                                             │
                                             ▼
                                agents/reviewer (consumer)
                                deterministic risk score (scoring.py) ->
                                LLM narrative (explains the score, never
                                sets it) -> Postgres (reports) -> Slack
                                             │
                                             │  review.completed
                                             ▼
                                          q.reviews (durable,
                                          dead-letters to q.reviews.dlq)
```

`tools/seed_commit.py` can publish synthetic `commit.detected` events
directly, bypassing the watcher/GitHub entirely, and `tools/seed_findings.py`
does the same for `findings.ready` directly against the reviewer — both
useful for local testing without a real repo or webhook. See
[`agents/researcher/README.md`](agents/researcher/README.md) and
[`agents/reviewer/README.md`](agents/reviewer/README.md) for each agent's
internal architecture.

Every event is an `Envelope[Payload]` (see [`shared/contracts.py`](shared/contracts.py))
carrying a `trace_id` that's generated once per webhook delivery (or
`seed_commit`/`seed_findings` invocation) and threaded through every log
line via [`shared/logging.py`](shared/logging.py)'s `trace_context`, so a
single commit's path through the entire watcher → researcher → reviewer
pipeline can be grepped out of JSON logs by `trace_id`.

`agents/orchestrator` remains a placeholder — see its README for what
it'll own in a later phase.

### LLM integration

`shared/llm.py` defines one `LLMClient` protocol and three
implementations (`AnthropicClient`, `OpenAIClient`, `OllamaClient`), each
a thin direct-HTTP wrapper (no vendor SDK) so provider identity never
leaks past this module. `LLM_PROVIDER` selects the provider entirely via
environment variables (see `.env.example`); an unset/`none` value, or a
configured provider missing its credential, returns a `NullLLMClient`
that raises `LLMError` on every call — every caller already has to handle
"the LLM is unavailable" as a normal case, so a missing provider and a
live outage take the same code path.

Two call sites, two purposes (`get_llm_client(purpose=...)`, tunable via
`MODEL_SELECTIONS`):

- **Researcher → `summary`**: `agents/researcher/summarize.py` asks a
  small/cheap model for a 2-3 sentence, ≤300-token semantic summary of a
  commit (message + changed files + a truncated `git show` diff). Falls
  back to `""` on failure — matches the empty-string placeholder Phase 2
  already put on the wire.
- **Reviewer → `narrative`**: `agents/reviewer/prompts.py` +
  `agents/reviewer/main.py::generate_narrative` ask a model to explain an
  *already-computed* score/severity in ≤500 tokens — why it's what it is,
  what may be affected, where to focus review. The model is given the
  score as fact and instructed never to restate a different one. Falls
  back to a deterministic template (`build_fallback_narrative`) on
  failure, so a report is never missing an explanation.

### Risk scoring methodology

`agents/reviewer/scoring.py::compute_score` is pure code — no model call,
no randomness. Five inputs, each bucketed to a small integer, summed into
one score, then mapped to a severity by configurable thresholds:

| Input | Signal | Buckets → points |
|---|---|---|
| Blast radius | `blast_radius.max_depth` | ≤3→0, ≤10→+2, else→+4 |
| Impacted modules | `blast_radius.impact_count` | ≤10→0, ≤50→+2, else→+4 |
| Sensitive path hits | `len(sensitive_hits)` | none→0, one→+2, multiple→+4 |
| Changed files | `len(changed_files)` | ≤5→0, ≤20→+1, else→+3 |
| Test proximity | path-marker heuristic (no repo checkout available) | proximate→0, not→+2 |

Severity: `< RISK_SCORE_MODERATE_THRESHOLD` → low, `< …HIGH…` → moderate,
`< …CRITICAL…` → high, else → critical (all three thresholds configurable
via `.env`; bucket boundaries above are the roadmap's own defaults).
`agents/reviewer/scoring.py::status_for_severity` then maps severity to
`ReviewCompleted.status` (`low`→passed, `moderate`/`high`→needs_review,
`critical`→failed).

### Slack integration

`shared/slack.py::build_review_message` renders a Slack Block Kit message
(repository, commit SHA, severity, score, blast radius, sensitive hits,
narrative, a "View Commit" link) wrapped in a colored `attachments` bar
keyed by severity. `SlackNotifier.send()` posts it to `SLACK_WEBHOOK_URL`
via a plain HTTPS POST; an unconfigured webhook is a no-op (logged, not
an error) so local dev/CI never needs a real Slack workspace.

## Repo structure

```
.
├── docker-compose.yml       # RabbitMQ, Postgres, Redis
├── .env.example             # every environment variable, documented
├── shared/
│   ├── contracts.py         # Envelope + CommitDetected/FindingsReady/ReviewCompleted (Pydantic v2)
│   ├── logging.py           # structured JSON logging, trace/correlation IDs
│   ├── broker.py            # aio-pika topology, publish, consume — used by every agent
│   ├── llm.py                # provider-agnostic LLMClient (Anthropic/OpenAI/Ollama)
│   └── slack.py              # Block Kit message + incoming-webhook delivery
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
│   │   ├── sensitive.py      # sensitive-path detection
│   │   ├── diff.py            # best-effort truncated `git show` diff
│   │   └── summarize.py       # LLM semantic-summary generation
│   ├── reviewer/              # findings.ready -> risk score + narrative -> review.completed
│   │   ├── scoring.py          # deterministic risk scoring (no LLM)
│   │   ├── prompts.py          # narrative prompt + deterministic fallback
│   │   └── storage.py          # Postgres persistence + idempotency
│   └── orchestrator/          # placeholder — future topology/retry ownership
├── tools/
│   ├── seed_commit.py        # publish synthetic commit.detected events, no GitHub needed
│   └── seed_findings.py      # publish synthetic findings.ready events, no researcher needed
├── scripts/
│   └── validate_stack.sh    # brings the stack up and checks it end-to-end
├── tests/
├── PHASE_0_REPORT.md
├── PHASE_1_REPORT.md
├── PHASE_2_REPORT.md
└── PHASE_3_REPORT.md
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
Postgres write, blast-radius query, semantic-summary generation) and
publishes `findings.ready` to `q.findings` with the computed blast
radius and any sensitive-path hits (`auth/login.py` above matches the
default `auth/` pattern). Running the same commit again logs a cache hit
instead of re-cloning, and re-storing the same graph leaves
`modules`/`imports` row counts unchanged (idempotent upserts).

### Testing the reviewer's scoring and narrative (Phase 3)

Apply `db/migrations/002_reviewer_reports.sql` once if your Postgres
volume predates Phase 3 (see [`db/README.md`](db/README.md)), then:

```bash
python -m agents.reviewer.main &

# low-risk commit
python -m tools.seed_findings --repo acme/widgets --sha low0001

# sensitive path touched (escalates severity even with a small blast radius)
python -m tools.seed_findings --repo acme/widgets --sha sens0001 \
    --changed-files auth/login.py --sensitive-hits auth/login.py

# large blast radius (high/critical severity)
python -m tools.seed_findings --repo acme/widgets --sha big0001 \
    --impact-count 60 --max-depth 9 \
    --impacted-modules pkg.a,pkg.b,pkg.c
```

The reviewer logs risk-score computation, narrative generation (or the
"skipped, using fallback" warning if no `LLM_PROVIDER` is configured),
the Postgres write, Slack delivery (skipped and logged as such if
`SLACK_WEBHOOK_URL` is unset), and `published review.completed`. Query
the persisted report with:

```bash
docker compose exec postgres psql -U swarm -d code_review_swarm \
    -c "SELECT repo, commit_sha, severity, score, status FROM reports ORDER BY generated_at DESC LIMIT 5;"
```

Re-running `seed_findings` with the same `event_id`/`--sha` combination
(via `--trace-id` reuse isn't needed here — idempotency keys on the
envelope's own `event_id`, generated fresh per invocation) demonstrates
nothing to dedupe on a normal re-seed; the idempotency guard is exercised
by `tests/test_reviewer_storage.py` and by RabbitMQ's own at-least-once
redelivery of a single unacked message.

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
