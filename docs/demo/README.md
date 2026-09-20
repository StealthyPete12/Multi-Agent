# Demo payloads

One coherent scenario, generated directly from the real Pydantic contract
models and `shared/slack.py::build_review_message` (not hand-typed), then
round-tripped back through `Envelope[...].from_json()` to confirm they
validate. Regenerate with:

```bash
python -m tools.generate_demo_assets
```

**Scenario:** a commit to `acme/widgets` changes `auth/login.py` and
`auth/session.py` (tightening a session-expiry check). Both files match
the default sensitive-path pattern (`auth/`), and the dependency graph
shows the change reaches `payments.charge` and `checkout` transitively —
the same worked example used in `PHASE_2_REPORT.md` through
`PHASE_4_REPORT.md`'s validation sections, so it's consistent with the
rest of the repo's documentation.

| File | What it is | Produced by |
|---|---|---|
| [`sample_commit.json`](sample_commit.json) | An `Envelope[CommitDetected]` — what the watcher publishes to `q.commits` | `agents/watcher` |
| [`sample_findings.json`](sample_findings.json) | An `Envelope[FindingsReady]` — repository analysis + blast radius + sensitive-path hits, published to `q.findings` | `agents/researcher` |
| [`sample_review.json`](sample_review.json) | The `Envelope[ReviewCompleted]` published to `q.reviews`, plus the `reports` row actually persisted to Postgres (`db/migrations/002_reviewer_reports.sql`'s columns) | `agents/reviewer` |
| [`sample_slack_message.json`](sample_slack_message.json) | The Slack Block Kit payload `shared/slack.py::SlackNotifier` would POST to `SLACK_WEBHOOK_URL` for this report | `agents/reviewer` via `shared/slack.py` |

All four share one `trace_id` (`8b2a38d0-19bf-4b71-a3fe-e68eafe880b3`) and
a correlation chain (`sample_commit`'s `event_id` is `sample_findings`'s
`correlation_id`, and so on) — exactly how a real run threads one commit's
identity through all three services; see
[`docs/architecture.md`](../architecture.md) for the full data-flow
diagram.
