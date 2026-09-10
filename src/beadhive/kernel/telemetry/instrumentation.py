"""Bounded construction and correlation of semantic telemetry observations."""

from __future__ import annotations

import contextlib
import time
import uuid
from collections.abc import Callable, Iterator
from contextvars import ContextVar
from datetime import UTC, datetime

from .contracts import (
    EmitDisposition,
    EventEnvelope,
    EventError,
    EventIdentity,
    EventPhase,
    Outcome,
    SemanticEventName,
    TelemetryAttribute,
    TelemetryContext,
    TelemetryObservation,
    TelemetrySink,
    emit_non_fatal,
)

_CURRENT_CONTEXT: ContextVar[TelemetryContext | None] = ContextVar(
    "beadhive_semantic_telemetry_context", default=None
)


def current_telemetry_context() -> TelemetryContext | None:
    """Return the immutable context inherited by work in this async/thread context."""

    return _CURRENT_CONTEXT.get()


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


class SemanticTelemetry:
    """Best-effort event construction around one injected semantic sink.

    The coordinator never retries and performs at most one sink call per phase.  Every event is
    built from typed allowlisted values; request payloads and exception text have no API path into
    the envelope.
    """

    def __init__(
        self,
        *,
        sink: TelemetrySink,
        identity: EventIdentity,
        event_id_factory: Callable[[], str] | None = None,
        occurred_at_factory: Callable[[], str] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self.sink = sink
        self.identity = identity
        self._event_id_factory = event_id_factory or (lambda: str(uuid.uuid4()))
        self._occurred_at_factory = occurred_at_factory or _utc_now
        self._monotonic = monotonic or time.monotonic

    @property
    def enabled(self) -> bool:
        """Return false for sinks which explicitly advertise disabled composition."""

        return bool(getattr(self.sink, "enabled", True))

    def begin(
        self,
        event_name: SemanticEventName,
        *,
        correlation_id: str | None = None,
        identity: EventIdentity | None = None,
        attributes: tuple[TelemetryAttribute, ...] = (),
    ) -> TelemetryObservation | None:
        """Emit one v1 start event and return a token; contain every telemetry failure."""

        try:
            if not self.enabled:
                return None
            parent = current_telemetry_context()
            event_id = self._event_id_factory()
            if correlation_id is None:
                selected_correlation = parent.correlation_id if parent is not None else event_id
            else:
                selected_correlation = correlation_id
            same_tree = parent is not None and parent.correlation_id == selected_correlation
            context = TelemetryContext(
                correlation_id=selected_correlation,
                causation_id=parent.causation_id if same_tree else None,
                trace_id=parent.trace_id if same_tree else None,
                span_id=parent.span_id if same_tree else None,
            )
            selected_identity = identity or self.identity
            started_at = self._monotonic()
            event = EventEnvelope(
                event_name=event_name,
                event_version=1,
                event_id=event_id,
                occurred_at=self._occurred_at_factory(),
                phase=EventPhase.STARTED,
                correlation_id=context.correlation_id,
                causation_id=context.causation_id,
                trace_id=context.trace_id,
                span_id=context.span_id,
                identity=selected_identity,
                attributes=attributes,
            )
            emit_non_fatal(self.sink, event)
            return TelemetryObservation(
                event_name=event_name,
                event_version=1,
                start_event_id=event_id,
                started_at=started_at,
                context=context,
                identity=selected_identity,
                attributes=attributes,
            )
        except BaseException:
            return None

    @contextlib.contextmanager
    def activate(self, observation: TelemetryObservation | None) -> Iterator[None]:
        """Expose an observation as the direct semantic parent for nested work."""

        if observation is None:
            yield
            return
        child_context = TelemetryContext(
            correlation_id=observation.context.correlation_id,
            causation_id=observation.start_event_id,
            trace_id=observation.context.trace_id,
            span_id=observation.context.span_id,
        )
        token = _CURRENT_CONTEXT.set(child_context)
        try:
            yield
        finally:
            _CURRENT_CONTEXT.reset(token)

    def complete(
        self,
        observation: TelemetryObservation | None,
        outcome: Outcome,
        *,
        error: EventError | None = None,
    ) -> EmitDisposition:
        """Emit one v1 completion, returning failed rather than raising on any defect."""

        try:
            if observation is None:
                return EmitDisposition.DISABLED if not self.enabled else EmitDisposition.FAILED
            elapsed = max(0.0, self._monotonic() - observation.started_at)
            event = EventEnvelope(
                event_name=observation.event_name,
                event_version=observation.event_version,
                event_id=self._event_id_factory(),
                occurred_at=self._occurred_at_factory(),
                phase=EventPhase.COMPLETED,
                correlation_id=observation.context.correlation_id,
                causation_id=observation.start_event_id,
                trace_id=observation.context.trace_id,
                span_id=observation.context.span_id,
                identity=observation.identity,
                outcome=outcome,
                duration_ms=int(round(elapsed * 1000)),
                error=error,
                attributes=observation.attributes,
            )
            return emit_non_fatal(self.sink, event)
        except BaseException:
            return EmitDisposition.FAILED
