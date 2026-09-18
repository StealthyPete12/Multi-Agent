"""In-process circuit breaker, integrated into every LLM call
(``shared/llm.py``).

Three states, the standard circuit-breaker state machine:

- **CLOSED** — normal operation. Calls pass through; consecutive
  failures are counted.
- **OPEN** — after ``failure_threshold`` consecutive failures, the
  breaker trips. Every call fails fast with :class:`CircuitOpenError`
  (no network call attempted) for ``open_duration_seconds``.
- **HALF_OPEN** — once the open duration elapses, the next call is let
  through as a trial. Success closes the breaker (reset); failure
  reopens it for another full ``open_duration_seconds``.

Scoped per provider (one breaker instance per ``LLMClient``), so one
provider's outage doesn't trip a breaker guarding a different provider.

Metrics (``failure_count``, ``open_count``, ``recovery_count``) are
plain instance counters, logged on every state transition — enough for
`tests/test_breaker.py` and the chaos-test scenario to assert against
without needing a metrics backend.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Awaitable, Callable, TypeVar

from shared import telemetry
from shared.logging import configure_logging

__all__ = ["CircuitState", "CircuitBreaker", "CircuitOpenError", "BreakerMetrics"]

log = configure_logging(service_name="breaker")

T = TypeVar("T")


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(RuntimeError):
    """Raised instead of attempting a call while the breaker is OPEN."""


@dataclass
class BreakerMetrics:
    failure_count: int = 0
    open_count: int = 0
    recovery_count: int = 0


@dataclass
class CircuitBreaker:
    """One breaker instance per protected resource (e.g. one per LLM
    provider). Not distributed — in-process only, matching the roadmap's
    "integrate into LLM calls" scope (a single agent process's view of a
    single provider's health)."""

    name: str
    failure_threshold: int = 5
    open_duration_seconds: float = 60.0

    _state: CircuitState = field(default=CircuitState.CLOSED, init=False)
    _consecutive_failures: int = field(default=0, init=False)
    _opened_at: float | None = field(default=None, init=False)
    metrics: BreakerMetrics = field(default_factory=BreakerMetrics, init=False)

    @property
    def state(self) -> CircuitState:
        """Current state, lazily transitioning OPEN -> HALF_OPEN once the
        open duration has elapsed (evaluated on read, not by a timer)."""
        if self._state is CircuitState.OPEN and self._opened_at is not None:
            if time.monotonic() - self._opened_at >= self.open_duration_seconds:
                self._state = CircuitState.HALF_OPEN
                log.info(
                    "circuit breaker half-open",
                    extra={"breaker": self.name, "circuit_state": self._state.value},
                )
        return self._state

    def _record_success(self) -> None:
        if self._state is CircuitState.HALF_OPEN:
            self.metrics.recovery_count += 1
            log.info(
                "circuit breaker recovered",
                extra={"breaker": self.name, "circuit_state": CircuitState.CLOSED.value},
            )
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at = None

    def _record_failure(self) -> None:
        self.metrics.failure_count += 1
        if self._state is CircuitState.HALF_OPEN:
            # Trial failed: straight back to OPEN for another full window.
            self._open()
            return

        self._consecutive_failures += 1
        if self._consecutive_failures >= self.failure_threshold:
            self._open()

    def _open(self) -> None:
        self._state = CircuitState.OPEN
        self._opened_at = time.monotonic()
        self.metrics.open_count += 1
        log.warning(
            "circuit breaker open",
            extra={
                "breaker": self.name,
                "circuit_state": self._state.value,
                "consecutive_failures": self._consecutive_failures,
                "open_count": self.metrics.open_count,
            },
        )
        telemetry.get_metrics().breaker_opens.add(1, {"breaker": self.name})

    async def call(self, fn: Callable[[], Awaitable[T]]) -> T:
        """Run ``fn()`` through the breaker: fails fast with
        :class:`CircuitOpenError` while OPEN, otherwise runs it and
        records success/failure."""
        current = self.state
        if current is CircuitState.OPEN:
            raise CircuitOpenError(f"circuit '{self.name}' is open")

        try:
            result = await fn()
        except Exception:
            self._record_failure()
            raise
        else:
            self._record_success()
            return result


_breakers: dict[str, CircuitBreaker] = {}


def get_circuit_breaker(name: str, **kwargs) -> CircuitBreaker:
    """Process-wide breaker instance per ``name`` (e.g. provider id), so
    every caller for the same provider shares failure-count state."""
    if name not in _breakers:
        _breakers[name] = CircuitBreaker(name=name, **kwargs)
    return _breakers[name]
