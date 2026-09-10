"""Transport-neutral semantic telemetry domain model and outbound port.

This module intentionally uses only the Python standard library.  OpenTelemetry and other
export mechanisms belong in adapters; kernel and application consumers depend on these values.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

EVENT_ENVELOPE_SCHEMA_ID = "urn:beadhive:wire-schema:telemetry.event-envelope:1"
EVENT_ENVELOPE_SCHEMA_VERSION = 1
MAX_ATTRIBUTES = 16

_EVENT_NAME = re.compile(r"^beadhive(?:\.[a-z][a-z0-9-]*){2,7}$")
_DOTTED_ID = re.compile(r"^[a-z][a-z0-9-]*(?:\.[a-z][a-z0-9-]*){1,7}$")
_OPAQUE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_HIVE_ID = re.compile(
    r"^[a-z0-9][a-z0-9._-]{0,63}/[A-Za-z0-9][A-Za-z0-9._-]{0,127}/"
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
)
_BEAD_ID = re.compile(r"^[A-Za-z][A-Za-z0-9]*-[A-Za-z0-9]+(?:\.[0-9]+)?$")
_ACTOR_ID = re.compile(r"^[a-z][a-z0-9-]*/[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_PLUGIN_ID = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_UTC_TIMESTAMP = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:"
    r"[0-9]{2}(?:\.[0-9]{1,6})?Z$"
)
_ERROR_CODE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+){0,7}$")
_TRACE_ID = re.compile(r"^[0-9a-f]{32}$")
_SPAN_ID = re.compile(r"^[0-9a-f]{16}$")


def _require_pattern(
    value: str, pattern: re.Pattern[str], field_name: str, *, max_length: int | None = None
) -> None:
    if (max_length is not None and len(value) > max_length) or not pattern.fullmatch(value):
        raise ValueError(f"invalid {field_name}: {value!r}")


class EventPhase(StrEnum):
    STARTED = "started"
    COMPLETED = "completed"
    OBSERVED = "observed"


class SemanticEventName(StrEnum):
    OPERATION_EXECUTION = "beadhive.operation.execution"
    LIFECYCLE_DELIVERY = "beadhive.lifecycle.delivery"
    TELEMETRY_FLUSH = "beadhive.telemetry.flush"


class Outcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed-out"
    CANCELLED = "cancelled"
    NO_OP = "no-op"


class ErrorClassification(StrEnum):
    VALIDATION = "validation"
    CONFIGURATION = "configuration"
    AUTHORIZATION = "authorization"
    CONFLICT = "conflict"
    DEPENDENCY = "dependency"
    TIMEOUT = "timeout"
    CANCELLATION = "cancellation"
    INTERNAL = "internal"
    UNAVAILABLE = "unavailable"


class Seat(StrEnum):
    DEVELOPER = "developer"
    DISPATCHER = "dispatcher"
    REVIEWER = "reviewer"
    MERGER = "merger"
    PLANNER = "planner"
    SUPERVISOR = "supervisor"
    DIRECTOR = "director"
    CUSTODIAN = "custodian"
    CONTROLLER = "controller"


class AttributeKey(StrEnum):
    OPERATION_KIND = "operation.kind"
    OPERATION_NAME = "operation.name"
    LIFECYCLE_EVENT = "lifecycle.event"
    SURFACE = "surface"
    TRANSPORT = "transport"
    HTTP_METHOD = "http.method"
    HTTP_STATUS_CLASS = "http.status-class"
    DEPENDENCY = "dependency"
    REASON_CODE = "reason.code"
    RETRY_COUNT = "retry.count"
    SAMPLING_DECISION = "sampling.decision"
    SHUTDOWN_PHASE = "shutdown.phase"


class EmitDisposition(StrEnum):
    ACCEPTED = "accepted"
    SAMPLED_OUT = "sampled-out"
    DISABLED = "disabled"
    CAPACITY_DROPPED = "capacity-dropped"
    FAILED = "failed"


class FlushOutcome(StrEnum):
    COMPLETED = "completed"
    TIMED_OUT = "timed-out"
    FAILED = "failed"
    NO_OP = "no-op"


_FINITE_ATTRIBUTE_VALUES: dict[AttributeKey, frozenset[str]] = {
    AttributeKey.OPERATION_KIND: frozenset(
        {"command", "tool", "resource", "route", "lifecycle", "internal"}
    ),
    AttributeKey.SURFACE: frozenset(
        {"cli", "mcp-stdio", "mcp-http", "daemon", "gateway", "internal"}
    ),
    AttributeKey.TRANSPORT: frozenset(
        {"in-process", "stdio", "http", "sse", "websocket", "grpc", "otlp-http", "other"}
    ),
    AttributeKey.HTTP_METHOD: frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "OTHER"}),
    AttributeKey.HTTP_STATUS_CLASS: frozenset({"1xx", "2xx", "3xx", "4xx", "5xx", "none"}),
    AttributeKey.DEPENDENCY: frozenset(
        {"beads", "dolt", "git", "hq", "collector", "plugin", "other"}
    ),
    AttributeKey.REASON_CODE: frozenset(
        {
            "already-satisfied",
            "auth-expired",
            "auth-revoked",
            "auth-rotated",
            "cancelled",
            "client-closed",
            "conflict",
            "daemon-shutdown",
            "deadline-exceeded",
            "invalid",
            "not-configured",
            "source-discontinuity",
            "timeout",
            "unavailable",
            "unknown",
            "other",
        }
    ),
    AttributeKey.SAMPLING_DECISION: frozenset(
        {"recorded", "sampled-out", "disabled", "capacity-dropped"}
    ),
    AttributeKey.SHUTDOWN_PHASE: frozenset(
        {"drain", "close-sessions", "cancel-processes", "close-resources", "flush-telemetry"}
    ),
}


@dataclass(frozen=True, slots=True)
class TelemetryAttribute:
    """One classified event dimension from the finite v1 key registry."""

    key: AttributeKey | str
    value: str | int | bool

    def __post_init__(self) -> None:
        try:
            key = AttributeKey(self.key)
        except ValueError as exc:
            raise ValueError(f"unclassified telemetry attribute: {self.key!r}") from exc
        object.__setattr__(self, "key", key)
        if key is AttributeKey.RETRY_COUNT:
            if type(self.value) is not int or not 0 <= self.value <= 10:
                raise ValueError("retry.count must be an integer from 0 through 10")
            return
        if type(self.value) is not str:
            raise TypeError(f"{key.value} must be a built-in string")
        finite = _FINITE_ATTRIBUTE_VALUES.get(key)
        if finite is not None:
            if self.value not in finite:
                raise ValueError(f"unregistered {key.value} value: {self.value!r}")
        elif key in {AttributeKey.OPERATION_NAME, AttributeKey.LIFECYCLE_EVENT}:
            _require_pattern(self.value, _DOTTED_ID, key.value, max_length=128)
        else:
            _require_pattern(self.value, _ERROR_CODE, key.value, max_length=128)


@dataclass(frozen=True, slots=True)
class EventIdentity:
    """Allowlisted attribution; fields absent at the observation point stay null."""

    service: str
    instance_id: str
    host_id: str | None = None
    hive_id: str | None = None
    bead_id: str | None = None
    run_id: str | None = None
    actor: str | None = None
    seat: Seat | str | None = None
    plugin_id: str | None = None
    plugin_version: str | None = None

    def __post_init__(self) -> None:
        _require_pattern(self.service, _PLUGIN_ID, "service")
        _require_pattern(self.instance_id, _OPAQUE_ID, "instance_id")
        for field_name in ("host_id", "run_id"):
            value = getattr(self, field_name)
            if value is not None:
                _require_pattern(value, _OPAQUE_ID, field_name)
        if self.hive_id is not None:
            _require_pattern(self.hive_id, _HIVE_ID, "hive_id")
        if self.bead_id is not None:
            _require_pattern(self.bead_id, _BEAD_ID, "bead_id")
        if self.actor is not None:
            _require_pattern(self.actor, _ACTOR_ID, "actor")
        if self.seat is not None:
            try:
                object.__setattr__(self, "seat", Seat(self.seat))
            except ValueError as exc:
                raise ValueError(f"unregistered seat: {self.seat!r}") from exc
        if self.plugin_id is not None:
            _require_pattern(self.plugin_id, _PLUGIN_ID, "plugin_id")
        if self.plugin_version is not None:
            _require_pattern(self.plugin_version, _SEMVER, "plugin_version")
        if self.plugin_version is not None and self.plugin_id is None:
            raise ValueError("plugin_version requires plugin_id")

    def to_document(self) -> dict[str, str | None]:
        return {
            "actor": self.actor,
            "bead_id": self.bead_id,
            "hive_id": self.hive_id,
            "host_id": self.host_id,
            "instance_id": self.instance_id,
            "plugin_id": self.plugin_id,
            "plugin_version": self.plugin_version,
            "run_id": self.run_id,
            "seat": self.seat.value if self.seat is not None else None,
            "service": self.service,
        }


@dataclass(frozen=True, slots=True)
class EventError:
    """Bounded machine classification; exception messages and stack text are excluded."""

    classification: ErrorClassification | str
    code: str
    retryable: bool = False

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "classification", ErrorClassification(self.classification))
        except ValueError as exc:
            raise ValueError(f"unregistered error classification: {self.classification!r}") from exc
        _require_pattern(self.code, _ERROR_CODE, "error code", max_length=128)
        if type(self.retryable) is not bool:
            raise TypeError("retryable must be a boolean")

    def to_document(self) -> dict[str, str | bool]:
        return {
            "classification": self.classification.value,
            "code": self.code,
            "retryable": self.retryable,
        }


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    """One immutable semantic observation, independent of its eventual transport."""

    event_name: SemanticEventName | str
    event_version: int
    event_id: str
    occurred_at: str
    phase: EventPhase | str
    correlation_id: str
    identity: EventIdentity
    causation_id: str | None = None
    trace_id: str | None = None
    span_id: str | None = None
    outcome: Outcome | str | None = None
    duration_ms: int | None = None
    error: EventError | None = None
    attributes: tuple[TelemetryAttribute, ...] = field(default_factory=tuple)
    schema_version: int = field(default=EVENT_ENVELOPE_SCHEMA_VERSION, init=False)

    def __post_init__(self) -> None:
        _require_pattern(self.event_name, _EVENT_NAME, "event_name", max_length=128)
        try:
            object.__setattr__(self, "event_name", SemanticEventName(self.event_name))
        except ValueError as exc:
            raise ValueError(f"unregistered semantic event name: {self.event_name!r}") from exc
        if type(self.event_version) is not int or self.event_version < 1:
            raise ValueError("event_version must be a positive integer")
        _require_pattern(self.event_id, _OPAQUE_ID, "event_id")
        _require_pattern(self.occurred_at, _UTC_TIMESTAMP, "occurred_at")
        try:
            datetime.fromisoformat(self.occurred_at.removesuffix("Z") + "+00:00")
        except ValueError as exc:
            raise ValueError(f"invalid occurred_at: {self.occurred_at!r}") from exc
        _require_pattern(self.correlation_id, _OPAQUE_ID, "correlation_id")
        if self.causation_id is not None:
            _require_pattern(self.causation_id, _OPAQUE_ID, "causation_id")
            if self.causation_id == self.event_id:
                raise ValueError("causation_id cannot identify the event itself")
        if self.trace_id is not None:
            _require_pattern(self.trace_id, _TRACE_ID, "trace_id")
            if set(self.trace_id) == {"0"}:
                raise ValueError("trace_id cannot be all zeroes")
        if self.span_id is not None:
            _require_pattern(self.span_id, _SPAN_ID, "span_id")
            if set(self.span_id) == {"0"}:
                raise ValueError("span_id cannot be all zeroes")
        if self.span_id is not None and self.trace_id is None:
            raise ValueError("span_id requires trace_id")
        try:
            object.__setattr__(self, "phase", EventPhase(self.phase))
        except ValueError as exc:
            raise ValueError(f"unregistered event phase: {self.phase!r}") from exc
        if self.outcome is not None:
            try:
                object.__setattr__(self, "outcome", Outcome(self.outcome))
            except ValueError as exc:
                raise ValueError(f"unregistered outcome: {self.outcome!r}") from exc
        attributes = tuple(sorted(self.attributes, key=lambda item: item.key.value))
        object.__setattr__(self, "attributes", attributes)
        if len(attributes) > MAX_ATTRIBUTES:
            raise ValueError(f"an event may carry at most {MAX_ATTRIBUTES} attributes")
        keys = tuple(attribute.key for attribute in attributes)
        if len(keys) != len(set(keys)):
            raise ValueError("telemetry attribute keys must be unique")
        if self.duration_ms is not None and (
            type(self.duration_ms) is not int or self.duration_ms < 0
        ):
            raise ValueError("duration_ms must be a non-negative integer")
        self._validate_completion()

    def _validate_completion(self) -> None:
        if self.phase is EventPhase.COMPLETED:
            if self.outcome is None or self.duration_ms is None:
                raise ValueError("completed events require outcome and duration_ms")
        elif any(value is not None for value in (self.outcome, self.duration_ms, self.error)):
            raise ValueError("started and observed events cannot carry completion fields")
        failed = self.outcome in {Outcome.FAILED, Outcome.TIMED_OUT, Outcome.CANCELLED}
        if failed != (self.error is not None):
            raise ValueError("failed, timed-out, and cancelled outcomes require one bounded error")
        if self.outcome is Outcome.TIMED_OUT and (
            self.error is None or self.error.classification is not ErrorClassification.TIMEOUT
        ):
            raise ValueError("timed-out outcome requires timeout error classification")
        if self.outcome is Outcome.CANCELLED and (
            self.error is None or self.error.classification is not ErrorClassification.CANCELLATION
        ):
            raise ValueError("cancelled outcome requires cancellation error classification")

    def to_document(self) -> dict[str, Any]:
        return {
            "attributes": {attribute.key.value: attribute.value for attribute in self.attributes},
            "causation_id": self.causation_id,
            "correlation_id": self.correlation_id,
            "duration_ms": self.duration_ms,
            "error": self.error.to_document() if self.error is not None else None,
            "event_id": self.event_id,
            "event_name": self.event_name.value,
            "event_version": self.event_version,
            "identity": self.identity.to_document(),
            "occurred_at": self.occurred_at,
            "outcome": self.outcome.value if self.outcome is not None else None,
            "phase": self.phase.value,
            "schema_version": self.schema_version,
            "span_id": self.span_id,
            "trace_id": self.trace_id,
        }


@dataclass(frozen=True, slots=True)
class TelemetryContext:
    """Transport-neutral correlation propagated through nested semantic work."""

    correlation_id: str
    causation_id: str | None = None
    trace_id: str | None = None
    span_id: str | None = None


@dataclass(frozen=True, slots=True)
class TelemetryObservation:
    """Start token used to close one semantic observation exactly once."""

    event_name: SemanticEventName
    event_version: int
    start_event_id: str
    started_at: float
    context: TelemetryContext
    identity: EventIdentity
    attributes: tuple[TelemetryAttribute, ...]


@dataclass(frozen=True, slots=True)
class FlushResult:
    outcome: FlushOutcome | str
    duration_ms: int

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "outcome", FlushOutcome(self.outcome))
        except ValueError as exc:
            raise ValueError(f"unregistered flush outcome: {self.outcome!r}") from exc
        if type(self.duration_ms) is not int or self.duration_ms < 0:
            raise ValueError("flush duration_ms must be a non-negative integer")


class TelemetrySink(Protocol):
    """Outbound telemetry port.

    ``emit`` must be non-blocking and must not raise. ``flush`` must respect the supplied finite
    total budget. Core callers still use the defensive helpers below so a nonconforming adapter
    cannot change application control flow.
    """

    def emit(self, event: EventEnvelope) -> EmitDisposition: ...

    def flush(self, timeout_seconds: float) -> FlushResult: ...


class SemanticTelemetryPort(Protocol):
    """Minimal semantic observation seam consumed by other kernel owners."""

    @property
    def identity(self) -> EventIdentity: ...

    def begin(
        self,
        event_name: SemanticEventName,
        *,
        correlation_id: str | None = None,
        identity: EventIdentity | None = None,
        attributes: tuple[TelemetryAttribute, ...] = (),
    ) -> TelemetryObservation | None: ...

    def activate(
        self, observation: TelemetryObservation | None
    ) -> AbstractContextManager[None]: ...

    def complete(
        self,
        observation: TelemetryObservation | None,
        outcome: Outcome,
        *,
        error: EventError | None = None,
    ) -> EmitDisposition: ...


@contextmanager
def activate_non_fatal(
    telemetry: SemanticTelemetryPort | None,
    observation: TelemetryObservation | None,
) -> Iterator[None]:
    """Activate correlation without allowing a telemetry fault to affect application work."""
    if telemetry is None:
        yield
        return

    activation: object | None = None
    exit_method: Callable[..., object] | None = None
    try:
        activation = telemetry.activate(observation)
        activation_type = type(activation)
        exit_method = activation_type.__exit__
        enter_method = activation_type.__enter__
        enter_method(activation)
    except BaseException as activation_error:
        if activation is not None and exit_method is not None:
            try:
                exit_method(
                    activation,
                    type(activation_error),
                    activation_error,
                    activation_error.__traceback__,
                )
            except BaseException:
                pass
        yield
        return

    try:
        yield
    except BaseException as application_error:
        try:
            exit_method(
                activation,
                type(application_error),
                application_error,
                application_error.__traceback__,
            )
        except BaseException:
            pass
        raise
    else:
        try:
            exit_method(activation, None, None, None)
        except BaseException:
            pass


def emit_non_fatal(sink: TelemetrySink, event: EventEnvelope) -> EmitDisposition:
    """Emit without allowing an adapter failure to affect application behavior."""
    try:
        result = sink.emit(event)
        return EmitDisposition(result)
    except BaseException:
        return EmitDisposition.FAILED


def flush_non_fatal(sink: TelemetrySink, timeout_seconds: float) -> FlushResult:
    """Request one finite flush and classify adapter failure without propagating it."""
    if timeout_seconds <= 0 or not math.isfinite(timeout_seconds):
        raise ValueError("telemetry flush timeout must be finite and greater than zero")
    try:
        result = sink.flush(timeout_seconds)
        return FlushResult(result.outcome, result.duration_ms)
    except Exception:
        return FlushResult(FlushOutcome.FAILED, 0)


class TraceVerb(Protocol):
    """Decorate one operation handler while preserving its callable signature."""

    def __call__(self, operation: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]: ...
