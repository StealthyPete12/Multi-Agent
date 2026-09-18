"""Reviewer agent: Phase 3 intelligence layer.

Consumes `findings.ready` events from ``q.findings`` and, for each one:

1. Skips it if already processed (idempotency via ``processed_events``,
   see ``storage.py``).
2. Computes a deterministic risk score/severity (``scoring.py`` — no LLM
   involved).
3. Generates an executive narrative explaining that score with an LLM
   (``prompts.py`` + ``shared/llm.py``), falling back to a deterministic
   template if no provider is configured or the call fails.
4. Persists the report to Postgres (``storage.py``).
5. Sends a Slack notification (``shared/slack.py``).
6. Publishes `review.completed` — only after both storage and Slack
   delivery succeed.

Run with::

    python -m agents.reviewer.main
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import time
from datetime import datetime, timezone

import asyncpg
from pydantic import ValidationError

from agents.reviewer.prompts import (
    NARRATIVE_MAX_TOKENS,
    NARRATIVE_SYSTEM_PROMPT,
    build_fallback_narrative,
    build_narrative_prompt,
)
from agents.reviewer.scoring import ScoreBreakdown, compute_score, status_for_severity
from agents.reviewer.storage import ReviewStorage
from shared.broker import Broker
from shared.contracts import Envelope, EventType, FindingsReady, ReviewCompleted, make_envelope
from shared.errors import FatalError, PoisonMessageError, RetryableError, classify_exception
from shared.idempotency import IdempotencyStore
from shared.llm import LLMClient, LLMError, get_llm_client
from shared.logging import configure_logging, trace_context
from shared.retry import MAX_RETRY_ATTEMPTS, RetryLadder
from shared.slack import SlackError, SlackNotifier, build_review_message

QUEUE_FINDINGS = os.environ.get("QUEUE_FINDINGS", "q.findings")
QUEUE_REVIEWS = os.environ.get("QUEUE_REVIEWS", "q.reviews")

log = configure_logging(service_name="reviewer")


def classify_reviewer_failure(exc: Exception) -> type[RetryableError | PoisonMessageError | FatalError]:
    """Map a failure from ``process_findings``'s pipeline (Postgres,
    Slack) to one of the three Phase 4 error categories. Kept in the
    consumer, not ``shared/errors.py``, for the same layering reason as
    the researcher's ``classify_researcher_failure``."""
    if isinstance(exc, SlackError):
        # SlackError always wraps an httpx.HTTPError (see shared/slack.py)
        # — delegate to the same HTTP-status/network classification the
        # LLM client uses.
        cause = exc.__cause__
        if cause is not None:
            return classify_exception(cause)
        return RetryableError
    if isinstance(exc, (ConnectionError, TimeoutError, OSError, asyncpg.PostgresConnectionError)):
        return RetryableError
    if isinstance(exc, LLMError):
        # generate_narrative() already degrades LLM failures to the
        # deterministic fallback template internally and never lets
        # LLMError escape — this branch is defensive only.
        return RetryableError
    return classify_exception(exc)


async def generate_narrative(
    findings: FindingsReady, breakdown: ScoreBreakdown, *, llm_client: LLMClient
) -> str:
    """LLM narrative for an already-final score, with a deterministic
    fallback so a review is never missing an explanation."""
    prompt = build_narrative_prompt(findings, breakdown)
    try:
        response = await llm_client.complete(
            system=NARRATIVE_SYSTEM_PROMPT,
            prompt=prompt,
            max_tokens=NARRATIVE_MAX_TOKENS,
        )
        text = response.text.strip()
        if text:
            return text
    except LLMError as exc:
        log.warning("narrative generation skipped, using fallback", extra={"reason": str(exc)})
    return build_fallback_narrative(findings, breakdown)


async def process_findings(
    findings: FindingsReady,
    *,
    event_id: str,
    trace_id: str,
    storage: ReviewStorage,
    llm_client: LLMClient,
    slack: SlackNotifier,
) -> ReviewCompleted | None:
    """Run the full score -> narrative -> persist -> notify pipeline for
    one `findings.ready` payload. Returns ``None`` if the event was
    already processed (idempotent no-op)."""
    if await storage.is_processed(event_id):
        log.info("findings.ready already processed, skipping", extra={"event_id": event_id})
        return None

    t0 = time.monotonic()
    breakdown = compute_score(findings)
    log.info(
        "risk score computed",
        extra={
            "repo": findings.repo,
            "commit_sha": findings.commit_sha,
            "severity": breakdown.severity,
            "score": breakdown.total,
            "duration_ms": (time.monotonic() - t0) * 1000,
        },
    )

    t0 = time.monotonic()
    narrative = await generate_narrative(findings, breakdown, llm_client=llm_client)
    log.info(
        "review narrative generated",
        extra={
            "repo": findings.repo,
            "commit_sha": findings.commit_sha,
            "narrative_length": len(narrative),
            "duration_ms": (time.monotonic() - t0) * 1000,
        },
    )

    status = status_for_severity(breakdown.severity)

    t0 = time.monotonic()
    saved = await storage.save_report(
        findings,
        event_id=event_id,
        trace_id=trace_id,
        breakdown=breakdown,
        status=status,
        narrative=narrative,
    )
    log.info(
        "database write complete",
        extra={"report_id": saved.report_id, "duration_ms": (time.monotonic() - t0) * 1000},
    )

    t0 = time.monotonic()
    message = build_review_message(
        repo=findings.repo,
        commit_sha=findings.commit_sha,
        severity=breakdown.severity,
        score=breakdown.total,
        blast_radius_impact_count=findings.blast_radius.impact_count,
        blast_radius_max_depth=findings.blast_radius.max_depth,
        sensitive_hits=findings.sensitive_hits,
        narrative=narrative,
    )
    await slack.send(message)
    log.info(
        "slack delivery complete",
        extra={
            "configured": slack.is_configured,
            "duration_ms": (time.monotonic() - t0) * 1000,
        },
    )

    return ReviewCompleted(
        commit_sha=findings.commit_sha,
        repo=findings.repo,
        report_id=saved.report_id,
        status=status,
        severity=breakdown.severity,
        score=breakdown.total,
        summary=findings.semantic_summary or narrative[:200],
        total_findings=len(findings.findings),
        completed_at=datetime.now(timezone.utc),
    )


async def handle_message(
    message,
    *,
    broker: Broker,
    storage: ReviewStorage,
    llm_client: LLMClient,
    slack: SlackNotifier,
    retry_ladder: RetryLadder,
    idempotency: IdempotencyStore,
) -> None:
    """Validate, review, publish `review.completed`, then ack/nack one
    `findings.ready` message — manual acknowledgement throughout, mirroring
    ``agents/researcher/main.py::handle_message``'s Phase 4 routing:
    poison -> DLQ immediately, retryable -> retry ladder (claim released
    so a later attempt can reclaim), fatal -> log, nack-with-requeue, stop
    the service. ``process_findings``/``storage.py`` (the actual scoring,
    narrative, persistence, Slack logic) are unchanged from Phase 3.
    """
    try:
        envelope = Envelope[FindingsReady].from_json(message.body)
    except ValidationError as exc:
        log.exception(
            "rejected message: contract validation failed",
            extra={"raw_body": message.body.decode("utf-8", errors="replace")},
        )
        await retry_ladder.send_raw_to_dlq(
            message.body,
            reason=f"contract validation failed: {exc}",
            original_queue=QUEUE_FINDINGS,
        )
        await message.ack()
        return

    attempt = RetryLadder.attempt_from_headers(message.headers)

    with trace_context(trace_id=envelope.trace_id, correlation_id=envelope.event_id):
        log.info(
            "findings.ready received",
            extra={
                "event_id": envelope.event_id,
                "repo": envelope.payload.repo,
                "commit_sha": envelope.payload.commit_sha,
                "impact_count": envelope.payload.blast_radius.impact_count,
                "sensitive_hits": len(envelope.payload.sensitive_hits),
                "retry_count": attempt,
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
            review = await process_findings(
                envelope.payload,
                event_id=envelope.event_id,
                trace_id=envelope.trace_id,
                storage=storage,
                llm_client=llm_client,
                slack=slack,
            )
            if review is None:
                # storage.is_processed() found it already fully persisted
                # (e.g. the outer claim above raced with a prior in-flight
                # completion) — nothing left to do.
                await idempotency.mark_complete(envelope.event_id)
                await message.ack()
                return

            review_envelope = make_envelope(
                review,
                event_type=EventType.REVIEW_COMPLETED,
                source="reviewer",
                trace_id=envelope.trace_id,
                correlation_id=envelope.event_id,
            )
            await broker.publish(review_envelope, routing_key=EventType.REVIEW_COMPLETED.value)
            log.info(
                "published review.completed",
                extra={
                    "event_id": review_envelope.event_id,
                    "commit_sha": review.commit_sha,
                    "severity": review.severity,
                    "score": review.score,
                    "report_id": review.report_id,
                },
            )
            await idempotency.mark_complete(envelope.event_id)
            await message.ack()
        except Exception as exc:
            category = classify_reviewer_failure(exc)

            if category is PoisonMessageError:
                await idempotency.release(envelope.event_id)
                await retry_ladder.send_to_dlq(
                    envelope, reason=str(exc), attempt=attempt, original_queue=QUEUE_FINDINGS
                )
                await message.ack()
                return

            if category is FatalError:
                log.critical(
                    "fatal error in reviewer pipeline, stopping service",
                    extra={"reason": str(exc), "event_id": envelope.event_id},
                )
                await idempotency.release(envelope.event_id)
                await message.nack(requeue=True)
                raise FatalError(str(exc)) from exc

            await idempotency.release(envelope.event_id)
            next_attempt = attempt + 1
            if next_attempt > MAX_RETRY_ATTEMPTS:
                await retry_ladder.send_to_dlq(
                    envelope, reason=str(exc), attempt=next_attempt, original_queue=QUEUE_FINDINGS
                )
            else:
                await retry_ladder.schedule_retry(
                    envelope,
                    routing_key=EventType.FINDINGS_READY.value,
                    reason=str(exc),
                    attempt=next_attempt,
                    original_queue=QUEUE_FINDINGS,
                )
            await message.ack()


GRACEFUL_SHUTDOWN_SECONDS = float(os.environ.get("GRACEFUL_SHUTDOWN_SECONDS", "30"))


async def run() -> None:
    # prefetch_count=1: see agents/researcher/main.py::run for the same
    # rationale — exactly one unacked findings.ready in flight at a time.
    broker = Broker(prefetch_count=1)
    await broker.connect()
    queue = await broker.declare_queue(
        QUEUE_FINDINGS, routing_keys=[EventType.FINDINGS_READY.value]
    )
    # Pre-declared so the queue/DLQ exist even before a downstream consumer
    # binds to it, matching how researcher pre-declares q.findings.
    await broker.declare_queue(QUEUE_REVIEWS, routing_keys=[EventType.REVIEW_COMPLETED.value])

    retry_ladder = RetryLadder(broker)
    await retry_ladder.declare_topology()

    storage = ReviewStorage()
    await storage.connect()
    idempotency = IdempotencyStore(storage.pool)
    llm_client = get_llm_client(purpose="narrative")
    slack = SlackNotifier()

    log.info(
        "reviewer started",
        extra={
            "queue": QUEUE_FINDINGS,
            "publishes_to": QUEUE_REVIEWS,
            "llm_provider": llm_client.provider,
            "slack_configured": slack.is_configured,
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
                storage=storage,
                llm_client=llm_client,
                slack=slack,
                retry_ladder=retry_ladder,
                idempotency=idempotency,
            )
        )
        stop_wait_task = asyncio.create_task(stop_event.wait())
        done, _pending = await asyncio.wait(
            {consume_task, stop_wait_task}, return_when=asyncio.FIRST_COMPLETED
        )

        if consume_task in done and consume_task.exception() is not None:
            fatal_error = consume_task.exception()
        else:
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

    await storage.close()
    await broker.close()
    log.info("reviewer stopped")
    if fatal_error is not None:
        raise fatal_error


async def _consume(
    queue_iter,
    *,
    broker: Broker,
    storage: ReviewStorage,
    llm_client: LLMClient,
    slack: SlackNotifier,
    retry_ladder: RetryLadder,
    idempotency: IdempotencyStore,
) -> None:
    async for message in queue_iter:
        await handle_message(
            message,
            broker=broker,
            storage=storage,
            llm_client=llm_client,
            slack=slack,
            retry_ladder=retry_ladder,
            idempotency=idempotency,
        )


def main() -> None:
    try:
        asyncio.run(run())
    except FatalError as exc:
        log.critical("reviewer stopped due to fatal error", extra={"reason": str(exc)})
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
