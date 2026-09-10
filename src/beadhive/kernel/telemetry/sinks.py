"""Small bounded semantic telemetry sinks for disabled and test composition."""

from __future__ import annotations

import math
import threading

from .contracts import EmitDisposition, EventEnvelope, FlushOutcome, FlushResult


def _validate_capacity(capacity: int) -> None:
    if type(capacity) is not int or capacity < 1:
        raise ValueError("recording telemetry capacity must be a positive integer")


def _validate_timeout(timeout_seconds: float) -> None:
    if timeout_seconds <= 0 or not math.isfinite(timeout_seconds):
        raise ValueError("telemetry flush timeout must be finite and greater than zero")


class NoOpTelemetrySink:
    """A disabled sink which allocates no exporter, queue, worker, or event storage."""

    enabled = False

    @property
    def events(self) -> tuple[EventEnvelope, ...]:
        return ()

    def emit(self, _event: EventEnvelope) -> EmitDisposition:
        return EmitDisposition.DISABLED

    def flush(self, timeout_seconds: float) -> FlushResult:
        _validate_timeout(timeout_seconds)
        return FlushResult(FlushOutcome.NO_OP, 0)


class RecordingTelemetrySink:
    """A deterministic, thread-safe, capacity-bounded sink for contract tests."""

    enabled = True

    def __init__(self, capacity: int = 1024) -> None:
        _validate_capacity(capacity)
        self._capacity = capacity
        self._events: list[EventEnvelope] = []
        self._lock = threading.Lock()

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def events(self) -> tuple[EventEnvelope, ...]:
        with self._lock:
            return tuple(self._events)

    def emit(self, event: EventEnvelope) -> EmitDisposition:
        with self._lock:
            if len(self._events) >= self._capacity:
                return EmitDisposition.CAPACITY_DROPPED
            self._events.append(event)
        return EmitDisposition.ACCEPTED

    def flush(self, timeout_seconds: float) -> FlushResult:
        _validate_timeout(timeout_seconds)
        return FlushResult(FlushOutcome.NO_OP, 0)
