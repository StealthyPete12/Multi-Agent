import asyncio
import os

import aio_pika
import asyncpg
import pytest

RABBITMQ_URL = os.environ.get("RABBITMQ_URL", "amqp://swarm:swarm_dev_password@localhost:5672/")
DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://swarm:swarm_dev_password@localhost:5432/code_review_swarm"
)
REDIS_URL = os.environ.get("REDIS_URL", "redis://:swarm_dev_password@localhost:6379/0")


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


async def _check_redis() -> bool:
    try:
        import redis.asyncio as redis

        client = redis.from_url(REDIS_URL)
        await asyncio.wait_for(client.ping(), timeout=3)
        await client.aclose()
        return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def redis_available() -> bool:
    """True if REDIS_URL is reachable. Integration tests skip on False
    instead of failing, so the suite still runs without Docker up."""
    return asyncio.run(_check_redis())


@pytest.fixture(autouse=True)
def _reset_process_wide_resilience_state():
    """``shared.breaker``/``shared.ratelimit`` cache one instance per
    provider/model key at module scope (deliberately, so every caller in
    a real process shares breaker/bucket state). That same caching would
    leak circuit-open/rate-limit state between otherwise-independent
    tests in the same pytest session, so reset it before every test."""
    import shared.breaker as breaker_module
    import shared.ratelimit as ratelimit_module

    breaker_module._breakers.clear()
    ratelimit_module._limiters.clear()
    # The cached Redis client is bound to the event loop it was created
    # on; pytest-asyncio gives each test function its own loop
    # (asyncio_mode=auto), so a client cached by an earlier test is dead
    # by the time a later test tries to reuse it. Drop the cache here
    # rather than trying to keep one client alive across loops.
    ratelimit_module._redis_client = None
    yield
    breaker_module._breakers.clear()
    ratelimit_module._limiters.clear()
    ratelimit_module._redis_client = None
