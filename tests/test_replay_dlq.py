import argparse
import uuid
from datetime import UTC, datetime

import pytest

from shared.broker import Broker
from shared.contracts import CommitDetected, EventType, make_envelope
from shared.retry import RetryLadder
from tools.replay_dlq import DlqEntry, _drain_dlq, _finish, _matches


class FakeHeaders(dict):
    pass


class FakeIncomingMessage:
    """Enough of aio_pika.IncomingMessage's surface for DlqEntry.from_message."""

    def __init__(self, body: bytes, headers: dict, message_type: str | None = None) -> None:
        self.body = body
        self.headers = headers
        self.content_type = "application/json"
        self.message_id = None
        self.correlation_id = None
        self.type = message_type

    async def ack(self):
        pass

    async def nack(self, requeue: bool = False):
        pass


def _envelope_bytes(repo="acme/widgets", commit_sha="deadbeef") -> bytes:
    payload = CommitDetected(
        repo=repo,
        commit_sha=commit_sha,
        branch="main",
        author="tester",
        message="x",
        committed_at=datetime.now(UTC),
        changed_files=[],
    )
    envelope = make_envelope(payload, event_type=EventType.COMMIT_DETECTED, source="test")
    return envelope.to_bytes(), envelope.event_id


def test_dlq_entry_parses_valid_envelope():
    body, event_id = _envelope_bytes(repo="acme/widgets", commit_sha="cafebabe")
    message = FakeIncomingMessage(
        body,
        headers={
            "x-retry-attempt": 2,
            "x-retry-reason": "boom",
            "x-retry-original-queue": "q.commits",
            "x-retry-first-failed-at": "2026-01-01T00:00:00+00:00",
        },
        message_type="commit.detected",
    )
    entry = DlqEntry.from_message(message)
    assert entry.event_id == event_id
    assert entry.repo == "acme/widgets"
    assert entry.commit_sha == "cafebabe"
    assert entry.attempt == 2
    assert entry.reason == "boom"
    assert entry.original_queue == "q.commits"


def test_dlq_entry_handles_unparseable_body_gracefully():
    message = FakeIncomingMessage(
        b"not json at all",
        headers={"x-retry-original-queue": "q.commits", "x-retry-reason": "malformed"},
    )
    entry = DlqEntry.from_message(message)
    assert entry.event_id is None
    assert entry.repo is None
    assert entry.original_queue == "q.commits"
    assert entry.reason == "malformed"


def _args(**overrides):
    defaults = {"event_id": None, "original_queue": None, "reason_contains": None, "repo": None}
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_matches_filters_by_event_id():
    entry = DlqEntry(
        message=None,
        event_id="abc",
        event_type="commit.detected",
        repo="r",
        commit_sha="s",
        attempt=1,
        reason="boom",
        original_queue="q.commits",
        first_failed_at=None,
    )
    assert _matches(entry, _args(event_id="abc")) is True
    assert _matches(entry, _args(event_id="other")) is False


def test_matches_filters_by_reason_substring_case_insensitive():
    entry = DlqEntry(
        message=None,
        event_id="abc",
        event_type="commit.detected",
        repo="r",
        commit_sha="s",
        attempt=1,
        reason="Connection RESET by peer",
        original_queue="q.commits",
        first_failed_at=None,
    )
    assert _matches(entry, _args(reason_contains="connection reset")) is True
    assert _matches(entry, _args(reason_contains="timeout")) is False


def test_matches_with_no_filters_matches_everything():
    entry = DlqEntry(
        message=None,
        event_id="abc",
        event_type="commit.detected",
        repo="r",
        commit_sha="s",
        attempt=1,
        reason="x",
        original_queue="q.commits",
        first_failed_at=None,
    )
    assert _matches(entry, _args()) is True


async def test_drain_and_finish_round_trip_preserves_unremoved_messages(rabbitmq_available):
    if not rabbitmq_available:
        pytest.skip("rabbitmq not reachable")

    broker = Broker()
    await broker.connect()
    ladder = RetryLadder(broker)
    await ladder.declare_topology()

    marker = uuid.uuid4().hex
    body_a, _ = _envelope_bytes(repo=f"replay-test-{marker}-a", commit_sha="a")
    body_b, _ = _envelope_bytes(repo=f"replay-test-{marker}-b", commit_sha="b")

    from shared.contracts import parse_envelope

    envelope_a = parse_envelope(body_a)
    envelope_b = parse_envelope(body_b)

    await ladder.send_to_dlq(
        envelope_a, reason=f"test-{marker}", attempt=4, original_queue="q.commits"
    )
    await ladder.send_to_dlq(
        envelope_b, reason=f"test-{marker}", attempt=4, original_queue="q.commits"
    )

    try:
        # Inspect-style drain: find our two, requeue everything (including
        # any unrelated real DLQ content) untouched.
        entries = await _drain_dlq(broker)
        ours = [e for e in entries if e.reason == f"test-{marker}"]
        assert len(ours) == 2
        await _finish(entries, remove=set())

        # Confirm nothing was removed: draining again still finds both.
        entries2 = await _drain_dlq(broker)
        ours2 = [e for e in entries2 if e.reason == f"test-{marker}"]
        assert len(ours2) == 2

        # Now actually remove just one of ours, requeue the rest.
        remove_index = next(i for i, e in enumerate(entries2) if e.reason == f"test-{marker}")
        await _finish(entries2, remove={remove_index})

        entries3 = await _drain_dlq(broker)
        ours3 = [e for e in entries3 if e.reason == f"test-{marker}"]
        assert len(ours3) == 1
        # Clean up: remove our own last remaining test message too, rather
        # than leaving it parked in the real, shared q.dlq indefinitely.
        remove_index = next(i for i, e in enumerate(entries3) if e.reason == f"test-{marker}")
        await _finish(entries3, remove={remove_index})
    finally:
        await broker.close()
