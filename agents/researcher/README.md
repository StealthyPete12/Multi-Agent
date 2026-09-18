# Researcher agent

**Status:** repository-analysis engine (Phase 2). No AI, no risk scoring,
no Slack — pure repository intelligence.

Formerly a pipeline-verification-only consumer (Phase 1). Phase 2 replaces
the log-only body with a real pipeline:

```
commit.detected
      |
      v
Repository Cache   (repository.py)  -- clone/reuse a local git checkout
      |
      v
AST Analysis       (graph.py)       -- ast.Import / ast.ImportFrom -> DependencyGraph
      |
      v
Dependency Graph    stored in Postgres (db.py: modules, imports)
      |
      v
Blast Radius        (db.py, recursive CTE over modules/imports)
      |
      v
Sensitive Paths     (sensitive.py)  -- auth/, payments/, infra/, migrations/
      |
      v
findings.ready
```

## Modules

- [`repository.py`](repository.py) — `RepositoryCache`: clones a repo
  shallowly (`--depth 1`) into `REPO_CACHE_DIR`, reuses the clone on
  subsequent commits (`git fetch` + `git checkout <sha>`), and falls back
  to an unshallow fetch when the requested commit isn't in the shallow
  history. A repo is refreshed automatically once its cache entry is
  older than `REPO_STALE_SECONDS`.
- [`graph.py`](graph.py) — `build_dependency_graph(repo_root)`: walks
  every `*.py` file, parses it with the stdlib `ast` module, and resolves
  `import`/`from`/relative imports into a `DependencyGraph` of internal
  edges (imports resolved to another file in the repo) plus external ones
  (stdlib/third-party, recorded but excluded from traversal). Handles
  `__init__.py` re-exports (`from pkg import thing` where `thing` is
  defined in `pkg/__init__.py`, not a submodule) and multi-level relative
  imports (`from .. import x`). Pure/DB-free, so it's reusable by a future
  reviewer agent.
- [`impact.py`](impact.py) — `compute_blast_radius(graph, changed_modules,
  max_depth=...)`: in-memory, cycle-safe BFS over the graph's reverse
  edges. Used directly by tests; the live pipeline uses the Postgres
  recursive-CTE equivalent in `db.py` (see below) since blast radius is
  computed *after* the graph has been persisted.
- [`sensitive.py`](sensitive.py) — flags changed files under configurable
  path patterns (`SENSITIVE_PATH_PATTERNS`, default
  `auth/,payments/,infra/,migrations/`).
- [`db.py`](db.py) — `Database`: upserts `modules`/`imports` rows
  (idempotent: `ON CONFLICT (repo, path)` for modules, delete+reinsert per
  module for imports, both inside one transaction per commit) and runs
  the blast-radius query as a Postgres `WITH RECURSIVE` CTE that walks
  `imports.imported_name -> modules.name` joins, bounded by
  `BLAST_RADIUS_MAX_DEPTH`.
- [`main.py`](main.py) — the consumer: `commit.detected` in, `analyze_commit()`
  runs the pipeline above, `findings.ready` out. Same ack/nack/dead-letter
  pattern as Phase 1 — a contract-validation failure nacks without
  requeue.

## `findings.ready` payload (Phase 2)

```json
{
  "commit_sha": "...",
  "agent_name": "researcher",
  "findings": [],
  "repo": "acme/widgets",
  "changed_files": ["database.py", "auth/login.py"],
  "blast_radius": {
    "impacted_modules": ["auth.login", "checkout", "database", "payments.charge"],
    "impact_count": 4,
    "max_depth": 2
  },
  "sensitive_hits": ["auth/login.py"],
  "semantic_summary": ""
}
```

`findings` is always `[]` and `semantic_summary` is always `""` in this
phase — no AI, no reviewer-style scoring. See
[`../../PHASE_2_REPORT.md`](../../PHASE_2_REPORT.md) for the full design
writeup, validation evidence, and known limitations.
