"""Slack notification for a completed review, via an Incoming Webhook.

Builds a Block Kit message with a colored side-bar (Slack's "attachments"
wrapper is still the only way to get a color bar alongside Block Kit
blocks) and posts it with a plain HTTPS POST — no Slack SDK dependency,
consistent with how ``shared/llm.py`` talks to model providers directly.
"""

from __future__ import annotations

import os

import httpx

from opentelemetry.trace import SpanKind

from shared import telemetry
from shared.logging import configure_logging

__all__ = [
    "SlackError",
    "SlackNotifier",
    "SEVERITY_COLORS",
    "build_review_message",
]

log = configure_logging(service_name="slack")

DEFAULT_TIMEOUT_SECONDS = 10.0

# Slack accepts hex colors on attachments; chosen for clear visual
# distinction and rough semaphore ordering (green -> red).
SEVERITY_COLORS: dict[str, str] = {
    "low": "#2eb67d",
    "moderate": "#ecb22e",
    "high": "#e8912d",
    "critical": "#e01e5a",
}


class SlackError(RuntimeError):
    """Raised when a configured Slack webhook rejects or fails a delivery."""


def _commit_url(repo: str, commit_sha: str) -> str:
    base = os.environ.get("REPO_WEB_BASE_URL", "https://github.com/").rstrip("/")
    return f"{base}/{repo}/commit/{commit_sha}"


def build_review_message(
    *,
    repo: str,
    commit_sha: str,
    severity: str,
    score: int,
    blast_radius_impact_count: int,
    blast_radius_max_depth: int,
    sensitive_hits: list[str],
    narrative: str,
    compare_url: str | None = None,
) -> dict:
    """Build the Slack Block Kit payload for one review report.

    Pure function (no network) so it's directly unit-testable and reused
    by both the Reviewer and its tests without a live webhook.
    """
    color = SEVERITY_COLORS.get(severity, "#6b7280")
    url = compare_url or _commit_url(repo, commit_sha)
    sensitive_text = ", ".join(sensitive_hits) if sensitive_hits else "None"

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"Code Review: {severity.upper()}", "emoji": True},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Repository:*\n{repo}"},
                {"type": "mrkdwn", "text": f"*Commit SHA:*\n`{commit_sha}`"},
                {"type": "mrkdwn", "text": f"*Severity:*\n{severity.upper()}"},
                {"type": "mrkdwn", "text": f"*Risk Score:*\n{score}"},
                {
                    "type": "mrkdwn",
                    "text": f"*Blast Radius:*\n{blast_radius_impact_count} modules, depth {blast_radius_max_depth}",
                },
                {"type": "mrkdwn", "text": f"*Sensitive Hits:*\n{sensitive_text}"},
            ],
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Narrative:*\n{narrative}"},
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "View Commit", "emoji": True},
                    "url": url,
                }
            ],
        },
    ]

    return {"attachments": [{"color": color, "blocks": blocks}]}


class SlackNotifier:
    """Thin webhook client. If ``SLACK_WEBHOOK_URL`` isn't configured,
    ``send`` is a no-op that logs and returns — Slack delivery is a
    best-effort notification, not a pipeline-blocking dependency in an
    environment where no webhook has been set up (e.g. local dev/CI)."""

    def __init__(self, webhook_url: str | None = None, *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self.webhook_url = webhook_url if webhook_url is not None else os.environ.get("SLACK_WEBHOOK_URL", "")
        self.timeout_seconds = timeout_seconds

    @property
    def is_configured(self) -> bool:
        return bool(self.webhook_url)

    async def send(self, payload: dict) -> bool:
        """Post ``payload`` to the configured webhook.

        Returns ``True`` on success, or when no webhook is configured
        (treated as "nothing to do", not a failure). Raises
        :class:`SlackError` only when a webhook *is* configured and the
        delivery actually fails, so a caller can tell "not set up" apart
        from "broken".
        """
        with telemetry.span("slack.deliver", kind=SpanKind.CLIENT, tracer_name="shared.slack") as current_span:
            if not self.is_configured:
                log.info("slack webhook not configured, skipping notification")
                current_span.set_attribute("slack.configured", False)
                telemetry.get_metrics().slack_deliveries.add(1, {"outcome": "not_configured"})
                return True

            current_span.set_attribute("slack.configured", True)
            try:
                async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                    resp = await client.post(self.webhook_url, json=payload)
                    resp.raise_for_status()
            except httpx.HTTPError as exc:
                telemetry.get_metrics().slack_deliveries.add(1, {"outcome": "failed"})
                raise SlackError(f"slack delivery failed: {exc}") from exc

            log.info("slack notification delivered")
            telemetry.get_metrics().slack_deliveries.add(1, {"outcome": "delivered"})
            return True
