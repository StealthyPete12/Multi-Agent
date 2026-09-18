#!/usr/bin/env python3
"""Publish synthetic `findings.ready` events without running the watcher
or researcher — useful for exercising the Reviewer agent in isolation.

Examples::

    # low-risk commit
    python -m tools.seed_findings --repo acme/widgets --sha abc123

    # large blast radius, high severity
    python -m tools.seed_findings --repo acme/widgets --sha abc123 \\
        --impact-count 60 --max-depth 9 --impacted-modules pkg.a,pkg.b

    # sensitive path touched
    python -m tools.seed_findings --repo acme/widgets --sha abc123 \\
        --changed-files auth/login.py --sensitive-hits auth/login.py
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from datetime import datetime, timezone

from shared.broker import Broker
from shared.contracts import BlastRadius, EventType, FindingsReady, make_envelope
from shared.logging import configure_logging

log = configure_logging(service_name="seed_findings")


def build_findings(args: argparse.Namespace) -> FindingsReady:
    now = datetime.now(timezone.utc)
    changed_files = args.changed_files.split(",") if args.changed_files else ["src/widget.py"]
    impacted_modules = args.impacted_modules.split(",") if args.impacted_modules else []
    sensitive_hits = args.sensitive_hits.split(",") if args.sensitive_hits else []

    return FindingsReady(
        commit_sha=args.sha,
        agent_name="researcher",
        findings=[],
        started_at=now,
        completed_at=now,
        repo=args.repo,
        changed_files=changed_files,
        blast_radius=BlastRadius(
            impacted_modules=impacted_modules,
            impact_count=args.impact_count,
            max_depth=args.max_depth,
        ),
        sensitive_hits=sensitive_hits,
        semantic_summary=args.summary,
    )


async def seed(args: argparse.Namespace) -> None:
    broker = Broker()
    await broker.connect()
    try:
        findings = build_findings(args)
        trace_id = args.trace_id or str(uuid.uuid4())
        envelope = make_envelope(
            findings,
            event_type=EventType.FINDINGS_READY,
            source="seed_findings",
            trace_id=trace_id,
        )
        await broker.publish(envelope, routing_key=EventType.FINDINGS_READY.value)
        log.info(
            "seeded findings.ready",
            extra={
                "event_id": envelope.event_id,
                "trace_id": trace_id,
                "repo": findings.repo,
                "commit_sha": findings.commit_sha,
                "impact_count": findings.blast_radius.impact_count,
                "sensitive_hits": findings.sensitive_hits,
            },
        )
    finally:
        await broker.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default="acme/widgets", help="repo full name")
    parser.add_argument("--sha", default="deadbeef1234", help="commit SHA")
    parser.add_argument("--changed-files", help="comma-separated changed file paths")
    parser.add_argument("--impacted-modules", help="comma-separated impacted module names")
    parser.add_argument("--impact-count", type=int, default=0, help="blast radius impact count")
    parser.add_argument("--max-depth", type=int, default=0, help="blast radius max depth")
    parser.add_argument("--sensitive-hits", help="comma-separated sensitive-path hits")
    parser.add_argument("--summary", default="", help="semantic_summary text")
    parser.add_argument("--trace-id", help="trace ID to attach (default: random)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    asyncio.run(seed(args))


if __name__ == "__main__":
    main(sys.argv[1:])
