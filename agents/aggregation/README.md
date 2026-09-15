# Aggregation agent (placeholder)

**Status:** not implemented — scaffolded in Phase 0 for repo structure only.

Will collect `FindingsReady` events for a commit from every expected
analysis agent, persist findings, generate a final report, and publish
`ReviewCompleted` (see [`shared/contracts.py`](../../shared/contracts.py)).
