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
import os
import signal
import time
from datetime import datetime, timezone

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
from shared.llm import LLMClient, LLMError, get_llm_client
from shared.logging import configure_logging, trace_context
from shared.slack import SlackNotifier, build_review_message

QUEUE_FINDINGS = os.environ.get("QUEUE_FINDINGS", "q.findings")
QUEUE_REVIEWS = os.environ.get("QUEUE_REVIEWS", "q.reviews")

log = configure_logging(service_name="reviewer")


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
) -> None:
    """Validate, review, publish `review.completed`, then ack/nack one
    `findings.ready` message.

    Rejects (without requeue) messages that fail contract validation so
    they dead-letter instead of blocking the queue on redelivery.
    """
    async with message.process(ignore_processed=True, requeue=False):
        try:
            envelope = Envelope[FindingsReady].from_json(message.body)
        except ValidationError:
            log.exception(
                "rejected message: contract validation failed",
                extra={"raw_body": message.body.decode("utf-8", errors="replace")},
            )
            raise

        with trace_context(trace_id=envelope.trace_id, correlation_id=envelope.event_id):
            log.info(
                "findings.ready received",
                extra={
                    "event_id": envelope.event_id,
                    "repo": envelope.payload.repo,
                    "commit_sha": envelope.payload.commit_sha,
                    "impact_count": envelope.payload.blast_radius.impact_count,
                    "sensitive_hits": len(envelope.payload.sensitive_hits),
                },
            )

            review = await process_findings(
                envelope.payload,
                event_id=envelope.event_id,
                trace_id=envelope.trace_id,
                storage=storage,
                llm_client=llm_client,
                slack=slack,
            )
            if review is None:
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


async def run() -> None:
    broker = Broker()
    await broker.connect()
    queue = await broker.declare_queue(
        QUEUE_FINDINGS, routing_keys=[EventType.FINDINGS_READY.value]
    )
    # Pre-declared so the queue/DLQ exist even before a downstream consumer
    # binds to it, matching how researcher pre-declares q.findings.
    await broker.declare_queue(QUEUE_REVIEWS, routing_keys=[EventType.REVIEW_COMPLETED.value])

    storage = ReviewStorage()
    await storage.connect()
    llm_client = get_llm_client(purpose="narrative")
    slack = SlackNotifier()

    log.info(
        "reviewer started",
        extra={
            "queue": QUEUE_FINDINGS,
            "publishes_to": QUEUE_REVIEWS,
            "llm_provider": llm_client.provider,
            "slack_configured": slack.is_configured,
        },
    )

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    async with queue.iterator() as queue_iter:
        consume_task = asyncio.create_task(
            _consume(queue_iter, broker=broker, storage=storage, llm_client=llm_client, slack=slack)
        )
        await stop_event.wait()
        consume_task.cancel()

    await storage.close()
    await broker.close()
    log.info("reviewer stopped")


async def _consume(
    queue_iter,
    *,
    broker: Broker,
    storage: ReviewStorage,
    llm_client: LLMClient,
    slack: SlackNotifier,
) -> None:
    async for message in queue_iter:
        await handle_message(message, broker=broker, storage=storage, llm_client=llm_client, slack=slack)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
