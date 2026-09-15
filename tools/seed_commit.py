#!/usr/bin/env python3
"""Publish synthetic `commit.detected` events without needing GitHub.

Examples::

    # one explicit commit
    python -m tools.seed_commit --repo acme/widgets --branch main \\
        --sha abc123 --author jane --message "fix: off by one"

    # random commit, useful for quick smoke tests
    python -m tools.seed_commit --random

    # burst of random commits
    python -m tools.seed_commit --random --count 5
"""

from __future__ import annotations

import argparse
import asyncio
import random
import string
import sys
import uuid
from datetime import datetime, timezone

from shared.broker import Broker
from shared.contracts import CommitDetected, EventType, make_envelope
from shared.logging import configure_logging

log = configure_logging(service_name="seed_commit")

_WORDS = [
    "fix",
    "feat",
    "refactor",
    "chore",
    "docs",
    "test",
    "widgets",
    "core",
    "auth",
    "pipeline",
    "off-by-one",
    "flaky",
    "timeout",
]


def _random_sha() -> str:
    return "".join(random.choices(string.hexdigits.lower()[:16], k=40))


def _random_commit(branch: str) -> CommitDetected:
    subject = f"{random.choice(_WORDS)}: {random.choice(_WORDS)} {random.choice(_WORDS)}"
    changed_files = [
        f"src/{random.choice(_WORDS)}/{random.choice(_WORDS)}.py"
        for _ in range(random.randint(1, 4))
    ]
    return CommitDetected(
        repo=f"acme/{random.choice(_WORDS)}",
        commit_sha=_random_sha(),
        branch=branch,
        author=f"{random.choice(_WORDS)}@acme.dev",
        message=subject,
        committed_at=datetime.now(timezone.utc),
        changed_files=changed_files,
    )


def build_commit(args: argparse.Namespace) -> CommitDetected:
    if args.random:
        return _random_commit(args.branch)

    if not args.repo or not args.author or not args.message:
        raise SystemExit(
            "--repo, --author, and --message are required unless --random is set"
        )

    return CommitDetected(
        repo=args.repo,
        commit_sha=args.sha or _random_sha(),
        branch=args.branch,
        author=args.author,
        message=args.message,
        committed_at=datetime.now(timezone.utc),
        changed_files=args.changed_files.split(",") if args.changed_files else [],
    )


async def seed(args: argparse.Namespace) -> None:
    broker = Broker()
    await broker.connect()
    try:
        for i in range(args.count):
            commit_payload = build_commit(args)
            trace_id = args.trace_id or str(uuid.uuid4())
            envelope = make_envelope(
                commit_payload,
                event_type=EventType.COMMIT_DETECTED,
                source="seed_commit",
                trace_id=trace_id,
            )
            await broker.publish(
                envelope, routing_key=EventType.COMMIT_DETECTED.value
            )
            log.info(
                "seeded commit.detected",
                extra={
                    "index": i,
                    "event_id": envelope.event_id,
                    "trace_id": trace_id,
                    "repo": commit_payload.repo,
                    "commit_sha": commit_payload.commit_sha,
                },
            )
    finally:
        await broker.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", help="repo full name, e.g. acme/widgets")
    parser.add_argument("--branch", default="main", help="branch name (default: main)")
    parser.add_argument("--sha", help="commit SHA (default: random)")
    parser.add_argument("--author", help="commit author")
    parser.add_argument("--message", help="commit message")
    parser.add_argument(
        "--changed-files", help="comma-separated list of changed file paths"
    )
    parser.add_argument("--trace-id", help="trace ID to attach (default: random per event)")
    parser.add_argument(
        "--random", action="store_true", help="generate a random synthetic commit"
    )
    parser.add_argument(
        "--count", type=int, default=1, help="number of events to publish (default: 1)"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    asyncio.run(seed(args))


if __name__ == "__main__":
    main(sys.argv[1:])
