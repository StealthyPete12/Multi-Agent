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
import contextlib
import os
import signal
import time
from datetime import datetime, timezone

import asyncpg
from opentelemetry.trace import SpanKind
from pydantic import ValidationError

from agents.researcher.db import Database
from agents.researcher.diff import get_truncated_diff
from agents.researcher.graph import build_dependency_graph
from agents.researcher.impact import DEFAULT_MAX_DEPTH, changed_files_to_modules
from agents.researcher.repository import CommitNotFoundError, GitCommandError, RepositoryCache
from agents.researcher.sensitive import detect_sensitive_hits
from agents.researcher.summarize import generate_semantic_summary
from shared import telemetry
from shared.broker import Broker
from shared.contracts import (
    BlastRadius,
    CommitDetected,
    Envelope,
    EventType,
    FindingsReady,
    make_envelope,
)
from shared.errors import FatalError, PoisonMessageError, RetryableError, classify_exception
from shared.idempotency import IdempotencyStore
from shared.llm import LLMClient, LLMError, get_llm_client
from shared.logging import configure_logging, trace_context
from shared.retry import MAX_RETRY_ATTEMPTS, RetryLadder

QUEUE_COMMITS = os.environ.get("QUEUE_COMMITS", "q.commits")
QUEUE_FINDINGS = os.environ.get("QUEUE_FINDINGS", "q.findings")
BLAST_RADIUS_MAX_DEPTH = int(os.environ.get("BLAST_RADIUS_MAX_DEPTH", DEFAULT_MAX_DEPTH))
RESEARCHER_METRICS_PORT = int(os.environ.get("RESEARCHER_METRICS_PORT", "9102"))

log = configure_logging(service_name="researcher")


def classify_researcher_failure(exc: Exception) -> type[RetryableError | PoisonMessageError | FatalError]:
    """Map a failure from ``analyze_commit``'s pipeline (repo clone, git,
    Postgres, LLM) to one of the three Phase 4 error categories.

    Kept in the consumer, not ``shared/errors.py``, because it needs
    knowledge of researcher-specific exception types
    (``CommitNotFoundError``/``GitCommandError``) that ``shared/`` must
    not depend on (layering: shared/ is imported by agents/, never the
    reverse).
    """
    if isinstance(exc, CommitNotFoundError):
        # The commit genuinely doesn't exist in the repo even after an
        # unshallow fetch — no amount of retrying changes that.
        return PoisonMessageError
    if isinstance(exc, GitCommandError):
        # Clone/fetch failed — almost always a transient network/remote
        # issue (DNS, connection reset, remote temporarily unavailable).
        return RetryableError
    if isinstance(exc, LLMError):
        # analyze_commit() already degrades LLM failures to "" internally
        # (see summarize.py) and never lets LLMError escape — this branch
        # is defensive only.
        return RetryableError
    if isinstance(exc, (ConnectionError, TimeoutError, OSError, asyncpg.PostgresConnectionError)):
        return RetryableError
    return classify_exception(exc)


async def analyze_commit(
    payload: CommitDetected,
    *,
    repo_cache: RepositoryCache,
    database: Database,
    llm_client: LLMClient | None = None,
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
            "event_type": "repository.ensure",
        },
    )

    t0 = time.monotonic()
    with telemetry.span(
        "researcher.graph_build",
        kind=SpanKind.INTERNAL,
        tracer_name="agents.researcher",
        attributes={"swarm.repo": payload.repo},
    ) as graph_span:
        graph = build_dependency_graph(repo_path)
        duration_ms = (time.monotonic() - t0) * 1000
        graph_span.set_attribute("swarm.modules", len(graph.modules))
        graph_span.set_attribute("swarm.edges", len(graph.edges))
    log.info(
        "dependency graph built",
        extra={
            "repo": payload.repo,
            "modules": len(graph.modules),
            "edges": len(graph.edges),
            "duration_ms": duration_ms,
            "event_type": "researcher.graph_build",
        },
    )

    await database.store_graph(payload.repo, graph)

    changed_modules = changed_files_to_modules(graph, payload.changed_files)
    radius = await database.blast_radius(
        payload.repo, changed_modules, max_depth=BLAST_RADIUS_MAX_DEPTH
    )

    sensitive_hits = detect_sensitive_hits(payload.changed_files)

    t0 = time.monotonic()
    diff_excerpt = await get_truncated_diff(repo_path, payload.commit_sha, payload.changed_files)
    active_llm_client = llm_client if llm_client is not None else get_llm_client(purpose="summary")
    semantic_summary = await generate_semantic_summary(
        active_llm_client,
        commit_message=payload.message,
        changed_files=payload.changed_files,
        diff_excerpt=diff_excerpt,
    )
    log.info(
        "semantic summary generated",
        extra={
            "repo": payload.repo,
            "commit_sha": payload.commit_sha,
            "summary_length": len(semantic_summary),
            "duration_ms": (time.monotonic() - t0) * 1000,
            "event_type": "researcher.semantic_summary",
        },
    )

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
        semantic_summary=semantic_summary,
    )


async def handle_message(
    message,
    *,
    broker: Broker,
    repo_cache: RepositoryCache,
    database: Database,
    retry_ladder: RetryLadder,
    idempotency: IdempotencyStore,
    llm_client: LLMClient | None = None,
) -> None:
    """Validate, analyze, publish `findings.ready`, then ack/nack one
    `commit.detected` message — with manual acknowledgement throughout so
    every outcome (success, retryable failure, poison message) makes an
    explicit, logged routing decision instead of relying on an
    ack-on-success/nack-on-exception context manager.

    - Contract validation failure -> poison, straight to DLQ, no retries.
    - A classified :class:`RetryableError` -> routed to the retry ladder
      (or the DLQ once attempts are exhausted), idempotency claim
      released so a later attempt can reclaim it.
    - A classified :class:`PoisonMessageError` -> DLQ immediately.
    - A classified :class:`FatalError` -> logged, message nacked with
      requeue so it isn't lost, and re-raised so ``run()`` stops the
      service instead of burning through the rest of the queue the same
      broken way.
    """
    try:
        envelope = Envelope[CommitDetected].from_json(message.body)
    except ValidationError as exc:
        log.exception(
            "rejected message: contract validation failed",
            extra={"raw_body": message.body.decode("utf-8", errors="replace")},
        )
        await retry_ladder.send_raw_to_dlq(
            message.body,
            reason=f"contract validation failed: {exc}",
            original_queue=QUEUE_COMMITS,
        )
        await message.ack()
        return

    attempt = RetryLadder.attempt_from_headers(message.headers)

    with trace_context(trace_id=envelope.trace_id, correlation_id=envelope.event_id), telemetry.consumer_span(
        "researcher.handle_commit_detected",
        message.headers,
        tracer_name="agents.researcher",
        attributes={
            "messaging.system": "rabbitmq",
            "messaging.destination.name": QUEUE_COMMITS,
            "swarm.repo": envelope.payload.repo,
            "swarm.commit_sha": envelope.payload.commit_sha,
            "swarm.retry_count": attempt,
        },
    ):
        telemetry.get_metrics().events_processed.add(1, {"event_type": "commit.detected", "direction": "consumed"})
        telemetry.get_metrics().commit_events.add(1, {"repo": envelope.payload.repo, "direction": "consumed"})
        log.info(
            "commit.detected received",
            extra={
                "event_id": envelope.event_id,
                "repo": envelope.payload.repo,
                "commit_sha": envelope.payload.commit_sha,
                "branch": envelope.payload.branch,
                "changed_files": envelope.payload.changed_files,
                "retry_count": attempt,
                "event_type": "commit.detected",
            },
        )

        claimed = await idempotency.claim(
            event_id=envelope.event_id,
            event_type=envelope.event_type.value,
            trace_id=envelope.trace_id,
        )
        if not claimed:
            log.info(
                "duplicate delivery, skipping (already processed)",
                extra={"event_id": envelope.event_id},
            )
            await message.ack()
            return

        try:
            findings_payload = await analyze_commit(
                envelope.payload, repo_cache=repo_cache, database=database, llm_client=llm_client
            )
            findings_envelope = make_envelope(
                findings_payload,
                event_type=EventType.FINDINGS_READY,
                source="researcher",
                trace_id=envelope.trace_id,
                correlation_id=envelope.event_id,
            )
            await broker.publish(findings_envelope, routing_key=EventType.FINDINGS_READY.value)
            telemetry.get_metrics().findings_events.add(1, {"repo": findings_payload.repo, "direction": "published"})
            log.info(
                "published findings.ready",
                extra={
                    "event_id": findings_envelope.event_id,
                    "commit_sha": findings_payload.commit_sha,
                    "impact_count": findings_payload.blast_radius.impact_count,
                    "sensitive_hits": len(findings_payload.sensitive_hits),
                    "event_type": "findings.ready",
                },
            )
            await idempotency.mark_complete(envelope.event_id)
            await message.ack()
        except Exception as exc:
            category = classify_researcher_failure(exc)

            if category is PoisonMessageError:
                await idempotency.release(envelope.event_id)
                await retry_ladder.send_to_dlq(
                    envelope,
                    reason=str(exc),
                    attempt=attempt,
                    original_queue=QUEUE_COMMITS,
                )
                await message.ack()
                return

            if category is FatalError:
                log.critical(
                    "fatal error in researcher pipeline, stopping service",
                    extra={"reason": str(exc), "event_id": envelope.event_id},
                )
                await idempotency.release(envelope.event_id)
                await message.nack(requeue=True)
                raise FatalError(str(exc)) from exc

            # RetryableError (or anything unclassified defaulting to it
            # via the researcher's own classify_researcher_failure).
            await idempotency.release(envelope.event_id)
            next_attempt = attempt + 1
            if next_attempt > MAX_RETRY_ATTEMPTS:
                await retry_ladder.send_to_dlq(
                    envelope,
                    reason=str(exc),
                    attempt=next_attempt,
                    original_queue=QUEUE_COMMITS,
                )
            else:
                await retry_ladder.schedule_retry(
                    envelope,
                    routing_key=EventType.COMMIT_DETECTED.value,
                    reason=str(exc),
                    attempt=next_attempt,
                    original_queue=QUEUE_COMMITS,
                )
            await message.ack()


GRACEFUL_SHUTDOWN_SECONDS = float(os.environ.get("GRACEFUL_SHUTDOWN_SECONDS", "30"))


async def run() -> None:
    telemetry.init_telemetry("researcher", metrics_port=RESEARCHER_METRICS_PORT)
    # prefetch_count=1: at most one unacked commit.detected in flight at a
    # time, so manual ack/nack below always reflects exactly the message
    # currently being handled — no risk of acking/nacking the wrong one,
    # and no batch of already-delivered-but-unprocessed messages to lose
    # on a hard shutdown.
    broker = Broker(prefetch_count=1)
    await broker.connect()
    queue = await broker.declare_queue(
        QUEUE_COMMITS, routing_keys=[EventType.COMMIT_DETECTED.value]
    )
    # Declared here so the queue/DLQ exist even before a reviewer consumer
    # binds to it, matching how the watcher pre-declares q.commits.
    await broker.declare_queue(QUEUE_FINDINGS, routing_keys=[EventType.FINDINGS_READY.value])

    retry_ladder = RetryLadder(broker)
    await retry_ladder.declare_topology()

    repo_cache = RepositoryCache()
    database = Database()
    await database.connect()
    idempotency = IdempotencyStore(database.pool)
    llm_client = get_llm_client(purpose="summary")

    log.info(
        "researcher started",
        extra={
            "queue": QUEUE_COMMITS,
            "publishes_to": QUEUE_FINDINGS,
            "llm_provider": llm_client.provider,
            "prefetch_count": broker.prefetch_count,
        },
    )

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    fatal_error: BaseException | None = None
    async with queue.iterator() as queue_iter:
        consume_task = asyncio.create_task(
            _consume(
                queue_iter,
                broker=broker,
                repo_cache=repo_cache,
                database=database,
                retry_ladder=retry_ladder,
                idempotency=idempotency,
                llm_client=llm_client,
            )
        )
        stop_wait_task = asyncio.create_task(stop_event.wait())
        done, _pending = await asyncio.wait(
            {consume_task, stop_wait_task}, return_when=asyncio.FIRST_COMPLETED
        )

        if consume_task in done and consume_task.exception() is not None:
            fatal_error = consume_task.exception()
        else:
            # SIGTERM/SIGINT: stop accepting new work, then give any
            # message currently being handled a bounded window to finish
            # naturally (ack/nack + any in-flight publish) rather than
            # cutting it off mid-processing.
            log.info(
                "shutdown signal received, draining in-flight work",
                extra={"grace_period_seconds": GRACEFUL_SHUTDOWN_SECONDS},
            )
            try:
                await asyncio.wait_for(consume_task, timeout=GRACEFUL_SHUTDOWN_SECONDS)
            except asyncio.TimeoutError:
                log.warning("graceful shutdown window elapsed, cancelling consumer")
                consume_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await consume_task
            except Exception as exc:  # pragma: no cover - defensive
                fatal_error = exc
        stop_wait_task.cancel()

    await database.close()
    await broker.close()
    telemetry.shutdown_telemetry()
    log.info("researcher stopped")
    if fatal_error is not None:
        raise fatal_error


async def _consume(
    queue_iter,
    *,
    broker: Broker,
    repo_cache: RepositoryCache,
    database: Database,
    retry_ladder: RetryLadder,
    idempotency: IdempotencyStore,
    llm_client: LLMClient | None = None,
) -> None:
    async for message in queue_iter:
        await handle_message(
            message,
            broker=broker,
            repo_cache=repo_cache,
            database=database,
            retry_ladder=retry_ladder,
            idempotency=idempotency,
            llm_client=llm_client,
        )


def main() -> None:
    try:
        asyncio.run(run())
    except FatalError as exc:
        log.critical("researcher stopped due to fatal error", extra={"reason": str(exc)})
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
