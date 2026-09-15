# Researcher agent

**Status:** pipeline-verification consumer only (Phase 1 — walking
skeleton). No AI, no database writes.

Formerly named `agents/analysis` (Phase 0 placeholder); renamed to
`researcher` in Phase 1 to match the architecture roadmap.

For Phase 1, this agent only proves the pipeline works end-to-end: it
consumes `commit.detected` events from `q.commits`, validates the
contract, logs the payload, and acknowledges the message. Future phases
will replace this body with real analysis/research logic (static
analysis, security, dependency checks, ...) that publishes
`FindingsReady` events.

See [`main.py`](main.py).
