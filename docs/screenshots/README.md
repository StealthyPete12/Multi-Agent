# Screenshots

Real captures from a live local stack in this session (headless Chrome via
Puppeteer, not mockups) — not committed as fabricated evidence: each one
corresponds to a specific pipeline run described in
[`docs/performance.md`](../performance.md) and the demo scenario in
[`docs/demo/`](../demo/).

| File | Shows |
|---|---|
| `rabbitmq_queues.png` | The real queue topology (`q.commits`, `q.findings`, `q.reviews`, the 3-rung retry ladder, `q.dlq`) with live message counts after a mixed batch of synthetic commits/findings |
| `grafana_system_overview.png` | The "System Overview" dashboard's event-throughput, retry-rate, and publish/consume-rate panels, populated by a real 100-commit load test run |
| `grafana_repository.png` | The "Repository" dashboard's blast-radius query duration, cache hit rate, and Postgres write/connection-pool panels |
| `phoenix_trace_waterfall.png` | A real distributed trace spanning `researcher.handle_commit_detected` -> repository/graph/blast-radius spans -> `rabbitmq.publish findings.ready` -> `reviewer.handle_findings_ready` -> narrative/DB/Slack spans -> `rabbitmq.publish review.completed` — one commit's full path through all three services |

Panels showing "No data" (the LLM dashboard, circuit-breaker/DLQ panels)
were left out — they're genuinely empty in this environment because
`LLM_PROVIDER=none` (no live LLM provider available in this sandbox, see
`docs/performance.md`'s "Known bottlenecks") and no message has ever
exhausted the retry ladder in this run, not because the feature doesn't
work (`tests/test_chaos.py` exercises both against real infrastructure).
