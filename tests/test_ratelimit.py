import uuid

import pytest

from shared.ratelimit import RateLimiter, get_redis_client


@pytest.fixture
def bucket_key() -> str:
    # Unique per test so concurrent/rerun test sessions never share state.
    return f"test:{uuid.uuid4()}"


async def test_acquire_allows_within_capacity(redis_available, bucket_key):
    if not redis_available:
        pytest.skip("redis not reachable")
    limiter = RateLimiter(get_redis_client(), tokens_per_minute=60, capacity=5)
    decision = await limiter.acquire(bucket_key, tokens=1)
    assert decision.allowed is True
    assert decision.remaining_tokens == pytest.approx(4, abs=0.01)


async def test_acquire_denies_once_capacity_exhausted(redis_available, bucket_key):
    if not redis_available:
        pytest.skip("redis not reachable")
    limiter = RateLimiter(get_redis_client(), tokens_per_minute=60, capacity=2)
    first = await limiter.acquire(bucket_key, tokens=1)
    second = await limiter.acquire(bucket_key, tokens=1)
    third = await limiter.acquire(bucket_key, tokens=1)
    assert first.allowed is True
    assert second.allowed is True
    assert third.allowed is False
    assert third.wait_seconds > 0


async def test_wait_and_acquire_raises_timeout_when_budget_too_small(redis_available, bucket_key):
    if not redis_available:
        pytest.skip("redis not reachable")
    # 1 token/minute capacity 1: first call succeeds, second needs ~60s to
    # refill, which exceeds a tiny max_wait_seconds budget.
    limiter = RateLimiter(
        get_redis_client(), tokens_per_minute=1, capacity=1, max_wait_seconds=0.2
    )
    first = await limiter.acquire(bucket_key, tokens=1)
    assert first.allowed is True

    with pytest.raises(TimeoutError):
        await limiter.wait_and_acquire(bucket_key, tokens=1)


async def test_wait_and_acquire_succeeds_once_refilled(redis_available, bucket_key):
    if not redis_available:
        pytest.skip("redis not reachable")
    # High refill rate so the wait is sub-second in test time.
    limiter = RateLimiter(
        get_redis_client(), tokens_per_minute=6000, capacity=1, max_wait_seconds=5
    )
    await limiter.acquire(bucket_key, tokens=1)  # drain the single token
    decision = await limiter.wait_and_acquire(bucket_key, tokens=1)
    assert decision.allowed is True


async def test_buckets_are_scoped_independently(redis_available):
    if not redis_available:
        pytest.skip("redis not reachable")
    limiter = RateLimiter(get_redis_client(), tokens_per_minute=60, capacity=1)
    key_a = f"test:{uuid.uuid4()}"
    key_b = f"test:{uuid.uuid4()}"
    a1 = await limiter.acquire(key_a, tokens=1)
    b1 = await limiter.acquire(key_b, tokens=1)
    assert a1.allowed is True
    assert b1.allowed is True
