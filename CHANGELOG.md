# Changelog

Format loosely follows [Keep a Changelog](https://keepachangelog.com/);
versions correspond to this repo's phase-based development history (see
each `PHASE_*_REPORT.md` for full detail — this file is the condensed
index).

## [0.6.0] - 2026-09-20 — Phase 6: Production Readiness & Portfolio Polish

Packaging, deployment, documentation, and release preparation — no
business logic changed. See `PHASE_6_REPORT.md`.

### Added
- Multi-stage, non-root Dockerfiles for watcher/researcher/reviewer
  (`agents/*/Dockerfile`) — the first Dockerfiles in the repo.
- `docker-compose.dev.yml` (fully containerized dev stack, bridge
  networking) and `docker-compose.prod.yml` (resource limits, required
  secrets, configurable bind-mounted persistence).
- CI: `.github/workflows/{tests,docker,eval-pr}.yml` — lint + real-service
  integration tests, multi-image Docker builds + compose validation, and
  an end-to-end synthetic-commit pipeline smoke test.
- Code quality tooling: ruff, black, mypy, pre-commit, all configured in
  `pyproject.toml`/`.pre-commit-config.yaml` and passing clean.
- `docs/architecture.md` (5 Mermaid diagrams), `docs/runbooks/{deploy,
  recovery,operations}.md`, `docs/performance.md`, `docs/demo/*.json`
  (generated from the real contract models via
  `tools/generate_demo_assets.py`).
- `LICENSE` (MIT), `CONTRIBUTING.md`, `SECURITY.md`, `ROADMAP.md`, this
  `CHANGELOG.md`.
- Portfolio-oriented `README.md` rewrite.

### Fixed
- 7 genuine mypy findings across `shared/contracts.py`, `shared/logging.py`,
  `shared/telemetry.py`, `shared/llm.py`, and `tools/replay_dlq.py`
  (type-correctness only, no behavior change — see the Phase 6 code
  quality commit for detail).

## [0.5.0] - 2026-09-18 — Phase 5: Observability

Distributed tracing (OpenTelemetry -> Arize Phoenix), a Prometheus metrics
catalog, three Grafana dashboards, LLM cost/latency tracking, and a
100-commit load test. See `PHASE_5_REPORT.md`.

### Added
- `shared/telemetry.py` — tracing/metrics init, W3C traceparent
  propagation over AMQP, cost estimation.
- Phoenix, Prometheus, Grafana, `postgres_exporter` docker services.
- `tools/load_test.py`.
- 28 new tests (234 total).

## [0.4.0] - 2026-09-18 — Phase 4: Fault Tolerance

Retry ladder, error classification, claim-based idempotency, rate
limiting, circuit breaking, graceful shutdown, DLQ tooling. See
`PHASE_4_REPORT.md`.

### Added
- `shared/{retry,errors,idempotency,ratelimit,breaker}.py`.
- `tools/replay_dlq.py`.
- `tests/test_chaos.py` (6 scenarios).
- 72 new tests (206 total).

### Changed
- Both consumers moved from ack-on-success/nack-on-any-exception to
  manual ack with explicit per-failure-category routing, `prefetch=1`.

## [0.3.0] - 2026-09-18 — Phase 3: Intelligence Layer

Provider-agnostic LLM abstraction, LLM-generated semantic summaries and
review narratives, deterministic risk scoring, Slack notifications. See
`PHASE_3_REPORT.md`.

### Added
- `shared/llm.py` (Anthropic/OpenAI/Ollama, `NullLLMClient` degrade path).
- `agents/reviewer/*` (scoring, prompts, storage, main).
- `shared/slack.py`.
- 59 new tests (134 total).

### Changed
- `ReviewCompleted` gained `repo`/`severity`/`score`.
- `reports` table extended (`db/migrations/002_reviewer_reports.sql`).

## [0.2.0] - 2026-09-18 — Phase 2: Repository Analysis Engine

Real repository intelligence: cloning, AST import graphs, blast-radius
analysis, sensitive-path detection. See `PHASE_2_REPORT.md`.

### Added
- `agents/researcher/{repository,graph,impact,db,sensitive}.py`.
- Postgres recursive-CTE blast radius.

### Changed
- `FindingsReady` gained `repo`/`changed_files`/`blast_radius`/
  `sensitive_hits`/`semantic_summary`; added `BlastRadius` model.

## [0.1.0] - 2026-09-15 — Phase 0 + Phase 1: Foundation & Walking Skeleton

Initial infrastructure and the first proven event path. See
`PHASE_0_REPORT.md` / `PHASE_1_REPORT.md`.

### Added
- Docker Compose (RabbitMQ, Postgres, Redis), `shared/contracts.py`,
  `shared/logging.py`, DB schema, `.env.example`.
- `shared/broker.py`, `agents/watcher` (GitHub webhook -> `commit.detected`),
  `agents/researcher` (pipeline-verification consumer).
- `tools/seed_commit.py`, `scripts/validate_stack.sh`.
