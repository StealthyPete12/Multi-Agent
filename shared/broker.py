"""Shared RabbitMQ broker layer, built on aio-pika.

Every agent (watcher, researcher, reviewer, ...) publishes and consumes
through this module rather than talking to aio-pika directly, so the
topology (exchange, queues, bindings, dead-lettering, QoS, publisher
confirms) stays consistent across services.

Topology
--------
One durable topic exchange (``RABBITMQ_EXCHANGE``, default
``swarm.events``) carries every event type, routed by
``EventType.value`` (e.g. ``commit.detected``). Each queue is durable and
dead-letters rejected/expired messages to a matching durable queue on a
secondary topic exchange (``<exchange>.dlx``) so a poison message doesn't
block a working queue or get silently dropped.

Usage::

    broker = Broker()
    await broker.connect()

    # producer
    await broker.publish(envelope, routing_key=EventType.COMMIT_DETECTED.value)

    # consumer
    queue = await broker.declare_queue("q.commits", routing_keys=["commit.detected"])
    async with queue.iterator() as it:
        async for message in it:
            async with message.process():
                ...

    await broker.close()
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import aio_pika
from aio_pika import ExchangeType

if TYPE_CHECKING:
    from aio_pika.abc import (
        AbstractChannel,
        AbstractQueue,
        AbstractRobustConnection,
    )

    from shared.contracts import Envelope

__all__ = ["Broker", "DEFAULT_EXCHANGE", "DEFAULT_PREFETCH"]

DEFAULT_EXCHANGE = "swarm.events"
DEFAULT_PREFETCH = 10


class Broker:
    """Async RabbitMQ client wrapping connection/channel/topology setup.

    One instance per process. Not thread-safe; intended for a single
    asyncio event loop per the aio-pika contract.
    """

    def __init__(
        self,
        url: str | None = None,
        *,
        exchange_name: str | None = None,
        prefetch_count: int | None = None,
    ) -> None:
        self.url = url or os.environ.get(
            "RABBITMQ_URL", "amqp://swarm:swarm_dev_password@localhost:5672/"
        )
        self.exchange_name = exchange_name or os.environ.get(
            "RABBITMQ_EXCHANGE", DEFAULT_EXCHANGE
        )
        self.dlx_name = f"{self.exchange_name}.dlx"
        self.prefetch_count = prefetch_count or int(
            os.environ.get("RABBITMQ_PREFETCH", DEFAULT_PREFETCH)
        )

        self._connection: AbstractRobustConnection | None = None
        self._channel: AbstractChannel | None = None
        self._exchange: aio_pika.abc.AbstractExchange | None = None
        self._dlx: aio_pika.abc.AbstractExchange | None = None

    async def connect(self) -> None:
        """Open a robust connection, a confirm-mode channel, and declare
        the durable topic exchange (and its dead-letter counterpart).

        Idempotent: safe to call again if already connected.
        """
        if self._connection is not None:
            return

        self._connection = await aio_pika.connect_robust(self.url)
        # publisher_confirms=True (the aio-pika default) makes every
        # `exchange.publish(...)` await the broker's ack/nack instead of
        # firing and forgetting.
        self._channel = await self._connection.channel(publisher_confirms=True)
        await self._channel.set_qos(prefetch_count=self.prefetch_count)

        self._dlx = await self._channel.declare_exchange(
            self.dlx_name, ExchangeType.TOPIC, durable=True
        )
        self._exchange = await self._channel.declare_exchange(
            self.exchange_name, ExchangeType.TOPIC, durable=True
        )

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()
            self._connection = None
            self._channel = None
            self._exchange = None
            self._dlx = None

    @property
    def channel(self) -> "AbstractChannel":
        if self._channel is None:
            raise RuntimeError("Broker.connect() must be awaited before use")
        return self._channel

    @property
    def exchange(self) -> "aio_pika.abc.AbstractExchange":
        if self._exchange is None:
            raise RuntimeError("Broker.connect() must be awaited before use")
        return self._exchange

    async def publish(
        self,
        envelope: "Envelope",
        *,
        routing_key: str,
    ) -> None:
        """Publish an envelope with persistent delivery mode, waiting for
        the broker's publisher confirm.

        Raises if the broker nacks the publish (e.g. no route, internal
        error) so callers can retry or surface the failure rather than
        silently losing the event.
        """
        message = aio_pika.Message(
            body=envelope.to_bytes(),
            content_type="application/json",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            message_id=envelope.event_id,
            correlation_id=envelope.correlation_id,
            type=envelope.event_type.value,
            headers={"trace_id": envelope.trace_id},
        )
        await self.exchange.publish(message, routing_key=routing_key)

    async def declare_queue(
        self,
        queue_name: str,
        *,
        routing_keys: list[str],
    ) -> "AbstractQueue":
        """Declare a durable queue bound to the main exchange for each of
        ``routing_keys``, dead-lettering to ``<queue_name>.dlq`` on the DLX.

        Reusable by any future consumer: pass the queue name and the
        event-type routing keys it should receive.
        """
        dlq_name = f"{queue_name}.dlq"
        dlq = await self.channel.declare_queue(dlq_name, durable=True)
        await dlq.bind(self._dlx_exchange(), routing_key=queue_name)

        queue = await self.channel.declare_queue(
            queue_name,
            durable=True,
            arguments={
                "x-dead-letter-exchange": self.dlx_name,
                "x-dead-letter-routing-key": queue_name,
            },
        )
        for routing_key in routing_keys:
            await queue.bind(self.exchange, routing_key=routing_key)

        return queue

    def _dlx_exchange(self) -> "aio_pika.abc.AbstractExchange":
        if self._dlx is None:
            raise RuntimeError("Broker.connect() must be awaited before use")
        return self._dlx

    async def __aenter__(self) -> "Broker":
        await self.connect()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()
