import asyncio
import os

import aio_pika
import asyncpg
import pytest

RABBITMQ_URL = os.environ.get(
    "RABBITMQ_URL", "amqp://swarm:swarm_dev_password@localhost:5672/"
)
DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://swarm:swarm_dev_password@localhost:5432/code_review_swarm"
)


async def _check_rabbitmq() -> bool:
    try:
        connection = await asyncio.wait_for(aio_pika.connect(RABBITMQ_URL), timeout=3)
        await connection.close()
        return True
    except Exception:
        return False


async def _check_postgres() -> bool:
    try:
        connection = await asyncio.wait_for(asyncpg.connect(DATABASE_URL), timeout=3)
        await connection.close()
        return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def rabbitmq_available() -> bool:
    """True if RABBITMQ_URL is reachable. Integration tests skip on False
    instead of failing, so the suite still runs without Docker up."""
    return asyncio.run(_check_rabbitmq())


@pytest.fixture(scope="session")
def postgres_available() -> bool:
    """True if DATABASE_URL is reachable. Integration tests skip on False
    instead of failing, so the suite still runs without Docker up."""
    return asyncio.run(_check_postgres())
