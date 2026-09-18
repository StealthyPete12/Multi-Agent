# Reviewer agent

**Status:** implemented (Phase 3) — deterministic risk scoring + LLM
narrative + persistence + Slack notification.

Consumes `findings.ready` from `q.findings` and, per commit:

1. **Idempotency check** (`storage.py::ReviewStorage.is_processed`) — a
   redelivered `event_id` is a no-op, not a duplicate report. Uses the
   `processed_events` table Phase 0 defined for exactly this purpose.
2. **Deterministic risk scoring** (`scoring.py::compute_score`) — pure
   code, no model in the loop. See that module's docstring for the full
   methodology (blast radius depth, impacted-module count, sensitive-path
   hits, changed-file count, test proximity).
3. **LLM narrative** (`prompts.py` + `shared/llm.py`) — explains a score
   that's already final; falls back to a deterministic template
   (`prompts.py::build_fallback_narrative`) if no provider is configured
   or the call fails.
4. **Persistence** (`storage.py::ReviewStorage.save_report`) — writes to
   the `reports` table (extended by `db/migrations/002_reviewer_reports.sql`)
   and marks the event processed, in one transaction.
5. **Slack notification** (`shared/slack.py`) — Block Kit message with a
   severity-colored side bar; a no-op (not a failure) if
   `SLACK_WEBHOOK_URL` isn't configured.
6. **Publishes `review.completed`** to `q.reviews`, only after storage and
   Slack both succeed.

Run with::

    python -m agents.reviewer.main

See [`PHASE_3_REPORT.md`](../../PHASE_3_REPORT.md) for architecture,
validation evidence, and known limitations.
