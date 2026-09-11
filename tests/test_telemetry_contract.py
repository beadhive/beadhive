"""Executable contract for semantic telemetry event-envelope v1."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, ValidationError

from beadhive.kernel.telemetry import (
    EVENT_ENVELOPE_SCHEMA_ID,
    EmitDisposition,
    ErrorClassification,
    EventEnvelope,
    EventError,
    EventIdentity,
    EventPhase,
    FlushOutcome,
    FlushResult,
    Outcome,
    RecordingTelemetrySink,
    SemanticEventName,
    SemanticTelemetry,
    TelemetryAttribute,
    emit_non_fatal,
    event_envelope_schema,
    flush_non_fatal,
)

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "docs" / "schemas" / "telemetry-event-envelope-v1.schema.json"
_SPEC = importlib.util.spec_from_file_location(
    "telemetry_contract_schema_compat", ROOT / "scripts" / "check_wire_schema_compat.py"
)
assert _SPEC is not None and _SPEC.loader is not None
_COMPAT = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _COMPAT
_SPEC.loader.exec_module(_COMPAT)
compatibility_errors = _COMPAT.compatibility_errors


class _RegistrySpoof(str):
    """A string whose Python identity disagrees with its serialized bytes."""

    def __new__(cls, raw: str, impersonates: str):
        value = super().__new__(cls, raw)
        value.impersonates = impersonates
        value.hash_calls = 0
        value.equality_calls = 0
        value.string_calls = 0
        return value

    def __hash__(self) -> int:
        self.hash_calls += 1
        return hash(self.impersonates)

    def __eq__(self, other: object) -> bool:
        self.equality_calls += 1
        return other == self.impersonates

    def __str__(self) -> str:
        self.string_calls += 1
        raise AssertionError("registry validation must not coerce caller-owned strings")


_FINITE_ATTRIBUTE_CASES = (
    ("operation.kind", "command"),
    ("surface", "internal"),
    ("transport", "in-process"),
    ("http.method", "GET"),
    ("http.status-class", "2xx"),
    ("dependency", "git"),
    ("reason.code", "invalid"),
    ("sampling.decision", "recorded"),
    ("shutdown.phase", "drain"),
)


def _event() -> EventEnvelope:
    return EventEnvelope(
        event_name=SemanticEventName.OPERATION_EXECUTION,
        event_version=1,
        event_id="018f47b1-2948-7c2a-b0f1-92d5be4d92ce",
        occurred_at="2026-09-10T00:00:00Z",
        phase=EventPhase.COMPLETED,
        correlation_id="018f47b1-2948-7c2a-b0f1-92d5be4d92cf",
        causation_id="018f47b1-2948-7c2a-b0f1-92d5be4d92d0",
        identity=EventIdentity(
            service="bh",
            instance_id="018f47b1-2948-7c2a-b0f1-92d5be4d92d1",
            hive_id="github/beadhive/beadhive",
            bead_id="bh-id9pp.1",
            actor="dev/telemetry-contract",
            seat="developer",
            plugin_id="observaloop",
        ),
        outcome=Outcome.SUCCEEDED,
        duration_ms=17,
        attributes=(
            TelemetryAttribute("operation.kind", "command"),
            TelemetryAttribute("operation.name", "work.claim"),
            TelemetryAttribute("surface", "cli"),
        ),
    )


@pytest.mark.parametrize(("key", "allowed"), _FINITE_ATTRIBUTE_CASES)
def test_finite_attributes_require_exact_builtin_strings_without_running_spoof_methods(
    key: str, allowed: str
) -> None:
    spoof = _RegistrySpoof(f"tenant-{key.replace('.', '-')}-customersecret", allowed)

    with pytest.raises(TypeError, match="built-in string"):
        TelemetryAttribute(key, spoof)

    assert (spoof.hash_calls, spoof.equality_calls, spoof.string_calls) == (0, 0, 0)
    assert TelemetryAttribute(key, allowed).value == allowed


@pytest.mark.parametrize(
    ("key", "allowed"),
    (("operation.name", "work.issue"), ("lifecycle.event", "plugin.discovered")),
)
def test_registry_identity_attributes_require_exact_builtin_strings_without_coercion(
    key: str, allowed: str
) -> None:
    spoof = _RegistrySpoof(f"tenant.customersecret.{key.replace('.', '-')}", allowed)

    with pytest.raises(TypeError, match="built-in string"):
        TelemetryAttribute(key, spoof)

    assert (spoof.hash_calls, spoof.equality_calls, spoof.string_calls) == (0, 0, 0)
    assert TelemetryAttribute(key, allowed).value == allowed


def test_checked_in_schema_is_valid_and_byte_deterministic() -> None:
    rendered = json.dumps(event_envelope_schema(), indent=2, sort_keys=True) + "\n"
    assert SCHEMA_PATH.read_text(encoding="utf-8") == rendered
    schema = json.loads(rendered)
    Draft202012Validator.check_schema(schema)
    assert schema["$id"] == EVENT_ENVELOPE_SCHEMA_ID


def test_event_serialization_validates_against_schema() -> None:
    event = _event()
    document = event.to_document()
    Draft202012Validator(event_envelope_schema()).validate(document)
    assert document == event.to_document()


@pytest.mark.parametrize(
    ("outcome", "error"),
    [
        (Outcome.SUCCEEDED, None),
        (Outcome.NO_OP, None),
        (Outcome.FAILED, EventError(ErrorClassification.DEPENDENCY, "dolt.unavailable", True)),
        (Outcome.TIMED_OUT, EventError(ErrorClassification.TIMEOUT, "deadline.exceeded", True)),
        (
            Outcome.CANCELLED,
            EventError(ErrorClassification.CANCELLATION, "operator.cancelled", False),
        ),
    ],
)
def test_completion_outcomes_have_explicit_bounded_semantics(
    outcome: Outcome, error: EventError | None
) -> None:
    event = replace(_event(), outcome=outcome, error=error)
    Draft202012Validator(event_envelope_schema()).validate(event.to_document())


@pytest.mark.parametrize("phase", [EventPhase.STARTED, EventPhase.OBSERVED])
def test_non_completion_phases_exclude_outcome_duration_and_error(phase: EventPhase) -> None:
    event = replace(_event(), phase=phase, outcome=None, duration_ms=None)
    document = event.to_document()
    assert (document["outcome"], document["duration_ms"], document["error"]) == (
        None,
        None,
        None,
    )
    Draft202012Validator(event_envelope_schema()).validate(document)


def test_invalid_completion_and_error_combinations_fail_closed() -> None:
    with pytest.raises(ValueError, match="require outcome and duration"):
        replace(_event(), outcome=None)
    with pytest.raises(ValueError, match="require one bounded error"):
        replace(_event(), outcome=Outcome.FAILED)
    with pytest.raises(ValueError, match="timeout error classification"):
        replace(
            _event(),
            outcome=Outcome.TIMED_OUT,
            error=EventError(ErrorClassification.INTERNAL, "deadline.exceeded"),
        )
    with pytest.raises(ValueError, match="cannot carry completion fields"):
        replace(_event(), phase=EventPhase.STARTED)


def test_causation_and_trace_identity_are_structurally_consistent() -> None:
    with pytest.raises(ValueError, match="cannot identify the event itself"):
        replace(_event(), causation_id=_event().event_id)
    with pytest.raises(ValueError, match="span_id requires trace_id"):
        replace(_event(), trace_id=None, span_id="1" * 16)
    with pytest.raises(ValueError, match="cannot be all zeroes"):
        replace(_event(), trace_id="0" * 32)
    with pytest.raises(ValueError, match="occurred_at"):
        replace(_event(), occurred_at="2026-99-99T00:00:00Z")


@pytest.mark.parametrize(
    "changes",
    [
        {"trace_id": "0" * 32},
        {"trace_id": "1" * 32, "span_id": "0" * 16},
    ],
)
def test_schema_and_model_reject_all_zero_trace_identity(changes: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        replace(_event(), **changes)

    document = _event().to_document()
    document.update(changes)
    with pytest.raises(ValidationError):
        Draft202012Validator(event_envelope_schema()).validate(document)


@pytest.mark.parametrize(
    ("trace_id", "span_id"),
    [
        (None, None),
        ("1" * 32, None),
        ("1" * 32, "2" * 16),
    ],
)
def test_schema_and_model_accept_valid_trace_span_relationships(
    trace_id: str | None, span_id: str | None
) -> None:
    event = replace(_event(), trace_id=trace_id, span_id=span_id)
    Draft202012Validator(event_envelope_schema()).validate(event.to_document())


def test_schema_and_model_reject_span_without_trace() -> None:
    span_id = "2" * 16
    with pytest.raises(ValueError, match="span_id requires trace_id"):
        replace(_event(), trace_id=None, span_id=span_id)

    document = _event().to_document()
    document["trace_id"] = None
    document["span_id"] = span_id
    with pytest.raises(ValidationError):
        Draft202012Validator(event_envelope_schema()).validate(document)


@pytest.mark.parametrize(
    "occurred_at",
    [
        "2024-02-29T23:59:59Z",
        "2026-01-01T00:00:00.1Z",
        "2026-12-31T23:59:59.123456Z",
    ],
)
def test_schema_and_model_accept_utc_timestamp_boundaries(occurred_at: str) -> None:
    event = replace(_event(), occurred_at=occurred_at)
    Draft202012Validator(event_envelope_schema()).validate(event.to_document())


@pytest.mark.parametrize(
    "occurred_at",
    [
        "2026-99-99T00:00:00Z",
        "2025-02-29T00:00:00Z",
        "2026-02-30T00:00:00Z",
        "2026-04-31T00:00:00Z",
        "0000-01-01T00:00:00Z",
        "2026-01-01T24:00:00Z",
        "2026-01-01T00:60:00Z",
        "2026-01-01T00:00:60Z",
        "2026-01-01T00:00:00.1234567Z",
    ],
)
def test_schema_and_model_reject_invalid_utc_timestamps(occurred_at: str) -> None:
    with pytest.raises(ValueError, match="occurred_at"):
        replace(_event(), occurred_at=occurred_at)

    document = _event().to_document()
    document["occurred_at"] = occurred_at
    with pytest.raises(ValidationError):
        Draft202012Validator(event_envelope_schema()).validate(document)


def test_identity_and_error_fields_cannot_be_raw_paths_or_text_dumps() -> None:
    with pytest.raises(ValueError, match="instance_id"):
        replace(_event(), identity=replace(_event().identity, instance_id="/home/user/token"))
    with pytest.raises(ValueError, match="error code"):
        EventError(ErrorClassification.INTERNAL, "RuntimeError: token=/secret/path")
    with pytest.raises(ValueError, match="plugin_version requires plugin_id"):
        EventIdentity(service="bh", instance_id="instance", plugin_version="1.2.3")
    document = _event().to_document()
    document["identity"]["plugin_id"] = None
    document["identity"]["plugin_version"] = "1.2.3"
    with pytest.raises(ValidationError):
        Draft202012Validator(event_envelope_schema()).validate(document)


def test_attribute_registry_rejects_duplicates_and_unbounded_values() -> None:
    with pytest.raises(ValueError, match="attribute keys must be unique"):
        replace(
            _event(),
            attributes=(
                TelemetryAttribute("surface", "cli"),
                TelemetryAttribute("surface", "daemon"),
            ),
        )
    with pytest.raises(ValueError, match="unregistered surface"):
        TelemetryAttribute("surface", "customer-specific-surface")
    with pytest.raises(ValueError, match="unclassified telemetry attribute"):
        TelemetryAttribute("plugin.payload", "opaque")


@pytest.mark.parametrize("key", ["token", "prompt", "env", "path", "plugin.payload"])
def test_schema_rejects_unclassified_or_sensitive_attributes(key: str) -> None:
    document = _event().to_document()
    document["attributes"][key] = "must-not-escape"
    with pytest.raises(ValidationError):
        Draft202012Validator(event_envelope_schema()).validate(document)


def test_same_major_compatibility_rejects_outcome_union_drift() -> None:
    released = event_envelope_schema()
    candidate = deepcopy(released)
    candidate["$defs"]["outcome"]["enum"].append("future-outcome")
    assert any(
        "closed-union members changed" in error
        for error in compatibility_errors(released, candidate)
    )


class _ExplodingSink:
    def emit(self, _event: EventEnvelope) -> EmitDisposition:
        raise RuntimeError("secret adapter failure")

    def flush(self, _timeout_seconds: float) -> FlushResult:
        raise RuntimeError("secret flush failure")


def test_adapter_exceptions_are_contained_at_both_port_operations() -> None:
    sink = _ExplodingSink()
    assert emit_non_fatal(sink, _event()) is EmitDisposition.FAILED
    assert flush_non_fatal(sink, 0.25) == FlushResult(FlushOutcome.FAILED, 0)


class _TelemetryControlFlow(BaseException):
    pass


@pytest.mark.parametrize("mode", ["sync", "awaited"])
@pytest.mark.parametrize("exception_type", [asyncio.CancelledError, _TelemetryControlFlow])
def test_flush_contains_exact_telemetry_side_control_flow_exceptions(
    mode: str, exception_type: type[BaseException]
) -> None:
    failure = exception_type(f"telemetry {mode} flush failed")
    raised: list[BaseException] = []

    async def fail_after_await() -> FlushResult:
        await asyncio.sleep(0)
        raised.append(failure)
        raise failure

    class Sink:
        def emit(self, _event: EventEnvelope) -> EmitDisposition:
            return EmitDisposition.ACCEPTED

        def flush(self, _timeout_seconds: float) -> FlushResult:
            if mode == "awaited":
                return asyncio.run(fail_after_await())
            raised.append(failure)
            raise failure

    assert flush_non_fatal(Sink(), 0.25) == FlushResult(FlushOutcome.FAILED, 0)
    assert len(raised) == 1
    assert raised[0] is failure


@pytest.mark.parametrize("exception_type", [asyncio.CancelledError, _TelemetryControlFlow])
def test_emit_contains_telemetry_side_control_flow_exceptions(
    exception_type: type[BaseException],
) -> None:
    class Sink:
        def emit(self, _event: EventEnvelope) -> EmitDisposition:
            raise exception_type("telemetry emit failed")

        def flush(self, _timeout_seconds: float) -> FlushResult:
            return FlushResult(FlushOutcome.NO_OP, 0)

    assert emit_non_fatal(Sink(), _event()) is EmitDisposition.FAILED


@pytest.mark.parametrize("seam", ["begin", "complete"])
@pytest.mark.parametrize("exception_type", [asyncio.CancelledError, _TelemetryControlFlow])
def test_semantic_telemetry_contains_construction_control_flow_exceptions(
    monkeypatch, seam: str, exception_type: type[BaseException]
) -> None:
    telemetry = SemanticTelemetry(
        sink=RecordingTelemetrySink(),
        identity=EventIdentity(service="bh", instance_id="instance-1"),
        event_id_factory=lambda: "event-id",
        occurred_at_factory=lambda: "2026-09-10T02:00:00Z",
        monotonic=lambda: 1.0,
    )

    def fail() -> float:
        raise exception_type(f"telemetry {seam} failed")

    if seam == "begin":
        monkeypatch.setattr(telemetry, "_monotonic", fail)
        assert telemetry.begin(SemanticEventName.OPERATION_EXECUTION) is None
    else:
        observation = telemetry.begin(SemanticEventName.OPERATION_EXECUTION)
        assert observation is not None
        monkeypatch.setattr(telemetry, "_monotonic", fail)
        assert telemetry.complete(observation, Outcome.SUCCEEDED) is EmitDisposition.FAILED


def test_flush_receives_one_finite_total_budget_and_validates_its_result() -> None:
    observed: list[float] = []

    class Sink:
        def emit(self, _event: EventEnvelope) -> EmitDisposition:
            return EmitDisposition.ACCEPTED

        def flush(self, timeout_seconds: float) -> FlushResult:
            observed.append(timeout_seconds)
            return FlushResult(FlushOutcome.NO_OP, 1)

    assert flush_non_fatal(Sink(), 0.25) == FlushResult(FlushOutcome.NO_OP, 1)
    assert observed == [0.25]
    for invalid in (0.0, -1.0, float("inf")):
        with pytest.raises(ValueError, match="finite and greater than zero"):
            flush_non_fatal(Sink(), invalid)
    assert observed == [0.25]


def test_sampling_and_disabled_outcomes_are_bounded_dispositions() -> None:
    assert {item.value for item in EmitDisposition} == {
        "accepted",
        "sampled-out",
        "disabled",
        "capacity-dropped",
        "failed",
    }
