from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from agents.researcher.main import handle_message
from shared.contracts import CommitDetected, EventType, make_envelope


class FakeMessage:
    """Minimal stand-in for aio_pika's IncomingMessage — enough surface
    (`body`, async `process()`) for handle_message's contract."""

    def __init__(self, body: bytes) -> None:
        self.body = body

    @asynccontextmanager
    async def process(self, ignore_processed: bool = True, requeue: bool = False):
        yield


async def test_handle_message_valid_commit_detected_does_not_raise():
    payload = CommitDetected(
        repo="acme/widgets",
        commit_sha="a" * 40,
        branch="main",
        author="jane",
        message="fix: bug",
        committed_at=datetime.now(timezone.utc),
    )
    envelope = make_envelope(
        payload, event_type=EventType.COMMIT_DETECTED, source="test"
    )

    await handle_message(FakeMessage(envelope.to_bytes()))


async def test_handle_message_rejects_invalid_contract():
    bad_body = b'{"event_type": "commit.detected", "source": "x", "payload": {}}'
    with pytest.raises(ValidationError):
        await handle_message(FakeMessage(bad_body))
