# Phase 5 Report — Observability

**Project:** Event-Driven Multi-Agent Code Review Swarm
**Phase:** 5 — make system behavior visible: distributed tracing,
metrics, dashboards, LLM cost/latency observability, load testing.
**Status:** Complete and validated live (not just unit-tested — see
"Distributed trace validation" and "Live validation summary" below)

## Pre-work audit (required before any change)

Read `README.md` and `PHASE_0_REPORT.md`–`PHASE_4_REPORT.md` in full
before touching code. Assessment against the Phase 5 objective ("make
system behaviour visible" — answer what happened, when, how long, which
service/LLM call failed, cost, trace path):

| Area the roadmap asked for | Before Phase 5 |
|---|---|
| Structured JSON logs with `service`/`trace_id`/`correlation_id`/`duration_ms` on key lifecycle events | **COMPLETE** (`shared/logging.py`, present since Phase 0/3) |
| A `trace_id` threaded through every hop via envelope + log context | **COMPLETE** (business-level only — no distributed-tracing backend, no spans, nothing visualized) |
| Retry/breaker/rate-limit *state* | **PARTIAL** — real state machines exist (`shared/retry.py`, `shared/breaker.py`, `shared/ratelimit.py`) and log their transitions, but as in-process counters/log lines only, never exported anywhere queryable |
| OpenTelemetry tracing/metrics | **MISSING** — no `shared/telemetry.py`, no span, no metric instrument anywhere in the repo |
| Phoenix / Prometheus / Grafana | **MISSING** — `docker-compose.yml` had exactly three services (RabbitMQ, Postgres, Redis) |
| LLM cost/token/retry/breaker-state observability | **MISSING** — `shared/llm.py::_log_usage` logged tokens/latency but recorded nothing, and no cost estimation existed anywhere |
| `/metrics` endpoints | **MISSING** |
| Load test / performance report | **MISSING** |

Everything below fills exactly this gap. No business logic changed:
watcher, researcher, reviewer, retry ladder, idempotency, circuit
breaker, and rate limiter are functionally identical to Phase 4 — every
touch to those files is additive (a span, a metric increment, a log
field), confirmed by running the full pre-existing Phase 0–4 test suite
unchanged after each edit (see "Testing").

## Architecture

```
shared/telemetry.py
  init_telemetry(service_name) -> installs a process-wide TracerProvider
  + MeterProvider once, entirely env-driven (OTEL_TRACES_EXPORTER /
  OTEL_METRICS_EXPORTER: otlp_http | otlp_grpc | console | none, +
  "prometheus" for metrics). Called once per agent's run()/lifespan.

Tracing (W3C traceparent over AMQP headers):
  watcher.webhook_received (SERVER)
    -> rabbitmq.publish commit.detected (PRODUCER, injects traceparent)
        -> [AMQP hop] ->
    researcher.handle_commit_detected (CONSUMER, extracts traceparent)
        -> repository.ensure / repository.clone / repository.refresh
        -> researcher.graph_build
        -> database.write (graph persistence)
        -> researcher.blast_radius_analysis
        -> rabbitmq.publish findings.ready (PRODUCER)
            -> [AMQP hop] ->
        reviewer.handle_findings_ready (CONSUMER)
            -> reviewer.review_generation
                -> llm.request (CLIENT, shared/llm.py)
                -> database.write (report persistence)
                -> slack.deliver (CLIENT)
            -> rabbitmq.publish review.completed (PRODUCER)

  One trace_id end to end. Verified live against a real Phoenix
  instance — see "Distributed trace validation".

Metrics (Prometheus exposition, via opentelemetry-exporter-prometheus):
  watcher   -> mounted at /metrics on its existing FastAPI app (:8001)
  researcher -> own exposition server (:9102, RESEARCHER_METRICS_PORT)
  reviewer   -> own exposition server (:9103, REVIEWER_METRICS_PORT)
  Prometheus scrapes all three + rabbitmq_prometheus (:15692) +
  postgres_exporter (:9187) + itself.

Logs (shared/logging.py, extended not replaced):
  every line already had service/trace_id/correlation_id; this phase adds
  severity (alias of level), otel_trace_id, otel_span_id (read from
  whatever span is active when the line is emitted), and an event_type
  field on every lifecycle log line (commit.detected, findings.ready,
  review.completed, repository.cache_hit, database.write, llm.completion,
  ...) — so a log line, a metric label, and a span name all use the same
  vocabulary.
```

### Why this design

- **Business `trace_id` (Phase 0) and OTel `trace_id` are deliberately
  kept as two separate identifiers**, not merged. Merging them would have
  meant either forcing `shared/contracts.py::Envelope.trace_id` to be a
  valid 128-bit OTel trace ID (a contract change to code explicitly
  marked strict/closed since Phase 0) or hand-constructing OTel
  `SpanContext`s with a borrowed trace ID (fragile, easy to get subtly
  wrong). Instead: OTel does real W3C `traceparent` propagation over the
  AMQP headers dict (`shared/broker.py::Broker.publish` already carried a
  `headers={"trace_id": ...}` dict — this phase adds `traceparent`
  alongside it, no removal), and every log line carries *both* IDs, so a
  human can pivot from a log line to a Phoenix trace (via `otel_trace_id`)
  or from a Phoenix trace back to logs (attach the business `trace_id` as
  a span attribute — `swarm.trace_id`). No contract changed; no span-id
  hacking.
- **Metrics are recorded at the exact call site that already logs the
  equivalent event** (e.g. `shared/retry.py::schedule_retry`'s existing
  `log.warning("message routed to retry ladder", ...)` gets one line
  added right after it: `telemetry.get_metrics().retry_count.add(...)`).
  No separate metrics-recording pass, no drift between what's logged and
  what's measured.
- **A no-op provider is always safe.** `shared/telemetry.get_tracer()`/
  `get_meter()` read whatever OpenTelemetry's *global* provider currently
  is. Every one of the 206 pre-existing tests calls `shared/broker.py`,
  `shared/llm.py`, etc. without ever calling `init_telemetry()` — so every
  span/metric call in this phase's instrumentation resolves against
  OTel's own inert no-op provider during those tests, doing nothing,
  costing nothing, never crashing. All 206 pre-existing tests pass
  unmodified (see "Testing").

## Metrics catalog

All in `shared/telemetry.py::Metrics`, Prometheus wire names shown (the
Prometheus exporter appends a unit suffix for `ms`→`_milliseconds`
and `s`→`_seconds` histograms — see "Known limitations" for the one time
this actually broke something):

| Metric (Prometheus name) | Type | Labels | Recorded in |
|---|---|---|---|
| `swarm_events_processed_total` | counter | `event_type`, `direction` | `shared/broker.py::publish`, both consumers |
| `swarm_commit_events_total` | counter | `repo`, `direction` | watcher, researcher |
| `swarm_findings_events_total` | counter | `repo`, `direction` | researcher, reviewer |
| `swarm_review_events_total` | counter | `repo`, `severity`, `direction` | reviewer |
| `swarm_dlq_total` | counter | `original_queue`, `poison` | `shared/retry.py` |
| `swarm_retry_total` | counter | `original_queue`/`queue`, `rung`/`provider` | `shared/retry.py`, `shared/llm.py` |
| `swarm_circuit_breaker_opens_total` | counter | `breaker` | `shared/breaker.py::_open` |
| `swarm_rate_limit_delays_total` | counter | `key` | `shared/ratelimit.py` |
| `swarm_rate_limit_delay_seconds` | histogram | `key` | `shared/ratelimit.py` |
| `swarm_llm_calls_total` | counter | `provider`, `model`, `outcome` | `shared/llm.py::_execute_with_resilience` |
| `swarm_llm_failures_total` | counter | `provider`, `model`, `reason` | `shared/llm.py` |
| `swarm_llm_tokens_in_total` / `_out_total` | counter | `provider`, `model` | `shared/llm.py::_log_usage` |
| `swarm_llm_cost_usd_total` | counter | `provider`, `model` | `shared/llm.py::_log_usage` |
| `swarm_llm_duration_milliseconds` | histogram | `provider`, `model` | `shared/llm.py::_log_usage` |
| `swarm_slack_deliveries_total` | counter | `outcome` | `shared/slack.py::send` |
| `swarm_repository_clones_total` | counter | `url` | `agents/researcher/repository.py::_clone` |
| `swarm_repository_cache_hits_total` | counter | `repo` | `agents/researcher/repository.py::ensure` |
| `swarm_repository_clone_duration_milliseconds` | histogram | `url` | `agents/researcher/repository.py::_clone` |
| `swarm_repository_refresh_duration_milliseconds` | histogram | `unshallow` | `agents/researcher/repository.py::_fetch` |
| `swarm_blast_radius_duration_milliseconds` | histogram | `repo` | `agents/researcher/db.py::blast_radius` |
| `swarm_review_duration_milliseconds` | histogram | `repo`, `severity` | `agents/reviewer/main.py::process_findings` |
| `swarm_db_write_duration_milliseconds` | histogram | `table`, `operation` | `agents/researcher/db.py`, `agents/reviewer/storage.py` |
| `swarm_db_failures_total` | counter | `operation` | `agents/researcher/db.py`, `agents/reviewer/storage.py` |
| `swarm_db_pool_connections` | observable gauge | `component`, `state` (total/idle/in_use) | `telemetry.register_pool_gauges`, sampled from the asyncpg pool at scrape time |

Plus everything RabbitMQ's own `rabbitmq_prometheus` plugin exposes
(`rabbitmq_queue_messages_ready`, `_channel_messages_published_total`,
`_channel_messages_delivered_total`, ...) and everything
`postgres_exporter` exposes (`pg_up`, connection/transaction stats).

## Docker services

`docker-compose.yml` adds `phoenix`, `prometheus`, `grafana`,
`postgres_exporter`, and enables RabbitMQ's `rabbitmq_prometheus` plugin
via a mounted `observability/rabbitmq/enabled_plugins` (the management
image doesn't enable it by default). All persist to named volumes
(`phoenix_data`, `prometheus_data`, `grafana_data`) and have healthchecks
(`docker compose ps` shows `healthy` for all seven services — see "Live
validation summary").

**All four new services run with `network_mode: host`.** This is the one
real architectural surprise of this phase — see "Known limitations" for
the full diagnosis; short version: this development sandbox's Docker
daemon blocks *fresh* container-to-container bridge traffic outright
(reproduced with a raw `ping` between two brand-new, unrelated
containers — 100% packet loss, `dial tcp: connect: connection timed
out`), while host↔container via a published port works reliably — the
exact path every existing agent (running as a host process, not a
container) already uses to reach Postgres/RabbitMQ/Redis. Rather than
fight a sandbox-specific restriction, the four new services use host
networking so Prometheus→RabbitMQ, Prometheus→postgres_exporter,
Grafana→Prometheus, and Prometheus→watcher/researcher/reviewer all go
through that same already-proven `localhost:<port>` path. This is a
Linux-only mechanism; a Docker Desktop (Mac/Windows) or Kubernetes
deployment would use normal bridge/service networking instead (undo the
`network_mode: host` lines, restore `ports:`, point configs back at
service DNS names) — noted explicitly in `docker-compose.yml`'s comments
and `.env.example`.

## Distributed trace validation

Ran the real pipeline end to end — `docker compose up -d`, then the
watcher/researcher/reviewer as host processes with
`OTEL_TRACES_EXPORTER=otlp_http`, `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:6006`
— and POSTed a real HMAC-signed GitHub push webhook (not
`tools/seed_commit.py`, so the trace genuinely originates at
`watcher.webhook_received`, matching "GitHub Push → Watcher → Researcher
→ Reviewer → Slack" literally). Watcher responded:

```json
{"status":"accepted","trace_id":"cdbd381f-c06d-4276-af4d-b01fd5fa1221","published_event_ids":["f812f480-ec1d-4e48-a691-5f506d9b4a4e"]}
```

Queried Phoenix's own GraphQL API afterward (`POST /graphql`) for every
span grouped by OTel `trace_id` — this is the same data the Phoenix UI at
http://localhost:6006 renders as a trace waterfall (this sandbox has no
way to capture a browser screenshot; the query below is the same data,
programmatically verified rather than eyeballed):

```
$ curl -s -X POST http://localhost:6006/graphql -H "Content-Type: application/json" \
    -d '{"query":"{ projects { edges { node { spans(first: 50) { edges { node { name context { traceId spanId } parentId } } } } } } }"}'
```

Result for OTel trace `8b2a38d019bffb71a3fee68eafe880b3` — **one trace,
13 spans, every service**:

```
watcher.webhook_received
  rabbitmq.publish commit.detected
    researcher.handle_commit_detected
      repository.ensure
      researcher.graph_build
      database.write            (graph persistence)
      researcher.blast_radius_analysis
      rabbitmq.publish findings.ready
        reviewer.handle_findings_ready
          reviewer.review_generation
            database.write      (report persistence)
            slack.deliver
          rabbitmq.publish review.completed
```

The corresponding structured logs (same run) show the *identical*
`otel_trace_id` on the watcher, researcher, and reviewer log lines —
proving the trace_id survives every hop, not just that spans happen to
share a name:

```json
{"service":"watcher","message":"published commit.detected","trace_id":"cdbd381f-...","otel_trace_id":"8b2a38d019bffb71a3fee68eafe880b3","otel_span_id":"62d3bee18c8672fe", ...}
{"service":"reviewer","message":"published review.completed","trace_id":"cdbd381f-...","otel_trace_id":"8b2a38d019bffb71a3fee68eafe880b3","otel_span_id":"386a384e8a71857b", ...}
```

`GET /v1/projects` (Phoenix's REST API) confirmed the project accumulated
**220 traces** over this session's testing (the one above, plus the
100-commit load test, plus retry-ladder activity generated incidentally
by `pytest tests/`'s live-broker integration tests while a researcher
process happened to be running).

An earlier run against the same watcher process (before
`OTEL_EXPORTER_OTLP_ENDPOINT` was correctly exported into the researcher/
reviewer processes' environment — see "Known limitations") produced spans
whose *export* failed with a logged, retried, eventually-gave-up
`Transient error ... Connection refused` from
`opentelemetry.exporter.otlp.proto.http.trace_exporter` — while
**trace propagation itself kept working perfectly** (watcher and
researcher still shared one `otel_trace_id` in their logs). That's
direct evidence the two concerns — "does context propagate" and "does
export succeed" — are correctly decoupled: an unreachable collector
degrades to "spans buffered/dropped, logged," never to "pipeline
breaks" or "trace correlation breaks."

## Metrics validation

`docker compose ps` (all seven services): `rabbitmq`, `postgres`,
`redis`, `postgres_exporter`, `phoenix`, `prometheus`, `grafana` — all
`healthy`.

`GET http://localhost:9090/api/v1/targets` — all six scrape jobs `up`:

```
postgres    up
prometheus  up
rabbitmq    up
researcher  up
reviewer    up
watcher     up
```

Sample query (real data from the load test run):

```
$ curl -s 'http://localhost:9090/api/v1/query?query=swarm_events_processed_total' | ...
swarm_events_processed_total{event_type="findings.ready",direction="consumed",job="reviewer",...} 1
swarm_events_processed_total{event_type="review.completed",direction="published",job="reviewer",...} 1
```

```
$ curl -G http://localhost:9090/api/v1/query \
    --data-urlencode 'query=histogram_quantile(0.95, sum(rate(swarm_review_duration_milliseconds_bucket[5m])) by (le))'
-> 24.95   # ms, p95 reviewer-side processing time (LLM disabled in this sandbox, see Known limitations)
```

```
$ curl -s http://localhost:9187/metrics | grep pg_up   # postgres_exporter, reachable via host networking
pg_up 1
```

```
$ curl -s http://localhost:15692/metrics | head -3      # rabbitmq_prometheus plugin
erlang_mnesia_held_locks 0
```

## Dashboard validation

Grafana's dashboard-search API confirms all three dashboards
auto-provisioned into the "Code Review Swarm" folder on first boot (no
manual import step):

```
$ curl -s -u admin:admin http://localhost:3000/api/search?type=dash-db
Swarm: LLM               /d/swarm-llm/...
Swarm: Repository        /d/swarm-repository/...
Swarm: System Overview   /d/swarm-system-overview/...
```

Grafana→Prometheus datasource proxy, run through the *same* query one of
the LLM dashboard's panels uses, confirms the datasource is wired and
returns live data (not just provisioned config):

```
$ curl -s -u admin:admin 'http://localhost:3000/api/datasources/proxy/uid/prometheus/api/v1/query?query=up'
{"status":"success","data":{"resultType":"vector","result":[...11 series, all job/instance pairs...]}}
```

`tests/test_observability_config.py` additionally cross-checks, offline,
that every `swarm_*` metric name a dashboard panel queries is one
`shared/telemetry.py::Metrics` actually registers under its real
(unit-suffixed) Prometheus wire name — the exact static-analysis check
that would have caught the `_ms`/`_milliseconds` naming bug below before
it ever reached a running Prometheus.

No browser is available in this sandbox to capture an actual screenshot
of a rendered dashboard; the validation above (dashboards provisioned,
datasource live, panel queries verified against real metric names both
statically and by direct Prometheus query) is the strongest evidence
obtainable here. Open http://localhost:3000 (`admin`/`admin`) to see them
rendered.

## LLM observability

`shared/llm.py::_BaseLLMClient._execute_with_resilience` — the single
choke point every provider already routes every call through (see
`README.md`'s "LLM retry/backoff") — now wraps its retry loop in one
`llm.request` CLIENT span per call, recording `llm.provider`,
`llm.model`, `llm.retry_count`, and `llm.breaker_state` as span
attributes, and `swarm_llm_calls_total{outcome=...}` /
`swarm_retry_total{component="llm",...}` as metrics at every attempt.
`_log_usage()` (already called once per successful response, already
logging token counts) additionally calls
`telemetry.record_llm_success()`, which estimates cost from a static
`$/1M tokens` table (`shared/telemetry.py::PRICING_PER_1M_TOKENS_USD`,
env-overridable per model via `LLM_PRICE_<MODEL>_IN_PER_1M`/
`_OUT_PER_1M`, `ollama` always free) and records it as both a span
attribute (`llm.cost_usd`) and `swarm_llm_cost_usd_total`.

**No live LLM provider was reachable in this sandbox** — the same
limitation every prior phase's report already documents (no API keys, no
outbound egress to `api.anthropic.com`/`api.openai.com`, no local Ollama
server). Every live validation run in this report used
`LLM_PROVIDER=none`/`NullLLMClient`, which — as `README.md` already
explains — raises `LLMError` directly without ever reaching
`_execute_with_resilience`, so it produces zero `llm.request` spans by
design (correctly: there was no LLM call to trace). The span/metrics/cost
logic itself is instead covered by unit tests
(`tests/test_telemetry.py::test_record_llm_success_updates_metrics_and_span`,
`test_record_llm_failure_updates_metrics_and_span`,
`test_estimate_cost_usd_*`) built against the exact `LLMResponse` shape
`shared/llm.py` produces, plus the existing `tests/test_llm.py`/
`test_llm_retry.py` mocked-HTTP-response tests, none of which needed
modification. Example (from `test_record_llm_success_updates_metrics_and_span`):
1,000 input + 500 output tokens on `claude-haiku-4-5-20251001` →
`llm.cost_usd = 0.0035`, matching `(1000/1e6)*$1.00 + (500/1e6)*$5.00`.

## Performance testing

`tools/load_test.py` — new tool, not a reuse of `tools/seed_commit.py` —
creates a real local git fixture repo (a small import graph, so blast
radius does real recursive-CTE work) with N *actual* commits, one per
change, publishes one `commit.detected` per commit through the real
`Broker`, then polls Postgres's `reports` table until every commit's
`review.completed` has been persisted (or a deadline elapses).

Run against this sandbox's stack (single researcher + single reviewer
process, `prefetch_count=1` each — see `README.md`'s "Consumer hygiene",
unchanged from Phase 4 — so this is deliberately a single-instance,
no-concurrency baseline, not a claim about the ceiling):

```
$ python -m tools.load_test --count 100 --timeout 120

Commits submitted: 100
Commits completed (review.completed persisted): 100/100
Wall time (first publish -> last completion): 13.21s
Throughput: 7.57 commits/sec

Latency (publish -> review.completed persisted)
  Average: 5017.5 ms
  p50:     3829.3 ms
  p95:    11712.3 ms
  p99:    12700.2 ms
  Min:      591.1 ms
  Max:    12704.9 ms
```

100/100 completed, zero DLQ/retry events, zero errors in either agent's
log for the run's duration (verified by grepping both logs for the exact
timestamp window — the only WARNING-level lines were the 100 expected
`"semantic summary generation skipped"` messages, since `LLM_PROVIDER=none`
in this sandbox). Latency grows with queue position (min 591ms for the
first commit, max 12.7s for the last of 100) because the researcher
processes strictly one commit at a time (`prefetch_count=1`) — an
intentional Phase 4 design choice (see README's "Consumer hygiene and
graceful shutdown"), not a Phase 5 regression; p50/p95/p99 reported above
characterize *this specific single-instance queueing behavior*, not a
theoretical multi-replica ceiling (see "Recommendations for Phase 6").

A second run (before restarting the researcher to fix the metric-name
bug below) produced statistically consistent numbers (100/100, 14.72s
wall, 6.79 commits/sec, p50 4.87s/p95 12.7s/p99 14.2s) — reassuring that
these aren't a one-off fluke.

## Testing

234 tests pass (`pytest tests/`), up from 206 at the start of this phase
— every one of the 206 pre-existing tests passes completely unmodified;
28 are new:

- **`tests/test_telemetry.py` (19 tests)** — span creation and
  attributes, ERROR status + exception event on a failing span,
  `current_trace_id()`/`current_span_id()` tracking the active span,
  producer→header→consumer trace-ID continuity (single hop and a
  two-hop watcher→researcher→reviewer chain, mirroring the live Phoenix
  validation above), a consumer span with no/garbled headers degrading to
  a fresh valid trace instead of crashing, every catalog metric
  instrument existing and recording, the asyncpg pool gauge callback,
  LLM cost estimation (known model, unknown model → `None` not a
  misleading `0`, `ollama` free, env-var price override), `record_llm_success`/
  `record_llm_failure` updating both span attributes and metrics
  together, and `init_telemetry()`'s console/`none` exporters + idempotent
  double-init — the last two run in a subprocess specifically so they
  never install a real global `TracerProvider`/`MeterProvider` into the
  pytest process itself (OpenTelemetry only allows that once per
  process; doing it inline would leak into every other test module in
  the same session).
- **`tests/test_observability_config.py` (9 tests)** — static validation
  of `observability/**` and the docker-compose services that mount them:
  `prometheus.yml` parses and has exactly the six expected scrape jobs
  with well-formed targets, the Grafana datasource/dashboard-provider
  YAML point at the right places, every dashboard JSON is valid and
  internally consistent (unique panel ids/refIds, every panel targets
  the `prometheus` datasource), `rabbitmq_prometheus` is actually in the
  enabled-plugins file, `docker-compose.yml` mounts every config file it's
  supposed to — and, most importantly,
  **`test_dashboard_panel_metrics_match_shared_telemetry_catalog`**,
  which parses `shared/telemetry.py`'s own `create_*` calls to build the
  set of real Prometheus wire names and asserts every `swarm_*` name a
  dashboard panel queries is one of them. This is the exact test that
  would have caught the bug below before a human had to.

Ran `pytest tests/ -q` after every edit throughout this phase, not just
at the end — the 206→225→234 progression above is the actual order
things landed in, not a final cleanup pass.

## A bug this phase found (and fixed) via its own live validation

While validating the LLM/Repository dashboards against a real Prometheus,
`histogram_quantile(..., swarm_review_duration_ms_bucket...)` returned
nothing. Cause: `opentelemetry-exporter-prometheus` appends a unit
suffix derived from each instrument's `unit=` kwarg (`ms` →
`_milliseconds`, `s` → `_seconds`) to the metric name it exposes — a
histogram registered as `"swarm_review_duration_ms"` with `unit="ms"`
is actually exposed as `swarm_review_duration_ms_milliseconds`, not
`swarm_review_duration_ms`. Confirmed directly:

```
$ curl -sL http://localhost:9103/metrics | grep review_duration
# TYPE swarm_review_duration_ms_milliseconds histogram
```

Fixed by dropping the unit from every histogram's *name* string (keeping
it only in `unit=`) — `shared/telemetry.py`'s Python attribute names
(e.g. `review_duration_ms`) still carry the unit for readability at call
sites, but the wire name is now just `swarm_review_duration`, exposed
correctly as `swarm_review_duration_milliseconds` with no double suffix.
Every dashboard JSON and `tests/test_observability_config.py` were
updated to match; re-validated against a live Prometheus afterward (see
"Metrics validation" above, which reflects the corrected names). Caught
this session, before it ever reached a commit — flagged here anyway
since it's a real, easy-to-reintroduce OTel/Prometheus interop gotcha,
and it's exactly why
`test_dashboard_panel_metrics_match_shared_telemetry_catalog` exists now.

## Known limitations

- **Docker networking on this sandbox blocks fresh container-to-container
  bridge traffic outright** — reproduced independent of this repo's
  config (`docker run --rm --network swarm_default alpine ping postgres`
  → 100% packet loss; `docker run --rm --network swarm_default postgres:16
  psql ...` → connection timeout). Host↔container via a published port
  works reliably (confirmed for every one of RabbitMQ/Postgres/Redis/
  Phoenix/Prometheus/Grafana/postgres_exporter). Worked around with
  `network_mode: host` on the four new services (Linux-only) rather than
  fighting the restriction — see "Docker services" above for the full
  reasoning. **A Docker Desktop (Mac/Windows) or Kubernetes deployment
  should not use `network_mode: host`** as-is; it would need normal
  bridge/service networking restored (this is noted inline in
  `docker-compose.yml`).
- **No live LLM provider was reachable in this sandbox** (same as every
  prior phase) — LLM spans/cost/metrics are validated by unit test
  against the exact response shape real providers produce, not a live
  call. See "LLM observability" above.
- **The load test's latency numbers are a single-instance,
  `prefetch_count=1` baseline**, not a scaled-out ceiling — see
  "Performance testing" and "Recommendations for Phase 6".
- **`postgres_exporter` and the RabbitMQ Prometheus plugin cover
  server-side stats only**; application-level Postgres observability
  (report/graph write duration, blast-radius query duration, connection
  pool in-use/idle, write failures) comes from `shared/telemetry.py`'s
  own instruments in `agents/researcher/db.py`/`agents/reviewer/storage.py`
  instead, which is the more directly useful signal for this system
  anyway (per-operation, not per-connection).
- **Cost estimates are a static price list**, not a billing API — see
  `shared/telemetry.py::PRICING_PER_1M_TOKENS_USD`'s docstring; override
  per-model via `.env` if prices drift.
- **The circuit breaker remains in-process, not cluster-wide** — a Phase
  4 limitation this phase didn't change (out of scope: "preserve...
  circuit breaker... unless observability integration absolutely
  requires a small touch," and it didn't). `swarm_circuit_breaker_opens_total`
  correctly reflects *this process's* breaker only; running multiple
  researcher/reviewer replicas would need a Redis-backed breaker (already
  flagged as a Phase 4 follow-up) before that metric means "the provider
  is down" cluster-wide rather than "this replica thinks it's down."
- **No OTel Collector in the middle.** Every service exports OTLP
  directly to Phoenix. Fine at this scale/for this environment; a
  production deployment fanning out to multiple backends (traces to
  Phoenix, metrics to a managed Prometheus, logs to a SIEM) would
  typically insert a Collector — noted as a Phase 6 candidate below, not
  implemented here since nothing in this phase's scope needed it.

## Recommendations for Phase 6

- **Multi-instance load testing.** `tools/load_test.py` is ready to point
  at a scaled-out deployment (more researcher/reviewer replicas,
  `RABBITMQ_PREFETCH`/`prefetch_count` tuning) — the single-instance
  numbers in this report are the baseline to compare against, not the
  final word on throughput.
- **A Redis-backed circuit breaker** (mirroring `shared/ratelimit.py`'s
  design, already flagged in Phase 4) would make
  `swarm_circuit_breaker_opens_total` meaningful cluster-wide, not
  per-replica.
- **An OTel Collector** between the agents and Phoenix/Prometheus, once
  there's a second consumer of the same telemetry (e.g. a managed APM
  backend alongside Phoenix) — batches/retries/fans out centrally instead
  of every service managing its own exporter retry logic.
- **Real LLM provider validation.** Every phase's report, including this
  one, has validated LLM-path logic against mocked/no-op responses only.
  The first environment with real API access should run the live
  pipeline once with `LLM_PROVIDER=anthropic` (or `openai`/`ollama`) and
  confirm `llm.request` spans/cost actually appear in Phoenix/Prometheus
  — everything is wired for this already; it's only ever been unit-tested.
- **Alerting rules** on top of the metrics this phase added (e.g.
  `swarm_circuit_breaker_opens_total` rate, `swarm_dlq_total` rate,
  `pg_up == 0`) — Prometheus is in place; no alerting rules/Alertmanager
  were added, since the roadmap asked for dashboards, not paging.

## Live validation summary (final handover)

- **Completed work:** `shared/telemetry.py` (tracing/metrics/propagation/
  cost estimation); OTel instrumentation across `shared/broker.py`,
  `shared/llm.py`, `shared/breaker.py`, `shared/ratelimit.py`,
  `shared/retry.py`, `shared/slack.py`, `shared/logging.py`, and all
  three agents' `main.py`/`repository.py`/`db.py`/`storage.py`; Phoenix +
  Prometheus + Grafana + postgres_exporter + RabbitMQ's Prometheus plugin
  in `docker-compose.yml`; three provisioned Grafana dashboards; a
  100-commit load test tool; 28 new tests; README + this report.
- **Total test count:** 234 passed (206 pre-existing, unmodified + 28
  new), `pytest tests/ -q`.
- **Metrics available:** 24 custom `swarm_*` instruments (see catalog
  above) + RabbitMQ's full `rabbitmq_prometheus` set + `postgres_exporter`'s
  full set, all confirmed scraped (`up` on all 6 Prometheus targets).
- **Trace validation:** one real, live, end-to-end trace — 13 spans, one
  `otel_trace_id`, `watcher.webhook_received` through
  `rabbitmq.publish review.completed` — verified via Phoenix's own
  GraphQL API (220 traces accumulated total across this session's
  testing).
- **Dashboard validation:** 3 dashboards auto-provisioned (no manual
  import), Grafana→Prometheus datasource confirmed live via its query
  proxy, every panel's metric name statically cross-checked against
  `shared/telemetry.py`'s real registered instruments.
- **Performance numbers:** 100/100 commits completed, 7.57 commits/sec,
  p50 3.83s / p95 11.71s / p99 12.70s (single researcher + single
  reviewer instance, `prefetch_count=1`).
- **Known limitations:** listed in full above — the two load-bearing ones
  are the sandbox's container-networking restriction (worked around with
  host networking, documented for non-Linux deployments) and the absence
  of a live LLM provider in this environment (consistent with every
  prior phase, worked around with mocked-response unit tests).
