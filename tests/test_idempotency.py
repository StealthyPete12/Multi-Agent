import asyncio
import uuid

import asyncpg
import pytest

from shared.idempotency import IdempotencyStore
from tests.conftest import DATABASE_URL


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
    assert await store.is_completed(event_id) is False
    await store.release(event_id)


async def test_claim_fails_on_second_attempt_while_active(pool, event_id):
    store = IdempotencyStore(pool)
    first = await store.claim(event_id=event_id, event_type="test.event", trace_id=None)
    second = await store.claim(event_id=event_id, event_type="test.event", trace_id=None)
    assert first is True
    assert second is False
    await store.release(event_id)


async def test_release_allows_immediate_reclaim(pool, event_id):
    store = IdempotencyStore(pool)
    await store.claim(event_id=event_id, event_type="test.event", trace_id=None)
    await store.release(event_id)
    reclaimed = await store.claim(event_id=event_id, event_type="test.event", trace_id=None)
    assert reclaimed is True
    await store.release(event_id)


async def test_mark_complete_then_claim_is_permanently_blocked(pool, event_id):
    store = IdempotencyStore(pool)
    await store.claim(event_id=event_id, event_type="test.event", trace_id=None)
    await store.mark_complete(event_id)
    assert await store.is_completed(event_id) is True

    # Even after the (long) stale window, a *completed* claim must never
    # be reclaimed — that would mean redoing already-finished, possibly
    # side-effecting work (duplicate Slack message, duplicate report).
    store_impatient = IdempotencyStore(pool, stale_after_seconds=0)
    reclaimed = await store_impatient.claim(
        event_id=event_id, event_type="test.event", trace_id=None
    )
    assert reclaimed is False


async def test_abandoned_claim_is_reclaimed_after_staleness_window(pool, event_id):
    """Simulates a hard process crash: claim() succeeds, but the process
    dies before mark_complete()/release() ever runs (no exception handler
    gets a chance to fire on a SIGKILL). A later attempt — e.g. after
    RabbitMQ redelivers the still-unacked message to a restarted consumer
    — must be able to reclaim it once the claim looks abandoned, or the
    message would be silently dropped from the pipeline's output forever
    even though the broker never lost it."""
    crashed_worker = IdempotencyStore(pool, stale_after_seconds=0.2)
    claimed = await crashed_worker.claim(event_id=event_id, event_type="test.event", trace_id=None)
    assert claimed is True
    # ... process is SIGKILLed here: no release(), no mark_complete() ...

    # Immediately after, a fresh attempt correctly sees it as still active.
    too_soon = await crashed_worker.claim(event_id=event_id, event_type="test.event", trace_id=None)
    assert too_soon is False

    await asyncio.sleep(0.3)  # past the 0.2s staleness window

    restarted_worker = IdempotencyStore(pool, stale_after_seconds=0.2)
    reclaimed = await restarted_worker.claim(
        event_id=event_id, event_type="test.event", trace_id=None
    )
    assert reclaimed is True
    await restarted_worker.mark_complete(event_id)
    assert await restarted_worker.is_completed(event_id) is True
