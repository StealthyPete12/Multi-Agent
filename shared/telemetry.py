"""Shared OpenTelemetry initialization: tracing, metrics, and context
propagation for every service in the pipeline.

Usage (once per process, at service startup)::

    from shared import telemetry

    tracer, meter = telemetry.init_telemetry("researcher")
    ...
    telemetry.shutdown_telemetry()  # on graceful shutdown, flushes exporters

Everywhere else, get the process-wide tracer/meter/metrics registry without
re-initializing::

    from shared import telemetry

    with telemetry.span("repository.clone", attributes={"repo": repo}):
        ...

    telemetry.get_metrics().repo_clones.add(1, {"repo": repo})

Exporters are entirely environment-driven (``OTEL_TRACES_EXPORTER`` /
``OTEL_METRICS_EXPORTER``: ``otlp_http`` | ``otlp_grpc`` | ``console`` |
``none``/``prometheus`` for metrics), so switching from "print spans to
stdout" (tests, offline dev) to "ship to Phoenix" (docker compose) to
"ship to any other OTLP collector" is a config change, never a code
change. Metrics default to a Prometheus exposition endpoint
(``/metrics`` via ``prometheus_client``) since that's what
``docker-compose.yml``'s Prometheus service scrapes.

Calling :func:`init_telemetry` more than once (e.g. across tests in the
same process) is safe — the global tracer/meter providers are installed
exactly once; later calls return the same tracer/meter.
"""

from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Iterator, Mapping, MutableMapping

from opentelemetry import metrics, trace
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from opentelemetry.propagate import extract as _propagate_extract
from opentelemetry.propagate import inject as _propagate_inject
from opentelemetry.propagate import set_global_textmap
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import ConsoleMetricExporter, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.trace import Span, SpanKind, Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

__all__ = [
    "init_telemetry",
    "shutdown_telemetry",
    "get_tracer",
    "get_meter",
    "get_metrics",
    "Metrics",
    "span",
    "producer_span",
    "consumer_span",
    "inject_headers",
    "extract_context",
    "current_trace_id",
    "current_span_id",
    "mark_span_error",
    "estimate_cost_usd",
    "record_llm_success",
    "record_llm_failure",
    "register_pool_gauges",
]

_SERVICE_NAMESPACE = "code-review-swarm"
_DEFAULT_PROMETHEUS_PORT = 9464

_init_lock = threading.Lock()
_tracer_provider_ready = False
_meter_provider_ready = False
_metrics_http_server_started = False
_metrics_singleton: "Metrics | None" = None


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("false", "0", "")


def _resource(service_name: str) -> Resource:
    return Resource.create(
        {
            "service.name": os.environ.get("OTEL_SERVICE_NAME", service_name),
            "service.namespace": _SERVICE_NAMESPACE,
            "deployment.environment": os.environ.get("ENVIRONMENT", "development"),
        }
    )


def _build_span_exporter():
    kind = os.environ.get("OTEL_TRACES_EXPORTER", "otlp_http").strip().lower()
    if kind == "none":
        return None
    if kind == "console":
        return ConsoleSpanExporter()
    if kind in ("otlp_grpc", "otlp-grpc", "grpc"):
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter as _GrpcSpanExporter,
        )

        return _GrpcSpanExporter(insecure=_env_bool("OTEL_EXPORTER_OTLP_INSECURE", True))
    if kind in ("otlp", "otlp_http", "otlp-http", "http"):
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter as _HttpSpanExporter,
        )

        return _HttpSpanExporter()
    return ConsoleSpanExporter()


def _build_metric_readers(metrics_port: int | None, start_metrics_server: bool) -> list:
    kind = os.environ.get("OTEL_METRICS_EXPORTER", "prometheus").strip().lower()
    if kind == "none":
        return []
    if kind == "console":
        return [PeriodicExportingMetricReader(ConsoleMetricExporter())]
    if kind in ("otlp_grpc", "otlp-grpc", "grpc"):
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
            OTLPMetricExporter as _GrpcMetricExporter,
        )

        return [
            PeriodicExportingMetricReader(
                _GrpcMetricExporter(insecure=_env_bool("OTEL_EXPORTER_OTLP_INSECURE", True))
            )
        ]
    if kind in ("otlp", "otlp_http", "otlp-http", "http"):
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
            OTLPMetricExporter as _HttpMetricExporter,
        )

        return [PeriodicExportingMetricReader(_HttpMetricExporter())]

    # Default: prometheus. Reads are pulled by Prometheus scraping
    # /metrics, exposed by prometheus_client's own tiny HTTP server
    # (start_metrics_server=False lets a caller that already runs an HTTP
    # server, e.g. the watcher's FastAPI app, mount /metrics itself
    # instead of opening a second port).
    from opentelemetry.exporter.prometheus import PrometheusMetricReader

    reader = PrometheusMetricReader()
    global _metrics_http_server_started
    if start_metrics_server and not _metrics_http_server_started:
        from prometheus_client import start_http_server

        port = metrics_port or int(os.environ.get("OTEL_EXPORTER_PROMETHEUS_PORT", _DEFAULT_PROMETHEUS_PORT))
        host = os.environ.get("OTEL_EXPORTER_PROMETHEUS_HOST", "0.0.0.0")
        start_http_server(port=port, addr=host)
        _metrics_http_server_started = True
    return [reader]


def init_telemetry(
    service_name: str,
    *,
    metrics_port: int | None = None,
    start_metrics_server: bool = True,
) -> tuple[trace.Tracer, metrics.Meter]:
    """Install the process-wide TracerProvider/MeterProvider (once) and
    return a tracer/meter scoped to ``service_name``.

    ``start_metrics_server=False`` is for services that already run their
    own HTTP server (the watcher) and want to mount the Prometheus
    exposition endpoint themselves via ``prometheus_client.make_asgi_app()``
    rather than opening a second port.
    """
    global _tracer_provider_ready, _meter_provider_ready

    with _init_lock:
        resource = _resource(service_name)

        if not _tracer_provider_ready:
            provider = TracerProvider(resource=resource)
            exporter = _build_span_exporter()
            if exporter is not None:
                provider.add_span_processor(BatchSpanProcessor(exporter))
            trace.set_tracer_provider(provider)
            set_global_textmap(
                CompositePropagator([TraceContextTextMapPropagator(), W3CBaggagePropagator()])
            )
            _tracer_provider_ready = True

        if not _meter_provider_ready:
            readers = _build_metric_readers(metrics_port, start_metrics_server)
            provider = MeterProvider(resource=resource, metric_readers=readers)
            metrics.set_meter_provider(provider)
            _meter_provider_ready = True

    tracer = trace.get_tracer(service_name)
    meter = metrics.get_meter(service_name)
    get_metrics()  # eagerly create instruments so the first scrape isn't empty
    return tracer, meter


def shutdown_telemetry() -> None:
    """Flush and shut down the tracer/meter providers. Call once at
    process exit so buffered spans/metrics aren't lost."""
    provider = trace.get_tracer_provider()
    shutdown = getattr(provider, "shutdown", None)
    if callable(shutdown):
        shutdown()
    meter_provider = metrics.get_meter_provider()
    shutdown = getattr(meter_provider, "shutdown", None)
    if callable(shutdown):
        shutdown()


def get_tracer(name: str = "swarm") -> trace.Tracer:
    return trace.get_tracer(name)


def get_meter(name: str = "swarm") -> metrics.Meter:
    return metrics.get_meter(name)


# ---------------------------------------------------------------------------
# Metrics catalog — every counter/histogram the roadmap asked for, created
# once (lazily, off whatever meter provider is currently installed — a
# real one after init_telemetry(), or the SDK's inert no-op provider
# before it, which keeps `get_metrics()` safe to call from tests that
# never call init_telemetry()).
# ---------------------------------------------------------------------------


@dataclass
class Metrics:
    meter: metrics.Meter

    def __post_init__(self) -> None:
        m = self.meter
        # Event/pipeline throughput
        self.events_processed = m.create_counter(
            "swarm_events_processed_total", unit="1", description="Envelopes published or consumed, by event_type/direction"
        )
        self.commit_events = m.create_counter(
            "swarm_commit_events_total", unit="1", description="commit.detected events received"
        )
        self.findings_events = m.create_counter(
            "swarm_findings_events_total", unit="1", description="findings.ready events received/published"
        )
        self.review_events = m.create_counter(
            "swarm_review_events_total", unit="1", description="review.completed events published"
        )
        # Fault tolerance
        self.dlq_count = m.create_counter("swarm_dlq_total", unit="1", description="Messages routed to q.dlq")
        self.retry_count = m.create_counter(
            "swarm_retry_total", unit="1", description="Messages routed to a retry-ladder rung"
        )
        self.breaker_opens = m.create_counter(
            "swarm_circuit_breaker_opens_total", unit="1", description="Circuit breaker CLOSED/HALF_OPEN -> OPEN transitions"
        )
        self.rate_limit_delays = m.create_counter(
            "swarm_rate_limit_delays_total", unit="1", description="LLM calls delayed by the rate limiter"
        )
        # Metric *names* below deliberately omit their unit (e.g.
        # "swarm_review_duration", not "swarm_review_duration_ms"): the
        # Prometheus exporter already appends a unit suffix derived from
        # `unit=` (ms -> "_milliseconds", s -> "_seconds") — naming the
        # unit in both places would produce
        # "swarm_review_duration_ms_milliseconds". The Python attribute
        # keeps its "_ms"/"_seconds" suffix purely as a call-site
        # readability aid; it isn't the wire name.
        self.rate_limit_delay_seconds = m.create_histogram(
            "swarm_rate_limit_delay", unit="s", description="Time spent waiting on the rate limiter"
        )
        # LLM observability
        self.llm_calls = m.create_counter(
            "swarm_llm_calls_total", unit="1", description="LLM completion attempts, by provider/model/outcome"
        )
        self.llm_failures = m.create_counter(
            "swarm_llm_failures_total", unit="1", description="LLM completion failures, by provider/model/reason"
        )
        self.llm_tokens_in = m.create_counter(
            "swarm_llm_tokens_in_total", unit="1", description="LLM input tokens consumed"
        )
        self.llm_tokens_out = m.create_counter(
            "swarm_llm_tokens_out_total", unit="1", description="LLM output tokens generated"
        )
        self.llm_cost_usd = m.create_counter(
            "swarm_llm_cost_usd_total", unit="usd", description="Estimated LLM spend (see PRICING_PER_1M_TOKENS_USD)"
        )
        self.llm_duration_ms = m.create_histogram(
            "swarm_llm_duration", unit="ms", description="LLM completion latency"
        )
        # Slack
        self.slack_deliveries = m.create_counter(
            "swarm_slack_deliveries_total", unit="1", description="Slack notification attempts, by outcome"
        )
        # Repository
        self.repo_clones = m.create_counter(
            "swarm_repository_clones_total", unit="1", description="Fresh `git clone` operations"
        )
        self.repo_cache_hits = m.create_counter(
            "swarm_repository_cache_hits_total", unit="1", description="Repository cache hits (no clone needed)"
        )
        self.repo_clone_duration_ms = m.create_histogram(
            "swarm_repository_clone_duration", unit="ms", description="`git clone` duration"
        )
        self.repo_refresh_duration_ms = m.create_histogram(
            "swarm_repository_refresh_duration", unit="ms", description="`git fetch` (refresh) duration"
        )
        # Analysis/review durations
        self.blast_radius_duration_ms = m.create_histogram(
            "swarm_blast_radius_duration", unit="ms", description="Blast-radius recursive-CTE query duration"
        )
        self.review_duration_ms = m.create_histogram(
            "swarm_review_duration", unit="ms", description="End-to-end findings.ready -> review.completed duration"
        )
        # Postgres
        self.db_write_duration_ms = m.create_histogram(
            "swarm_db_write_duration", unit="ms", description="Postgres write duration, by table/operation"
        )
        self.db_query_duration_ms = m.create_histogram(
            "swarm_db_query_duration", unit="ms", description="Postgres read/query duration"
        )
        self.db_failures = m.create_counter(
            "swarm_db_failures_total", unit="1", description="Postgres operations that raised"
        )


def get_metrics() -> Metrics:
    global _metrics_singleton
    if _metrics_singleton is None:
        _metrics_singleton = Metrics(meter=get_meter("swarm"))
    return _metrics_singleton


def register_pool_gauges(component: str, pool_getter: Callable[[], object | None]) -> None:
    """Register observable gauges for an asyncpg pool's in-use/idle/size,
    sampled at scrape time via ``pool_getter()`` (returns ``None`` before
    ``connect()`` — the callback just reports nothing that tick).

    ``component`` distinguishes the researcher's ``Database`` from the
    reviewer's ``ReviewStorage`` in the ``component`` label.
    """
    meter = get_meter("swarm")

    def _callback(options):
        pool = pool_getter()
        if pool is None:
            return
        size = pool.get_size()
        idle = pool.get_idle_size()
        attrs = {"component": component}
        yield metrics.Observation(size, {**attrs, "state": "total"})
        yield metrics.Observation(idle, {**attrs, "state": "idle"})
        yield metrics.Observation(max(size - idle, 0), {**attrs, "state": "in_use"})

    meter.create_observable_gauge(
        "swarm_db_pool_connections",
        callbacks=[_callback],
        unit="1",
        description="asyncpg connection pool size (total/idle/in_use)",
    )


# ---------------------------------------------------------------------------
# Span helpers
# ---------------------------------------------------------------------------


def mark_span_error(current_span: Span, exc: BaseException) -> None:
    current_span.record_exception(exc)
    current_span.set_status(Status(StatusCode.ERROR, str(exc)))


@contextmanager
def span(
    name: str,
    *,
    kind: SpanKind = SpanKind.INTERNAL,
    attributes: Mapping[str, object] | None = None,
    tracer_name: str = "swarm",
) -> Iterator[Span]:
    """Generic internal span. ``start_as_current_span`` already records an
    exception event and sets an ERROR status by default when one
    propagates out of the block, so a failed step is visible in Phoenix
    without every call site having to do that bookkeeping."""
    tracer = get_tracer(tracer_name)
    with tracer.start_as_current_span(name, kind=kind, attributes=dict(attributes or {})) as current_span:
        yield current_span


def inject_headers(headers: MutableMapping[str, str]) -> None:
    """Inject the current span's W3C traceparent/tracestate into an AMQP
    message headers dict (mutated in place) before publishing."""
    _propagate_inject(headers)


def extract_context(headers: Mapping[str, object] | None):
    """Build an OTel Context from an inbound AMQP message's headers
    (whatever :func:`inject_headers` wrote on the publishing side)."""
    if not headers:
        return _propagate_extract({})
    # aio-pika header values may come back as bytes/int/etc depending on
    # the AMQP field-table decoder; the propagator only understands str.
    string_headers = {k: v.decode() if isinstance(v, bytes) else str(v) for k, v in headers.items()}
    return _propagate_extract(string_headers)


@contextmanager
def producer_span(
    name: str,
    headers: MutableMapping[str, str],
    *,
    attributes: Mapping[str, object] | None = None,
    tracer_name: str = "shared.broker",
) -> Iterator[Span]:
    """Open a PRODUCER span and inject its context into ``headers`` (the
    AMQP message headers dict, mutated in place) so the consuming service
    continues the same trace."""
    tracer = get_tracer(tracer_name)
    with tracer.start_as_current_span(
        name, kind=SpanKind.PRODUCER, attributes=dict(attributes or {})
    ) as current_span:
        inject_headers(headers)
        yield current_span


@contextmanager
def consumer_span(
    name: str,
    headers: Mapping[str, object] | None,
    *,
    attributes: Mapping[str, object] | None = None,
    tracer_name: str = "shared.broker",
) -> Iterator[Span]:
    """Open a CONSUMER span as a child of the remote PRODUCER span whose
    context was injected into ``headers`` — this is what makes a trace
    survive a RabbitMQ hop instead of starting a new, orphaned one."""
    tracer = get_tracer(tracer_name)
    parent_ctx = extract_context(headers)
    with tracer.start_as_current_span(
        name, context=parent_ctx, kind=SpanKind.CONSUMER, attributes=dict(attributes or {})
    ) as current_span:
        yield current_span


def current_trace_id() -> str | None:
    ctx = trace.get_current_span().get_span_context()
    if not ctx.is_valid:
        return None
    return format(ctx.trace_id, "032x")


def current_span_id() -> str | None:
    ctx = trace.get_current_span().get_span_context()
    if not ctx.is_valid:
        return None
    return format(ctx.span_id, "016x")


# ---------------------------------------------------------------------------
# LLM cost estimation + usage recording
# ---------------------------------------------------------------------------

# USD per 1M tokens, (input, output). Approximate list pricing at time of
# writing — not a billing source of truth. Override per model with
# LLM_PRICE_<MODEL>_IN_PER_1M / LLM_PRICE_<MODEL>_OUT_PER_1M (model name
# upper-cased, non-alphanumerics -> "_"), e.g.
# LLM_PRICE_CLAUDE_HAIKU_4_5_20251001_IN_PER_1M=1.00
PRICING_PER_1M_TOKENS_USD: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5-20251001": (1.00, 5.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-opus-5": (15.00, 75.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
}


def _env_price_override(model: str) -> tuple[float, float] | None:
    key = "".join(c if c.isalnum() else "_" for c in model.upper())
    in_raw = os.environ.get(f"LLM_PRICE_{key}_IN_PER_1M")
    out_raw = os.environ.get(f"LLM_PRICE_{key}_OUT_PER_1M")
    if in_raw is None and out_raw is None:
        return None
    try:
        return float(in_raw or 0), float(out_raw or 0)
    except ValueError:
        return None


def estimate_cost_usd(provider: str, model: str, input_tokens: int, output_tokens: int) -> float | None:
    """Best-effort USD cost estimate from a static price list (env
    overridable). Returns ``None`` for a local/free provider (Ollama) or
    an unrecognized model — callers should skip recording cost rather
    than reporting a misleading 0."""
    if provider == "ollama":
        return 0.0
    prices = _env_price_override(model) or PRICING_PER_1M_TOKENS_USD.get(model)
    if prices is None:
        return None
    price_in, price_out = prices
    return (input_tokens / 1_000_000) * price_in + (output_tokens / 1_000_000) * price_out


def record_llm_success(
    *,
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    latency_ms: float,
) -> None:
    """Update LLM metrics/span attributes after a successful completion.
    Called from ``shared/llm.py::_BaseLLMClient._log_usage`` — see that
    module for why this stays a "small touch" rather than a bigger change."""
    m = get_metrics()
    attrs = {"provider": provider, "model": model}
    m.llm_tokens_in.add(input_tokens, attrs)
    m.llm_tokens_out.add(output_tokens, attrs)
    m.llm_duration_ms.record(latency_ms, attrs)
    cost = estimate_cost_usd(provider, model, input_tokens, output_tokens)
    span_attrs: dict[str, object] = {
        "llm.provider": provider,
        "llm.model": model,
        "llm.tokens.input": input_tokens,
        "llm.tokens.output": output_tokens,
        "llm.duration_ms": latency_ms,
        "llm.status": "success",
    }
    if cost is not None:
        m.llm_cost_usd.add(cost, attrs)
        span_attrs["llm.cost_usd"] = cost
    trace.get_current_span().set_attributes(span_attrs)


def record_llm_failure(*, provider: str, model: str, reason: str) -> None:
    m = get_metrics()
    m.llm_failures.add(1, {"provider": provider, "model": model, "reason": reason})
    trace.get_current_span().set_attributes(
        {"llm.provider": provider, "llm.model": model, "llm.status": "failed", "llm.failure_reason": reason}
    )
