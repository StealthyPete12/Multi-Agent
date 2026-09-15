# Phase 1 Report — Walking Skeleton

**Project:** Event-Driven Multi-Agent Code Review Swarm
**Phase:** 1 — walking skeleton (webhook → RabbitMQ → consumer)
**Status:** Complete and validated

## Starting state

Phase 0 delivered infrastructure only: Docker Compose (RabbitMQ,
Postgres, Redis), `shared/contracts.py`, `shared/logging.py`, DB schema,
and four empty agent placeholders (`ingestion`, `analysis`,
`aggregation`, `orchestrator`) inferred from the Phase 0 task brief since
no architecture roadmap document existed in the repo. No message broker
topology, no agent logic, no tests beyond contract round-trips.

## Structure decision: renamed placeholders

This phase's brief specifies exact deliverable paths —
`agents/watcher/main.py` and `agents/researcher/main.py` — which don't
match Phase 0's `ingestion`/`analysis` naming but do match those
placeholders' documented responsibilities 1:1. Renamed accordingly:

| Phase 0 name | Phase 1 name | Reason |
|---|---|---|
| `agents/ingestion` | `agents/watcher` | Same role: watches for commits, publishes `commit.detected` |
| `agents/analysis` | `agents/researcher` | Same role: consumes and investigates a commit |
| `agents/aggregation` | `agents/reviewer` | Same role: collects findings, produces the final review |
| `agents/orchestrator` | *(unchanged)* | Cross-cutting coordination concern, doesn't fit the 3-stage watcher/researcher/reviewer naming — kept and documented as out of scope for this phase |

No agent logic existed yet, so these were plain directory renames
(`git mv`) plus updated READMEs — nothing was rebuilt.

## Completed work

| # | Deliverable | Status | Notes |
|---|-------------|--------|-------|
| 1 | Broker layer (`shared/broker.py`) | ✅ Done | aio-pika, robust connection, durable topic exchange + DLX, publisher confirms, persistent delivery, per-queue dead-lettering, configurable QoS, reusable `declare_queue`/`publish` API |
| 2 | Watcher agent (`agents/watcher/main.py`) | ✅ Done | FastAPI + Uvicorn, HMAC-SHA256 signature verification, push-event + branch filtering, commit extraction, `commit.detected` publishing |
| 3 | Researcher consumer (`agents/researcher/main.py`) | ✅ Done | Consumes `q.commits`, validates contract, logs, acks — no AI, no DB writes |
| 4 | Seed script (`tools/seed_commit.py`) | ✅ Done | CLI, explicit or `--random` synthetic events, `--count` for bursts, custom/auto trace IDs |
| 5 | `.env.example` updates | ✅ Done | `RABBITMQ_EXCHANGE`, `RABBITMQ_PREFETCH`, queue names, `WATCHER_PORT`, `GITHUB_WEBHOOK_SECRET`, `WATCHED_BRANCHES`, `SMEE_URL` |
| 6 | Tests | ✅ Done | 22 new tests + 10 existing = 32 passing (see **Test results**) |
| 7 | Validation (Path A, Path B, durability, confirms) | ✅ Done | See **Validation results** |
| 8 | Documentation | ✅ Done | README architecture/event-flow/testing/webhook/Smee sections, this report |

No item was partially completed.

## Architecture decisions

- **One topic exchange, per-stage durable queues, per-queue DLQ.**
  `swarm.events` (topic) routes by `EventType.value` (e.g.
  `commit.detected`). Each queue declares `x-dead-letter-exchange` /
  `x-dead-letter-routing-key` pointing at a matching queue on
  `swarm.events.dlx`, so a message that's nacked without requeue (e.g.
  contract validation failure) lands in `<queue>.dlq` instead of being
  dropped or blocking the queue. This directly closes the "no message
  broker topology" gap flagged in `PHASE_0_REPORT.md`.
- **Publisher confirms via aio-pika's channel default.** `aio_pika`
  channels are opened in confirm mode by default; `exchange.publish(...)`
  already awaits the broker's ack/nack, so a caller that awaits
  `Broker.publish()` without an exception knows the broker persisted the
  message before returning. No extra confirm-tracking code was needed.
- **`Broker` is a thin, reusable class, not per-agent boilerplate.**
  `connect()`/`declare_queue()`/`publish()`/`close()` are generic enough
  for the researcher, watcher, seed script, and future reviewer/
  orchestrator agents to share without duplicating topology declarations.
- **Signature verification and payload extraction are pure functions.**
  `verify_signature()` and `extract_commit_events()` in
  `agents/watcher/main.py` take plain bytes/dicts and raise/return,
  independent of FastAPI or the network — this is what makes them unit
  testable without spinning up a server (see `tests/test_watcher_signature.py`,
  `tests/test_watcher_extract.py`).
- **`compare_url` is extracted but not published.** The task asked the
  watcher to extract `compare_url`, but `shared/contracts.py::CommitDetected`
  has no such field, and the instructions were explicit not to modify
  `shared/contracts.py` unless absolutely necessary. `compare_url` is
  extracted and logged (see the `published commit.detected` log line)
  but dropped before constructing the envelope. If a future phase wants
  it on the wire, that's a deliberate contract change, not a Phase 1
  side effect.
- **One `commit.detected` event per commit in a push.** GitHub push
  payloads carry a list of commits; the watcher emits one envelope per
  commit (sharing the push's `trace_id`) rather than one event for the
  whole push, so a downstream consumer's per-commit processing model
  doesn't need to unpack a batch.
- **Researcher nacks invalid contracts without requeue.** A message that
  fails `Envelope[CommitDetected].from_json(...)` validation is
  unrecoverable by retrying, so it's rejected with `requeue=False` and
  dead-letters instead of looping forever between redelivery and
  re-validation failure.

## Test results

```
$ pytest tests/ -v
32 passed, 1 warning in 0.91s
```

| File | Covers |
|---|---|
| `tests/test_contracts.py` | (existing, Phase 0) contract round-trips, strict rejection |
| `tests/test_watcher_signature.py` | valid/missing/wrong-secret/malformed/tampered-body HMAC verification |
| `tests/test_watcher_extract.py` | branch parsing, single/multi-commit extraction, empty-commit (branch delete) push |
| `tests/test_watcher_webhook.py` | full FastAPI endpoint: signature rejection (401), non-push/unwatched-branch ignoring, successful publish, `/healthz` |
| `tests/test_researcher_consumer.py` | valid message ack path, invalid-contract rejection (raises, would dead-letter) |
| `tests/test_broker.py` | connect + topology declare, publish/consume round-trip, message durability across a publisher-then-consumer connection cycle — **skips gracefully if RabbitMQ isn't reachable** rather than failing the suite |
| `tests/test_seed_commit.py` | explicit-field and `--random` commit construction, required-field validation, CLI defaults |

The one warning is a Starlette deprecation notice about `httpx` version
compatibility with `TestClient`, unrelated to this phase's code.

## Validation results

**Path A — `seed_commit.py` → RabbitMQ → consumer log:**

```
$ python -m tools.seed_commit --repo acme/widgets --branch main --author jane --message "fix: off by one" --sha deadbeef1234
{"...", "message": "seeded commit.detected", "event_id": "119fd8f9-...", "commit_sha": "deadbeef1234"}

$ python -m agents.researcher.main
{"...", "message": "researcher started", "queue": "q.commits"}
{"...", "message": "commit.detected received", "trace_id": "bdfd23ed-...",
 "event_id": "119fd8f9-...", "repo": "acme/widgets", "commit_sha": "deadbeef1234",
 "branch": "main", "author": "jane"}
```

**Path B — GitHub-style signed push → watcher → RabbitMQ → consumer log:**

```
$ uvicorn agents.watcher.main:app --port 8001
{"...", "message": "watcher started", "watched_branches": ["main"], "queue": "q.commits"}

$ curl -X POST http://127.0.0.1:8001/webhook/github \
    -H "X-GitHub-Event: push" -H "X-Hub-Signature-256: sha256=<valid hmac>" \
    -d '<push payload>'
200 {"status":"accepted","trace_id":"619d2e1e-...","published_event_ids":["32643d57-..."]}

# watcher log:
{"...", "message": "published commit.detected", "trace_id": "619d2e1e-...",
 "event_id": "32643d57-...", "repo": "acme/webhook-test", "compare_url": "https://.../compare/a...b"}

# invalid-signature request in the same run:
401 (rejected with "signature mismatch")

# researcher log after consuming:
{"...", "message": "commit.detected received", "trace_id": "619d2e1e-...",
 "event_id": "32643d57-...", "repo": "acme/webhook-test", "branch": "main",
 "author": "gh-author", "changed_files": ["src/main.py"]}
```

The `trace_id` (`619d2e1e-...`) is identical across the watcher's accept
response, its publish log, and the researcher's consume log — proving
trace propagation works across the HTTP → broker → consumer hop.

**Publisher confirms:** `Broker.publish()` awaits `aio_pika`'s confirm-mode
`exchange.publish(...)`; both Path A and Path B runs above completed the
`await` without raising, meaning RabbitMQ acked persistence of every
message before the caller (seed script / watcher handler) proceeded.

**Durability across consumer restart:**

```
$ python -m tools.seed_commit --repo acme/durability-test --sha durabilitytest001 ...
$ docker exec swarm_rabbitmq rabbitmqctl list_queues name messages
q.commits    1        # message sitting in the queue with no consumer attached

$ python -m agents.researcher.main   # start the consumer after the fact
{"...", "message": "commit.detected received", "commit_sha": "durabilitytest001", ...}

$ docker exec swarm_rabbitmq rabbitmqctl list_queues name messages
q.commits    0        # drained and acked
```

**Infrastructure regression check:**

```
$ ./scripts/validate_stack.sh
OK: rabbitmq is healthy
OK: postgres is healthy
OK: redis is healthy
OK: RabbitMQ management UI reachable at http://localhost:15672
OK: table 'modules'/'imports'/'commits'/'findings'/'reports'/'processed_events'/'audit_log' exist
==> all checks passed
```

No changes were made to `docker-compose.yml`, `db/migrations/`, or
`shared/contracts.py` — Phase 0's infrastructure is untouched and still
validates cleanly.

## Known limitations

- **No screenshots included.** This was validated in a headless
  CLI/container environment with no browser available to capture the
  RabbitMQ management UI visually. The queue/message-count evidence
  above comes from `rabbitmqctl list_queues` instead, which reports the
  same state the UI would show for `q.commits`/`q.commits.dlq`.
- **`compare_url` is extracted but not on the wire** (see architecture
  decisions above) — logged only, since adding it means changing the
  shared contract.
- **No CI wiring.** `pytest tests/` runs locally; nothing runs it or
  `scripts/validate_stack.sh` automatically yet (carried over from
  Phase 0's known gaps).
- **Single researcher instance, no orchestration.** One `q.commits`
  consumer with no work distribution, retry/backoff policy, or
  timeout tracking across multiple researcher instances —
  `agents/orchestrator` is still a placeholder, as scoped.
- **Dev-only webhook secret.** `.env.example`'s
  `GITHUB_WEBHOOK_SECRET=changeme_dev_secret` is a placeholder; real
  deployments need a generated secret and out-of-band secrets
  management (same caveat Phase 0 raised for DB/broker credentials).
- **DLQ messages are inert.** Dead-lettered messages accumulate in
  `<queue>.dlq` with no re-processing or alerting; that's a Phase 2+
  concern once there's a consumer that would care.

## Next-phase readiness assessment

Ready to build on:

- **Broker layer is agent-agnostic** — a future `researcher` doing real
  analysis, or the `reviewer` consuming `findings.ready`, can call
  `Broker.declare_queue(name, routing_keys=[...])` and
  `Broker.publish(envelope, routing_key=...)` without touching topology
  code.
- **Contract-driven consumption pattern is proven** — `parse_envelope`/
  `Envelope[T].from_json` plus dead-lettering on validation failure is
  the pattern any future consumer should copy.
- **Trace propagation works end-to-end**, which will matter once
  multiple agents need to correlate logs for the same commit.

Needed before Phase 2 (real analysis logic) can start:

- Decide whether `agents/researcher` gets AI/static-analysis logic added
  directly, or whether `q.findings`/`FindingsReady` publishing is built
  out first so `agents/reviewer` has something to consume.
- Idempotency: `processed_events` (Postgres) exists from Phase 0 but
  nothing writes to it yet — needed once a consumer's side effects
  aren't safely repeatable (unlike this phase's log-only researcher).
- A decision on `agents/orchestrator`'s scope (retries, timeouts,
  multi-researcher dispatch) before more than one researcher instance
  is expected to run.
