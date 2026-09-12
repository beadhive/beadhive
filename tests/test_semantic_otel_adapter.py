"""Contract tests for the semantic-event OpenTelemetry adapter.

These tests inject the narrow legacy OTel runtime seam.  They never import the SDK, start a
service, or perform network I/O.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from beadhive.adapters.telemetry import OpenTelemetrySink, SemanticHttpTelemetryMiddleware
from beadhive.kernel.telemetry import (
    AttributeKey,
    EmitDisposition,
    EventIdentity,
    FlushOutcome,
    Outcome,
    RecordingTelemetrySink,
    SemanticEventName,
    SemanticTelemetry,
    TelemetryAttribute,
)


class _Instrument:
    def __init__(self) -> None:
        self.values: list[tuple[float, dict[str, object]]] = []

    def add(self, value: float, attributes: dict[str, object]) -> None:
        self.values.append((value, dict(attributes)))

    def record(self, value: float, attributes: dict[str, object]) -> None:
        self.values.append((value, dict(attributes)))


class _Meter:
    def __init__(self) -> None:
        self.counters: dict[str, _Instrument] = {}
        self.histograms: dict[str, _Instrument] = {}

    def create_counter(self, name: str, **_kwargs) -> _Instrument:
        return self.counters.setdefault(name, _Instrument())

    def create_histogram(self, name: str, **_kwargs) -> _Instrument:
        return self.histograms.setdefault(name, _Instrument())


class _Span:
    def __init__(self, name: str, attributes: dict[str, object]) -> None:
        self.name = name
        self.attributes = dict(attributes)
        self.events: list[tuple[str, dict[str, object]]] = []
        self.ended = 0

    def set_attribute(self, key: str, value: object) -> None:
        self.attributes[key] = value

    def add_event(self, name: str, attributes: dict[str, object]) -> None:
        self.events.append((name, dict(attributes)))

    def end(self) -> None:
        self.ended += 1


class _Tracer:
    def __init__(self) -> None:
        self.spans: list[_Span] = []

    def start_span(self, name: str, *, attributes: dict[str, object]) -> _Span:
        span = _Span(name, attributes)
        self.spans.append(span)
        return span


@dataclass(frozen=True)
class _Shutdown:
    status: str
    duration_seconds: float


class _Runtime:
    def __init__(self) -> None:
        self.tracer = _Tracer()
        self.meter = _Meter()
        self.shutdown_budgets: list[float] = []

    def is_active(self) -> bool:
        return True

    def get_tracer(self, _name: str):
        return self.tracer

    def get_meter(self, _name: str):
        return self.meter

    def shutdown(self, *, timeout_seconds: float):
        self.shutdown_budgets.append(timeout_seconds)
        return _Shutdown("completed", 0.012)


def _semantic(sink: OpenTelemetrySink, *, monotonic: list[float]) -> SemanticTelemetry:
    ids = iter(("event-start", "event-complete"))
    ticks = iter(monotonic)
    return SemanticTelemetry(
        sink=sink,
        identity=EventIdentity(service="bh", instance_id="instance-one"),
        event_id_factory=lambda: next(ids),
        occurred_at_factory=lambda: "2026-09-10T00:00:00Z",
        monotonic=lambda: next(ticks),
    )


def test_semantic_adapter_projects_one_correlated_span_and_bounded_metrics() -> None:
    runtime = _Runtime()
    sink = OpenTelemetrySink(runtime=runtime, instrumentation_scope="bh-semantic")
    telemetry = _semantic(sink, monotonic=[10.0, 10.025])

    observation = telemetry.begin(
        SemanticEventName.OPERATION_EXECUTION,
        correlation_id="correlation-one",
        attributes=(
            TelemetryAttribute(AttributeKey.OPERATION_KIND, "command"),
            TelemetryAttribute(AttributeKey.OPERATION_NAME, "work.issue"),
            TelemetryAttribute(AttributeKey.SURFACE, "cli"),
        ),
    )
    assert telemetry.complete(observation, Outcome.SUCCEEDED) is EmitDisposition.ACCEPTED

    assert len(runtime.tracer.spans) == 1
    span = runtime.tracer.spans[0]
    assert span.name == "beadhive.operation.execution"
    assert span.ended == 1
    assert span.attributes["beadhive.telemetry.correlation_id"] == "correlation-one"
    assert span.attributes["beadhive.telemetry.outcome"] == "succeeded"
    assert span.attributes["operation.name"] == "work.issue"
    assert runtime.meter.counters["beadhive.semantic.events"].values == [
        (
            1,
            {
                "event.name": "beadhive.operation.execution",
                "event.phase": "started",
                "event.version": 1,
                "operation.kind": "command",
                "operation.name": "work.issue",
                "surface": "cli",
            },
        ),
        (
            1,
            {
                "event.name": "beadhive.operation.execution",
                "event.outcome": "succeeded",
                "event.phase": "completed",
                "event.version": 1,
                "operation.kind": "command",
                "operation.name": "work.issue",
                "surface": "cli",
            },
        ),
    ]
    assert runtime.meter.histograms["beadhive.semantic.duration"].values == [
        (
            25,
            {
                "event.name": "beadhive.operation.execution",
                "event.outcome": "succeeded",
                "event.version": 1,
                "operation.kind": "command",
                "operation.name": "work.issue",
                "surface": "cli",
            },
        )
    ]
    metric_text = repr(runtime.meter.counters) + repr(runtime.meter.histograms)
    assert "instance-one" not in metric_text
    assert "event-start" not in metric_text
    assert "correlation-one" not in metric_text


def test_semantic_adapter_failure_is_nonfatal_and_abandons_no_open_span() -> None:
    class BrokenTracer:
        def start_span(self, *_args, **_kwargs):
            raise RuntimeError("collector token=/secret")

    runtime = _Runtime()
    runtime.tracer = BrokenTracer()
    sink = OpenTelemetrySink(runtime=runtime)
    telemetry = _semantic(sink, monotonic=[1.0, 1.0])

    observation = telemetry.begin(SemanticEventName.OPERATION_EXECUTION)

    assert observation is not None
    assert telemetry.complete(observation, Outcome.SUCCEEDED) is EmitDisposition.ACCEPTED
    assert sink.open_span_count == 0


def test_metric_projection_failure_ends_partially_started_span_nonfatally() -> None:
    class BrokenInstrument(_Instrument):
        def add(self, _value, _attributes) -> None:
            raise RuntimeError("collector credential=secret")

    runtime = _Runtime()
    runtime.meter.create_counter = lambda *_args, **_kwargs: BrokenInstrument()
    sink = OpenTelemetrySink(runtime=runtime)
    telemetry = _semantic(sink, monotonic=[1.0])

    observation = telemetry.begin(SemanticEventName.OPERATION_EXECUTION)

    assert observation is not None
    assert sink.open_span_count == 0
    assert len(runtime.tracer.spans) == 1
    assert runtime.tracer.spans[0].ended == 1


def test_semantic_adapter_flush_uses_one_finite_total_budget_and_closes_open_spans() -> None:
    runtime = _Runtime()
    sink = OpenTelemetrySink(runtime=runtime)
    recording = RecordingTelemetrySink()
    del recording  # prove adapter construction has no dependency on a test sink
    telemetry = _semantic(sink, monotonic=[1.0])
    assert telemetry.begin(SemanticEventName.OPERATION_EXECUTION) is not None
    assert sink.open_span_count == 1

    result = sink.flush(0.125)

    assert result.outcome is FlushOutcome.COMPLETED
    assert result.duration_ms == 12
    assert runtime.shutdown_budgets == [0.125]
    assert sink.open_span_count == 0
    assert runtime.tracer.spans[0].ended == 1
    assert sink.emit_count == 1


def test_flush_adapter_failure_is_nonfatal_and_closes_every_open_span() -> None:
    class BrokenRuntime(_Runtime):
        def shutdown(self, *, timeout_seconds: float):
            self.shutdown_budgets.append(timeout_seconds)
            raise RuntimeError("collector unavailable token=secret")

    runtime = BrokenRuntime()
    sink = OpenTelemetrySink(runtime=runtime)
    telemetry = _semantic(sink, monotonic=[1.0])
    assert telemetry.begin(SemanticEventName.OPERATION_EXECUTION) is not None

    result = sink.flush(0.05)

    assert result.outcome is FlushOutcome.FAILED
    assert result.duration_ms == 0
    assert runtime.shutdown_budgets == [0.05]
    assert sink.open_span_count == 0
    assert runtime.tracer.spans[0].ended == 1


@pytest.mark.parametrize("timeout", [0.0, -1.0, float("inf"), float("nan")])
def test_semantic_adapter_rejects_nonfinite_flush_budgets_without_runtime_io(timeout) -> None:
    runtime = _Runtime()
    sink = OpenTelemetrySink(runtime=runtime)

    with pytest.raises(ValueError, match="finite and greater than zero"):
        sink.flush(timeout)

    assert runtime.shutdown_budgets == []


def test_semantic_http_cancellation_closes_one_correlated_attempt_without_request_data() -> None:
    sink = RecordingTelemetrySink()
    telemetry = SemanticTelemetry(
        sink=sink,
        identity=EventIdentity(service="bh-gateway", instance_id="gateway-one"),
    )

    async def cancelled(_scope, _receive, _send) -> None:
        raise asyncio.CancelledError

    middleware = SemanticHttpTelemetryMiddleware(
        cancelled,
        telemetry=telemetry,
        surface="gateway",
    )

    async def exercise() -> None:
        with pytest.raises(asyncio.CancelledError):
            await middleware(
                {
                    "type": "http",
                    "method": "GET",
                    "path": "/v1/instances/dev/private/events",
                    "query_string": b"token=secret",
                    "headers": [(b"authorization", b"Bearer secret")],
                },
                lambda: None,
                lambda _message: None,
            )

    asyncio.run(exercise())

    assert len(sink.events) == 2
    started, completed = sink.events
    assert started.correlation_id == completed.correlation_id
    assert completed.causation_id == started.event_id
    assert completed.outcome is Outcome.CANCELLED
    assert [item.value for item in started.attributes] == ["GET", "route", "gateway", "sse"]
    assert "private" not in repr(sink.events)
    assert "secret" not in repr(sink.events)


def test_gateway_cancellation_ends_adapter_span_without_worker_or_span_leak() -> None:
    runtime = _Runtime()
    sink = OpenTelemetrySink(runtime=runtime)
    telemetry = _semantic(sink, monotonic=[1.0, 1.01])

    async def cancelled(_scope, _receive, _send) -> None:
        raise asyncio.CancelledError

    middleware = SemanticHttpTelemetryMiddleware(
        cancelled,
        telemetry=telemetry,
        surface="gateway",
    )

    async def exercise() -> None:
        with pytest.raises(asyncio.CancelledError):
            await middleware(
                {"type": "http", "method": "GET", "path": "/private/events"},
                lambda: None,
                lambda _message: None,
            )

    asyncio.run(exercise())

    assert sink.open_span_count == 0
    assert sink.emit_count == 2
    assert len(runtime.tracer.spans) == 1
    assert runtime.tracer.spans[0].ended == 1
