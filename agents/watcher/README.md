# Watcher agent

**Status:** implemented (Phase 1 — walking skeleton).

Formerly named `agents/ingestion` (Phase 0 placeholder); renamed to
`watcher` in Phase 1 to match the architecture roadmap.

Receives GitHub webhook deliveries over HTTP, verifies their
`X-Hub-Signature-256` HMAC, filters to `push` events on configured
branches, extracts commit metadata, and publishes `commit.detected`
events (see [`shared/contracts.py`](../../shared/contracts.py)) onto
RabbitMQ via [`shared/broker.py`](../../shared/broker.py) for downstream
agents to consume.

See [`main.py`](main.py) and the root [`README.md`](../../README.md) for
setup, webhook configuration, and local testing (including Smee.io for
tunneling GitHub webhooks to localhost).
