# Analysis agents (placeholder)

**Status:** not implemented — scaffolded in Phase 0 for repo structure only.

Will consume `CommitDetected` events, run one or more review strategies
(static analysis, security, style, dependency checks, ...) against the
commit, and publish `FindingsReady` events (see
[`shared/contracts.py`](../../shared/contracts.py)) with the results.
