"""Public transport-neutral semantic telemetry contract."""

from .contracts import (
    EVENT_ENVELOPE_SCHEMA_ID,
    EVENT_ENVELOPE_SCHEMA_VERSION,
    MAX_ATTRIBUTES,
    AttributeKey,
    EmitDisposition,
    ErrorClassification,
    EventEnvelope,
    EventError,
    EventIdentity,
    EventPhase,
    FlushOutcome,
    FlushResult,
    Outcome,
    Seat,
    SemanticEventName,
    SemanticTelemetryPort,
    TelemetryAttribute,
    TelemetryContext,
    TelemetryObservation,
    TelemetrySink,
    TraceVerb,
    activate_non_fatal,
    emit_non_fatal,
    flush_non_fatal,
)
from .schema import event_envelope_schema

__all__ = [
    "EVENT_ENVELOPE_SCHEMA_ID",
    "EVENT_ENVELOPE_SCHEMA_VERSION",
    "MAX_ATTRIBUTES",
    "NoOpTelemetrySink",
    "AttributeKey",
    "EmitDisposition",
    "ErrorClassification",
    "EventEnvelope",
    "EventError",
    "EventIdentity",
    "EventPhase",
    "FlushOutcome",
    "FlushResult",
    "Outcome",
    "RecordingTelemetrySink",
    "Seat",
    "SemanticEventName",
    "SemanticTelemetry",
    "SemanticTelemetryPort",
    "TelemetryAttribute",
    "TelemetryContext",
    "TelemetryObservation",
    "TelemetrySink",
    "TraceVerb",
    "activate_non_fatal",
    "emit_non_fatal",
    "current_telemetry_context",
    "event_envelope_schema",
    "flush_non_fatal",
]


def __getattr__(name: str) -> object:
    """Load concrete telemetry implementations only when the facade symbol is requested."""

    if name in {"SemanticTelemetry", "current_telemetry_context"}:
        from . import instrumentation

        return getattr(instrumentation, name)
    if name in {"NoOpTelemetrySink", "RecordingTelemetrySink"}:
        from . import sinks

        return getattr(sinks, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
