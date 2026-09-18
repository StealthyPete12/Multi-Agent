"""Tests for shared/telemetry.py: span creation, trace propagation across
an AMQP-header hop, metrics collection, and OpenTelemetry SDK setup.

Most tests monkeypatch ``shared.telemetry.get_tracer``/``get_meter`` to
point at a local TracerProvider/MeterProvider backed by in-memory
exporters, rather than touching OpenTelemetry's process-global provider
(``trace.set_tracer_provider()`` can only be installed once per process —
polluting it here would leak into every other test module in the same
pytest session). ``init_telemetry()`` itself — which *does* install the
global provider — is exercised in a subprocess instead, so it never
touches this process's global state.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

from shared import telemetry


@pytest.fixture()
def local_tracing(monkeypatch):
    """A local TracerProvider(SimpleSpanProcessor -> InMemorySpanExporter),
    wired in place of shared.telemetry.get_tracer for the test's duration.
    SimpleSpanProcessor exports synchronously, so spans are visible to
    assertions immediately after the `with` block exits — no flush/sleep
    needed."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    def _get_tracer(name: str = "swarm"):
        return provider.get_tracer(name)

    monkeypatch.setattr(telemetry, "get_tracer", _get_tracer)
    return exporter


@pytest.fixture()
def local_metrics(monkeypatch):
    """A local MeterProvider(InMemoryMetricReader), wired in place of
    shared.telemetry.get_meter/get_metrics for the test's duration."""
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    meter = provider.get_meter("swarm")

    monkeypatch.setattr(telemetry, "get_meter", lambda name="swarm": meter)
    monkeypatch.setattr(telemetry, "_metrics_singleton", None)
    monkeypatch.setattr(telemetry, "get_metrics", lambda: telemetry.Metrics(meter=meter))
    return reader


def _all_metric_points(reader: InMemoryMetricReader) -> list[dict]:
    """Flatten every data point across every metric into one list. Counter/
    gauge points expose ``value``; histogram points expose ``sum``/``count``
    instead — both are copied onto ``value``/``sum``/``count`` uniformly so
    callers don't need to know which aggregation a given metric uses."""
    data = reader.get_metrics_data()
    points = []
    if data is None:
        return points
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                for point in metric.data.data_points:
                    points.append(
                        {
                            "name": metric.name,
                            "value": getattr(point, "value", None),
                            "sum": getattr(point, "sum", None),
                            "count": getattr(point, "count", None),
                            "attributes": dict(point.attributes),
                        }
                    )
    return points


# ---------------------------------------------------------------------------
# Span generation
# ---------------------------------------------------------------------------


def test_span_records_attributes_and_kind(local_tracing):
    with telemetry.span("test.operation", kind=SpanKind.INTERNAL, attributes={"foo": "bar"}):
        pass

    spans = local_tracing.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "test.operation"
    assert spans[0].kind == SpanKind.INTERNAL
    assert spans[0].attributes["foo"] == "bar"
    assert spans[0].status.status_code == StatusCode.UNSET


def test_span_marks_error_status_and_reraises(local_tracing):
    with pytest.raises(ValueError):
        with telemetry.span("test.failing_operation"):
            raise ValueError("boom")

    spans = local_tracing.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].status.status_code == StatusCode.ERROR
    assert len(spans[0].events) == 1  # record_exception() adds an event
    assert spans[0].events[0].name == "exception"


def test_current_trace_id_and_span_id_track_the_active_span(local_tracing):
    assert telemetry.current_trace_id() is None
    assert telemetry.current_span_id() is None

    with telemetry.span("test.operation") as current_span:
        trace_id = telemetry.current_trace_id()
        span_id = telemetry.current_span_id()
        expected_trace_id = format(current_span.get_span_context().trace_id, "032x")
        expected_span_id = format(current_span.get_span_context().span_id, "016x")

    assert trace_id == expected_trace_id
    assert span_id == expected_span_id
    assert len(trace_id) == 32
    assert len(span_id) == 16


# ---------------------------------------------------------------------------
# Trace propagation across an AMQP-header hop
# ---------------------------------------------------------------------------


def test_producer_span_injects_traceparent_header(local_tracing):
    headers: dict = {}
    with telemetry.producer_span("rabbitmq.publish test.event", headers) as current_span:
        expected_trace_id = format(current_span.get_span_context().trace_id, "032x")

    assert "traceparent" in headers
    assert expected_trace_id in headers["traceparent"]


def test_consumer_span_continues_the_producer_trace(local_tracing):
    """The core Phase 5 guarantee: a trace_id survives a RabbitMQ hop.
    Two independent producer_span/consumer_span calls (simulating two
    separate processes, since AMQP headers are the only thing that
    actually crosses that boundary) must land on the same trace."""
    headers: dict = {}
    with telemetry.producer_span("rabbitmq.publish commit.detected", headers) as producer:
        producer_trace_id = format(producer.get_span_context().trace_id, "032x")

    with telemetry.consumer_span("researcher.handle_commit_detected", headers) as consumer:
        consumer_trace_id = format(consumer.get_span_context().trace_id, "032x")
        consumer_parent_span_id = consumer.parent.span_id

    assert consumer_trace_id == producer_trace_id
    assert consumer_parent_span_id == producer.get_span_context().span_id


def test_consumer_span_with_no_headers_starts_a_new_trace_not_a_crash(local_tracing):
    """No orphan crash: a message with no/garbled trace headers (e.g. sent
    by a tool that never called init_telemetry()) still gets a valid,
    self-contained trace rather than raising."""
    with telemetry.consumer_span("researcher.handle_commit_detected", None) as current_span:
        assert current_span.get_span_context().is_valid


def test_multi_hop_propagation_stays_on_one_trace(local_tracing):
    """watcher -> publish -> researcher -> publish -> reviewer, chained
    through two independent AMQP hops, stays one trace end to end —
    mirrors what PHASE_5_REPORT.md's live Phoenix validation showed."""
    with telemetry.span("watcher.webhook_received") as root:
        root_trace_id = format(root.get_span_context().trace_id, "032x")
        headers_1: dict = {}
        with telemetry.producer_span("rabbitmq.publish commit.detected", headers_1):
            pass

    with telemetry.consumer_span("researcher.handle_commit_detected", headers_1):
        headers_2: dict = {}
        with telemetry.producer_span("rabbitmq.publish findings.ready", headers_2):
            pass

    with telemetry.consumer_span("reviewer.handle_findings_ready", headers_2) as leaf:
        leaf_trace_id = format(leaf.get_span_context().trace_id, "032x")

    assert leaf_trace_id == root_trace_id


# ---------------------------------------------------------------------------
# Metrics collection
# ---------------------------------------------------------------------------


def test_metrics_counter_increments_are_visible(local_metrics):
    telemetry.get_metrics().llm_calls.add(1, {"provider": "anthropic", "model": "x", "outcome": "success"})
    telemetry.get_metrics().llm_calls.add(2, {"provider": "anthropic", "model": "x", "outcome": "success"})

    points = _all_metric_points(local_metrics)
    matching = [p for p in points if p["name"] == "swarm_llm_calls_total"]
    assert len(matching) == 1
    assert matching[0]["value"] == 3
    assert matching[0]["attributes"]["provider"] == "anthropic"


def test_metrics_histogram_records_observations(local_metrics):
    telemetry.get_metrics().llm_duration_ms.record(120.5, {"provider": "openai", "model": "y"})

    points = _all_metric_points(local_metrics)
    matching = [p for p in points if p["name"] == "swarm_llm_duration"]
    assert len(matching) == 1
    assert matching[0]["sum"] == pytest.approx(120.5)
    assert matching[0]["count"] == 1


def test_all_catalog_metrics_are_created_without_error(local_metrics):
    """Every metric the roadmap asked for exists as a real instrument (not
    just documented) — a crude but effective regression guard against a
    typo'd/removed instrument name."""
    m = telemetry.get_metrics()
    expected = [
        "events_processed",
        "commit_events",
        "findings_events",
        "review_events",
        "dlq_count",
        "retry_count",
        "llm_calls",
        "llm_failures",
        "llm_tokens_in",
        "llm_tokens_out",
        "llm_cost_usd",
        "slack_deliveries",
        "breaker_opens",
        "rate_limit_delays",
        "repo_clones",
        "repo_cache_hits",
        "repo_clone_duration_ms",
        "repo_refresh_duration_ms",
        "blast_radius_duration_ms",
        "review_duration_ms",
        "db_write_duration_ms",
        "db_query_duration_ms",
        "db_failures",
    ]
    for attr in expected:
        assert hasattr(m, attr), f"Metrics is missing {attr!r}"


def test_register_pool_gauges_reports_size_idle_in_use(local_metrics):
    class FakePool:
        def get_size(self) -> int:
            return 5

        def get_idle_size(self) -> int:
            return 2

    telemetry.register_pool_gauges("researcher", lambda: FakePool())

    points = _all_metric_points(local_metrics)
    matching = {p["attributes"]["state"]: p["value"] for p in points if p["name"] == "swarm_db_pool_connections"}
    assert matching == {"total": 5, "idle": 2, "in_use": 3}


# ---------------------------------------------------------------------------
# LLM cost estimation
# ---------------------------------------------------------------------------


def test_estimate_cost_usd_known_model():
    cost = telemetry.estimate_cost_usd("anthropic", "claude-haiku-4-5-20251001", 1_000_000, 1_000_000)
    assert cost == pytest.approx(1.00 + 5.00)


def test_estimate_cost_usd_unknown_model_returns_none():
    assert telemetry.estimate_cost_usd("anthropic", "some-future-model", 1000, 1000) is None


def test_estimate_cost_usd_ollama_is_free():
    assert telemetry.estimate_cost_usd("ollama", "llama3.1", 1_000_000, 1_000_000) == 0.0


def test_estimate_cost_usd_env_override(monkeypatch):
    monkeypatch.setenv("LLM_PRICE_MY_CUSTOM_MODEL_IN_PER_1M", "2.5")
    monkeypatch.setenv("LLM_PRICE_MY_CUSTOM_MODEL_OUT_PER_1M", "10")
    cost = telemetry.estimate_cost_usd("anthropic", "my-custom-model", 1_000_000, 1_000_000)
    assert cost == pytest.approx(12.5)


def test_record_llm_success_updates_metrics_and_span(local_tracing, local_metrics):
    with telemetry.span("llm.request", kind=SpanKind.CLIENT):
        telemetry.record_llm_success(
            provider="anthropic",
            model="claude-haiku-4-5-20251001",
            input_tokens=1000,
            output_tokens=500,
            latency_ms=250.0,
        )

    spans = local_tracing.get_finished_spans()
    assert spans[0].attributes["llm.status"] == "success"
    assert spans[0].attributes["llm.tokens.input"] == 1000
    assert spans[0].attributes["llm.cost_usd"] == pytest.approx(0.0035)

    points = _all_metric_points(local_metrics)
    assert any(p["name"] == "swarm_llm_tokens_in_total" and p["value"] == 1000 for p in points)
    assert any(p["name"] == "swarm_llm_cost_usd_total" for p in points)


def test_record_llm_failure_updates_metrics_and_span(local_tracing, local_metrics):
    with telemetry.span("llm.request", kind=SpanKind.CLIENT):
        telemetry.record_llm_failure(provider="openai", model="gpt-4o-mini", reason="timeout")

    spans = local_tracing.get_finished_spans()
    assert spans[0].attributes["llm.status"] == "failed"
    assert spans[0].attributes["llm.failure_reason"] == "timeout"

    points = _all_metric_points(local_metrics)
    assert any(p["name"] == "swarm_llm_failures_total" for p in points)


# ---------------------------------------------------------------------------
# OpenTelemetry SDK setup (init_telemetry) — run in a subprocess so this
# never installs a real global TracerProvider/MeterProvider into the
# pytest process itself (OTel only allows that once per process; doing it
# here would leak into every other test module in the same session).
# ---------------------------------------------------------------------------


def test_init_telemetry_console_exporters_smoke():
    script = """
import json
from shared import telemetry

tracer, meter = telemetry.init_telemetry("smoke-test", start_metrics_server=False)
with telemetry.span("smoke.span"):
    trace_id = telemetry.current_trace_id()

# Calling it again must not raise (idempotent global provider install).
tracer2, meter2 = telemetry.init_telemetry("smoke-test", start_metrics_server=False)

telemetry.shutdown_telemetry()
print(json.dumps({"trace_id_len": len(trace_id)}))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=".",
        capture_output=True,
        text=True,
        env={
            "OTEL_TRACES_EXPORTER": "console",
            "OTEL_METRICS_EXPORTER": "none",
            "PATH": __import__("os").environ.get("PATH", ""),
        },
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    last_line = [line for line in result.stdout.splitlines() if line.strip()][-1]
    payload = json.loads(last_line)
    assert payload["trace_id_len"] == 32


def test_init_telemetry_none_exporter_creates_no_processors():
    script = """
from opentelemetry.sdk.trace import TracerProvider
from shared import telemetry

telemetry.init_telemetry("smoke-test-none", start_metrics_server=False)
provider = __import__("opentelemetry").trace.get_tracer_provider()
assert isinstance(provider, TracerProvider)
assert provider._active_span_processor._span_processors == ()
print("OK")
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env={
            "OTEL_TRACES_EXPORTER": "none",
            "OTEL_METRICS_EXPORTER": "none",
            "PATH": __import__("os").environ.get("PATH", ""),
        },
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout
