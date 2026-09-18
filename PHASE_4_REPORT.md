# Phase 4 Report — Fault Tolerance

**Project:** Event-Driven Multi-Agent Code Review Swarm
**Phase:** 4 — transform the pipeline into a fault-tolerant distributed
system: retries, dead letters, idempotency, rate limiting, circuit
breaking, recovery tooling.
**Status:** Complete and validated

## Starting state

Phase 3 delivered the intelligence layer: `shared/llm.py` (retry hooks
stubbed as a documented no-op), the reviewer's deterministic scoring +
LLM narrative + Postgres persistence + Slack notification, and the
researcher's semantic-summary step. RabbitMQ topology had one durable
topic exchange with per-queue dead-lettering (`q.commits.dlq` etc.) for
anything nacked without requeue — but every nack came from exactly one
cause (contract validation failure); there was no distinction between
"this message is poison" and "this failure is transient, retry it
later." `processed_events` existed and had one real user (the reviewer),
but as a single insert-at-the-end-of-success marker, not a
claim-before-work primitive — a genuine duplicate-processing race
existed under concurrent redelivery. No Redis client, no circuit
breaker, no DLQ tooling, and both consumers used aio-pika's
ack-on-success/nack-on-any-exception `message.process()` context
manager with no differentiated failure routing.

## Audit checklist against the roadmap

| Area | Before Phase 4 | After Phase 4 |
|---|---|---|
| `shared/retry.py` | Missing | Complete — 3-rung TTL+DLX retry ladder, dedicated per-rung exchange |
| `shared/errors.py` | Missing | Complete — Retryable/Poison/Fatal classification |
| `shared/idempotency.py` | Missing | Complete — claim/complete/release against `processed_events` |
| `shared/ratelimit.py` | Missing | Complete — Redis Lua-script token bucket |
| `shared/breaker.py` | Missing | Complete — CLOSED/OPEN/HALF_OPEN, integrated into `shared/llm.py` |
| `shared/llm.py` retry hook | Documented single-attempt stub | Complete — backoff+jitter+classification+rate-limit+breaker |
| Consumer hygiene | `prefetch=10` (default), auto ack/nack | `prefetch=1`, manual ack/nack, explicit routing per failure category |
| Graceful shutdown | Hard `task.cancel()` on signal | Bounded drain window, then cancel fallback |
| `tools/replay_dlq.py` | Missing | Complete — inspect/filter/replay/dry-run |
| Idempotency crash-safety | N/A | `processed_events.claimed_at`/`completed_at` + staleness reclaim (`003_idempotency_claims.sql`) |
| Chaos tests | Missing | Complete — `tests/test_chaos.py`, scenarios A-F, all passing |
| Tests | 134 (Phase 0-3) | 206 |

Nothing under `agents/watcher`, the researcher's repository cache/AST
graph/blast-radius logic, `agents/reviewer/scoring.py`,
`agents/reviewer/prompts.py`, or `shared/slack.py` was modified —
Phase 4 only touches reliability/fault-handling plumbing, plus one
necessary compatibility fix in `agents/reviewer/storage.py` (see
**A regression caught by live validation** below).

## Architecture

```
GitHub push
    │  HTTPS POST /webhook/github (HMAC-signed)
    ▼
agents/watcher ───────────────► RabbitMQ (swarm.events exchange)
(unchanged)         commit.detected         │
                     routing key             │  q.commits (durable,
                                              │  prefetch=1, manual ack)
                                              ▼
                                 agents/researcher (consumer)
                                 claim event_id (shared/idempotency.py)
                                     │
                                     ▼ (claim denied: duplicate, ack+skip)
                                 clone → AST graph → Postgres → blast
                                 radius → sensitive paths → semantic
                                 summary (LLM, rate-limited + circuit
                                 breaker + backoff, shared/llm.py)
                                     │
                         success ───┼─── failure
                            │       │
                    mark_complete   classify (shared/errors.py)
                    publish             │
                    findings.ready      ├─ RetryableError → retry ladder
                    ack                 │   (or q.dlq once exhausted)
                                        ├─ PoisonMessageError → q.dlq now
                                        └─ FatalError → nack(requeue) +
                                                          stop service
                                     │
                                     ▼
                                  q.findings (durable, prefetch=1)
                                     │
                                     ▼
                                 agents/reviewer (consumer)
                                 claim event_id → compute_score
                                 (scoring.py, unchanged, no LLM) →
                                 narrative (LLM) → Postgres (reports,
                                 completed_at set atomically with the
                                 report row) → Slack → mark_complete
                                     │
                         same classify/retry/DLQ/fatal routing as above
                                     │
                                     ▼
                                  q.reviews (durable, prefetch=1)
```

### Retry-ladder flow

```
attempt 1 fails ──► publish to rung-1 exchange (routing key preserved)
                     └─► q.retry.5s  (TTL 5000ms, DLX → main exchange)
                            │ TTL expires
                            ▼
                     redelivered to origin queue with the SAME routing
                     key it entered the retry queue with (verified
                     empirically — see "Routing-key preservation" below)

attempt 2 fails ──► q.retry.30s (TTL 30000ms) ──► same redelivery
attempt 3 fails ──► q.retry.5m  (TTL 300000ms) ─► same redelivery
attempt 4 fails ──► q.dlq (terminal; no further automatic redelivery —
                     operator/tools/replay_dlq.py from here)
```

**Routing-key preservation.** RabbitMQ has no native delayed-retry
primitive without the (non-default) delayed-message-exchange plugin, so
this is the standard TTL+dead-letter-exchange "parking lot" pattern,
implemented by hand. The one subtlety it depends on was verified
empirically against a live broker *before* writing any code (a throwaway
script publishing through a TTL queue and inspecting the redelivered
message's `x-death`/routing key): a message dead-lettered by TTL expiry
is redelivered using the routing key it was *originally published with
when it entered the expiring queue*, not the queue's name — as long as
that queue never sets `x-dead-letter-routing-key` itself. Each rung
therefore gets its own small dedicated topic exchange (bound catch-all,
`#`, to exactly one queue) purely as a named "entry door" for that delay
tier. Scheduling a retry means picking the rung's exchange and publishing
with the message's real business routing key (`commit.detected`); the
main exchange's own topic bindings then route it back to the correct
origin queue with zero relay code.

## Error classification

| Category | Examples | Routing |
|---|---|---|
| `RetryableError` | HTTP 429/5xx, `httpx` timeout/connection errors, transient `git clone`/`fetch` failures, Postgres connection errors | Retry ladder → `q.dlq` after 3 attempts |
| `PoisonMessageError` | Envelope/contract validation failure, a commit SHA that genuinely doesn't exist (`CommitNotFoundError`), a malformed LLM provider response | `q.dlq` immediately, zero retries |
| `FatalError` | Anything unclassified | Logged critical, message `nack(requeue=True)`, service exits (`SystemExit(1)`) |

`shared/errors.py::classify_exception` covers the provider-agnostic HTTP/
network cases `shared/llm.py` needs directly. Each agent's `main.py` has
its own `classify_researcher_failure`/`classify_reviewer_failure` on top
of that, for exception types specific to that agent
(`CommitNotFoundError`, `GitCommandError`, `SlackError`) — kept out of
`shared/errors.py` deliberately, since `shared/` must never import from
`agents/`.

## Idempotency design

Two-phase, backed by `processed_events` (extended by
`db/migrations/003_idempotency_claims.sql` with `claimed_at`/
`completed_at`):

1. **Claim** — `INSERT ... ON CONFLICT (event_id) DO NOTHING`, atomic, so
   two concurrent redeliveries can never both proceed.
2. **Perform work** — the consumer's normal pipeline.
3. **Mark complete** — set only once every side effect (publish, persist,
   notify) has actually happened.

A caught `RetryableError` calls `release()` (deletes the claim) so an
immediate retry-ladder redelivery can reclaim it right away. A **hard
crash** (SIGKILL, OOM-kill) never runs any exception handler — no
`release()`, no `mark_complete()`. Without distinguishing "claimed and
completed" from "claimed, then the process died mid-work", a crash
followed by RabbitMQ's normal redeliver-the-unacked-message behavior
would find the event permanently "already claimed" and skip it forever:
the message survives at the broker, but the pipeline silently never
produces its output. `claim()` treats a claim that's neither completed
nor reclaimed within `IDEMPOTENCY_STALE_CLAIM_SECONDS` (default 300s) as
abandoned and lets a new caller reclaim it — a *completed* claim is never
reclaimable regardless of age (verified by
`tests/test_idempotency.py::test_mark_complete_then_claim_is_permanently_blocked`).

## Rate limiter design

`shared/ratelimit.py`: a distributed token bucket, Redis-backed (so
multiple instances of the same agent share one limit rather than each
enforcing its own in-memory bucket), scoped per `"<provider>:<model>"`.
Refills continuously (tokens/ms) rather than resetting at a fixed window
boundary, avoiding a 2x burst at a window edge. Atomicity comes from a
Lua script executed via Redis `EVAL` — the whole read-modify-write is one
atomic server-side operation, safe across concurrent callers in
different processes. `shared/llm.py` calls `wait_and_acquire()` before
every provider request; a Redis outage degrades gracefully (logs a
warning, proceeds without limiting) rather than becoming a new single
point of failure.

## Circuit breaker design

`shared/breaker.py`: one breaker per LLM provider, shared by every caller
in the process. CLOSED (normal, counts consecutive failures) → OPEN
(after `LLM_BREAKER_FAILURE_THRESHOLD`, default 5, consecutive failures —
every call fails fast with `CircuitOpenError`, no network attempt, for
`LLM_BREAKER_OPEN_SECONDS`, default 60) → HALF_OPEN (one trial call after
the open window: success closes it and increments `recovery_count`,
failure reopens it for another full window). Integrated into
`shared/llm.py::_BaseLLMClient._execute_with_resilience`, so every
provider (Anthropic/OpenAI/Ollama) gets it automatically, with
`failure_count`/`open_count`/`recovery_count` logged on every transition.

## Consumer hygiene and graceful shutdown

Both consumers run `prefetch_count=1` (at most one unacked message in
flight, so manual ack/nack always applies to exactly the message being
handled) with manual acknowledgement throughout instead of the Phase 1-3
`message.process()` context manager. On SIGTERM/SIGINT, a consumer stops
accepting new work and gives any in-flight message a bounded
`GRACEFUL_SHUTDOWN_SECONDS` (default 30) window to finish naturally
before a hard-cancel fallback; broker/database connections close only
after. An AMQP heartbeat (`RABBITMQ_HEARTBEAT`, default 60s) lets both
sides detect a dead connection well before a kernel-level timeout would.

## A regression caught by live validation

While validating the fully-wired reviewer against the live stack, report
generation silently stopped after the very first message. Root cause: a
composition bug between the new outer claim and the untouched Phase 3
`process_findings()`'s own `storage.is_processed(event_id)` check — both
query `processed_events`, but the outer claim now inserts a row *before*
any work starts (`completed_at` still `NULL`), and the old
`is_processed()` only checked row *existence*, so it read "already
processed" on the very first delivery, every time.

Live evidence of the bug (reports count flat across a fresh seeded
commit) and the fix (count incrementing by exactly one immediately
after) is in the Validation section below.
`agents/reviewer/storage.py::is_processed()` now checks
`completed_at IS NOT NULL`, and `save_report()`'s own
`processed_events` upsert (`ON CONFLICT DO UPDATE SET completed_at`,
changed from `DO NOTHING`) completes the outer claim's row atomically
with the report insert. `scoring.py`, `prompts.py`, and
`process_findings()`'s own control flow are untouched — this is a
one-column compatibility fix, not a scoring/business-logic change.

## Chaos test evidence

All six scenarios are automated and repeatable in
[`tests/test_chaos.py`](tests/test_chaos.py), run against the live
RabbitMQ/Postgres stack. Short substitute delays stand in for the real
5s/30s/5m ladder and 60s breaker window *only where waiting the real
duration would make the test impractically slow* — the mechanism under
test (real RabbitMQ TTL expiry, real Postgres claims, the real
`CircuitBreaker` state machine) is never mocked away.

| Scenario | What's verified | Result |
|---|---|---|
| A — researcher crash mid-processing | Claim with no release/complete (simulating SIGKILL) → immediate redelivery correctly no-ops (claim still active) → redelivery past the staleness window reclaims and produces exactly one `findings.ready` → a further redelivery after completion produces zero more | PASS |
| B — reviewer crash mid-processing | Same pattern → exactly one report, exactly one Slack message, across three total delivery attempts | PASS |
| C — persistent failure (429-equivalent) | Real 3-rung TTL retry ladder, chained through actual RabbitMQ redelivery (not simulated) → attempts observed in order `[1, 2, 3]` → final message lands in `q.dlq` with `x-retry-attempt=4` | PASS |
| D — malformed contract | Zero retry-ladder calls, straight to `q.dlq` | PASS |
| E — duplicate delivery | Three exact-duplicate deliveries of one `findings.ready` → one report, one Slack message | PASS |
| F — circuit breaker | 5 consecutive LLM failures → OPEN (6th call fails fast, zero network requests) → HALF_OPEN after the open window → a successful trial call closes it, `recovery_count` increments | PASS |

```
$ pytest tests/test_chaos.py -v
tests/test_chaos.py::test_scenario_a_researcher_crash_produces_exactly_one_findings_ready PASSED
tests/test_chaos.py::test_scenario_b_reviewer_crash_produces_exactly_one_report_and_slack_message PASSED
tests/test_chaos.py::test_scenario_c_retry_ladder_progression_to_dlq PASSED
tests/test_chaos.py::test_scenario_d_malformed_contract_immediate_dlq_no_retries PASSED
tests/test_chaos.py::test_scenario_e_duplicate_delivery_single_report_persisted PASSED
tests/test_chaos.py::test_scenario_f_circuit_breaker_full_cycle PASSED
6 passed in 2.08s
```

## Test results

```
$ pytest tests/ -q
206 passed, 1 warning in ~6s
```

| File | Covers |
|---|---|
| `tests/test_errors.py` | HTTP status classification, `httpx` exception mapping, typed-error passthrough |
| `tests/test_breaker.py` | Full CLOSED→OPEN→HALF_OPEN→CLOSED/reopen state machine, metrics |
| `tests/test_ratelimit.py` | Token acquisition/denial, `wait_and_acquire` timeout/success, independent bucket scoping (live Redis) |
| `tests/test_idempotency.py` | Claim/release/reclaim, completed claims never reclaimable, the abandoned-claim staleness scenario (live Postgres) |
| `tests/test_retry.py` | Ladder progression bounds, header parsing, real TTL-expiry redelivery with routing-key preservation, DLQ publish (live RabbitMQ) |
| `tests/test_llm_retry.py` | Backoff/jitter bounds, retry-then-succeed, retries-exhausted, non-retryable fails immediately, breaker integration |
| `tests/test_llm.py` (updated) | Provider request/response shape — one test pinned to `max_retries=0` now that 500 is correctly retried by default |
| `tests/test_researcher_consumer.py` (rewritten) | Manual-ack contract: success/duplicate/retryable/poison/fatal routing, `classify_researcher_failure` |
| `tests/test_reviewer_consumer.py` (rewritten) | Same, plus Slack-failure classification |
| `tests/test_replay_dlq.py` | Header/body parsing, filter matching, drain-then-restore round-trip against a live, shared `q.dlq` without destroying unrelated content |
| `tests/test_chaos.py` | Scenarios A-F, see above |
| All Phase 0-3 files | Unchanged in intent, still passing |

## Validation evidence

All scenarios below were run against the live Docker stack already
running in this environment (RabbitMQ/Postgres/Redis, `docker compose
ps` showed all three `healthy`), using a local git "remote" for the
researcher (same offline technique Phase 2/3 validation used) so the run
has zero external network dependency.

**1. Researcher kill/restart (real SIGTERM, not simulated):**

```
$ python -m agents.researcher.main &
$ python -m tools.seed_commit --repo acme/widgets --sha 58c297d... \
    --changed-files database.py,auth/login.py
researcher log: ... "published findings.ready" impact_count=4 sensitive_hits=1

$ kill -TERM <researcher-pid>
researcher log: "shutdown signal received, draining in-flight work" grace_period_seconds=30.0
# process exits cleanly once idle (no in-flight message to lose)
```

**2. Reviewer kill/restart:** same SIGTERM → "shutdown signal received,
draining in-flight work" → clean exit pattern, exercised identically
(`agents/reviewer/main.py::run` shares the same graceful-shutdown
implementation as the researcher).

**3. 429 retry ladder (real, via `tests/test_chaos.py::test_scenario_c`):**
see **Chaos test evidence** above — attempts observed in order `[1, 2,
3]` via real TTL-expiry redelivery, final landing in `q.dlq` with
`x-retry-attempt=4`.

**4. Poison message routing (real, live broker):**

```
$ python -c "... publish {\"event_type\":\"commit.detected\",\"source\":\"poison-test\",\"payload\":{}} ..."

researcher log:
  "rejected message: contract validation failed" (6 pydantic errors: missing repo/commit_sha/branch/author/message/committed_at)
  "poison message routed to DLQ" dlq_routing=q.dlq reason="contract validation failed: ..." original_queue=q.commits

$ rabbitmqctl list_queues name messages | grep dlq
q.dlq    1
```

Zero retry attempts — routed straight to `q.dlq` on first sight, exactly
as Scenario D requires.

**5. DLQ replay working (real, via `tools/replay_dlq.py`):**

```
$ python -m tools.replay_dlq inspect
DLQ contents (2):
  - event_id=f7a816fa-... type=commit.detected repo=acme/widgets commit_sha=aaaa...
    reason: git checkout aaaa... failed: fatal: unable to read tree (aaaa...)
  - event_id=None type=commit.detected repo=None commit_sha=None
    reason: contract validation failed: 6 validation errors ...

$ python -m tools.replay_dlq replay --original-queue q.commits --dry-run
Selected for replay (2): ...
[dry-run] no messages were replayed or removed from the DLQ.
$ rabbitmqctl list_queues name messages | grep dlq   # unchanged: q.dlq 2

$ python -m tools.replay_dlq replay --event-id f7a816fa-...
Replayed 1 message(s) back to their original queue.
$ rabbitmqctl list_queues name messages | grep -E "dlq|q.commits"
q.commits   4      # replayed message landed back in its origin queue
q.dlq       1

$ python -m tools.replay_dlq replay --all
Replayed 1 message(s) back to their original queue.
$ rabbitmqctl list_queues name messages | grep dlq   # q.dlq 0
```

**6. Circuit breaker state changes:** see Chaos Scenario F above — 5
failures → OPEN (verified via `breaker.state`/`breaker.metrics`), 6th
call fails fast with zero additional HTTP requests
(`httpx_mock.get_requests()` stayed at 5), HALF_OPEN after the open
window, successful trial call → CLOSED, `recovery_count == 1`.

**7. Rate limiting functioning:**
`tests/test_ratelimit.py::test_acquire_denies_once_capacity_exhausted`
and `test_wait_and_acquire_raises_timeout_when_budget_too_small`/
`test_wait_and_acquire_succeeds_once_refilled` against live Redis —
token exhaustion is denied deterministically, `wait_and_acquire` blocks
and retries until refill or a bounded timeout. `shared/llm.py`'s own
`rate_limiter_delay`-logged sleep is exercised implicitly by every
provider call once `LLM_RATE_LIMIT_ENABLED=true` (the default).

**8. Duplicate-event protection (real, live broker + Postgres):**

```
$ python -c "... publish the exact same commit.detected envelope (same event_id) twice ..."

researcher log (second delivery):
  "commit.detected received" event_id=f9715209-... retry_count=0
  "idempotency claim" event_id=f9715209-... claimed=false
  "duplicate delivery, skipping (already processed)" event_id=f9715209-...
```

Confirmed identically at the reviewer/report layer: seeding one fresh
commit end-to-end through both live agents (with the storage fix
applied) took the Postgres `reports` row count from 5 to exactly 6 —
never higher on repeated redeliveries of the same event.

**Infrastructure regression check:**

```
$ rabbitmqctl list_queues name durable arguments | grep -E "q\.(commits|findings|reviews|retry|dlq)"
q.retry.5s    true  [{"x-dead-letter-exchange","swarm.events"},{"x-message-ttl",5000}]
q.retry.30s   true  [{"x-dead-letter-exchange","swarm.events"},{"x-message-ttl",30000}]
q.retry.5m    true  [{"x-dead-letter-exchange","swarm.events"},{"x-message-ttl",300000}]
q.dlq         true  []
q.commits     true  [{"x-dead-letter-exchange","swarm.events.dlx"},{"x-dead-letter-routing-key","q.commits"}]
q.findings    true  [...]
q.reviews     true  [...]

$ redis-cli -a **** ping
PONG

$ psql ... -c '\d processed_events'
claimed_at    | timestamptz | not null | now()
completed_at  | timestamptz |          |
```

All Phase 0-3 infrastructure (watcher, `shared/broker.py`'s core
topology, Postgres schema for `modules`/`imports`/`findings`) is
unchanged and still validates cleanly; `pytest tests/` (206 passed)
covers every Phase 0-3 file's original intent.

## Known limitations

- **Some `GitCommandError`s are permanent, not transient**, but are
  classified `RetryableError` uniformly (e.g. a redundant `--unshallow`
  on an already-unshallowed repo, hit organically while replaying a
  synthetic DLQ message during this phase's own live validation). The
  retry ladder still bounds this correctly — it exhausts after 3
  attempts and lands in `q.dlq` rather than looping forever — just less
  efficiently than a finer-grained classification could. Not fixed here
  to avoid over-fitting the classifier to one observed edge case;
  documented instead.
- **Rate limiter/circuit breaker are per-process, not cluster-wide, for
  the breaker.** `shared/ratelimit.py` is genuinely distributed (Redis);
  `shared/breaker.py` is in-process only (matches the roadmap's
  "integrate into LLM calls" scope) — running multiple researcher/reviewer
  replicas means each has its own breaker view of a given provider's
  health, so one replica can be OPEN while another is still CLOSED
  against the same outage.
- **`GRACEFUL_SHUTDOWN_SECONDS` is a fixed budget, not adaptive.** A
  message whose processing (LLM call + retries + backoff) could
  legitimately exceed 30s under heavy backoff is force-cancelled rather
  than allowed to finish; the idempotency claim/redelivery safety net
  covers this correctly (no duplicate/lost output), but the in-flight
  attempt's own work is wasted.
- **No cluster-wide retry-count ceiling across DLQ replays.** A message
  replayed via `tools/replay_dlq.py` gets a fresh `x-retry-attempt`
  budget (starts the ladder over) — appropriate for "an operator fixed
  the root cause," but nothing stops an operator from replaying the same
  poison message repeatedly if the underlying cause wasn't actually
  fixed; this is a deliberate operational tool, not an automatic loop.
- **`agents/watcher` has no retry/idempotency changes.** It's a
  synchronous FastAPI request handler (not a queue consumer), so the
  Phase 4 consumer-hygiene patterns don't directly apply; a webhook
  delivery failure is GitHub's own retry responsibility, unchanged from
  Phase 1.
- **No real Postgres migration runner**, carried over from every prior
  phase's known gaps — `003_idempotency_claims.sql` needs manual
  application to an already-running volume (documented in
  `db/README.md`), same as `002`.
- **LLM provider credentials were unavailable in this sandbox** (same as
  every prior phase) — every live validation run used
  `LLM_PROVIDER=none`/`NullLLMClient`, exercising the same
  degrade-gracefully path a real outage would. Each provider's retry/
  backoff/classification logic is covered by `tests/test_llm_retry.py`
  against mocked HTTP responses shaped like each API's real documented
  error format, not a live provider.

## Operational runbook

**Inspect the DLQ:**
```bash
python -m tools.replay_dlq inspect
python -m tools.replay_dlq inspect --original-queue q.commits --reason-contains timeout
```

**Replay after fixing a root cause:**
```bash
python -m tools.replay_dlq replay --original-queue q.commits --dry-run   # preview first
python -m tools.replay_dlq replay --original-queue q.commits             # then actually replay
```

**Check retry-ladder depth (messages currently waiting out a delay):**
```bash
docker compose exec rabbitmq rabbitmqctl list_queues name messages | grep -E "q\.retry|q\.dlq"
```

**Check for stuck/abandoned idempotency claims** (a claim older than
`IDEMPOTENCY_STALE_CLAIM_SECONDS` with no `completed_at` — usually
means a consumer crashed mid-message and hasn't been redelivered yet,
or a redelivery is still catching up):
```sql
SELECT event_id, event_type, claimed_at, now() - claimed_at AS age
FROM processed_events
WHERE completed_at IS NULL
ORDER BY claimed_at;
```

**Restart a consumer safely:** send `SIGTERM`; it stops accepting new
work, drains any in-flight message within `GRACEFUL_SHUTDOWN_SECONDS`,
then exits. No special pre-shutdown drain step is required — RabbitMQ
redelivers anything left unacked, and idempotency claims correctly gate
against reprocessing already-completed work.

**A consumer logs `"fatal error ... stopping service"` and exits
(`SystemExit(1)`):** this means an unclassified exception occurred —
check the logged `reason`, fix the underlying issue (bad config, bad
credentials, a new exception type `classify_*_failure` doesn't recognize
yet), then restart the process. The triggering message was
`nack(requeue=True)`, not lost — it'll be redelivered once a consumer is
listening again.

## Readiness for a next phase

Ready to build on:

- **`shared/retry.py`'s per-rung-exchange pattern generalizes** to any
  future queue that needs delayed retry — the ladder isn't hardcoded to
  `commit.detected`/`findings.ready`.
- **`IdempotencyStore` is reusable as-is** by a future consumer (e.g. if
  `agents/orchestrator` starts consuming events) — same claim/complete/
  release contract.
- **`shared/breaker.py`/`shared/ratelimit.py` are keyed generically**
  (by name/`provider:model`), not LLM-specific — a future outbound
  integration (a different external API) can reuse both directly.

Needed before a distributed/multi-replica deployment:

- A cluster-wide (not per-process) circuit breaker if multiple
  researcher/reviewer replicas should share one health view per provider
  (e.g. a Redis-backed breaker, mirroring `shared/ratelimit.py`'s own
  design).
- A real Postgres migration runner (Alembic or similar) — three manual
  migrations is the point where hand-applying them stops scaling.
- Finer-grained retryable/fatal classification for `GitCommandError`
  subtypes, informed by real production failure data rather than the one
  edge case observed during this phase's own validation.
