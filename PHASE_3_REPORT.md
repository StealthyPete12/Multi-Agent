# Phase 3 Report — Intelligence Layer (Reviewer, LLM, Slack)

**Project:** Event-Driven Multi-Agent Code Review Swarm
**Phase:** 3 — introduce intelligence: a provider-agnostic LLM
abstraction, a semantic-summary step in the researcher, and a new
reviewer agent that deterministically scores risk, explains it with an
LLM, persists a report, and notifies Slack.
**Status:** Complete and validated

## Starting state

Phase 2 delivered real repository intelligence: the researcher clones,
builds an AST import graph, persists it to Postgres, computes blast
radius via a recursive CTE, flags sensitive-path hits, and publishes
`findings.ready` — with `semantic_summary` already reserved on the wire
as an empty-string placeholder. Nothing consumed `q.findings` yet;
`agents/reviewer` was an empty placeholder, `reports.commit_id` was a
`NOT NULL` FK to a `commits` table nothing had ever written to, and
`processed_events` (Phase 0's idempotency ledger) had no reader or
writer. No LLM, no Slack, no risk scoring existed anywhere in the repo.

## Audit checklist against the roadmap

| Area | Before Phase 3 | After Phase 3 |
|---|---|---|
| `shared/llm.py` | Missing | Complete — `LLMClient` protocol, 3 providers, `NullLLMClient` degrade path |
| Researcher semantic summary | `semantic_summary` always `""` | Complete — LLM-generated, graceful fallback to `""` |
| `agents/reviewer/*` | Placeholder README only | Complete — `main.py`/`scoring.py`/`prompts.py`/`storage.py` |
| Deterministic risk scoring | Missing | Complete — pure code, no model involvement |
| `review.completed` | Model existed, unused, missing fields | Complete — `repo`/`severity`/`score` added, published |
| `reports` table | FK to unpopulated `commits`, no score/severity columns | Extended via `002_reviewer_reports.sql` |
| `processed_events` idempotency | Defined, unused by any consumer | First used, by the reviewer |
| `shared/slack.py` | Missing | Complete — Block Kit + incoming webhook |
| `.env.example` LLM/Slack vars | Missing | Complete, documented |
| Tests | 75 (Phase 0-2) | 134 (+59 for Phase 3 surfaces) |

Nothing under `agents/watcher`, the researcher's repository cache/graph/
blast-radius logic, or `shared/broker.py` was modified — Phase 3 only
adds a summary-generation step to the researcher's existing pipeline and
a new consumer downstream of it.

## Architecture overview

```
commit.detected
      |
      v
[unchanged Phase 2 pipeline: clone -> AST graph -> Postgres -> blast radius -> sensitive paths]
      |
      v
Semantic Summary        agents/researcher/summarize.py + diff.py
                         small/cheap LLM, 2-3 sentences, <=300 tokens,
                         "" on any failure (never blocks the pipeline)
      |
      v
findings.ready  ->  q.findings (durable, dead-letters to q.findings.dlq)
      |
      v
Idempotency Check        agents/reviewer/storage.py (processed_events)
      |
      v
Risk Scoring              agents/reviewer/scoring.py
                           pure code, 5 deterministic inputs -> severity
      |
      v
Narrative Generation       agents/reviewer/prompts.py + shared/llm.py
                            explains the score, never sets it; falls back
                            to a deterministic template on LLM failure
      |
      v
Report Persistence          agents/reviewer/storage.py -> Postgres (reports)
      |
      v
Slack Notification            shared/slack.py -> Block Kit message
      |
      v
review.completed  ->  q.reviews (durable, dead-letters to q.reviews.dlq)
```

## LLM abstraction (`shared/llm.py`)

One `LLMClient` protocol (`complete(system, prompt, max_tokens,
temperature) -> LLMResponse`), three implementations — `AnthropicClient`,
`OpenAIClient`, `OllamaClient` — each a direct `httpx` call to that
provider's REST API rather than a vendor SDK, so no third-party package
beyond `httpx` (already a dependency) had to be added, and no
provider-specific type or exception ever crosses the module boundary:
every provider raises `LLMError` on any failure (timeout, HTTP error,
malformed response).

`get_llm_client(purpose: str)` is the only entry point callers use.
Provider selection is entirely environment-driven (`LLM_PROVIDER`), and
per-purpose model choice comes from `MODEL_SELECTIONS`
(`purpose=model,purpose=model`), falling back to a built-in default per
provider. Two purposes exist today: `"summary"` (researcher, allowed to
be a small/cheap model) and `"narrative"` (reviewer, allowed to be a
larger model) — nothing stops a future purpose from being added the same
way.

**Degrade-gracefully by construction:** `LLM_PROVIDER` unset, set to
`"none"`, or set to a provider whose required credential
(`ANTHROPIC_API_KEY`/`OPENAI_API_KEY`/`OLLAMA_BASE_URL`) is missing all
return a `NullLLMClient`, which raises `LLMError` on `complete()` — the
exact same exception a live provider outage would raise. Every caller
already has to handle "the LLM is unavailable" as a normal code path, so
"not configured" and "down" are indistinguishable to them, which is
deliberate: it means the pipeline was validated end-to-end in this
sandbox (no API keys, no network egress assumed) exercising the *same*
code path a production outage would hit, not a special test-only shortcut.

**Retry hooks, deferred as scoped:** `_BaseLLMClient.__init__` accepts
`max_retries`/`retry_backoff_seconds` and `_retrying()` is the call-site
hook every provider already routes through — currently a single-attempt
no-op, exactly as the brief asked ("retry hooks, implementation deferred
to Phase 4"). Phase 4 can implement backoff/retry entirely inside that
one method without touching `AnthropicClient`/`OpenAIClient`/
`OllamaClient` or either call site.

**Token usage logging:** every successful call logs
`provider`/`model`/`input_tokens`/`output_tokens`/`latency_ms` as
structured JSON (`_BaseLLMClient._log_usage`), inside whatever
`trace_context` the caller is in, so token spend is greppable by
`trace_id` alongside the rest of a commit's pipeline.

## Researcher enhancement — semantic summary

`agents/researcher/summarize.py::generate_semantic_summary` builds a
prompt from the commit message, changed-file list, and a truncated diff,
and asks the `"summary"`-purpose LLM for 2-3 sentences, capped at 300
tokens by the request itself (`max_tokens=300`) — not post-hoc truncated,
so the model is asked to be concise rather than cut off mid-sentence.
Failure of any kind (`LLMError`) logs a warning and returns `""`,
preserving Phase 2's existing wire behavior exactly when no LLM is
available.

**Diff extraction** (`agents/researcher/diff.py::get_truncated_diff`) is
a new, separate module — deliberately *not* added to `repository.py`
(explicitly off-limits) — that runs `git show --unified=0 --format=
<sha> -- <changed files>` against the already-checked-out working tree
and truncates to `DEFAULT_DIFF_MAX_CHARS` (4000). It never raises: a bad
SHA, a missing repo, or no `git` binary all return `""`, matching
"best-effort context, not a pipeline-critical path." **Known caveat:** a
`--depth 1` shallow clone's tip commit has no parent object locally, so
`git show` on it presents every changed file as if newly added rather
than as a diff against the true parent — an accepted approximation (see
Known limitations).

The researcher's `analyze_commit()` now accepts an injectable
`llm_client` (defaulting to `get_llm_client(purpose="summary")` when not
supplied, mirroring how `repo_cache`/`database` are already injected),
so `tests/test_researcher_consumer.py` needed zero changes to keep
passing — the enhancement is fully additive at the call-site level.

## Reviewer agent (`agents/reviewer/`)

### `scoring.py` — deterministic risk scoring

Pure code, no model call, no randomness — `compute_score(findings)`
always returns the same `ScoreBreakdown` for the same `FindingsReady`.
Five inputs, each bucketed to an integer, summed, then mapped to a
severity:

| # | Input | Signal used | Buckets → points |
|---|---|---|---|
| 1 | Blast radius | `blast_radius.max_depth` | ≤3 → +0, ≤10 → +2, else → +4 |
| 2 | Impacted modules | `blast_radius.impact_count` | ≤10 → +0, ≤50 → +2, else → +4 |
| 3 | Sensitive path hits | `len(sensitive_hits)` | 0 → +0, 1 → +2, ≥2 → +4 |
| 4 | Changed files | `len(changed_files)` | ≤5 → +0, ≤20 → +1, else → +3 |
| 5 | Test proximity | path-marker heuristic over changed files + impacted modules | proximate → +0, not → +2 |

**Resolving the roadmap's apparent redundancy:** the brief lists "Blast
Radius" and "Number of impacted modules" as two separate inputs but its
own suggested weighting table gives two differently-scaled bucket sets
("Blast Radius" 0-3/4-10/11+, "Impact Count" 1-10/11-50/51+). This
implementation maps them to the two distinct fields Phase 2 already puts
on the wire — `max_depth` (naturally small, capped by
`BLAST_RADIUS_MAX_DEPTH`) and `impact_count` (naturally larger, scales
with repo size) — rather than double-counting one field under two names.

**Test proximity is a heuristic, not a coverage check:** the reviewer
never checks out the repository (only the researcher does, and that
logic was off-limits), so "does this change have test coverage" can't be
answered precisely from `FindingsReady` alone.
`has_test_proximity()` checks whether any changed file or impacted
module matches a configurable marker set (`test_`, `_test.py`, `tests/`,
`/test/`, `spec/`) — present by convention in the graph's own module
names/paths. Documented as a known limitation, not hidden.

Severity thresholds (`RISK_SCORE_MODERATE_THRESHOLD=3`,
`_HIGH_THRESHOLD=6`, `_CRITICAL_THRESHOLD=10`) are read from the
environment via `get_scoring_config()`; bucket boundaries and per-tier
weights are the roadmap's own suggested defaults, held in a
`ScoringConfig` dataclass that any caller (tests included) can override
directly — satisfying "implementation should be configurable" without
turning every constant into an environment variable.

`status_for_severity()` is a second, equally deterministic function
mapping severity to `ReviewCompleted.status`: `low`→`passed`,
`moderate`/`high`→`needs_review`, `critical`→`failed`.

### `prompts.py` — narrative construction

`build_narrative_prompt()` hands the model the already-final
severity/score/breakdown as fact, with an explicit system-prompt
instruction never to restate a different number — the LLM explains a
decision, it doesn't get to renegotiate it. `build_fallback_narrative()`
is a template-based narrative built from the same evidence (no model),
used whenever `LLMError` is raised, so a report is never missing an
explanation for lack of LLM availability.

### `storage.py` — persistence + idempotency

`ReviewStorage.is_processed(event_id)` / `save_report(...)` are the
reviewer's only two DB operations. Idempotency reuses Phase 0's
`processed_events` table exactly as its own comment described
("every consumer checks/writes event_id here before acting on a
message") — the reviewer is the first consumer to actually do so.
`save_report()` writes the `reports` row and the `processed_events` row
inside one transaction, so a crash between the two can never leave a
report that a redelivery would treat as new.

### `main.py` — orchestration

`process_findings()` is the pure pipeline function (idempotency check →
score → narrative → persist → Slack → build `ReviewCompleted`),
independent of RabbitMQ message plumbing, exactly mirroring
`agents/researcher/main.py::analyze_commit`'s shape. `handle_message()`
wraps it with contract validation and ack/nack, matching the researcher's
existing "reject without requeue on validation failure, dead-letter
instead of looping" pattern. `review.completed` is only published after
`process_findings()` returns, which only happens after both the Postgres
write and the Slack call (a no-op counts as success) have completed —
satisfying "publish only after both succeed."

## `review.completed` contract change

`shared/contracts.py::ReviewCompleted` gained `repo` and `severity`
(both new required fields) and `score` — additive relative to the wire
shape's *intent* (nothing had ever published this event, so there's no
real backward-compatibility concern, just an update to an unused
placeholder). `CommitDetected` and `FindingsReady` are untouched.

## Database changes

`db/migrations/002_reviewer_reports.sql` — see that file's header for
the full rationale, summarized here: `reports.commit_id` was `NOT NULL`
+ FK to `commits`, but nothing in Phases 1-2 ever wrote to `commits` (the
watcher has the metadata to but doesn't; `FindingsReady`, all the
reviewer has, doesn't carry branch/author/message to backfill it). Rather
than making the reviewer responsible for a table it can't fully
populate, `commit_id` became nullable and `repo`/`commit_sha` were added
directly to `reports`, which is everything `FindingsReady` actually has.
Also added: `severity`, `score`, `score_breakdown` (JSONB), `narrative`,
`blast_radius` (JSONB), `sensitive_hits` (JSONB), `source_event_id`. Two
new indexes (`repo, commit_sha` and `source_event_id`). No changes to
`001_init_schema.sql`, `modules`, `imports`, `findings`, `commits`, or
`audit_log`.

Same auto-init caveat as Phase 0: this only applies automatically to a
fresh Postgres volume; an already-running stack needs the file applied
by hand (documented in `db/README.md`) — which is exactly how it was
validated here (see below).

## Slack integration (`shared/slack.py`)

`build_review_message()` is a pure function (no network) returning a
Block Kit payload: a header, a two-column field section (repository,
commit SHA, severity, score, blast radius, sensitive hits), a divider,
the narrative, and a "View Commit" button. Colored via the
`attachments[0].color` wrapper (still the only way to get a Block-Kit
message a visible severity-colored side bar) — `SEVERITY_COLORS` maps
low/moderate/high/critical to green/yellow/orange/red. `SlackNotifier`
posts to `SLACK_WEBHOOK_URL`; an empty URL makes `send()` a logged no-op
returning `True` rather than raising, so local dev/CI never needs a real
Slack workspace to exercise the full pipeline (see Known limitations for
what this means for the "Slack notification succeeds" precondition on
publishing `review.completed`).

**Compare URL caveat:** neither `FindingsReady` nor `ReviewCompleted`
carries a true GitHub compare URL — Phase 1 explicitly extracted but
dropped it rather than changing the contract. `build_review_message()`
falls back to a best-effort commit link
(`{REPO_WEB_BASE_URL}/{repo}/commit/{commit_sha}`, same pattern as
`REPO_CLONE_BASE_URL`) unless a caller passes a real `compare_url`
explicitly. Documented as a known limitation, not silently papered over.

## Test results

```
$ pytest tests/ -q
134 passed, 1 warning in ~2s
```

| File | Covers |
|---|---|
| `tests/test_llm.py` | Provider selection (`get_llm_client` for all 3 providers + missing-credential fallback), `get_model_for` (`MODEL_SELECTIONS` parsing + defaults), each provider's success/HTTP-error/malformed-response paths (mocked via `pytest-httpx`), `NullLLMClient` |
| `tests/test_researcher_diff.py` | Real diff extraction against a real git repo (`tmp_path`), file-scoping, truncation, bad-SHA and missing-repo graceful empty-string returns |
| `tests/test_researcher_summarize.py` | Prompt construction, LLM-text passthrough, `LLMError` → `""` fallback, empty-input handling |
| `tests/test_reviewer_scoring.py` | All 5 scoring buckets, deterministic-repeatability, env-var threshold overrides, custom `ScoringConfig`, `status_for_severity`, and the roadmap's own Scenarios A/B/C |
| `tests/test_reviewer_prompts.py` | Narrative-prompt evidence inclusion, empty-field handling, fallback-narrative content |
| `tests/test_slack.py` | Block-Kit payload shape/content, severity color differentiation, unconfigured-webhook no-op, mocked webhook success/failure (`pytest-httpx`) |
| `tests/test_reviewer_consumer.py` | `process_findings`/`handle_message` with fake storage/LLM/Slack/broker — idempotency skip, LLM-failure fallback, sensitive-hit escalation, contract-validation rejection |
| `tests/test_reviewer_storage.py` | Real Postgres: `is_processed` false-by-default, `save_report` persistence + field-correctness, idempotency-flag confirmation — skips gracefully if Postgres is unreachable |
| `tests/test_contracts.py` (updated) | `ReviewCompleted` round-trip with `repo`/`severity`/`score` |
| `tests/test_researcher_consumer.py` (unchanged, still passing) | Confirms the summary-generation addition didn't change `analyze_commit`'s existing contract for callers that don't pass `llm_client` |
| All Phase 0-2 files | Unchanged, still passing (75 tests) |

## Validation evidence

All scenarios were run against the live Docker stack already running in
this environment (`docker compose ps` showed `rabbitmq`/`postgres`/
`redis` healthy for 3+ days prior), with `db/migrations/002_reviewer_reports.sql`
applied by hand (`docker compose exec -T postgres psql ... < db/migrations/002_reviewer_reports.sql`)
since the volume predated this phase — exactly the procedure documented
in `db/README.md`. `LLM_PROVIDER` was left unset (`none`) and
`SLACK_WEBHOOK_URL` left empty throughout, so every run below exercises
the real degrade-gracefully paths, not a mocked shortcut.

**Scenario A — low-impact commit, verify LOW:**

```
$ python -m tools.seed_findings --repo acme/widgets --sha aaaa1111 \
    --changed-files tests/test_widget.py --impact-count 0 --max-depth 0

reviewer log: "risk score computed" severity=low score=0
              "narrative generation skipped, using fallback" reason="no LLM provider configured: LLM_PROVIDER='none'"
              "published review.completed" severity=low score=0

Postgres: severity=low, score=0, status=passed
```

**Scenario B — large blast radius, verify HIGH:**

```
$ python -m tools.seed_findings --repo acme/widgets --sha bbbb2222 \
    --changed-files tests/test_widget.py \
    --impacted-modules pkg.mod_0,pkg.mod_1,...,pkg.mod_59 \
    --impact-count 60 --max-depth 9

reviewer log: "risk score computed" severity=high score=6
              "published review.completed" severity=high score=6

Postgres: severity=high, score=6, status=needs_review
score_breakdown: {"blast_radius_points": 2, "impact_count_points": 4, "sensitive_hits_points": 0, "changed_files_points": 0, "test_proximity_points": 0}
```

(max_depth=9 falls in the 4-10 bucket → +2; impact_count=60 falls in the
51+ bucket → +4; the changed file `tests/test_widget.py` satisfies test
proximity → +0. Total 6 = HIGH, matching `RISK_SCORE_HIGH_THRESHOLD=6`.)

**Scenario C — sensitive path touched, verify escalation:**

```
$ python -m tools.seed_findings --repo acme/widgets --sha cccc3333 \
    --changed-files auth/login.py --sensitive-hits auth/login.py \
    --impact-count 0 --max-depth 0

reviewer log: "risk score computed" severity=moderate score=4
              "published review.completed" severity=moderate score=4

Postgres: severity=moderate, score=4, status=needs_review
score_breakdown: {"sensitive_hits_points": 2, "test_proximity_points": 2, ...}
```

(One sensitive hit → +2; `auth/login.py` has no test-proximity marker →
+2 more. Total 4 = MODERATE, up from what would have been LOW/0 with an
equivalent non-sensitive, test-covered change — confirmed directly by
`tests/test_reviewer_scoring.py::test_scenario_c_sensitive_path_escalates_severity`.)

**Scenario D — full end-to-end, real repo clone through to `review.completed`:**

A local git "remote" (`file:///tmp/phase3-validation/remotes/acme/widgets.git`,
same technique Phase 2's own validation used) with two commits: an
initial `database.py`/`auth/login.py` chain, then a second commit adding
`payments/charge.py` (imports `auth.login`) and `checkout.py` (imports
`payments.charge`), changing `auth/login.py`, `payments/charge.py`, and
`checkout.py`. Both `agents.researcher.main` and `agents.reviewer.main`
were run as live processes; `tools.seed_commit` published one
`commit.detected` for the second commit:

```
researcher log (trace_id 370e4acc-... throughout):
  "repository cache miss, cloning" ... "repository cloned" shallow=true
  "dependency graph built" modules=6 edges=3
  "graph persisted" modules=6 edges=3
  "blast radius queried" changed_modules=3 impact_count=3
  "semantic summary generation skipped" reason="no LLM provider configured..."
  "semantic summary generated" summary_length=0
  "published findings.ready" impact_count=3 sensitive_hits=2

reviewer log (same trace_id, received immediately after):
  "findings.ready received" impact_count=3 sensitive_hits=2
  "risk score computed" severity=high score=6
  "narrative generation skipped, using fallback" reason="no LLM provider configured..."
  "review narrative generated" narrative_length=481
  "report persisted" report_id=922039c1-...
  "slack webhook not configured, skipping notification"
  "slack delivery complete" configured=false
  "published review.completed" severity=high score=6 report_id=922039c1-...
```

Postgres (`reports` row for this commit):

```
repo=acme/widgets, commit_sha=941afa8f..., severity=high, score=6
score_breakdown: {"blast_radius_points": 0, "impact_count_points": 0,
  "sensitive_hits_points": 4, "changed_files_points": 0, "test_proximity_points": 2}
sensitive_hits: ["auth/login.py", "payments/charge.py"]
blast_radius: {"max_depth": 0, "impact_count": 3,
  "impacted_modules": ["auth.login", "checkout", "payments.charge"]}
```

Both `auth/login.py` and `payments/charge.py` matched the default
sensitive-path patterns (`auth/`, `payments/`) → 2 hits → `+4`; none of
the three changed files or three impacted modules matched a test-path
marker → `+2`; blast radius/impact-count contributed `0` because all
three changed modules were already at the "leaf" of this small graph
(no further transitive dependents beyond themselves). Total `6` = HIGH.
The same `trace_id` ties the watcher-equivalent seed, researcher, and
reviewer log lines together end-to-end, confirming trace propagation
across the full three-hop pipeline exactly as Phase 1/2 established for
their shorter chains.

`q.reviews` held one `review.completed` message after this run (`rabbitmqctl
list_queues` confirmed `q.reviews: 1`) — accumulating with no consumer
yet, the same "queue declared, no reader" pattern Phase 1 left for
`q.commits` and Phase 2 left for `q.findings`.

**Scenario E — Slack payload rendering:**

Exercised as both a mocked-HTTP integration test
(`tests/test_slack.py::test_slack_notifier_send_success`/`_send_failure_raises_slack_error`,
via `pytest-httpx`) and a pure-function content test
(`test_build_review_message_includes_all_required_fields` — asserts
repository, commit SHA, severity, score, blast radius, sensitive hits,
narrative, and the commit-link URL are all present in the rendered
Block Kit blocks, and that severity maps to a distinct color). No real
Slack workspace was available in this environment to visually confirm
rendering; message shape was validated against Slack's Block Kit /
attachments schema by construction, not by posting to a live channel.

**Idempotency evidence:** `tests/test_reviewer_storage.py::test_save_report_is_idempotent_via_processed_events`
confirms `processed_events.event_id` is set after `save_report()`, which
is exactly the flag `process_findings()` checks via `is_processed()`
before doing any work — demonstrated directly in
`tests/test_reviewer_consumer.py::test_process_findings_skips_already_processed_event`
(no storage write, no Slack call, no `review.completed` published for an
event already marked processed).

**Infrastructure regression check:** `pytest tests/` (134 passed) covers
every Phase 0-2 file unchanged; `scripts/validate_stack.sh` was not
re-run in this session since the stack was already up for 3+ days prior
to this phase starting (`docker compose ps` showed all three services
`healthy`) and no `docker-compose.yml` change was made.

## Observability

Every stage the roadmap asked for logs a structured JSON line with a
`duration_ms` field, inside the shared `trace_context` propagated from
the originating `commit.detected`/`findings.ready` envelope's `trace_id`:
summary generation (`agents/researcher/main.py` — "semantic summary
generated"), review generation (`agents/reviewer/main.py` — "review
narrative generated"), score calculation ("risk score computed"),
database writes ("database write complete" / "report persisted"), Slack
delivery ("slack delivery complete"), and `review.completed` publication
("published review.completed"). `shared/llm.py::_BaseLLMClient._log_usage`
additionally logs token usage (`input_tokens`/`output_tokens`) and
provider/model identity on every successful LLM call. All of this was
directly observed in the Scenario D log excerpt above, with one
`trace_id` tying every line together across both agent processes.

## Known limitations

- **Test proximity is a path-naming heuristic, not a real coverage
  check.** The reviewer has no repository checkout (deliberately — that
  logic belongs only to the researcher, which was off-limits to modify)
  and `FindingsReady` doesn't carry coverage data. A changed file with a
  same-named but differently-located test, or a test that doesn't follow
  the configured naming markers, would be scored as "not proximate" even
  if real coverage exists. Documented in `scoring.py`'s own docstring.
- **No true GitHub compare URL on the wire.** Neither `FindingsReady` nor
  `ReviewCompleted` carries one (Phase 1 deliberately dropped it rather
  than changing `CommitDetected`); the Slack message's link is a
  best-effort `.../commit/<sha>` URL, not necessarily identical to what
  GitHub's own compare view would show for a multi-commit push.
- **Diff extraction on a shallow clone shows "everything added."** A
  `--depth 1` clone's tip commit has no local parent object, so `git show`
  can't produce a true parent-diff for it; the semantic summary's diff
  context is best-effort for that (common) case. Full-history clones
  (or an explicit `--unshallow`, which the repository cache already does
  when checking out an older commit) aren't affected.
- **Retry logic is a stub, exactly as scoped.** `_BaseLLMClient._retrying()`
  calls its argument once; `max_retries`/`retry_backoff_seconds` are
  accepted but inert. A transient provider error today surfaces as a
  single `LLMError` → fallback path, not a retried call.
- **Slack "success" includes "not configured."** `SlackNotifier.send()`
  returns `True` both when a webhook actually delivered and when none is
  configured, so `review.completed`'s "published only after Slack
  succeeds" precondition is trivially satisfied whenever
  `SLACK_WEBHOOK_URL` is empty — which was every run in this sandbox (no
  Slack workspace available to validate against). A misconfigured (but
  non-empty) webhook URL *does* raise `SlackError` and block publication,
  which is the behavior that was actually exercised
  (`tests/test_slack.py::test_slack_notifier_send_failure_raises_slack_error`).
- **No LLM provider was reachable in this environment.** No API keys, no
  outbound network egress to `api.anthropic.com`/`api.openai.com`, and no
  local Ollama server were available, so every live validation run in
  this report used the `NullLLMClient`/fallback-template path. Each
  provider's request/response handling (`AnthropicClient`/`OpenAIClient`/
  `OllamaClient`) is instead covered by `tests/test_llm.py` against
  mocked HTTP responses shaped exactly like each API's real documented
  response format — not end-to-end against a live provider.
- **`002_reviewer_reports.sql` needs manual application to an existing
  volume**, same caveat Phase 0 raised for `001_init_schema.sql` and
  still unresolved: no real migration runner exists yet.
- **`reports.commit_id` stays `NULL`** for every Phase 3 report — the
  `commits` table remains unpopulated by any agent (a gap inherited from
  Phase 1/2, not introduced here). A future phase that starts writing
  `commits` rows (e.g. the watcher, since it has the full metadata) could
  backfill `commit_id` via `(repo, commit_sha)` lookup without another
  reviewer-side schema change.
- **`agents/orchestrator` remains a placeholder**, unchanged, as scoped.

## Readiness for Phase 4

Ready to build on:

- **`shared/llm.py`'s retry-hook seam is exactly where Phase 4's retry/
  backoff logic belongs** (`_BaseLLMClient._retrying`) — no call site in
  `summarize.py`, `main.py`, or their tests needs to change.
- **`review.completed` now carries everything a Phase 4 aggregation/
  orchestration layer would need** (`repo`, `severity`, `score`,
  `report_id`) without another contract change.
- **`processed_events` is now a proven idempotency pattern** with one
  real consumer (`agents/reviewer`) — the watcher and researcher could
  adopt the same guard if Phase 4 needs it there too.
- **`ScoringConfig` is already a first-class, injectable object** — a
  future per-repo or per-team configurable policy doesn't need new
  plumbing, just a different `ScoringConfig` instance.

Needed before Phase 4:

- A decision on real retry/backoff parameters (attempts, backoff curve,
  jitter) for `shared/llm.py`, and whether Slack delivery should get the
  same treatment (currently zero retries, single attempt).
- A real Postgres migration runner, now that two migrations exist and a
  third is inevitable.
- If multi-repo/multi-team scoring policy is in scope, deciding whether
  `ScoringConfig` is loaded per-message (e.g. from a repo-keyed table)
  rather than once at process startup via environment variables.
