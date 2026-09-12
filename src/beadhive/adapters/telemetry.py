"""OpenTelemetry projection of the kernel-owned semantic telemetry port.

The kernel owns event meaning and redaction/cardinality policy.  This adapter owns SDK objects,
span lifetime, metric projection, and the one bounded provider flush.  Collector processes,
deployment, storage, and retry durability are deliberately not owned here.
"""

from __future__ import annotations

import asyncio
import math
import threading
from typing import Any, Protocol

from beadhive.kernel.telemetry import (
    AttributeKey,
    EmitDisposition,
    ErrorClassification,
    EventEnvelope,
    EventError,
    EventPhase,
    FlushOutcome,
    FlushResult,
    Outcome,
    SemanticEventName,
    SemanticTelemetryPort,
    TelemetryAttribute,
)

_EVENTS_METRIC = "beadhive.semantic.events"
_DURATION_METRIC = "beadhive.semantic.duration"


class _TelemetryRuntime(Protocol):
    """Narrow composition seam implemented by :mod:`beadhive.otel`."""

    def is_active(self) -> bool: ...

    def get_tracer(self, name: str) -> Any: ...

    def get_meter(self, name: str) -> Any: ...

    def shutdown(self, *, timeout_seconds: float) -> Any: ...


def _metric_attributes(event: EventEnvelope) -> dict[str, str | int | bool]:
    """Project only finite semantic dimensions; correlation and identities never label metrics."""

    attributes: dict[str, str | int | bool] = {
        "event.name": event.event_name.value,
        "event.phase": event.phase.value,
        "event.version": event.event_version,
    }
    if event.outcome is not None:
        attributes["event.outcome"] = event.outcome.value
    for item in event.attributes:
        attributes[item.key.value] = item.value
    return attributes


def _duration_attributes(event: EventEnvelope) -> dict[str, str | int | bool]:
    attributes = _metric_attributes(event)
    attributes.pop("event.phase", None)
    return attributes


def _span_attributes(event: EventEnvelope) -> dict[str, str | int | bool]:
    """Project allowlisted identities for trace/log correlation, never as metric dimensions."""

    attributes = _metric_attributes(event)
    attributes.update(
        {
            "beadhive.telemetry.schema_version": event.schema_version,
            "beadhive.telemetry.event_id": event.event_id,
            "beadhive.telemetry.correlation_id": event.correlation_id,
            "service.name": event.identity.service,
            "service.instance.id": event.identity.instance_id,
        }
    )
    nullable = {
        "beadhive.telemetry.causation_id": event.causation_id,
        "beadhive.telemetry.trace_id": event.trace_id,
        "beadhive.telemetry.span_id": event.span_id,
        "beadhive.host.id": event.identity.host_id,
        "beadhive.hive.id": event.identity.hive_id,
        "beadhive.bead.id": event.identity.bead_id,
        "beadhive.run.id": event.identity.run_id,
        "beadhive.actor": event.identity.actor,
        "beadhive.seat": event.identity.seat.value if event.identity.seat is not None else None,
        "beadhive.plugin.id": event.identity.plugin_id,
        "beadhive.plugin.version": event.identity.plugin_version,
    }
    attributes.update({key: value for key, value in nullable.items() if value is not None})
    if event.error is not None:
        attributes.update(
            {
                "beadhive.telemetry.error.classification": event.error.classification.value,
                "beadhive.telemetry.error.code": event.error.code,
                "beadhive.telemetry.error.retryable": event.error.retryable,
            }
        )
    if event.outcome is not None:
        attributes["beadhive.telemetry.outcome"] = event.outcome.value
    if event.duration_ms is not None:
        attributes["beadhive.telemetry.duration_ms"] = event.duration_ms
    return attributes


class OpenTelemetrySink:
    """Non-blocking semantic sink over an already-composed OpenTelemetry runtime.

    ``emit`` touches only SDK in-process APIs.  Export remains owned by the runtime's batch
    processors.  ``flush`` closes every still-open semantic span and delegates exactly once to
    the runtime's cooperative total-budget shutdown.
    """

    def __init__(
        self,
        *,
        runtime: _TelemetryRuntime,
        instrumentation_scope: str = "beadhive.semantic-telemetry",
    ) -> None:
        self._runtime = runtime
        self._scope = instrumentation_scope
        self._lock = threading.RLock()
        self._spans: dict[str, Any] = {}
        self._counter: Any = None
        self._duration: Any = None
        self._closed = False
        self._emit_count = 0

    @property
    def enabled(self) -> bool:
        try:
            return not self._closed and self._runtime.is_active() is True
        except BaseException:
            return False

    @property
    def open_span_count(self) -> int:
        with self._lock:
            return len(self._spans)

    @property
    def emit_count(self) -> int:
        with self._lock:
            return self._emit_count

    def _instruments(self) -> tuple[Any, Any]:
        if self._counter is None or self._duration is None:
            meter = self._runtime.get_meter(self._scope)
            self._counter = meter.create_counter(
                _EVENTS_METRIC,
                unit="1",
                description="accepted semantic telemetry envelopes",
            )
            self._duration = meter.create_histogram(
                _DURATION_METRIC,
                unit="ms",
                description="completed semantic operation duration",
            )
        return self._counter, self._duration

    def emit(self, event: EventEnvelope) -> EmitDisposition:
        if not self.enabled:
            return EmitDisposition.DISABLED
        span: Any = None
        accepted = False
        try:
            counter, duration = self._instruments()
            if event.phase is EventPhase.STARTED:
                span = self._runtime.get_tracer(self._scope).start_span(
                    event.event_name.value,
                    attributes=_span_attributes(event),
                )
                counter.add(1, _metric_attributes(event))
                with self._lock:
                    if self._closed:
                        return EmitDisposition.DISABLED
                    self._spans[event.event_id] = span
                    self._emit_count += 1
                accepted = True
                return EmitDisposition.ACCEPTED

            with self._lock:
                if event.causation_id is not None:
                    span = self._spans.pop(event.causation_id, None)
            attributes = _span_attributes(event)
            if span is not None:
                for key, value in attributes.items():
                    span.set_attribute(key, value)
                span.add_event(event.phase.value, attributes)
            counter.add(1, _metric_attributes(event))
            if event.duration_ms is not None:
                duration.record(event.duration_ms, _duration_attributes(event))
            with self._lock:
                self._emit_count += 1
            accepted = True
            return EmitDisposition.ACCEPTED
        except BaseException:
            return EmitDisposition.FAILED
        finally:
            # A completion owns its matching span.  A failed start must not retain a partially
            # initialized span; a successful start deliberately stays open for its completion.
            should_end = event.phase is not EventPhase.STARTED or not accepted
            if span is not None and should_end:
                try:
                    span.end()
                except BaseException:
                    pass

    def close_open_spans(self) -> None:
        """End and forget every unfinished semantic span without exporting or blocking."""

        with self._lock:
            spans = tuple(self._spans.values())
            self._spans.clear()
        for span in spans:
            try:
                span.end()
            except BaseException:
                pass

    def flush(self, timeout_seconds: float) -> FlushResult:
        if timeout_seconds <= 0 or not math.isfinite(timeout_seconds):
            raise ValueError("telemetry flush timeout must be finite and greater than zero")
        with self._lock:
            if self._closed:
                return FlushResult(FlushOutcome.NO_OP, 0)
            self._closed = True
        self.close_open_spans()
        try:
            result = self._runtime.shutdown(timeout_seconds=timeout_seconds)
            status = str(getattr(result, "status", "error"))
            duration_seconds = float(getattr(result, "duration_seconds", 0.0))
            duration_ms = max(0, int(round(duration_seconds * 1000)))
        except BaseException:
            return FlushResult(FlushOutcome.FAILED, 0)
        outcome = {
            "completed": FlushOutcome.COMPLETED,
            "timed_out": FlushOutcome.TIMED_OUT,
            "inactive": FlushOutcome.NO_OP,
        }.get(status, FlushOutcome.FAILED)
        return FlushResult(outcome, duration_ms)


class SemanticHttpTelemetryMiddleware:
    """Translate one complete HTTP/SSE/WebSocket exchange through the semantic port.

    The middleware intentionally never copies request paths, headers, bodies, addresses, or user
    agents into telemetry.  Transport and method are selected from closed vocabularies, and an SSE
    event route remains active until the response body finishes or is cancelled.
    """

    def __init__(self, app: Any, *, telemetry: SemanticTelemetryPort, surface: str) -> None:
        self.app = app
        self.telemetry = telemetry
        self.surface = surface

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        scope_type = str(scope.get("type", ""))
        if scope_type not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        raw_method = "OTHER" if scope_type == "websocket" else str(scope.get("method", "OTHER"))
        method = raw_method if raw_method in {"GET", "POST", "PUT", "PATCH", "DELETE"} else "OTHER"
        path = str(scope.get("path", ""))
        transport = (
            "websocket"
            if scope_type == "websocket"
            else "sse"
            if path.endswith("/events")
            else "http"
        )
        observation = None
        try:
            observation = self.telemetry.begin(
                SemanticEventName.OPERATION_EXECUTION,
                attributes=(
                    TelemetryAttribute(AttributeKey.OPERATION_KIND, "route"),
                    TelemetryAttribute(AttributeKey.SURFACE, self.surface),
                    TelemetryAttribute(AttributeKey.TRANSPORT, transport),
                    TelemetryAttribute(AttributeKey.HTTP_METHOD, method),
                ),
            )
        except BaseException:
            pass
        status_code = 500

        async def observe(message: dict[str, Any]) -> None:
            nonlocal status_code
            if message.get("type") == "http.response.start":
                status_code = int(message.get("status", 500))
            elif message.get("type") == "websocket.accept":
                status_code = 101
            await send(message)

        try:
            await self.app(scope, receive, observe)
        except asyncio.CancelledError:
            try:
                self.telemetry.complete(
                    observation,
                    Outcome.CANCELLED,
                    error=EventError(ErrorClassification.CANCELLATION, "request.cancelled"),
                )
            except BaseException:
                pass
            raise
        except BaseException:
            try:
                self.telemetry.complete(
                    observation,
                    Outcome.FAILED,
                    error=EventError(ErrorClassification.INTERNAL, "request.failed"),
                )
            except BaseException:
                pass
            raise
        else:
            try:
                if status_code >= 400:
                    self.telemetry.complete(
                        observation,
                        Outcome.FAILED,
                        error=EventError(ErrorClassification.INTERNAL, "request.failed"),
                    )
                else:
                    self.telemetry.complete(observation, Outcome.SUCCEEDED)
            except BaseException:
                pass


__all__ = ["OpenTelemetrySink", "SemanticHttpTelemetryMiddleware"]
