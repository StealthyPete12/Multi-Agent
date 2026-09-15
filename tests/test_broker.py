import uuid
from datetime import datetime, timezone

import pytest

from shared.broker import Broker
from shared.contracts import CommitDetected, Envelope, EventType, make_envelope


def _sample_payload() -> CommitDetected:
    return CommitDetected(
        repo="acme/widgets",
        commit_sha="a" * 40,
        branch="main",
        author="jane",
        message="test commit",
        committed_at=datetime.now(timezone.utc),
    )


async def _cleanup(broker: Broker, queue_name: str) -> None:
    await broker.channel.queue_delete(queue_name)
    await broker.channel.queue_delete(f"{queue_name}.dlq")


async def test_broker_connects_and_declares_topology(rabbitmq_available):
    if not rabbitmq_available:
        pytest.skip("RabbitMQ not reachable at RABBITMQ_URL")

    broker = Broker()
    await broker.connect()
    try:
        assert broker.exchange is not None
    finally:
        await broker.close()


async def test_publish_and_consume_roundtrip(rabbitmq_available):
    if not rabbitmq_available:
        pytest.skip("RabbitMQ not reachable at RABBITMQ_URL")

    queue_name = f"q.test.{uuid.uuid4().hex[:8]}"
    broker = Broker()
    await broker.connect()
    try:
        queue = await broker.declare_queue(
            queue_name, routing_keys=[EventType.COMMIT_DETECTED.value]
        )

        payload = _sample_payload()
        envelope = make_envelope(
            payload, event_type=EventType.COMMIT_DETECTED, source="test"
        )
        await broker.publish(envelope, routing_key=EventType.COMMIT_DETECTED.value)

        received: Envelope[CommitDetected] | None = None
        async with queue.iterator() as queue_iter:
            async for message in queue_iter:
                async with message.process():
                    received = Envelope[CommitDetected].from_json(message.body)
                break

        assert received is not None
        assert received.payload.commit_sha == payload.commit_sha
        assert received.event_id == envelope.event_id
    finally:
        await _cleanup(broker, queue_name)
        await broker.close()


async def test_message_survives_connection_cycle(rabbitmq_available):
    """A published, durable/persistent message sits in the queue even
    after the publisher's connection closes — proving durability doesn't
    depend on a consumer being connected at publish time."""
    if not rabbitmq_available:
        pytest.skip("RabbitMQ not reachable at RABBITMQ_URL")

    queue_name = f"q.test.{uuid.uuid4().hex[:8]}"

    publisher = Broker()
    await publisher.connect()
    queue = await publisher.declare_queue(
        queue_name, routing_keys=[EventType.COMMIT_DETECTED.value]
    )
    payload = _sample_payload()
    envelope = make_envelope(
        payload, event_type=EventType.COMMIT_DETECTED, source="test"
    )
    await publisher.publish(envelope, routing_key=EventType.COMMIT_DETECTED.value)
    await publisher.close()

    consumer = Broker()
    await consumer.connect()
    try:
        queue = await consumer.declare_queue(
            queue_name, routing_keys=[EventType.COMMIT_DETECTED.value]
        )
        received = None
        async with queue.iterator() as queue_iter:
            async for message in queue_iter:
                async with message.process():
                    received = Envelope[CommitDetected].from_json(message.body)
                break

        assert received is not None
        assert received.event_id == envelope.event_id
    finally:
        await _cleanup(consumer, queue_name)
        await consumer.close()
