"""Researcher agent: Phase 2 repository-analysis engine.

Consumes `commit.detected` events from ``q.commits`` and, for each one:

1. Clones/refreshes a local checkout of the repository (``repository.py``).
2. Builds a Python import dependency graph via AST analysis (``graph.py``).
3. Persists the graph to Postgres (``modules``/``imports`` tables, via
   ``db.py``).
4. Computes the blast radius of the commit's changed files with a
   recursive CTE over the stored graph (``db.py``).
5. Detects sensitive-path hits among the changed files (``sensitive.py``).
6. Publishes a `findings.ready` event carrying the above — no findings,
   no risk scoring, no LLM summary (``semantic_summary`` is always ``""``
   in this phase).

Run with::

    python -m agents.researcher.main
"""

from __future__ import annotations

import asyncio
import os
import signal
import time
from datetime import datetime, timezone

from pydantic import ValidationError

from agents.researcher.db import Database
from agents.researcher.graph import build_dependency_graph
from agents.researcher.impact import DEFAULT_MAX_DEPTH, changed_files_to_modules
from agents.researcher.repository import RepositoryCache
from agents.researcher.sensitive import detect_sensitive_hits
from shared.broker import Broker
from shared.contracts import (
    BlastRadius,
    CommitDetected,
    Envelope,
    EventType,
    FindingsReady,
    make_envelope,
)
from shared.logging import configure_logging, trace_context

QUEUE_COMMITS = os.environ.get("QUEUE_COMMITS", "q.commits")
QUEUE_FINDINGS = os.environ.get("QUEUE_FINDINGS", "q.findings")
BLAST_RADIUS_MAX_DEPTH = int(os.environ.get("BLAST_RADIUS_MAX_DEPTH", DEFAULT_MAX_DEPTH))

log = configure_logging(service_name="researcher")


async def analyze_commit(
    payload: CommitDetected,
    *,
    repo_cache: RepositoryCache,
    database: Database,
) -> FindingsReady:
    """Run the full clone -> AST -> graph -> store -> blast-radius pipeline
    for one commit and build the `findings.ready` payload."""
    started_at = datetime.now(timezone.utc)

    t0 = time.monotonic()
    repo_path = await repo_cache.ensure(payload.repo, payload.commit_sha)
    log.info(
        "repository ready",
        extra={
            "repo": payload.repo,
            "commit_sha": payload.commit_sha,
            "duration_ms": (time.monotonic() - t0) * 1000,
        },
    )

    t0 = time.monotonic()
    graph = build_dependency_graph(repo_path)
    log.info(
        "dependency graph built",
        extra={
            "repo": payload.repo,
            "modules": len(graph.modules),
            "edges": len(graph.edges),
            "duration_ms": (time.monotonic() - t0) * 1000,
        },
    )

    await database.store_graph(payload.repo, graph)

    changed_modules = changed_files_to_modules(graph, payload.changed_files)
    radius = await database.blast_radius(
        payload.repo, changed_modules, max_depth=BLAST_RADIUS_MAX_DEPTH
    )

    sensitive_hits = detect_sensitive_hits(payload.changed_files)

    completed_at = datetime.now(timezone.utc)
    return FindingsReady(
        commit_sha=payload.commit_sha,
        agent_name="researcher",
        findings=[],
        started_at=started_at,
        completed_at=completed_at,
        repo=payload.repo,
        changed_files=payload.changed_files,
        blast_radius=BlastRadius(
            impacted_modules=radius.impacted_modules,
            impact_count=radius.impact_count,
            max_depth=radius.max_depth_reached,
        ),
        sensitive_hits=sensitive_hits,
        semantic_summary="",
    )


async def handle_message(
    message,
    *,
    broker: Broker,
    repo_cache: RepositoryCache,
    database: Database,
) -> None:
    """Validate, analyze, publish `findings.ready`, then ack/nack one
    `commit.detected` message.

    Rejects (without requeue) messages that fail contract validation so
    they dead-letter instead of blocking the queue on redelivery.
    """
    async with message.process(ignore_processed=True, requeue=False):
        try:
            envelope = Envelope[CommitDetected].from_json(message.body)
        except ValidationError:
            log.exception(
                "rejected message: contract validation failed",
                extra={"raw_body": message.body.decode("utf-8", errors="replace")},
            )
            raise

        with trace_context(trace_id=envelope.trace_id, correlation_id=envelope.event_id):
            log.info(
                "commit.detected received",
                extra={
                    "event_id": envelope.event_id,
                    "repo": envelope.payload.repo,
                    "commit_sha": envelope.payload.commit_sha,
                    "branch": envelope.payload.branch,
                    "changed_files": envelope.payload.changed_files,
                },
            )

            findings_payload = await analyze_commit(
                envelope.payload, repo_cache=repo_cache, database=database
            )
            findings_envelope = make_envelope(
                findings_payload,
                event_type=EventType.FINDINGS_READY,
                source="researcher",
                trace_id=envelope.trace_id,
                correlation_id=envelope.event_id,
            )
            await broker.publish(findings_envelope, routing_key=EventType.FINDINGS_READY.value)
            log.info(
                "published findings.ready",
                extra={
                    "event_id": findings_envelope.event_id,
                    "commit_sha": findings_payload.commit_sha,
                    "impact_count": findings_payload.blast_radius.impact_count,
                    "sensitive_hits": len(findings_payload.sensitive_hits),
                },
            )


async def run() -> None:
    broker = Broker()
    await broker.connect()
    queue = await broker.declare_queue(
        QUEUE_COMMITS, routing_keys=[EventType.COMMIT_DETECTED.value]
    )
    # Declared here so the queue/DLQ exist even before a reviewer consumer
    # binds to it, matching how the watcher pre-declares q.commits.
    await broker.declare_queue(QUEUE_FINDINGS, routing_keys=[EventType.FINDINGS_READY.value])

    repo_cache = RepositoryCache()
    database = Database()
    await database.connect()

    log.info("researcher started", extra={"queue": QUEUE_COMMITS, "publishes_to": QUEUE_FINDINGS})

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    async with queue.iterator() as queue_iter:
        consume_task = asyncio.create_task(
            _consume(queue_iter, broker=broker, repo_cache=repo_cache, database=database)
        )
        await stop_event.wait()
        consume_task.cancel()

    await database.close()
    await broker.close()
    log.info("researcher stopped")


async def _consume(
    queue_iter, *, broker: Broker, repo_cache: RepositoryCache, database: Database
) -> None:
    async for message in queue_iter:
        await handle_message(message, broker=broker, repo_cache=repo_cache, database=database)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
