# Orchestrator (placeholder)

**Status:** not implemented — out of scope for Phase 1.

Kept under its original Phase 0 name: it's a cross-cutting coordination
concern (queue/exchange topology ownership, per-commit review state,
retries/timeouts across every stage), not a fourth pipeline stage
alongside watcher → researcher → reviewer, so it doesn't fit that
3-stage rename.

Phase 1 declares its own topology directly in
[`shared/broker.py`](../../shared/broker.py) as a stopgap. Once multiple
researcher agents and retry/timeout logic exist, that responsibility
should move here.

Will own queue/exchange topology, dispatch work across analysis agents,
track per-commit review state, and handle retries/timeouts for agents
that never publish `FindingsReady`.
