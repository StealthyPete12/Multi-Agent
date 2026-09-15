# Reviewer agent (placeholder)

**Status:** not implemented — out of scope for Phase 1.

Formerly named `agents/aggregation` (Phase 0 placeholder); renamed to
`reviewer` in Phase 1 to match the architecture roadmap.

Will collect `FindingsReady` events for a commit from every expected
researcher/analysis agent, persist findings, generate a final report,
and publish `ReviewCompleted` (see
[`shared/contracts.py`](../../shared/contracts.py)).
