"""Phase 4: retry/backoff/classification/circuit-breaker coverage for
shared/llm.py, on top of tests/test_llm.py's provider request/response
shape coverage. Every client here is constructed with
``rate_limit_enabled=False`` to keep these hermetic (no Redis dependency)
and with tiny backoff bounds so retry tests run in well under a second."""

import pytest

from shared.breaker import CircuitState, get_circuit_breaker
from shared.llm import AnthropicClient, LLMError


def _fast_client(**overrides) -> AnthropicClient:
    kwargs = {
        "api_key": "sk-test",
        "model": "retry-test-model",
        "rate_limit_enabled": False,
        "retry_backoff_seconds": 0.001,
        "retry_backoff_max_seconds": 0.01,
    }
    kwargs.update(overrides)
    return AnthropicClient(**kwargs)


async def test_retries_on_retryable_status_then_succeeds(httpx_mock):
    httpx_mock.add_response(url="https://api.anthropic.com/v1/messages", status_code=503)
    httpx_mock.add_response(url="https://api.anthropic.com/v1/messages", status_code=503)
    httpx_mock.add_response(
        url="https://api.anthropic.com/v1/messages",
        json={
            "model": "retry-test-model",
            "content": [{"type": "text", "text": "ok after retries"}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "stop_reason": "end_turn",
        },
    )
    client = _fast_client(max_retries=3)
    response = await client.complete(system="s", prompt="p", max_tokens=10)
    assert response.text == "ok after retries"


async def test_exhausts_retries_and_raises_llm_error(httpx_mock):
    for _ in range(3):
        httpx_mock.add_response(url="https://api.anthropic.com/v1/messages", status_code=503)
    client = _fast_client(max_retries=2)
    with pytest.raises(LLMError):
        await client.complete(system="s", prompt="p", max_tokens=10)
    # 1 initial attempt + 2 retries = 3 requests total.
    assert len(httpx_mock.get_requests()) == 3


async def test_non_retryable_status_fails_immediately(httpx_mock):
    httpx_mock.add_response(url="https://api.anthropic.com/v1/messages", status_code=400)
    client = _fast_client(max_retries=5)
    with pytest.raises(LLMError):
        await client.complete(system="s", prompt="p", max_tokens=10)
    # No retries spent on a non-retryable (poison) response.
    assert len(httpx_mock.get_requests()) == 1


async def test_malformed_response_fails_immediately(httpx_mock):
    httpx_mock.add_response(url="https://api.anthropic.com/v1/messages", json={"nope": True})
    client = _fast_client(max_retries=5)
    with pytest.raises(LLMError):
        await client.complete(system="s", prompt="p", max_tokens=10)
    assert len(httpx_mock.get_requests()) == 1


async def test_backoff_delay_grows_and_is_capped():
    client = _fast_client(retry_backoff_seconds=1.0, retry_backoff_max_seconds=4.0)
    # Full-jitter delay is in [0, ceiling); sample many times and check
    # the ceiling grows with attempt number, then caps.
    ceilings = []
    for attempt in (1, 2, 3, 10):
        samples = [client._backoff_delay(attempt) for _ in range(200)]
        ceilings.append(max(samples))
    assert ceilings[0] <= 1.0 + 1e-9
    assert ceilings[1] <= 2.0 + 1e-9
    assert ceilings[2] <= 4.0 + 1e-9
    assert ceilings[3] <= 4.0 + 1e-9  # capped at retry_backoff_max_seconds


async def test_circuit_breaker_opens_after_repeated_llm_failures(httpx_mock):
    for _ in range(5):
        httpx_mock.add_response(url="https://api.anthropic.com/v1/messages", status_code=503)
    client = _fast_client(max_retries=0, breaker_failure_threshold=5, breaker_open_seconds=60)
    for _ in range(5):
        with pytest.raises(LLMError):
            await client.complete(system="s", prompt="p", max_tokens=10)

    breaker = get_circuit_breaker("anthropic")
    assert breaker.state is CircuitState.OPEN

    # A 6th call should fail fast without hitting the network at all.
    with pytest.raises(LLMError):
        await client.complete(system="s", prompt="p", max_tokens=10)
    assert len(httpx_mock.get_requests()) == 5
