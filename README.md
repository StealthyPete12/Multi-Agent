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
- **Phase 4: fault tolerance** — a RabbitMQ delayed-retry ladder
  (`shared/retry.py`), error classification (`shared/errors.py`),
  claim-before-processing idempotency (`shared/idempotency.py`), a
  Redis-backed rate limiter (`shared/ratelimit.py`) and circuit breaker
  (`shared/breaker.py`) wired into every LLM call, manual-ack consumer
  hygiene with graceful shutdown, and a DLQ inspection/replay tool
  (`tools/replay_dlq.py`). No business logic changed — watcher, repo
  cache, AST graph, blast radius, reviewer scoring, and Slack formatting
  are exactly as Phase 3 left them. See
  [`PHASE_4_REPORT.md`](PHASE_4_REPORT.md).

## Architecture

```
GitHub push
    │  HTTPS POST /webhook/github (HMAC-signed)
    ▼
agents/watcher  ──────────────► RabbitMQ (swarm.events exchange)
(FastAPI)          commit.detected         │
                    routing key             │  q.commits (durable, prefetch=1,
                                             │  manual ack; dead-letters to
                                             │  q.commits.dlq on a raw nack)
                                             ▼
                                agents/researcher (consumer)
                                claim event_id (shared/idempotency.py) ->
                                clone repo -> AST import graph -> Postgres
                                (modules/imports) -> blast radius (recursive
                                CTE) -> sensitive-path check -> semantic
                                summary (small/cheap LLM, shared/llm.py,
                                rate-limited + circuit-broken) -> mark
                                complete
                                             │
                             on failure: classify (shared/errors.py) ->
                             RetryableError -> retry ladder (5s/30s/5m) ->
                             PoisonMessageError -> q.dlq immediately ->
                             FatalError -> log + stop the service
                                             │
                                             │  findings.ready (success path)
                                             ▼
                                          q.findings (durable, prefetch=1)
                                             │
                                             ▼
                                agents/reviewer (consumer)
                                claim event_id -> deterministic risk score
                                (scoring.py, unchanged) -> LLM narrative
                                (explains the score, never sets it) ->
                                Postgres (reports) -> Slack -> mark complete
                                             │
                             same retry/DLQ/fatal routing as the researcher
                                             │
                                             │  review.completed
                                             ▼
                                          q.reviews (durable, prefetch=1)

Retry ladder (shared/retry.py), shared by every consumer:

  attempt 1 fails ──► q.retry.5s  ──(TTL expiry)──► back to origin queue
  attempt 2 fails ──► q.retry.30s ──(TTL expiry)──► back to origin queue
  attempt 3 fails ──► q.retry.5m  ──(TTL expiry)──► back to origin queue
  attempt 4 fails ──► q.dlq (terminal — inspect/replay with tools/replay_dlq.py)
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

### Error classification (`shared/errors.py`)

Every failure a consumer or `shared/llm.py` can hit is classified into
exactly one of three types, each with a distinct routing outcome:

| Type | Examples | Routing |
|---|---|---|
| `RetryableError` | HTTP 429/5xx, connection/timeout errors, a transient `git clone`/`fetch` failure | Retry ladder (or `q.dlq` once attempts are exhausted) |
| `PoisonMessageError` | Contract/schema validation failure, an unknown `schema_version`, a commit SHA that genuinely doesn't exist in the repo, a malformed LLM response | `q.dlq` immediately, zero retry attempts |
| `FatalError` | Anything unclassified/unexpected | Logged critical, message nacked with `requeue=True` (not lost), service stops (`SystemExit(1)`) rather than grinding through the rest of the queue the same broken way |

`shared/errors.py::classify_exception` handles the provider-agnostic
cases (HTTP status codes, `httpx` network exceptions) that `shared/llm.py`
uses directly. Each agent's `main.py` has its own
`classify_researcher_failure`/`classify_reviewer_failure` on top of that
for exception types specific to that agent (`CommitNotFoundError`,
`GitCommandError`, `SlackError`) — kept out of `shared/errors.py`
deliberately, since `shared/` must never depend on `agents/`.

### Retry ladder (`shared/retry.py`)

RabbitMQ has no native "retry in N seconds" without the (non-default)
delayed-message-exchange plugin, so this implements the standard
TTL+dead-letter-exchange "parking lot" pattern by hand: `q.retry.5s` →
`q.retry.30s` → `q.retry.5m`, each a durable queue with a fixed
`x-message-ttl` whose `x-dead-letter-exchange` points back at the main
`swarm.events` exchange. When a message's TTL expires, RabbitMQ
redelivers it automatically — no relay process needed.

The one subtlety this depends on (verified empirically against a live
broker before building on it): a TTL-expired message is redelivered
using the routing key it was *originally published with when it entered
the expiring queue*, not the queue's own name, as long as the queue
doesn't set `x-dead-letter-routing-key` itself. Each rung therefore gets
its own small dedicated topic exchange (bound catch-all, `#`, to exactly
one queue) purely as a named "entry door" for that delay tier — scheduling
a retry means picking the rung's exchange and publishing with the
message's real business routing key (`commit.detected`/`findings.ready`),
and the main exchange's own topic bindings route it back to the correct
origin queue for free.

Retry metadata (`x-retry-attempt`, `x-retry-reason`,
`x-retry-original-queue`, `x-retry-first-failed-at`) travels as AMQP
message headers, not inside the envelope body — the strict/closed
contracts from Phase 0-3 stay untouched. `MAX_RETRY_ATTEMPTS` (3, one per
rung) is derived from the ladder's own length; a 4th failure routes to
the terminal `q.dlq` instead of another rung.

### DLQ flow and replay

`q.dlq` is one shared, terminal queue for both routing paths: a poison
message (`RetryLadder.send_raw_to_dlq`, preserving the *original* bytes —
never reconstructed — since the message may not even be a valid envelope)
and a retryable failure that exhausted the ladder
(`RetryLadder.send_to_dlq`). Per-queue `<queue>.dlq` queues still exist
(from Phase 1's broker topology) as RabbitMQ's own automatic fallback for
anything nacked without an explicit Phase 4 routing decision.

`tools/replay_dlq.py` inspects and replays it:

```bash
python -m tools.replay_dlq inspect                                   # list, changes nothing
python -m tools.replay_dlq replay --original-queue q.commits --dry-run  # preview a replay
python -m tools.replay_dlq replay --all                              # replay everything
python -m tools.replay_dlq replay --event-id <id>                    # replay one message
```

AMQP has no server-side "peek", so inspecting means consuming — the tool
always drains the queue into memory first, then decides per message:
`inspect` and `--dry-run` requeue everything unchanged (nothing is ever
lost or removed); a real replay acks (permanently removes) only the
messages actually replayed and requeues the rest. A replayed message goes
straight back to its original queue (default exchange, routing key = the
queue name recorded in `x-retry-original-queue`) with a fresh attempt
budget, since a human is presumably replaying only after fixing whatever
caused the failure.

### Idempotency (`shared/idempotency.py`)

Reuses the `processed_events` table from Phase 0, extended by
`db/migrations/003_idempotency_claims.sql` with `claimed_at`/
`completed_at` so a claim and its completion are two distinct states:

1. **Claim** — `INSERT ... ON CONFLICT (event_id) DO NOTHING` before any
   work starts. Atomic, so two concurrent redeliveries of the same
   `event_id` can never both proceed — one gets `claimed=True`, the other
   bails out before doing anything observable (no duplicate Slack
   message, no duplicate report).
2. **Perform work** — the consumer's normal pipeline.
3. **Mark complete** — `mark_complete()`, called once every side effect
   (publish, persist, notify) has actually happened.

A caught `RetryableError` calls `release()` (deletes the claim) so an
immediate retry-ladder redelivery can reclaim it. A **hard crash**
(SIGKILL, OOM-kill) never runs any exception handler at all — no
`release()`, no `mark_complete()`. Without a way to tell "claimed and
completed" apart from "claimed, then the process died mid-work", the
message would survive at the broker (RabbitMQ redelivers an unacked
message) but the pipeline would silently never produce its output,
because every redelivery would see the claim as still active. `claim()`
treats a claim that's neither completed nor reclaimed within
`IDEMPOTENCY_STALE_CLAIM_SECONDS` (default 300s) as abandoned and lets a
new caller reclaim it — a completed claim is never reclaimable regardless
of age.

### Rate limiting (`shared/ratelimit.py`)

A distributed token bucket, Redis-backed so multiple instances of the
same agent share one limit instead of each enforcing its own in-memory
bucket, and scoped per `"<provider>:<model>"` (`RATE_LIMIT_TOKENS_PER_MINUTE`,
overridable per provider via `RATE_LIMIT_<PROVIDER>_TPM`). Refills
continuously (tokens/ms) rather than resetting at a fixed window
boundary, so it can't allow a 2x burst right at a window edge. Atomicity
comes from a Lua script run via Redis `EVAL` — the whole
read-modify-write happens as one atomic operation, safe across
concurrent callers in different processes. `shared/llm.py` calls
`wait_and_acquire()` before every provider request; a Redis outage
degrades gracefully (logs a warning, proceeds without limiting) rather
than becoming a new single point of failure for the whole pipeline.

### Circuit breaker (`shared/breaker.py`)

One breaker per LLM provider (shared by every caller in the process),
standard three-state machine:

- **CLOSED** — normal operation, consecutive failures counted.
- **OPEN** — after `LLM_BREAKER_FAILURE_THRESHOLD` (default 5)
  consecutive failures, every call fails fast with `CircuitOpenError`
  (no network attempt at all) for `LLM_BREAKER_OPEN_SECONDS` (default 60).
- **HALF_OPEN** — once the open window elapses, the next call is a trial:
  success closes the breaker and increments `recovery_count`; failure
  reopens it for another full window.

Integrated into `shared/llm.py::_BaseLLMClient._execute_with_resilience`,
so every provider (Anthropic/OpenAI/Ollama) gets it automatically. Metrics
(`failure_count`/`open_count`/`recovery_count`) are logged on every state
transition.

### LLM retry/backoff

`shared/llm.py::_BaseLLMClient._execute_with_resilience` is the single
choke point every provider's `complete()` routes through: acquire a rate
limit token → run the request behind the circuit breaker → on a failure
classified `RetryableError`, sleep an exponential-backoff-with-full-jitter
delay (`min(base * 2**(attempt-1), max)`, then a random delay in
`[0, that)`, to avoid synchronized retry storms across instances) and try
again, up to `LLM_MAX_RETRIES` (default 3) times. A `PoisonMessageError`
(malformed response) or an open circuit fails immediately with no
retries spent on it. Every attempt logs `retry_count`/
`retry_delay_seconds`/the classified reason.

### Consumer hygiene and graceful shutdown

Both the researcher and reviewer consumers run with `prefetch_count=1`
(at most one unacked message in flight, so manual ack/nack always applies
to exactly the message being handled) and manual acknowledgement
throughout — no more ack-on-success/nack-on-any-exception context
manager; every outcome (success, retryable failure, poison, fatal) makes
an explicit, logged routing decision. On SIGTERM/SIGINT, a consumer stops
accepting new work and gives any in-flight message a bounded
`GRACEFUL_SHUTDOWN_SECONDS` (default 30) window to finish naturally
before a hard-cancel fallback; broker/database connections close only
after. An AMQP heartbeat (`RABBITMQ_HEARTBEAT`, default 60s) lets both
sides detect a dead connection well before a kernel-level timeout would.

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
│   ├── llm.py                # provider-agnostic LLMClient (Anthropic/OpenAI/Ollama), retry/breaker/rate-limit wired in
│   ├── slack.py              # Block Kit message + incoming-webhook delivery
│   ├── errors.py             # RetryableError/PoisonMessageError/FatalError classification
│   ├── retry.py              # RabbitMQ delayed-retry ladder (q.retry.5s/30s/5m -> q.dlq)
│   ├── idempotency.py        # claim-before-processing against processed_events
│   ├── ratelimit.py          # Redis-backed distributed token bucket
│   └── breaker.py            # CLOSED/OPEN/HALF_OPEN circuit breaker
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
│   ├── seed_findings.py      # publish synthetic findings.ready events, no researcher needed
│   └── replay_dlq.py         # inspect/filter/replay q.dlq messages
├── scripts/
│   └── validate_stack.sh    # brings the stack up and checks it end-to-end
├── tests/
│   └── test_chaos.py         # repeatable chaos scenarios A-F
├── PHASE_0_REPORT.md
├── PHASE_1_REPORT.md
├── PHASE_2_REPORT.md
├── PHASE_3_REPORT.md
└── PHASE_4_REPORT.md
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
