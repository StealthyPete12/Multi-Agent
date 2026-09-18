import uuid

import asyncpg
import pytest

from shared.errors import RetryableError
from shared.idempotency import IdempotencyStore

DATABASE_URL = "postgresql://swarm:swarm_dev_password@localhost:5432/code_review_swarm"


@pytest.fixture
async def pool(postgres_available):
    if not postgres_available:
        pytest.skip("postgres not reachable")
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=2)
    yield pool
    await pool.close()


@pytest.fixture
def event_id() -> str:
    return str(uuid.uuid4())


async def test_claim_succeeds_first_time(pool, event_id):
    store = IdempotencyStore(pool)
    claimed = await store.claim(event_id=event_id, event_type="test.event", trace_id=None)
    assert claimed is True
    assert await store.is_claimed(event_id) is True
    await store.release(event_id)


async def test_claim_fails_on_second_attempt(pool, event_id):
    store = IdempotencyStore(pool)
    first = await store.claim(event_id=event_id, event_type="test.event", trace_id=None)
    second = await store.claim(event_id=event_id, event_type="test.event", trace_id=None)
    assert first is True
    assert second is False
    await store.release(event_id)


async def test_release_allows_reclaim(pool, event_id):
    store = IdempotencyStore(pool)
    await store.claim(event_id=event_id, event_type="test.event", trace_id=None)
    await store.release(event_id)
    reclaimed = await store.claim(event_id=event_id, event_type="test.event", trace_id=None)
    assert reclaimed is True
    await store.release(event_id)


async def test_claim_and_process_skips_duplicate(pool, event_id):
    store = IdempotencyStore(pool)
    calls = []

    async def work():
        calls.append(1)
        return "done"

    first = await store.claim_and_process(
        event_id=event_id, event_type="test.event", trace_id=None, work=work
    )
    second = await store.claim_and_process(
        event_id=event_id, event_type="test.event", trace_id=None, work=work
    )
    assert first == "done"
    from shared.idempotency import _DUPLICATE

    assert second is _DUPLICATE
    assert len(calls) == 1
    await store.release(event_id)


async def test_claim_and_process_releases_on_retryable_failure(pool, event_id):
    store = IdempotencyStore(pool)

    async def failing_work():
        raise RetryableError("transient")

    with pytest.raises(RetryableError):
        await store.claim_and_process(
            event_id=event_id, event_type="test.event", trace_id=None, work=failing_work
        )

    # Claim was released, so a later attempt (e.g. a retry-ladder
    # redelivery) can reclaim and try again rather than being
    # permanently skipped as "already processed".
    assert await store.is_claimed(event_id) is False
