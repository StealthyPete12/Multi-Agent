"""Error classification shared by every consumer and by ``shared/llm.py``.

Phase 4 requires every failure a consumer can hit to be classified into
exactly one of three buckets, each with a distinct routing outcome (see
``shared/retry.py`` for how a consumer acts on the classification):

- :class:`RetryableError` — transient, worth retrying (network blip, rate
  limit, upstream 5xx/timeout/unavailable). Routed to the retry ladder
  (``q.retry.5s`` -> ``q.retry.30s`` -> ``q.retry.5m`` -> ``q.dlq``).
- :class:`PoisonMessageError` — the message itself is unrecoverable
  (invalid schema, unknown contract version, malformed payload). Retrying
  can never succeed, so it is routed straight to the dead-letter queue
  with zero retry attempts.
- :class:`FatalError` — the *service* can't function (bad configuration,
  invalid credentials, a startup failure). Not a property of one message;
  the consumer logs it and stops rather than burning through every queued
  message the same way.

``classify_exception`` centralizes the mapping from "raw" failures (HTTP
status codes, network exceptions) to these three types, so call sites
(``shared/llm.py``, agent consumers) don't each reimplement the same
if/elif ladder over ``httpx`` exception types.
"""

from __future__ import annotations

import httpx

__all__ = [
    "RetryableError",
    "PoisonMessageError",
    "FatalError",
    "classify_exception",
    "classify_http_status",
    "RETRYABLE_STATUS_CODES",
]


class RetryableError(Exception):
    """Transient failure — safe to retry with backoff.

    Examples: HTTP 429/5xx, connection/timeout errors, "service
    unavailable". A future attempt at the same work may succeed once the
    transient condition clears.
    """


class PoisonMessageError(Exception):
    """The message itself is unrecoverable — retrying can never succeed.

    Examples: contract/schema validation failure, an unknown
    ``schema_version``, a malformed payload. Routed straight to the DLQ,
    no retry attempts spent on it.
    """


class FatalError(Exception):
    """The service itself can't operate — not a per-message failure.

    Examples: missing/invalid configuration, invalid credentials, a
    dependency unreachable at startup. The consumer should log this and
    stop rather than repeatedly failing every message the same way.
    """


# HTTP status codes that represent a transient condition worth retrying.
# 429 (rate limited) and every 5xx (server-side failure) qualify; 4xx
# other than 429 is a client-side/request problem that won't change on
# retry and is treated as poison by callers that classify HTTP responses.
RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})


def classify_http_status(status_code: int) -> bool:
    """True if an HTTP response with this status code should be retried."""
    return status_code in RETRYABLE_STATUS_CODES or status_code >= 500


def classify_exception(exc: BaseException) -> type[RetryableError | PoisonMessageError | FatalError]:
    """Map a raw exception (typically from an HTTP client) to one of the
    three error categories.

    Network failures, timeouts, and retryable HTTP statuses classify as
    :class:`RetryableError`. An HTTP response with a non-retryable status
    (4xx other than 429) classifies as :class:`PoisonMessageError` — the
    request itself was rejected and won't succeed unchanged. Anything
    else (unexpected exception types) classifies as :class:`FatalError`
    so it surfaces loudly instead of being silently retried forever.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        status_code = exc.response.status_code
        return RetryableError if classify_http_status(status_code) else PoisonMessageError

    if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError, httpx.RemoteProtocolError)):
        return RetryableError

    if isinstance(exc, httpx.HTTPError):
        # Any other httpx-level failure (proxy errors, protocol errors,
        # etc.) is treated as a transient network condition.
        return RetryableError

    if isinstance(exc, (RetryableError, PoisonMessageError, FatalError)):
        return type(exc)

    return FatalError
