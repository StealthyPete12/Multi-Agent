"""Watcher agent: receives GitHub webhooks and publishes `commit.detected`.

Run with::

    uvicorn agents.watcher.main:app --host 0.0.0.0 --port 8001

Endpoint
--------
POST /webhook/github
    Verifies the ``X-Hub-Signature-256`` HMAC, accepts only ``push``
    events on configured branches, and publishes one ``commit.detected``
    event per commit in the push to RabbitMQ.

The signature check and payload extraction are plain functions (no
FastAPI/network dependency) so they can be unit tested directly.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request, Response
from opentelemetry.trace import SpanKind
from prometheus_client import make_asgi_app

from shared import telemetry
from shared.broker import Broker
from shared.contracts import CommitDetected, EventType, make_envelope
from shared.logging import configure_logging, trace_context

GITHUB_WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
WATCHED_BRANCHES = [
    b.strip()
    for b in os.environ.get("WATCHED_BRANCHES", "main").split(",")
    if b.strip()
]
QUEUE_COMMITS = os.environ.get("QUEUE_COMMITS", "q.commits")

log = configure_logging(service_name="watcher")


class InvalidSignatureError(Exception):
    """Raised when a webhook signature is missing or does not match."""


def verify_signature(secret: str, body: bytes, signature_header: str | None) -> None:
    """Verify a GitHub ``X-Hub-Signature-256`` header against ``body``.

    Raises :class:`InvalidSignatureError` if the header is missing,
    malformed, or does not match the HMAC-SHA256 of ``body`` computed
    with ``secret``. Uses a constant-time comparison to avoid leaking
    timing information about the expected signature.
    """
    if not signature_header:
        raise InvalidSignatureError("missing X-Hub-Signature-256 header")

    prefix = "sha256="
    if not signature_header.startswith(prefix):
        raise InvalidSignatureError("malformed X-Hub-Signature-256 header")

    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    provided = signature_header[len(prefix) :]

    if not hmac.compare_digest(expected, provided):
        raise InvalidSignatureError("signature mismatch")


def branch_from_ref(ref: str) -> str:
    """Extract the branch name from a GitHub ``ref`` (e.g. ``refs/heads/main``)."""
    prefix = "refs/heads/"
    return ref[len(prefix) :] if ref.startswith(prefix) else ref


def extract_commit_events(
    payload: dict[str, Any], *, trace_id: str
) -> list[CommitDetected]:
    """Build one :class:`CommitDetected` per commit in a GitHub push payload.

    Returns an empty list if the payload has no commits (e.g. a branch
    delete push, ``payload["deleted"] is True``).
    """
    repo = payload["repository"]["full_name"]
    branch = branch_from_ref(payload["ref"])

    events: list[CommitDetected] = []
    for commit in payload.get("commits", []):
        changed_files = sorted(
            set(commit.get("added", []))
            | set(commit.get("modified", []))
            | set(commit.get("removed", []))
        )
        events.append(
            CommitDetected(
                repo=repo,
                commit_sha=commit["id"],
                branch=branch,
                author=commit.get("author", {}).get("name", "unknown"),
                message=commit.get("message", ""),
                committed_at=datetime.fromisoformat(commit["timestamp"]),
                changed_files=changed_files,
            )
        )
    return events


@asynccontextmanager
async def lifespan(app: FastAPI):
    telemetry.init_telemetry("watcher", start_metrics_server=False)
    broker = Broker()
    await broker.connect()
    await broker.declare_queue(
        QUEUE_COMMITS, routing_keys=[EventType.COMMIT_DETECTED.value]
    )
    app.state.broker = broker
    log.info(
        "watcher started",
        extra={"watched_branches": WATCHED_BRANCHES, "queue": QUEUE_COMMITS},
    )
    try:
        yield
    finally:
        await broker.close()
        telemetry.shutdown_telemetry()


app = FastAPI(title="watcher", lifespan=lifespan)
# Prometheus scrapes this directly (mounted on the watcher's own FastAPI
# app rather than a second HTTP server, since one's already running here).
app.mount("/metrics", make_asgi_app())


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/webhook/github")
async def github_webhook(
    request: Request,
    x_hub_signature_256: str | None = Header(default=None),
    x_github_event: str | None = Header(default=None),
) -> dict[str, Any]:
    body = await request.body()

    if not GITHUB_WEBHOOK_SECRET:
        raise HTTPException(status_code=500, detail="GITHUB_WEBHOOK_SECRET not configured")

    try:
        verify_signature(GITHUB_WEBHOOK_SECRET, body, x_hub_signature_256)
    except InvalidSignatureError as exc:
        log.warning("rejected webhook: invalid signature", extra={"reason": str(exc)})
        raise HTTPException(status_code=401, detail="invalid signature") from exc

    if x_github_event != "push":
        return {"status": "ignored", "reason": f"unsupported event type: {x_github_event}"}

    payload = await request.json()

    branch = branch_from_ref(payload.get("ref", ""))
    if branch not in WATCHED_BRANCHES:
        return {"status": "ignored", "reason": f"branch not watched: {branch}"}

    trace_id = str(uuid.uuid4())
    compare_url = payload.get("compare")

    with trace_context(trace_id=trace_id), telemetry.span(
        "watcher.webhook_received",
        kind=SpanKind.SERVER,
        tracer_name="agents.watcher",
        attributes={
            "http.method": "POST",
            "http.route": "/webhook/github",
            "swarm.branch": branch,
            "swarm.trace_id": trace_id,
        },
    ):
        commit_events = extract_commit_events(payload, trace_id=trace_id)

        broker: Broker = request.app.state.broker
        published = []
        for commit_payload in commit_events:
            envelope = make_envelope(
                commit_payload,
                event_type=EventType.COMMIT_DETECTED,
                source="watcher",
                trace_id=trace_id,
            )
            await broker.publish(envelope, routing_key=EventType.COMMIT_DETECTED.value)
            published.append(envelope.event_id)
            telemetry.get_metrics().commit_events.add(1, {"repo": commit_payload.repo, "direction": "published"})
            log.info(
                "published commit.detected",
                extra={
                    "event_id": envelope.event_id,
                    "repo": commit_payload.repo,
                    "commit_sha": commit_payload.commit_sha,
                    "branch": commit_payload.branch,
                    "compare_url": compare_url,
                    "event_type": "commit.detected",
                },
            )

    return {
        "status": "accepted",
        "trace_id": trace_id,
        "published_event_ids": published,
    }
