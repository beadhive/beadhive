from __future__ import annotations

from beadhive.kernel.telemetry import (
    EmitDisposition,
    EventEnvelope,
    EventIdentity,
    FlushOutcome,
    NoOpTelemetrySink,
    RecordingTelemetrySink,
)


def _event(event_id: str) -> EventEnvelope:
    return EventEnvelope(
        event_name="beadhive.operation.execution",
        event_version=1,
        event_id=event_id,
        occurred_at="2026-09-10T02:00:00Z",
        phase="started",
        correlation_id="corr-1",
        identity=EventIdentity(service="bh", instance_id="instance-1"),
    )


def test_no_op_sink_is_disabled_and_has_no_side_effects() -> None:
    sink = NoOpTelemetrySink()

    assert sink.emit(_event("event-1")) is EmitDisposition.DISABLED
    assert sink.flush(0.25).outcome is FlushOutcome.NO_OP
    assert sink.events == ()


def test_recording_sink_is_ordered_and_capacity_bounded() -> None:
    sink = RecordingTelemetrySink(capacity=2)
    first = _event("event-1")
    second = _event("event-2")

    assert sink.emit(first) is EmitDisposition.ACCEPTED
    assert sink.emit(second) is EmitDisposition.ACCEPTED
    assert sink.emit(_event("event-3")) is EmitDisposition.CAPACITY_DROPPED
    assert sink.events == (first, second)
    assert sink.flush(0.25).outcome is FlushOutcome.NO_OP
