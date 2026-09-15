# Ingestion agent (placeholder)

**Status:** not implemented — scaffolded in Phase 0 for repo structure only.

Will watch configured repositories, detect new commits, and publish
`CommitDetected` events (see [`shared/contracts.py`](../../shared/contracts.py))
onto RabbitMQ for analysis agents to consume.
