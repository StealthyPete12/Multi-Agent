# Phase 2 Report — Repository Analysis Engine

**Project:** Event-Driven Multi-Agent Code Review Swarm
**Phase:** 2 — replace the pipeline-verification researcher with real
repository intelligence (clone, AST dependency graph, blast radius,
sensitive-path detection)
**Status:** Complete and validated

## Starting state

Phase 1 delivered a walking skeleton: `agents/watcher` (GitHub webhook →
`commit.detected`) and `agents/researcher` (a consumer that validated the
contract, logged it, and acked — no analysis, no database writes). Phase
0's Postgres schema already defined `modules`/`imports` tables that
nothing wrote to yet. This phase's brief was explicit about scope: real
repository analysis only — no Slack, no reviewer/aggregation logic, no
risk scoring, no LLM usage.

## Architecture overview

```
commit.detected
      |
      v
Repository Cache   agents/researcher/repository.py
                    clone (shallow) or reuse + refresh a local git checkout
      |
      v
AST Analysis        agents/researcher/graph.py
                     ast.Import / ast.ImportFrom -> DependencyGraph
      |
      v
Postgres Storage     agents/researcher/db.py
                      idempotent upserts into modules / imports
      |
      v
Blast Radius          agents/researcher/db.py
                       recursive CTE over modules/imports
      |
      v
Sensitive Paths        agents/researcher/sensitive.py
      |
      v
findings.ready  ->  q.findings (durable, dead-letters to q.findings.dlq)
```

**Deliberate ordering decision:** the roadmap's two descriptions of this
pipeline disagreed slightly — one ASCII diagram put "Blast Radius
Analysis" before "PostgreSQL Storage"; the itemized "RESEARCHER CONSUMER"
section put "store graph" before "calculate blast radius". This
implementation follows the latter (store, then query), because it's what
lets blast radius actually use a **Postgres recursive CTE** as the
roadmap explicitly asks for ("where possible") — you can't run a SQL
query against a graph that isn't in SQL yet. An in-memory,
DB-independent equivalent (`impact.py::compute_blast_radius`) also
exists and is covered by its own unit tests, both because it's directly
testable without Postgres and because a future reviewer agent that only
has the graph object (not a live DB connection) can reuse it.

## Repository caching approach

`agents/researcher/repository.py::RepositoryCache`:

- One local clone per repo slug, keyed deterministically
  (`acme/widgets` → `<REPO_CACHE_DIR>/acme__widgets`).
- **Clone:** `git clone --depth 1 <url> <path>` — shallow where the
  transport honors it (real remote URLs; note that git silently ignores
  `--depth` for a bare local filesystem path, which is why the test
  suite and the local-testing README section both use `file://` URLs to
  exercise real shallow-clone behavior).
- **Reuse:** a second `ensure()` call for an already-cloned repo skips
  cloning entirely (`is_cached()`).
- **Staleness:** a `.swarm-last-fetch` marker file's mtime is compared
  against `REPO_STALE_SECONDS` (default 300s); a stale cache triggers
  `git fetch origin` before checkout.
- **Checkout:** `git checkout --detach <sha>`. If the commit isn't in the
  shallow history (`CommitNotFoundError`), the cache automatically falls
  back to `git fetch --unshallow` and retries once — this is what makes
  an older commit reachable without permanently giving up shallow clones
  for the common case (latest commit on the default branch).
- All git invocations are async subprocesses (`asyncio.create_subprocess_exec`),
  so the consumer's event loop isn't blocked during a clone/fetch.
- Structured logging at every stage: cache hit/miss, clone/fetch/checkout
  duration in ms, staleness/unshallow decisions — all inside the
  message's `trace_context`, so one `trace_id` ties repository operations
  to the rest of that commit's pipeline in the logs.

## Dependency graph design

`agents/researcher/graph.py::build_dependency_graph`:

- Walks every `*.py` file under the checkout (skipping `.git`,
  `__pycache__`, `.venv`/`venv`, `node_modules`, etc.), converting each
  path to a dotted module name (`pkg/sub/mod.py` → `pkg.sub.mod`;
  `pkg/sub/__init__.py` → `pkg.sub`, since `__init__.py` *is* the
  package for import-resolution purposes).
- Every file is parsed once with `ast.parse`; `ast.Import` and
  `ast.ImportFrom` nodes are walked for edges:
  - **Absolute** (`import a.b.c`): resolved only on an exact module-name
    match against the repo's own modules; otherwise external.
  - **`from` (level 0)** (`from pkg import thing`): tries `pkg.thing` as
    a submodule first, then falls back to `pkg` itself — this is what
    correctly resolves a symbol re-exported from `pkg/__init__.py`
    rather than a real submodule (`test_init_py_reexport_resolves_to_package`).
  - **Relative** (`from . import x`, `from .. import y`): the "current
    package" is computed from the importing file's own location
    (itself, if it's an `__init__.py`; its parent directory otherwise),
    then walked up `level - 1` more times — standard Python
    relative-import semantics, reimplemented without needing the
    interpreter's own import machinery.
- A file that fails to parse (`SyntaxError`) is still registered as a
  module (it exists on disk) but contributes no edges — one broken file
  doesn't abort analysis of the rest of the repo.
- Cycles are just adjacency-set membership — the graph doesn't need
  special cycle handling to *build*; only *traversal* (blast radius)
  needs a `visited` guard, since a naive walk over a cyclic graph would
  never terminate.
- The graph is a plain dataclass with no Postgres/broker dependency, so
  it's directly reusable by a future reviewer agent operating purely on
  in-memory analysis.

## Blast radius methodology

Two equivalent implementations, covering the roadmap's dual requirement
("recursive traversal... where possible use recursive CTE queries"):

**In-memory** (`impact.py::compute_blast_radius`) — breadth-first over
`DependencyGraph.dependents_of()`, frontier-by-frontier, bounded by
`max_depth`; a `visited: dict[module, depth]` both prevents infinite
loops on cycles and records the depth each module was first reached at.

**Postgres recursive CTE** (`db.py::Database.blast_radius`) — the one the
live pipeline actually uses, since it queries the graph *after* it's
been persisted:

```sql
WITH RECURSIVE dependents(name, depth) AS (
    SELECT u, 0 FROM unnest($2::text[]) AS u
    UNION ALL
    SELECT m2.name, d.depth + 1
    FROM dependents d
    JOIN imports i ON i.imported_name = d.name
    JOIN modules m2 ON m2.id = i.module_id AND m2.repo = $1
    WHERE d.depth < $3
)
SELECT name, MIN(depth) AS depth FROM dependents GROUP BY name ORDER BY name
```

`$1` = repo, `$2` = changed module names (array), `$3` = max depth. The
`WHERE d.depth < $3` bound is also what guarantees termination on a
cyclic import graph — recursion can revisit a node, but depth strictly
increases each step and is capped.

**Worked example**, both matching the roadmap's own:

```
database.py changed
auth.py imports database.py
payments.py imports auth.py
checkout.py imports payments.py

Blast radius: {database, auth, payments, checkout}
Impact count: 4
```

Verified two ways: `tests/test_researcher_impact.py::test_transitive_dependents_full_chain`
(in-memory) and `tests/test_researcher_db.py::test_blast_radius_recursive_cte_matches_chain_example`
(live Postgres, same fixture graph), plus end-to-end in the **Validation
evidence** section below using real cloned files instead of a synthetic
graph.

## Sensitive path detection

`agents/researcher/sensitive.py`: a changed file is a hit if its
(forward-slash-normalized) path contains any configured pattern.
Defaults: `auth/`, `payments/`, `infra/`, `migrations/` — overridable via
`SENSITIVE_PATH_PATTERNS` (comma-separated) without a code change.

## `findings.ready` contract change

`shared/contracts.py::FindingsReady` gained fields the roadmap requires
on the wire that didn't exist after Phase 1 (`repo`, `changed_files`,
`blast_radius`, `sensitive_hits`, `semantic_summary`) plus a new
`BlastRadius` model (`impacted_modules`, `impact_count`, `max_depth`).
This is the one change to `shared/contracts.py` in this phase — additive
only, no existing field renamed or removed, `CommitDetected`/`Envelope`
untouched. `agents/watcher`, `shared/broker.py`, and `docker-compose.yml`
were not modified at all.

## Database changes

None to the schema. `db/migrations/001_init_schema.sql`'s existing
`modules(repo, path)` unique constraint is exactly what
`ON CONFLICT (repo, path) DO UPDATE` needs for idempotent module upserts.
`imports` has no natural unique key suited to `ON CONFLICT`, so import
idempotency instead comes from delete-then-reinsert of a module's full
import set inside the same transaction as its module upsert
(`Database.store_graph`) — re-running analysis for an unchanged commit
converges to the same rows rather than accumulating duplicates.

## Test results

```
$ pytest tests/ -v
75 passed, 1 warning in 1.46s
```

| File | Covers |
|---|---|
| `tests/test_contracts.py` | (updated) `FindingsReady`/`BlastRadius` round-trip with the new fields |
| `tests/test_researcher_graph.py` | absolute/from/relative import resolution, `__init__.py` re-exports, package-level imports, cycles, ignored dirs, syntax-error tolerance, path→module mapping |
| `tests/test_researcher_impact.py` | direct/transitive dependents, depth limits (0, partial, full), cycle termination, multi-changed-file union, the roadmap's chain example |
| `tests/test_researcher_repository.py` | clone, cache-hit reuse (no re-clone), stale-cache refresh, unshallow fallback for an older commit, picking up a new upstream commit — all against real local git repos, no network |
| `tests/test_researcher_db.py` | upsert idempotency (rerun same graph → same IDs, same row counts), stale-import removal, module upsert conflict update, **transaction rollback** (a forced FK violation inside a transaction leaves zero rows), recursive-CTE blast radius (chain example + depth limit + empty input) — against live Postgres, skips gracefully if unreachable |
| `tests/test_researcher_sensitive.py` | default patterns, no-hit case, custom patterns, env-var override, path normalization |
| `tests/test_researcher_consumer.py` | (rewritten) `analyze_commit`/`handle_message` with fake repository/database/broker — findings.ready publication, sensitive-path detection, contract-validation rejection |
| All Phase 0/1 files | unchanged, still passing (32 tests) |

Everything above except the DB-dependent suite runs with zero external
services; the DB and RabbitMQ suites skip (not fail) when their service
isn't reachable, matching the pattern `tests/conftest.py` already
established for `rabbitmq_available` — extended here with a matching
`postgres_available` fixture.

## Validation evidence

All four scenarios were run against the live Docker stack (RabbitMQ +
Postgres, `docker compose up -d`) using a local git "remote" instead of
GitHub (`REPO_CLONE_BASE_URL=file:///tmp/validation/remotes/`), so the
run below has zero external network dependency and is fully reproducible
offline. Repo layout: `database.py` ← `auth/login.py` ← `payments/charge.py`
← `checkout.py`, matching the roadmap's worked example.

**Scenario A/B — highly-imported module + deep chain, `commit.detected` → `findings.ready`:**

```
$ python -m tools.seed_commit --repo acme/widgets --sha 2702121... \
    --changed-files database.py,auth/login.py

researcher log (trace_id ties every line together):
  "commit.detected received" repo=acme/widgets commit_sha=2702121...
  "repository cache miss, cloning" url=file:///tmp/validation/remotes/acme/widgets.git
  "repository cloned" duration_ms=30.5 shallow=true
  "repository checked out" duration_ms=5.6
  "dependency graph built" modules=6 edges=3 duration_ms=2.8
  "graph persisted" modules=6 edges=3 duration_ms=96.9
  "blast radius queried" changed_modules=2 max_depth=10 impact_count=4 duration_ms=2.1
  "published findings.ready" impact_count=4 sensitive_hits=1

findings.ready payload (consumed from q.findings):
  "repo": "acme/widgets",
  "changed_files": ["database.py", "auth/login.py"],
  "blast_radius": {
    "impacted_modules": ["auth.login", "checkout", "database", "payments.charge"],
    "impact_count": 4,
    "max_depth": 2
  },
  "sensitive_hits": ["auth/login.py"],
  "semantic_summary": ""
```

Postgres rows after this run (`modules`/`imports` for `repo='acme/widgets'`):

```
path                    name
auth/__init__.py        auth
auth/login.py           auth.login
checkout.py             checkout
database.py             database
payments/charge.py      payments.charge
payments/__init__.py    payments

importer          imported_name       import_type
auth.login        database            import
checkout          payments.charge     from
payments.charge   auth.login          from
```

Impact count (4) and the chain (`database → auth.login → payments.charge
→ checkout`) match the roadmap's worked example exactly, scaled to real
package names.

**Scenario C — sensitive path change:** `auth/login.py` in the changed
set above produced `"sensitive_hits": ["auth/login.py"]` on the wire (the
default `auth/` pattern), confirming end-to-end sensitive-path detection
alongside a real blast-radius computation in the same event.

**Scenario D — repository cache hit:** re-running the researcher against
the same commit logged `"repository cache hit"` (no `"repository
cloned"` line) and re-storing the same graph left row counts unchanged
(6 modules, 3 imports) — confirming both clone reuse and upsert
idempotency in the same pass:

```
$ python -m tools.seed_commit --repo acme/widgets --sha 2702121... \
    --changed-files checkout.py
researcher log: "repository cache hit" (no clone)
                "blast radius queried" changed_modules=1 impact_count=1
Postgres: modules=6, imports=3   (unchanged from the prior run)
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

No changes were made to `docker-compose.yml`, `db/migrations/`,
`shared/broker.py`, or `agents/watcher` — all Phase 0/1 infrastructure
and the watcher are untouched and still validate cleanly.

## Known limitations

- **Clone URL construction is `org/repo` → `<base>/org/repo.git` only.**
  No support yet for self-hosted GitHub Enterprise/GitLab path shapes
  beyond overriding `REPO_CLONE_BASE_URL` wholesale, and no
  authentication (private repos need credentials baked into the URL or a
  credential helper configured on the host — out of scope for this
  phase).
- **No incremental/differential graph analysis.** Every commit triggers a
  full re-walk of every `*.py` file in the checkout, even if only one
  file changed. Fine at the repo sizes tested; would need per-file
  hashing/caching to scale to very large monorepos.
- **Import resolution is best-effort, not a full import-system
  reimplementation.** Dynamic imports (`importlib.import_module(...)`),
  `sys.path` manipulation, namespace packages spanning multiple
  directories, and `__all__`-based re-export filtering aren't modeled —
  matching what static analysis can reasonably do without executing the
  code.
- **Blast radius depth vs. fan-out.** The recursive CTE bounds *depth*,
  not the number of rows visited per level, so a very high fan-out node
  in a large graph could still produce a large intermediate result
  before the `GROUP BY` dedups it. Not observed as a problem at any scale
  tested here.
- **`imports` idempotency via delete+reinsert, not a DB constraint.**
  Simpler and avoids modifying `db/migrations/001_init_schema.sql`, but
  means idempotency is a property of `store_graph`'s transaction, not
  something the schema itself enforces — a caller that inserted directly
  (bypassing `db.py`) could still create duplicates.
- **No re-processing of `findings.ready`.** Nothing consumes `q.findings`
  yet (by design — that's `agents/reviewer`'s job); this phase only
  proves it's published correctly.
- **Findings are always `[]`.** Phase 2 explicitly excludes
  finding-generation logic (linting, security rules, etc.) — only
  structural repository intelligence.

## Readiness for Phase 3

Ready to build on:

- **`DependencyGraph` is a clean, DB-free reusable object** — a reviewer
  agent that wants graph-shaped context (not just the flattened
  `blast_radius` on the wire) can call `build_dependency_graph` directly,
  or query the same `modules`/`imports` tables the researcher already
  populates.
- **`findings.ready` now carries real repository intelligence** —
  `blast_radius` and `sensitive_hits` are exactly the signals a Phase 3
  risk-scoring/LLM-summary agent would want as input alongside whatever
  static-analysis `findings` a future pass adds.
- **The recursive-CTE blast-radius query is reusable as-is** by
  `agents/reviewer` or `agents/orchestrator` for cross-commit or
  cross-agent impact queries, without re-deriving the graph in memory.

Needed before Phase 3:

- A decision on whether Phase 3 adds real static-analysis `Finding`
  generation to the researcher itself, or introduces a separate agent
  that also publishes into `q.findings`/consumes `commit.detected` — the
  contract already supports either (`FindingsReady.agent_name`
  distinguishes producers).
- `agents/reviewer` needs to actually consume `q.findings` — right now
  events accumulate there with no consumer, same "queue declared, no
  reader yet" situation Phase 1 left for `q.commits` before this phase.
- Semantic summary (LLM-based) is explicitly deferred; `semantic_summary`
  is already on the wire as an empty string placeholder so adding it
  later doesn't require another contract change.
