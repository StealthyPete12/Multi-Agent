#!/usr/bin/env python3
"""Inspect and replay messages parked in ``q.dlq``.

Every message that lands in the DLQ — a poison message
(``shared/retry.py::RetryLadder.send_raw_to_dlq``) or a retryable failure
that exhausted the ladder (``RetryLadder.send_to_dlq``) — carries the
retry metadata headers set by ``shared/retry.py``:
``x-retry-attempt``/``x-retry-reason``/``x-retry-original-queue``/
``x-retry-first-failed-at``. This tool reads those headers to filter and
report on DLQ contents, and replays selected messages by publishing them
straight back to their original queue (via the default exchange, routing
key = queue name) — a fresh attempt budget, since a human is presumably
replaying only after fixing whatever caused the failure.

AMQP has no server-side "peek" — inspecting a queue means consuming from
it. This tool always drains the queue into memory first, then decides
what happens to each message: **inspect** and **dry-run replay** requeue
every message unchanged (nothing is lost, nothing is removed); a real
(non-dry-run) **replay** only permanently removes the messages that were
actually replayed, immediately requeuing everything else.

Examples::

    # see what's in the DLQ without changing anything
    python -m tools.replay_dlq inspect

    # see what a replay of everything from q.commits would do, without doing it
    python -m tools.replay_dlq replay --original-queue q.commits --dry-run

    # actually replay every DLQ message
    python -m tools.replay_dlq replay --all

    # replay one specific message by event_id
    python -m tools.replay_dlq replay --event-id 9c1e...
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime

import aio_pika

from shared.broker import Broker
from shared.logging import configure_logging
from shared.retry import (
    DLQ_QUEUE_NAME,
    HEADER_ATTEMPT,
    HEADER_FIRST_FAILED_AT,
    HEADER_ORIGINAL_QUEUE,
    HEADER_REASON,
)

log = configure_logging(service_name="replay_dlq")


def _header_int(value: object, default: int) -> int:
    """Coerce an AMQP field-table header value to int. Headers this tool
    reads are always written as plain ints by ``shared/retry.py``, but the
    AMQP decoder's declared value type is a broad union (bytes/Decimal/
    datetime/...), so this stays defensive rather than assuming that."""
    if value is None:
        return default
    try:
        return int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return default


def _header_str_or_none(value: object) -> str | None:
    if value is None:
        return None
    return value.decode() if isinstance(value, bytes) else str(value)


@dataclass
class DlqEntry:
    message: aio_pika.abc.AbstractIncomingMessage
    event_id: str | None
    event_type: str | None
    repo: str | None
    commit_sha: str | None
    attempt: int
    reason: str
    original_queue: str
    first_failed_at: str | None

    @classmethod
    def from_message(cls, message: aio_pika.abc.AbstractIncomingMessage) -> DlqEntry:
        headers = message.headers or {}
        event_id = None
        event_type = None
        repo = None
        commit_sha = None
        try:
            raw = json.loads(message.body)
            event_id = raw.get("event_id")
            event_type = raw.get("event_type")
            payload = raw.get("payload") or {}
            repo = payload.get("repo")
            commit_sha = payload.get("commit_sha")
        except (json.JSONDecodeError, AttributeError, TypeError):
            pass  # a truly malformed poison message may not even be JSON

        return cls(
            message=message,
            event_id=event_id,
            event_type=event_type,
            repo=repo,
            commit_sha=commit_sha,
            attempt=_header_int(headers.get(HEADER_ATTEMPT), 0),
            reason=_header_str_or_none(headers.get(HEADER_REASON)) or "",
            original_queue=_header_str_or_none(headers.get(HEADER_ORIGINAL_QUEUE)) or "unknown",
            first_failed_at=_header_str_or_none(headers.get(HEADER_FIRST_FAILED_AT)),
        )

    def summary(self) -> dict:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "repo": self.repo,
            "commit_sha": self.commit_sha,
            "attempt": self.attempt,
            "reason": self.reason,
            "original_queue": self.original_queue,
            "first_failed_at": self.first_failed_at,
        }


async def _drain_dlq(broker: Broker, *, limit: int | None = None) -> list[DlqEntry]:
    """Pull every (or up to ``limit``) message currently in q.dlq into
    memory as :class:`DlqEntry` objects. Callers are responsible for
    acking (permanently removing) or nacking-with-requeue (putting back)
    every entry returned here — see ``_finish``."""
    queue = await broker.channel.declare_queue(DLQ_QUEUE_NAME, durable=True)
    entries: list[DlqEntry] = []
    while limit is None or len(entries) < limit:
        message = await queue.get(no_ack=False, fail=False)
        if message is None:
            break
        entries.append(DlqEntry.from_message(message))
    return entries


async def _finish(entries: list[DlqEntry], *, remove: set[int]) -> None:
    """Ack the entries whose index is in ``remove`` (permanently drop
    them from the DLQ); nack-with-requeue everything else so inspecting
    or partially replaying the queue never loses a message."""
    for i, entry in enumerate(entries):
        if i in remove:
            await entry.message.ack()
        else:
            await entry.message.nack(requeue=True)


def _matches(entry: DlqEntry, args: argparse.Namespace) -> bool:
    if args.event_id and entry.event_id != args.event_id:
        return False
    if args.original_queue and entry.original_queue != args.original_queue:
        return False
    if args.reason_contains and args.reason_contains.lower() not in entry.reason.lower():
        return False
    if args.repo and entry.repo != args.repo:
        return False
    return True


def _print_entries(entries: list[DlqEntry], *, label: str) -> None:
    print(f"\n{label} ({len(entries)}):")
    if not entries:
        print("  (none)")
        return
    for entry in entries:
        print(
            f"  - event_id={entry.event_id} type={entry.event_type} "
            f"repo={entry.repo} commit_sha={entry.commit_sha} "
            f"attempt={entry.attempt} original_queue={entry.original_queue} "
            f"first_failed_at={entry.first_failed_at}\n"
            f"    reason: {entry.reason}"
        )


async def cmd_inspect(args: argparse.Namespace) -> None:
    broker = Broker()
    await broker.connect()
    try:
        entries = await _drain_dlq(broker, limit=args.limit)
        matched = [e for e in entries if _matches(e, args)]
        _print_entries(matched if _has_filters(args) else entries, label="DLQ contents")
        if _has_filters(args):
            print(f"\n({len(entries)} total in DLQ, {len(matched)} match the given filter)")
        # Inspection never removes anything.
        await _finish(entries, remove=set())
    finally:
        await broker.close()


async def cmd_replay(args: argparse.Namespace) -> None:
    if not (args.all or args.event_id or args.original_queue or args.reason_contains or args.repo):
        print(
            "refusing to replay: pass --all or a filter (--event-id/--original-queue/--reason-contains/--repo)"
        )
        sys.exit(2)

    broker = Broker()
    await broker.connect()
    try:
        entries = await _drain_dlq(broker, limit=args.limit)
        to_replay = [(i, e) for i, e in enumerate(entries) if args.all or _matches(e, args)]

        _print_entries([e for _, e in to_replay], label="Selected for replay")
        print(f"\n({len(entries)} total in DLQ, {len(to_replay)} selected)")

        if args.dry_run:
            print("\n[dry-run] no messages were replayed or removed from the DLQ.")
            await _finish(entries, remove=set())
            return

        remove: set[int] = set()
        for i, entry in to_replay:
            message = aio_pika.Message(
                body=entry.message.body,
                content_type=entry.message.content_type or "application/json",
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                message_id=entry.message.message_id,
                correlation_id=entry.message.correlation_id,
                type=entry.message.type,
                headers={
                    "trace_id": (entry.message.headers or {}).get("trace_id"),
                    "x-replayed-from-dlq-at": datetime.now(UTC).isoformat(),
                    "x-replayed-original-reason": entry.reason,
                },
            )
            await broker.channel.default_exchange.publish(message, routing_key=entry.original_queue)
            remove.add(i)
            log.info(
                "replayed message from DLQ",
                extra={
                    "event_id": entry.event_id,
                    "original_queue": entry.original_queue,
                    "replay_activity": "replayed",
                },
            )

        await _finish(entries, remove=remove)
        print(f"\nReplayed {len(remove)} message(s) back to their original queue.")
    finally:
        await broker.close()


def _has_filters(args: argparse.Namespace) -> bool:
    return bool(args.event_id or args.original_queue or args.reason_contains or args.repo)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser(
        "inspect", help="List DLQ contents without changing anything"
    )
    inspect_parser.add_argument(
        "--limit", type=int, default=None, help="Only inspect the first N messages"
    )
    inspect_parser.add_argument("--event-id", default=None)
    inspect_parser.add_argument("--original-queue", default=None)
    inspect_parser.add_argument("--reason-contains", default=None)
    inspect_parser.add_argument("--repo", default=None)
    inspect_parser.set_defaults(func=cmd_inspect)

    replay_parser = subparsers.add_parser("replay", help="Replay selected (or all) DLQ messages")
    replay_parser.add_argument("--all", action="store_true", help="Replay every message in the DLQ")
    replay_parser.add_argument("--event-id", default=None, help="Replay only this event_id")
    replay_parser.add_argument(
        "--original-queue",
        default=None,
        help="Replay only messages originally destined for this queue",
    )
    replay_parser.add_argument(
        "--reason-contains",
        default=None,
        help="Replay only messages whose failure reason contains this substring",
    )
    replay_parser.add_argument("--repo", default=None, help="Replay only messages for this repo")
    replay_parser.add_argument(
        "--limit", type=int, default=None, help="Only consider the first N messages in the DLQ"
    )
    replay_parser.add_argument(
        "--dry-run", action="store_true", help="Show what would be replayed without doing it"
    )
    replay_parser.set_defaults(func=cmd_replay)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    asyncio.run(args.func(args))


if __name__ == "__main__":
    main()
