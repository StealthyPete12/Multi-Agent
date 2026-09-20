# Roadmap

This project was built in six phases (`PHASE_0_REPORT.md` through
`PHASE_5_REPORT.md`), each adding one architectural layer without
redesigning what came before, plus a Phase 6 packaging/documentation
pass (`PHASE_6_REPORT.md`). What's below is what every phase report
already flagged as a known gap or explicit follow-up, consolidated in
one place instead of scattered across six documents.

## Near-term (would meaningfully improve production-readiness)

- **A real Postgres migration runner** (Alembic or similar). Three manual
  migrations (`db/migrations/00{1,2,3}*.sql`) is already the point where
  hand-applying them to a running volume stops scaling — flagged as a gap
  since `PHASE_0_REPORT.md`, still open.
- **Multi-instance / horizontal-scale load testing.** `tools/load_test.py`
  and `docker-compose.prod.yml --scale researcher=N --scale reviewer=N`
  are both ready for this; it's never actually been run past a single
  instance of each agent (see `docs/performance.md`'s "Known
  bottlenecks").
- **Real LLM provider validation under load.** Every phase's validation,
  including Phase 6's, has run with `LLM_PROVIDER=none` (no API keys/
  network egress available in the development sandbox). The first
  environment with real provider access should confirm `llm.request`
  spans/cost actually populate Phoenix/Prometheus under the same
  100-commit load test.
- **A Redis-backed circuit breaker**, mirroring `shared/ratelimit.py`'s
  design, so `swarm_circuit_breaker_opens_total` means "the provider is
  down" cluster-wide rather than "this replica thinks it's down" once
  multiple researcher/reviewer replicas are running (flagged in
  `PHASE_4_REPORT.md`, restated in `PHASE_5_REPORT.md`).
- **Alerting rules** on top of the existing Prometheus metrics (e.g.
  `swarm_circuit_breaker_opens_total` rate, `swarm_dlq_total` rate,
  `pg_up == 0`) — the metrics exist; no Alertmanager rules were ever
  added, since the original scope asked for dashboards, not paging.

## Medium-term

- **Kubernetes/multi-host deployment.** Both compose files
  (`docker-compose.dev.yml`/`.prod.yml`) are single-host. A real
  multi-node deployment needs an orchestrator and is out of scope for
  this repo's compose-based tooling as-is.
- **An OTel Collector** between the agents and Phoenix/Prometheus, once
  there's a second consumer of the same telemetry (e.g. a managed APM
  backend alongside Phoenix) — batches/retries/fans out centrally instead
  of every service managing its own exporter retry logic
  (`PHASE_5_REPORT.md`).
- **Finer-grained `GitCommandError` classification.** Some git failures
  are permanent, not transient, but are currently classified
  `RetryableError` uniformly — bounded correctly by the retry ladder
  (exhausts after 3 attempts, lands in `q.dlq`), just less efficiently
  than a finer classifier could (`PHASE_4_REPORT.md`).
- **Incremental/differential AST analysis.** The researcher re-walks
  every `*.py` file in the checkout on every commit rather than only
  changed files — fine at the repo sizes exercised so far, would need
  per-file hashing/caching for a very large monorepo (`PHASE_2_REPORT.md`).

## Explicitly out of scope for this repo (by design, not oversight)

- **`agents/orchestrator`** remains a placeholder across every phase —
  cross-cutting coordination (multi-researcher dispatch, topology
  ownership) was deliberately deferred rather than built speculatively.
- **A real coverage-aware test-proximity check** in
  `agents/reviewer/scoring.py` — the reviewer has no repository checkout
  by design (only the researcher does), so "does this change have test
  coverage" stays a path-naming heuristic, not a real coverage signal
  (`PHASE_3_REPORT.md`).
- **True GitHub compare URLs on the wire.** `CommitDetected` never gained
  a `compare_url` field (a deliberate Phase 1 decision to avoid an
  unnecessary contract change); Slack messages link to a best-effort
  `.../commit/<sha>` URL instead.
