# Runbook: Recovery

## How to replay the DLQ

`tools/replay_dlq.py` is the only supported way to touch `q.dlq` — AMQP
has no server-side "peek", so every operation drains the queue into
memory first, then decides per message (see `PHASE_4_REPORT.md`'s "DLQ
flow and replay" for the mechanics).

```bash
# 1. Always inspect first — this never removes or mutates anything.
python -m tools.replay_dlq inspect
python -m tools.replay_dlq inspect --original-queue q.commits --reason-contains timeout

# 2. Preview what a replay would do, still without changing anything.
python -m tools.replay_dlq replay --original-queue q.commits --dry-run

# 3. Fix the root cause the `reason` field points at (bad credentials,
#    an unreachable git remote, a downstream outage), THEN replay:
python -m tools.replay_dlq replay --original-queue q.commits
python -m tools.replay_dlq replay --event-id <id>     # one specific message
python -m tools.replay_dlq replay --all               # everything
```

A replayed message goes back to its original queue with a **fresh retry
budget** — this is an operator asserting "I fixed it," not an automatic
retry. Replaying a poison message without fixing the underlying cause
just re-poisons the DLQ; there's no cluster-wide ceiling stopping that
(documented in `PHASE_4_REPORT.md`'s known limitations).

## How to investigate a failure

1. **Find the `trace_id`.** It's in every log line for the affected
   commit/event, the watcher's webhook response body, and (if a real LLM
   provider was configured) the OTel trace. It's also the join key across
   watcher/researcher/reviewer logs — `grep '"trace_id": "<id>"'` across
   all three services' output reconstructs the whole path.
2. **Classify what happened**, from the log line itself:
   - `"poison message routed to DLQ"` — a `PoisonMessageError`
     (contract validation failure, a commit SHA that doesn't exist, a
     malformed LLM response). Zero retries were attempted; fix the root
     cause before replaying (see above).
   - `"message routed to retry ladder"` — a `RetryableError` (HTTP
     429/5xx, network timeout, transient git failure). Check
     `x-retry-attempt` in the log/headers; it'll retry automatically up
     to 3 times before landing in `q.dlq`.
   - `"fatal error ... stopping service"` — an unclassified exception.
     The service exited (`SystemExit(1)`); the triggering message was
     `nack(requeue=True)`, not lost. Read the logged `reason`, fix it
     (bad config/credentials, or a new exception type
     `classify_*_failure` in `agents/*/main.py` doesn't recognize yet),
     then restart the process/container.
3. **Check for a stuck idempotency claim** (a consumer crashed mid-message
   and hasn't been redelivered yet):
   ```sql
   SELECT event_id, event_type, claimed_at, now() - claimed_at AS age
   FROM processed_events
   WHERE completed_at IS NULL
   ORDER BY claimed_at;
   ```
   A claim older than `IDEMPOTENCY_STALE_CLAIM_SECONDS` (default 300s)
   with no `completed_at` is safe to leave alone — the next redelivery of
   that `event_id` will reclaim it automatically (see
   `shared/idempotency.py` and `PHASE_4_REPORT.md`'s chaos scenario A/B).
4. **Check the retry ladder's current depth** (messages waiting out a
   delay right now):
   ```bash
   docker compose exec rabbitmq rabbitmqctl list_queues name messages | grep -E "q\.retry|q\.dlq"
   ```

## How to verify traces

1. Confirm `OTEL_TRACES_EXPORTER=otlp_http` and
   `OTEL_EXPORTER_OTLP_ENDPOINT` point at a reachable Phoenix instance
   (`http://localhost:6006` for `docker-compose.yml`'s host networking, or
   `http://phoenix:6006` from inside a container on
   `docker-compose.dev.yml`/`.prod.yml`'s bridge network).
2. Open Phoenix at `http://localhost:6006` (or query its GraphQL API
   directly — see `PHASE_5_REPORT.md`'s "Distributed trace validation" for
   the exact query used to confirm one trace spans all three services).
3. A healthy trace shows `watcher.webhook_received` as the root span, with
   `rabbitmq.publish` / `*.handle_*` / `llm.request` / `database.write` /
   `slack.deliver` children — 13 spans in the reference run documented in
   `PHASE_5_REPORT.md`.
4. Cross-reference with logs: every log line carries both the business
   `trace_id` (from the envelope) and `otel_trace_id`/`otel_span_id` (from
   the active span) — pivot from a log line to Phoenix via
   `otel_trace_id`, or from a Phoenix trace back to logs via the
   `swarm.trace_id` span attribute.
5. If spans aren't appearing: check `OTEL_TRACES_EXPORTER` isn't `none`,
   confirm the exporter endpoint is reachable from where the process
   actually runs (host vs. container — see `docs/architecture.md`'s
   networking note), and check the service's own logs for a
   `Transient error ... Connection refused` from the OTLP exporter, which
   means context propagation still worked but *export* failed — the two
   are decoupled by design (see `PHASE_5_REPORT.md`).

## How to inspect reports

```bash
docker compose exec postgres psql -U swarm -d code_review_swarm \
    -c "SELECT repo, commit_sha, severity, score, status, generated_at
        FROM reports ORDER BY generated_at DESC LIMIT 20;"

# Full detail for one report, including the LLM narrative and blast radius:
docker compose exec postgres psql -U swarm -d code_review_swarm \
    -c "SELECT * FROM reports WHERE commit_sha = '<sha>';"
```

See [`docs/demo/sample_review.json`](../demo/sample_review.json) for the
exact shape of a `reports` row alongside the `review.completed` event it
was generated from.

## How to troubleshoot LLM providers

`shared/llm.py` makes "not configured" and "provider is down" the same
code path on purpose (`LLMError` either way) — every caller already
degrades gracefully (empty semantic summary, template-based narrative),
so a provider issue never blocks the pipeline. To diagnose *why* it's
degrading:

1. Check `LLM_PROVIDER` is set to `anthropic`/`openai`/`ollama` (not
   `none`/unset) and the matching credential
   (`ANTHROPIC_API_KEY`/`OPENAI_API_KEY`/`OLLAMA_BASE_URL`) is present —
   a missing credential silently falls back to `NullLLMClient`, logged as
   `"no LLM provider configured"`.
2. Check the circuit breaker state in the logs
   (`llm.breaker_state` span attribute / `swarm_circuit_breaker_opens_total`
   metric). After `LLM_BREAKER_FAILURE_THRESHOLD` (default 5) consecutive
   failures it opens for `LLM_BREAKER_OPEN_SECONDS` (default 60) and fails
   fast with `CircuitOpenError` — no network attempt at all during that
   window. This is expected behavior during a real provider outage, not a
   bug.
3. Check the rate limiter isn't the bottleneck: `swarm_rate_limit_delays_total`
   / `swarm_rate_limit_delay_seconds` (Redis-backed, per
   `"<provider>:<model>"`). A Redis outage degrades to "no limiting,"
   logged as a warning — it never blocks the pipeline either.
4. Check `swarm_llm_failures_total{provider=...,reason=...}` in
   Prometheus/Grafana's LLM dashboard for the failure reason breakdown
   (timeout, HTTP error, malformed response).
5. Each provider is a direct HTTP client (`shared/llm.py`'s
   `AnthropicClient`/`OpenAIClient`/`OllamaClient`), so a provider-side
   outage or credential problem shows up as a normal `LLMError` with the
   underlying HTTP status/timeout in the log's `reason` field — check that
   against the provider's own status page before assuming a bug here.
