import time

import pytest

from shared.breaker import CircuitBreaker, CircuitOpenError, CircuitState


async def _ok() -> str:
    return "ok"


async def _fail() -> str:
    raise RuntimeError("boom")


async def test_breaker_starts_closed():
    breaker = CircuitBreaker(name="t", failure_threshold=5, open_duration_seconds=60)
    assert breaker.state is CircuitState.CLOSED


async def test_breaker_opens_after_consecutive_failures():
    breaker = CircuitBreaker(name="t2", failure_threshold=5, open_duration_seconds=60)
    for _ in range(4):
        with pytest.raises(RuntimeError):
            await breaker.call(_fail)
        assert breaker.state is CircuitState.CLOSED

    with pytest.raises(RuntimeError):
        await breaker.call(_fail)
    assert breaker.state is CircuitState.OPEN
    assert breaker.metrics.failure_count == 5
    assert breaker.metrics.open_count == 1


async def test_breaker_fails_fast_while_open():
    breaker = CircuitBreaker(name="t3", failure_threshold=1, open_duration_seconds=60)
    with pytest.raises(RuntimeError):
        await breaker.call(_fail)
    assert breaker.state is CircuitState.OPEN

    with pytest.raises(CircuitOpenError):
        await breaker.call(_ok)


async def test_breaker_success_resets_consecutive_failure_count():
    breaker = CircuitBreaker(name="t4", failure_threshold=3, open_duration_seconds=60)
    with pytest.raises(RuntimeError):
        await breaker.call(_fail)
    with pytest.raises(RuntimeError):
        await breaker.call(_fail)
    assert breaker.state is CircuitState.CLOSED

    await breaker.call(_ok)
    assert breaker._consecutive_failures == 0

    # Two more failures shouldn't be enough to open now the streak reset.
    with pytest.raises(RuntimeError):
        await breaker.call(_fail)
    with pytest.raises(RuntimeError):
        await breaker.call(_fail)
    assert breaker.state is CircuitState.CLOSED


async def test_breaker_half_open_after_open_duration_elapses():
    breaker = CircuitBreaker(name="t5", failure_threshold=1, open_duration_seconds=0.05)
    with pytest.raises(RuntimeError):
        await breaker.call(_fail)
    assert breaker.state is CircuitState.OPEN

    time.sleep(0.06)
    assert breaker.state is CircuitState.HALF_OPEN


async def test_breaker_half_open_success_recovers_to_closed():
    breaker = CircuitBreaker(name="t6", failure_threshold=1, open_duration_seconds=0.05)
    with pytest.raises(RuntimeError):
        await breaker.call(_fail)
    time.sleep(0.06)
    assert breaker.state is CircuitState.HALF_OPEN

    result = await breaker.call(_ok)
    assert result == "ok"
    assert breaker.state is CircuitState.CLOSED
    assert breaker.metrics.recovery_count == 1


async def test_breaker_half_open_failure_reopens():
    breaker = CircuitBreaker(name="t7", failure_threshold=1, open_duration_seconds=0.05)
    with pytest.raises(RuntimeError):
        await breaker.call(_fail)
    time.sleep(0.06)
    assert breaker.state is CircuitState.HALF_OPEN

    with pytest.raises(RuntimeError):
        await breaker.call(_fail)
    assert breaker.state is CircuitState.OPEN
    assert breaker.metrics.open_count == 2
