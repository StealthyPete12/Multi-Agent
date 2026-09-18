import httpx
import pytest

from shared.errors import (
    FatalError,
    PoisonMessageError,
    RetryableError,
    classify_exception,
    classify_http_status,
)


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504, 599])
def test_classify_http_status_retryable(status):
    assert classify_http_status(status) is True


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_classify_http_status_not_retryable(status):
    assert classify_http_status(status) is False


def _http_status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://example.test/x")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError("boom", request=request, response=response)


def test_classify_exception_retryable_status():
    assert classify_exception(_http_status_error(503)) is RetryableError


def test_classify_exception_poison_status():
    assert classify_exception(_http_status_error(400)) is PoisonMessageError


def test_classify_exception_timeout_is_retryable():
    exc = httpx.TimeoutException("timed out")
    assert classify_exception(exc) is RetryableError


def test_classify_exception_connect_error_is_retryable():
    exc = httpx.ConnectError("connection refused")
    assert classify_exception(exc) is RetryableError


def test_classify_exception_passthrough_of_typed_errors():
    assert classify_exception(RetryableError("x")) is RetryableError
    assert classify_exception(PoisonMessageError("x")) is PoisonMessageError
    assert classify_exception(FatalError("x")) is FatalError


def test_classify_exception_unknown_is_fatal():
    assert classify_exception(ValueError("unexpected")) is FatalError
