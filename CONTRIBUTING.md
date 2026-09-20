# Contributing

Thanks for considering a contribution to the Event-Driven Multi-Agent
Code Review Swarm. This is a portfolio/reference project, but it's built
and tested like a production system — the bar for a PR is the same one
every phase of this repo's own history was held to (see the
`PHASE_*_REPORT.md` files for what "done" has meant here).

## Getting set up

```bash
git clone <repo-url> && cd Multi-Agent
cp .env.example .env
pip install -e '.[dev]'
pre-commit install          # runs ruff/black/mypy + hygiene checks on every commit
docker compose up -d        # or docker-compose.dev.yml — see docs/runbooks/deploy.md
./scripts/validate_stack.sh
pytest tests/
```

## Before opening a PR

1. **Tests pass.** `pytest tests/` — the DB/RabbitMQ/Redis-backed tests
   skip gracefully if a service isn't reachable locally, but CI
   (`.github/workflows/tests.yml`) runs them against real services, so a
   skip locally isn't a pass in CI.
2. **Lint/format/type-check clean.** `pre-commit run --all-files`
   (ruff, black, mypy — see `pyproject.toml` for exact config). CI fails
   the PR otherwise.
3. **New behavior has tests.** This repo's own history (see any
   `PHASE_*_REPORT.md`) adds tests in the same change that adds the
   behavior, not after. Match the existing pattern in `tests/` for the
   area you're touching — most modules pair 1:1 with a `test_<module>.py`.
4. **Docs stay in sync.** If you change the wire contract
   (`shared/contracts.py`), the retry/DLQ behavior, or add a new
   environment variable, update `README.md` / `docs/architecture.md` /
   `.env.example` in the same PR.
5. **Docker/CI still validate**, if you touched a Dockerfile, compose
   file, or workflow: `docker compose -f docker-compose.dev.yml config -q`
   and a local build (`docker build -f agents/<service>/Dockerfile .`)
   before pushing.

## Scope conventions

- `shared/` must never import from `agents/` (enforced by convention, not
  a lint rule yet) — it's the layer every agent depends on, not the
  reverse.
- Contract changes (`shared/contracts.py`) are additive by default — see
  any phase report's "contract change" section for the pattern (new
  optional/required fields on a payload, never renaming or repurposing an
  existing one on the wire).
- A new failure mode gets classified in `shared/errors.py` (if
  provider-agnostic) or an agent's own `classify_*_failure` (if specific
  to that agent) — see `PHASE_4_REPORT.md`'s "Error classification" for
  the three-way Retryable/Poison/Fatal split this depends on.

## Commit style

Small, focused commits with a message explaining *why*, not just *what*
(the diff already shows what). This repo's own git history is the best
example to follow.

## Reporting a bug or proposing a feature

Open an issue describing the observed vs. expected behavior (for a bug)
or the use case it'd unlock (for a feature). For anything touching
security, see [`SECURITY.md`](SECURITY.md) instead of a public issue.
