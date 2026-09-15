"""Structured JSON logging shared by every agent/service.

Usage::

    from shared.logging import configure_logging, trace_context

    log = configure_logging(service_name="ingestion")

    with trace_context(trace_id=envelope.trace_id, correlation_id=envelope.event_id):
        log.info("commit detected", extra={"commit_sha": commit.commit_sha})

Every record is emitted as a single line of JSON on stdout, which is what
container log collectors (Docker, Kubernetes, CloudWatch, etc.) expect —
one event per line, no multi-line stack traces to reassemble.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
import uuid
from datetime import datetime, timezone
from typing import Any

__all__ = [
    "configure_logging",
    "get_logger",
    "trace_context",
    "set_trace_id",
    "get_trace_id",
    "set_correlation_id",
    "get_correlation_id",
]

_trace_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "trace_id", default=None
)
_correlation_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "correlation_id", default=None
)

# Standard attributes every logging.LogRecord carries. Anything else found
# on a record (i.e. passed via `extra={...}`) is treated as structured
# context and merged into the JSON output.
_STANDARD_RECORD_ATTRS = frozenset(
    {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "message",
        "taskName",
    }
)


class JsonFormatter(logging.Formatter):
    """Renders a LogRecord as a single-line JSON object."""

    def __init__(self, service_name: str) -> None:
        super().__init__()
        self.service_name = service_name

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "service": self.service_name,
            "logger": record.name,
            "message": record.getMessage(),
            "trace_id": _trace_id_var.get(),
            "correlation_id": _correlation_id_var.get(),
        }

        for key, value in record.__dict__.items():
            if key not in _STANDARD_RECORD_ATTRS and key not in payload:
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)

        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(
    service_name: str, level: str | int = "INFO"
) -> logging.Logger:
    """Configure the root logger for JSON-on-stdout output and return a
    logger namespaced to ``service_name``.

    Idempotent: safe to call multiple times (e.g. in tests) — it replaces
    the root handler set rather than appending to it.
    """

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service_name))

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)

    return logging.getLogger(service_name)


def get_logger(name: str) -> logging.Logger:
    """Return a child logger. Call `configure_logging` once at process
    startup before using this."""
    return logging.getLogger(name)


def set_trace_id(trace_id: str | None = None) -> str:
    """Bind a trace_id to the current context (e.g. thread/async task).
    Generates one if not supplied. Prefer `trace_context` for scoped use."""
    tid = trace_id or str(uuid.uuid4())
    _trace_id_var.set(tid)
    return tid


def get_trace_id() -> str | None:
    return _trace_id_var.get()


def set_correlation_id(correlation_id: str | None = None) -> str:
    """Bind a correlation_id to the current context. Generates one if not
    supplied. Prefer `trace_context` for scoped use."""
    cid = correlation_id or str(uuid.uuid4())
    _correlation_id_var.set(cid)
    return cid


def get_correlation_id() -> str | None:
    return _correlation_id_var.get()


class trace_context:
    """Context manager that binds trace_id/correlation_id for its scope,
    restoring the previous values on exit. Use one per handled message so
    concurrent handlers (asyncio tasks, threads) don't leak IDs into each
    other's logs.
    """

    def __init__(
        self,
        trace_id: str | None = None,
        correlation_id: str | None = None,
    ) -> None:
        self.trace_id = trace_id or str(uuid.uuid4())
        self.correlation_id = correlation_id or self.trace_id
        self._trace_token: contextvars.Token[str | None] | None = None
        self._correlation_token: contextvars.Token[str | None] | None = None

    def __enter__(self) -> "trace_context":
        self._trace_token = _trace_id_var.set(self.trace_id)
        self._correlation_token = _correlation_id_var.set(self.correlation_id)
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._trace_token is not None:
            _trace_id_var.reset(self._trace_token)
        if self._correlation_token is not None:
            _correlation_id_var.reset(self._correlation_token)
