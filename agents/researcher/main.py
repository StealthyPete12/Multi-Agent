"""Researcher agent: Phase 1 pipeline-verification consumer.

Consumes `commit.detected` events from ``q.commits``, validates the
contract, logs the payload, and acknowledges the message. No AI, no
database writes — this exists purely to prove the pipeline (webhook /
seed script -> RabbitMQ -> consumer) works end-to-end with durability
and acknowledgements.

Run with::

    python -m agents.researcher.main
"""

from __future__ import annotations

import asyncio
import os
import signal

from pydantic import ValidationError

from shared.broker import Broker
from shared.contracts import CommitDetected, Envelope, EventType
from shared.logging import configure_logging, trace_context

QUEUE_COMMITS = os.environ.get("QUEUE_COMMITS", "q.commits")

log = configure_logging(service_name="researcher")


async def handle_message(message) -> None:
    """Validate and log one `commit.detected` message, then ack/nack it.

    Rejects (without requeue) messages that fail contract validation so
    they dead-letter instead of blocking the queue on redelivery.
    """
    async with message.process(ignore_processed=True, requeue=False):
        try:
            envelope = Envelope[CommitDetected].from_json(message.body)
        except ValidationError:
            log.exception(
                "rejected message: contract validation failed",
                extra={"raw_body": message.body.decode("utf-8", errors="replace")},
            )
            raise

        with trace_context(
            trace_id=envelope.trace_id, correlation_id=envelope.event_id
        ):
            log.info(
                "commit.detected received",
                extra={
                    "event_id": envelope.event_id,
                    "repo": envelope.payload.repo,
                    "commit_sha": envelope.payload.commit_sha,
                    "branch": envelope.payload.branch,
                    "author": envelope.payload.author,
                    "changed_files": envelope.payload.changed_files,
                },
            )


async def run() -> None:
    broker = Broker()
    await broker.connect()
    queue = await broker.declare_queue(
        QUEUE_COMMITS, routing_keys=[EventType.COMMIT_DETECTED.value]
    )
    log.info("researcher started", extra={"queue": QUEUE_COMMITS})

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    async with queue.iterator() as queue_iter:
        consume_task = asyncio.create_task(_consume(queue_iter))
        await stop_event.wait()
        consume_task.cancel()

    await broker.close()
    log.info("researcher stopped")


async def _consume(queue_iter) -> None:
    async for message in queue_iter:
        await handle_message(message)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
