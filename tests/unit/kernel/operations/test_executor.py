from __future__ import annotations

import asyncio
import importlib
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from beadhive.kernel.operations import OperationExecutor, OperationSpec, operations
from beadhive.kernel.telemetry import (
    EmitDisposition,
    EventEnvelope,
    EventIdentity,
    FlushOutcome,
    FlushResult,
    NoOpTelemetrySink,
    Outcome,
    RecordingTelemetrySink,
    SemanticEventName,
    SemanticTelemetry,
    current_telemetry_context,
)
from harness import processes


def _next(values: Iterator[Any]) -> Callable[[], Any]:
    return lambda: next(values)


def _spec(name: str, kind: str = "action") -> OperationSpec:
    return OperationSpec(
        name=name,
        parameters=(),
        result_schema="urn:test:result",
        kind=kind,
        privilege="ordinary-mutation",
        constraints={},
        surfaces={},
    )


class _SideEffectOperation:
    def __init__(
        self,
        *,
        names: tuple[str | BaseException, ...] = ("work.issue",),
        kinds: tuple[str | BaseException, ...] = ("action",),
    ) -> None:
        self._names = names
        self._kinds = kinds
        self.name_reads = 0
        self.kind_reads = 0

    @staticmethod
    def _read(values: tuple[str | BaseException, ...], number: int) -> str:
        value = values[min(number - 1, len(values) - 1)]
        if isinstance(value, BaseException):
            raise value
        return value

    @property
    def name(self) -> str:
        self.name_reads += 1
        return self._read(self._names, self.name_reads)

    @property
    def kind(self) -> str:
        self.kind_reads += 1
        return self._read(self._kinds, self.kind_reads)


class _RegistrySpoof(str):
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
        raise AssertionError("operation instrumentation must not coerce structural strings")


def _telemetry(sink: RecordingTelemetrySink, ids: list[str], monotonic: list[float]):
    return SemanticTelemetry(
        sink=sink,
        identity=EventIdentity(service="bh", instance_id="instance-1"),
        event_id_factory=_next(iter(ids)),
        occurred_at_factory=lambda: "2026-09-10T02:00:00Z",
        monotonic=_next(iter(monotonic)),
    )


def _record_registered_name_in_fresh_process(results: Any) -> None:
    sink = RecordingTelemetrySink()
    result = OperationExecutor(_telemetry(sink, ["start", "end"], [1.0, 1.1])).execute(
        _spec("work.issue"), lambda: "application-ok"
    )
    results.put((result, sink.events[0].to_document()["attributes"].get("operation.name")))


_ACTIVATION_FAULTS = ("call", "none", "non-context", "enter", "exit")


class _TelemetryControlFlow(BaseException):
    pass


class _ApplicationControlFlow(BaseException):
    pass


_TELEMETRY_EXCEPTIONS = (asyncio.CancelledError, _TelemetryControlFlow)


def _install_activation_fault(
    monkeypatch,
    telemetry: SemanticTelemetry,
    fault: str,
    exception_type: type[BaseException] = RuntimeError,
) -> None:
    original = telemetry.activate

    def activate(observation):
        if fault == "call":
            raise exception_type("telemetry activation call failed")
        if fault == "none":
            return None
        if fault == "non-context":
            return object()
        delegate = original(observation)

        class FaultingContext:
            def __enter__(self):
                entered = delegate.__enter__()
                if fault == "enter":
                    raise exception_type("telemetry activation enter failed")
                return entered

            def __exit__(self, *exc):
                delegate.__exit__(*exc)
                if fault == "exit":
                    raise exception_type("telemetry activation exit failed")
                return False

        return FaultingContext()

    monkeypatch.setattr(telemetry, "activate", activate)


def _install_port_fault(
    monkeypatch,
    telemetry: SemanticTelemetry,
    seam: str,
    exception_type: type[BaseException],
) -> None:
    def fail(*_args, **_kwargs):
        raise exception_type(f"telemetry {seam} failed")

    monkeypatch.setattr(telemetry, seam, fail)


@pytest.mark.parametrize("seam", ["begin", "complete"])
@pytest.mark.parametrize("exception_type", _TELEMETRY_EXCEPTIONS)
def test_sync_port_control_flow_failure_preserves_handler_and_result(
    monkeypatch, seam: str, exception_type: type[BaseException]
) -> None:
    sink = RecordingTelemetrySink()
    telemetry = _telemetry(sink, ["start", "end"], [1.0, 1.25])
    _install_port_fault(monkeypatch, telemetry, seam, exception_type)
    result = object()
    calls: list[str] = []

    assert (
        OperationExecutor(telemetry).execute(
            _spec("work.issue"), lambda: calls.append("ran") or result
        )
        is result
    )
    assert calls == ["ran"]
    assert current_telemetry_context() is None


@pytest.mark.parametrize("seam", ["begin", "complete"])
@pytest.mark.parametrize("exception_type", _TELEMETRY_EXCEPTIONS)
def test_async_port_control_flow_failure_preserves_handler_and_result(
    monkeypatch, seam: str, exception_type: type[BaseException]
) -> None:
    sink = RecordingTelemetrySink()
    telemetry = _telemetry(sink, ["start", "end"], [1.0, 1.25])
    _install_port_fault(monkeypatch, telemetry, seam, exception_type)
    result = object()
    calls: list[str] = []

    async def handler():
        calls.append("ran")
        return result

    assert (
        asyncio.run(OperationExecutor(telemetry).execute_async(_spec("work.issue"), handler))
        is result
    )
    assert calls == ["ran"]
    assert current_telemetry_context() is None


@pytest.mark.parametrize("exception_type", _TELEMETRY_EXCEPTIONS)
@pytest.mark.parametrize("fault", ["call", "enter", "exit"])
def test_activation_control_flow_failures_preserve_sync_and_async_results(
    monkeypatch, fault: str, exception_type: type[BaseException]
) -> None:
    sink = RecordingTelemetrySink()
    telemetry = _telemetry(
        sink,
        ["sync-start", "sync-end", "async-start", "async-end"],
        [1.0, 1.1, 2.0, 2.1],
    )
    _install_activation_fault(monkeypatch, telemetry, fault, exception_type)
    executor = OperationExecutor(telemetry)

    async def async_handler():
        return "async-result"

    assert executor.execute(_spec("work.issue"), lambda: "sync-result") == "sync-result"
    assert asyncio.run(executor.execute_async(_spec("work.issue"), async_handler)) == "async-result"
    assert current_telemetry_context() is None


@pytest.mark.parametrize("fault", _ACTIVATION_FAULTS)
def test_sync_activation_failures_never_prevent_handler_or_replace_result(
    monkeypatch, fault: str
) -> None:
    sink = RecordingTelemetrySink()
    telemetry = _telemetry(sink, ["start", "end"], [1.0, 1.25])
    _install_activation_fault(monkeypatch, telemetry, fault)
    executor = OperationExecutor(telemetry)
    result = object()
    calls: list[str] = []

    assert executor.execute(_spec("work.issue"), lambda: calls.append("ran") or result) is result
    assert calls == ["ran"]
    assert current_telemetry_context() is None
    assert sink.events[-1].outcome is Outcome.SUCCEEDED


@pytest.mark.parametrize("fault", _ACTIVATION_FAULTS)
def test_async_activation_failures_never_prevent_handler_or_replace_result(
    monkeypatch, fault: str
) -> None:
    sink = RecordingTelemetrySink()
    telemetry = _telemetry(sink, ["start", "end"], [1.0, 1.25])
    _install_activation_fault(monkeypatch, telemetry, fault)
    executor = OperationExecutor(telemetry)
    result = object()
    calls: list[str] = []

    async def handler():
        calls.append("ran")
        return result

    assert asyncio.run(executor.execute_async(_spec("work.issue"), handler)) is result
    assert calls == ["ran"]
    assert current_telemetry_context() is None
    assert sink.events[-1].outcome is Outcome.SUCCEEDED


@pytest.mark.parametrize(
    ("failure", "outcome"),
    [
        (RuntimeError("application failed"), Outcome.FAILED),
        (TimeoutError("application timed out"), Outcome.TIMED_OUT),
        (asyncio.CancelledError("application cancelled"), Outcome.CANCELLED),
    ],
)
def test_activation_exit_failure_never_replaces_sync_application_failure(
    monkeypatch, failure: BaseException, outcome: Outcome
) -> None:
    sink = RecordingTelemetrySink()
    telemetry = _telemetry(sink, ["start", "end"], [1.0, 1.25])
    _install_activation_fault(monkeypatch, telemetry, "exit")

    def handler() -> None:
        raise failure

    with pytest.raises(type(failure)) as caught:
        OperationExecutor(telemetry).execute(_spec("work.issue"), handler)

    assert caught.value is failure
    assert current_telemetry_context() is None
    assert sink.events[-1].outcome is outcome


@pytest.mark.parametrize(
    ("failure", "outcome"),
    [
        (RuntimeError("application failed"), Outcome.FAILED),
        (TimeoutError("application timed out"), Outcome.TIMED_OUT),
        (asyncio.CancelledError("application cancelled"), Outcome.CANCELLED),
    ],
)
def test_activation_exit_failure_never_replaces_async_application_failure(
    monkeypatch, failure: BaseException, outcome: Outcome
) -> None:
    sink = RecordingTelemetrySink()
    telemetry = _telemetry(sink, ["start", "end"], [1.0, 1.25])
    _install_activation_fault(monkeypatch, telemetry, "exit")

    async def handler() -> None:
        raise failure

    with pytest.raises(type(failure)) as caught:
        asyncio.run(OperationExecutor(telemetry).execute_async(_spec("work.issue"), handler))

    assert caught.value is failure
    assert current_telemetry_context() is None
    assert sink.events[-1].outcome is outcome


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("application failed"),
        TimeoutError("application timed out"),
        asyncio.CancelledError("application cancelled"),
        _ApplicationControlFlow("application control flow"),
    ],
)
@pytest.mark.parametrize("exception_type", _TELEMETRY_EXCEPTIONS)
def test_complete_control_flow_failure_never_replaces_sync_application_failure(
    monkeypatch, failure: BaseException, exception_type: type[BaseException]
) -> None:
    telemetry = _telemetry(RecordingTelemetrySink(), ["start"], [1.0])
    _install_port_fault(monkeypatch, telemetry, "complete", exception_type)

    def handler() -> None:
        raise failure

    with pytest.raises(type(failure)) as caught:
        OperationExecutor(telemetry).execute(_spec("work.issue"), handler)

    assert caught.value is failure
    assert current_telemetry_context() is None


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("application failed"),
        TimeoutError("application timed out"),
        asyncio.CancelledError("application cancelled"),
        _ApplicationControlFlow("application control flow"),
    ],
)
@pytest.mark.parametrize("exception_type", _TELEMETRY_EXCEPTIONS)
def test_complete_control_flow_failure_never_replaces_async_application_failure(
    monkeypatch, failure: BaseException, exception_type: type[BaseException]
) -> None:
    telemetry = _telemetry(RecordingTelemetrySink(), ["start"], [1.0])
    _install_port_fault(monkeypatch, telemetry, "complete", exception_type)

    async def handler() -> None:
        raise failure

    with pytest.raises(type(failure)) as caught:
        asyncio.run(OperationExecutor(telemetry).execute_async(_spec("work.issue"), handler))

    assert caught.value is failure
    assert current_telemetry_context() is None


def test_nested_operations_propagate_one_correlation_and_direct_causation() -> None:
    sink = RecordingTelemetrySink()
    telemetry = _telemetry(
        sink,
        ["outer-start", "inner-start", "inner-end", "outer-end"],
        [10.0, 10.1, 10.2, 10.4],
    )
    executor = OperationExecutor(telemetry, surface="cli")

    def outer() -> str:
        return "outer:" + executor.execute(_spec("work.issue", "read-resource"), lambda: "inner")

    assert executor.execute(_spec("work.claim"), outer) == "outer:inner"
    assert [event.phase.value for event in sink.events] == [
        "started",
        "started",
        "completed",
        "completed",
    ]
    assert [event.event_name for event in sink.events] == [
        SemanticEventName.OPERATION_EXECUTION,
    ] * 4
    assert {event.event_version for event in sink.events} == {1}
    assert {event.correlation_id for event in sink.events} == {"outer-start"}
    assert [event.causation_id for event in sink.events] == [
        None,
        "outer-start",
        "inner-start",
        "outer-start",
    ]
    assert [event.outcome for event in sink.events] == [
        None,
        None,
        Outcome.SUCCEEDED,
        Outcome.SUCCEEDED,
    ]
    assert sink.events[0].to_document()["attributes"] == {
        "operation.kind": "command",
        "operation.name": "work.claim",
        "surface": "cli",
    }
    assert sink.events[1].to_document()["attributes"]["operation.kind"] == "resource"
    assert current_telemetry_context() is None


def test_registered_operation_name_is_emitted_from_the_canonical_catalog() -> None:
    sink = RecordingTelemetrySink()
    telemetry = _telemetry(sink, ["start", "end"], [1.0, 1.1])

    assert OperationExecutor(telemetry).execute(_spec("work.issue"), lambda: 42) == 42

    assert len(sink.events) == 2
    assert all(
        event.to_document()["attributes"]["operation.name"] == "work.issue" for event in sink.events
    )


def test_many_unknown_secret_operation_names_are_omitted_from_telemetry() -> None:
    sink = RecordingTelemetrySink(capacity=24)
    telemetry = _telemetry(
        sink,
        [f"event-{number}" for number in range(24)],
        [number / 10 for number in range(24)],
    )
    executor = OperationExecutor(telemetry)
    unknown_names = tuple(f"tenant-{number:02d}.customersecret.work" for number in range(12))
    registered_names = {operation.name for operation in operations()}
    assert registered_names.isdisjoint(unknown_names)

    for name in unknown_names:
        assert executor.execute(_spec(name), lambda value=name: value) == name

    assert len(sink.events) == 24
    for event in sink.events:
        document = event.to_document()
        assert event.event_name.value == "beadhive.operation.execution"
        assert document["attributes"] == {
            "operation.kind": "command",
            "surface": "internal",
        }
        assert not any(name in repr(document) for name in unknown_names)
        assert "customersecret" not in repr(document)


def test_mutable_operation_name_emits_only_the_once_validated_snapshot() -> None:
    sink = RecordingTelemetrySink()
    telemetry = _telemetry(sink, ["start", "end"], [1.0, 1.1])
    operation = _SideEffectOperation(names=("work.issue", "tenant.customersecret.work"))
    result = object()

    assert OperationExecutor(telemetry).execute(operation, lambda: result) is result

    assert len(sink.events) == 2
    for event in sink.events:
        document = event.to_document()
        assert document["attributes"]["operation.name"] == "work.issue"
        assert "customersecret" not in repr(document)
    assert operation.name_reads == 1
    assert operation.kind_reads == 1


def test_registered_and_unknown_operation_names_are_each_read_exactly_once() -> None:
    registered_sink = RecordingTelemetrySink()
    registered = _SideEffectOperation()
    assert (
        OperationExecutor(_telemetry(registered_sink, ["start", "end"], [1.0, 1.1])).execute(
            registered, lambda: "registered"
        )
        == "registered"
    )
    assert registered.name_reads == registered.kind_reads == 1
    assert all(
        event.to_document()["attributes"]["operation.name"] == "work.issue"
        for event in registered_sink.events
    )

    unknown_sink = RecordingTelemetrySink()
    unknown = _SideEffectOperation(names=("tenant.customersecret.work", "work.issue"))
    assert (
        OperationExecutor(_telemetry(unknown_sink, ["start", "end"], [1.0, 1.1])).execute(
            unknown, lambda: "unknown"
        )
        == "unknown"
    )
    assert unknown.name_reads == unknown.kind_reads == 1
    assert all(
        "operation.name" not in event.to_document()["attributes"] for event in unknown_sink.events
    )


@pytest.mark.parametrize("field", ["name", "kind"])
def test_raising_operation_descriptor_is_contained_and_handler_still_runs(field: str) -> None:
    failure = _TelemetryControlFlow(f"operation {field} descriptor failed")
    operation = _SideEffectOperation(
        names=(failure,) if field == "name" else ("work.issue",),
        kinds=(failure,) if field == "kind" else ("action",),
    )
    sink = RecordingTelemetrySink()
    telemetry = _telemetry(sink, ["unused"], [1.0])
    result = object()

    assert OperationExecutor(telemetry).execute(operation, lambda: result) is result
    assert sink.events == ()
    assert operation.kind_reads == 1
    assert operation.name_reads == (1 if field == "name" else 0)


@pytest.mark.parametrize("field", ["kind", "name", "surface"])
def test_spoofed_registry_value_cannot_leak_or_change_application_result(field: str) -> None:
    kind = _RegistrySpoof("tenant-customersecret-kind", "action")
    name = _RegistrySpoof("tenant.customersecret.command", "work.issue")
    surface = _RegistrySpoof("tenant-customersecret", "internal")
    operation = _SideEffectOperation(
        names=(name if field == "name" else "work.issue",),
        kinds=(kind if field == "kind" else "action",),
    )
    sink = RecordingTelemetrySink()
    telemetry = _telemetry(sink, ["start", "end"], [1.0, 1.1])
    result = object()

    assert (
        OperationExecutor(telemetry, surface=surface if field == "surface" else "internal").execute(
            operation, lambda: result
        )
        is result
    )

    documents = [event.to_document() for event in sink.events]
    assert "customersecret" not in repr(documents)
    assert operation.name_reads == operation.kind_reads == 1
    selected = {"kind": kind, "name": name, "surface": surface}[field]
    assert (selected.hash_calls, selected.equality_calls, selected.string_calls) == (0, 0, 0)


def test_spoofed_registry_values_do_not_replace_exact_application_failure() -> None:
    surface = _RegistrySpoof("tenant-customersecret", "internal")
    sink = RecordingTelemetrySink()
    failure = LookupError("application failure")

    def fail() -> None:
        raise failure

    with pytest.raises(LookupError) as caught:
        OperationExecutor(_telemetry(sink, ["unused"], [1.0]), surface=surface).execute(
            _spec("work.issue"), fail
        )

    assert caught.value is failure
    assert sink.events == ()
    assert (surface.hash_calls, surface.equality_calls, surface.string_calls) == (0, 0, 0)


def test_failure_is_redacted_and_preserves_the_original_exception() -> None:
    sink = RecordingTelemetrySink()
    executor = OperationExecutor(
        _telemetry(sink, ["start", "end"], [1.0, 1.125]), surface="internal"
    )
    failure = RuntimeError("token=secret /home/operator/private prompt text")

    def fail() -> None:
        raise failure

    with pytest.raises(RuntimeError) as caught:
        executor.execute(_spec("work.submit"), fail)

    assert caught.value is failure
    completion = sink.events[-1]
    assert completion.outcome is Outcome.FAILED
    assert completion.error is not None
    assert completion.error.code == "operation.failed"
    assert "secret" not in repr(completion.to_document())
    assert "private" not in repr(completion.to_document())
    assert current_telemetry_context() is None


def test_sync_timeout_has_bounded_classification_and_preserves_exception() -> None:
    sink = RecordingTelemetrySink()
    executor = OperationExecutor(
        _telemetry(sink, ["start", "end"], [1.0, 1.125]), surface="internal"
    )
    failure = TimeoutError("secret dependency deadline details")

    def fail() -> None:
        raise failure

    with pytest.raises(TimeoutError) as caught:
        executor.execute(_spec("work.check"), fail)

    assert caught.value is failure
    completion = sink.events[-1]
    assert completion.outcome is Outcome.TIMED_OUT
    assert completion.error is not None
    assert completion.error.code == "deadline.exceeded"
    assert completion.error.retryable is True
    assert "secret" not in repr(completion.to_document())


def test_async_cancellation_emits_completion_and_propagates_cancellation() -> None:
    sink = RecordingTelemetrySink()
    executor = OperationExecutor(_telemetry(sink, ["start", "end"], [1.0, 1.25]), surface="daemon")
    entered = asyncio.Event()

    async def slow() -> None:
        entered.set()
        await asyncio.sleep(60)

    async def scenario() -> None:
        task = asyncio.create_task(executor.execute_async(_spec("host.daemon.serve"), slow))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    assert sink.events[-1].outcome is Outcome.CANCELLED
    assert sink.events[-1].error is not None
    assert sink.events[-1].error.code == "operation.cancelled"
    assert current_telemetry_context() is None


def test_async_generic_failure_is_classified_and_propagated() -> None:
    sink = RecordingTelemetrySink()
    executor = OperationExecutor(_telemetry(sink, ["start", "end"], [1.0, 1.25]), surface="daemon")
    failure = LookupError("secret response body")

    async def fail() -> None:
        raise failure

    with pytest.raises(LookupError) as caught:
        asyncio.run(executor.execute_async(_spec("host.daemon.serve"), fail))

    assert caught.value is failure
    assert sink.events[-1].outcome is Outcome.FAILED
    assert sink.events[-1].error is not None
    assert sink.events[-1].error.code == "operation.failed"
    assert current_telemetry_context() is None


def test_async_timeout_is_distinct_from_failure_and_preserves_exception() -> None:
    sink = RecordingTelemetrySink()
    executor = OperationExecutor(_telemetry(sink, ["start", "end"], [1.0, 1.25]), surface="daemon")
    failure = TimeoutError("secret remote deadline")

    async def fail() -> None:
        raise failure

    with pytest.raises(TimeoutError) as caught:
        asyncio.run(executor.execute_async(_spec("host.daemon.serve"), fail))

    assert caught.value is failure
    assert sink.events[-1].outcome is Outcome.TIMED_OUT
    assert sink.events[-1].error is not None
    assert sink.events[-1].error.code == "deadline.exceeded"
    assert current_telemetry_context() is None


def test_disabled_telemetry_does_not_allocate_observations_or_change_results() -> None:
    def forbidden() -> Any:
        raise AssertionError("disabled telemetry allocated an observation")

    telemetry = SemanticTelemetry(
        sink=NoOpTelemetrySink(),
        identity=EventIdentity(service="bh", instance_id="instance-1"),
        event_id_factory=forbidden,
        occurred_at_factory=forbidden,
        monotonic=forbidden,
    )
    executor = OperationExecutor(telemetry, surface="internal")

    assert executor.execute(_spec("work.issue"), lambda: {"ok": True}) == {"ok": True}


def test_nonconforming_sink_failure_is_non_fatal_and_attempted_only_twice() -> None:
    class ExplodingSink:
        def __init__(self) -> None:
            self.calls = 0

        def emit(self, _event: EventEnvelope) -> EmitDisposition:
            self.calls += 1
            raise RuntimeError("collector secret")

        def flush(self, _timeout_seconds: float) -> FlushResult:
            return FlushResult(FlushOutcome.NO_OP, 0)

    sink = ExplodingSink()
    telemetry = SemanticTelemetry(
        sink=sink,
        identity=EventIdentity(service="bh", instance_id="instance-1"),
        event_id_factory=_next(iter(["start", "end"])),
        occurred_at_factory=lambda: "2026-09-10T02:00:00Z",
        monotonic=_next(iter([1.0, 1.1])),
    )

    assert OperationExecutor(telemetry).execute(_spec("work.issue"), lambda: 42) == 42
    assert sink.calls == 2


@pytest.mark.parametrize("exception_type", _TELEMETRY_EXCEPTIONS)
def test_sink_control_flow_failure_preserves_sync_and_async_results(
    exception_type: type[BaseException],
) -> None:
    class ExplodingSink:
        def __init__(self) -> None:
            self.calls = 0

        def emit(self, _event: EventEnvelope) -> EmitDisposition:
            self.calls += 1
            raise exception_type("telemetry sink failed")

        def flush(self, _timeout_seconds: float) -> FlushResult:
            return FlushResult(FlushOutcome.NO_OP, 0)

    sink = ExplodingSink()
    telemetry = SemanticTelemetry(
        sink=sink,
        identity=EventIdentity(service="bh", instance_id="instance-1"),
        event_id_factory=_next(iter(["sync-start", "sync-end", "async-start", "async-end"])),
        occurred_at_factory=lambda: "2026-09-10T02:00:00Z",
        monotonic=_next(iter([1.0, 1.1, 2.0, 2.1])),
    )
    executor = OperationExecutor(telemetry)

    async def async_handler() -> str:
        return "async-result"

    assert executor.execute(_spec("work.issue"), lambda: "sync-result") == "sync-result"
    assert asyncio.run(executor.execute_async(_spec("work.issue"), async_handler)) == "async-result"
    assert sink.calls == 4
    assert current_telemetry_context() is None


def test_invalid_sink_disposition_is_contained_and_has_no_retry_loop() -> None:
    class InvalidSink:
        def __init__(self) -> None:
            self.calls = 0

        def emit(self, _event: EventEnvelope) -> str:
            self.calls += 1
            return "unbounded-unknown-disposition"

        def flush(self, _timeout_seconds: float) -> FlushResult:
            return FlushResult(FlushOutcome.NO_OP, 0)

    sink = InvalidSink()
    telemetry = SemanticTelemetry(
        sink=sink,  # type: ignore[arg-type] -- deliberately violates the port contract
        identity=EventIdentity(service="bh", instance_id="instance-1"),
        event_id_factory=_next(iter(["start", "end"])),
        occurred_at_factory=lambda: "2026-09-10T02:00:00Z",
        monotonic=_next(iter([1.0, 1.1])),
    )

    assert OperationExecutor(telemetry).execute(_spec("work.issue"), lambda: 42) == 42
    assert sink.calls == 2


def test_domain_and_application_modules_do_not_import_otel_sdk() -> None:
    root = Path(__file__).resolve().parents[4] / "src" / "beadhive"
    forbidden = ("opentelemetry", "beadhive.otel", "beadhive.daemon_telemetry")
    scoped = [
        path for path in root.rglob("*.py") if "domain" in path.parts or "application" in path.parts
    ]

    assert scoped
    violations = {
        str(path.relative_to(root)): name
        for path in scoped
        for name in forbidden
        if name in path.read_text(encoding="utf-8")
    }
    assert violations == {}


@pytest.mark.usefixtures("runtime_test_scope")
def test_operation_registry_survives_fresh_process_reload_and_cached_facade() -> None:
    import beadhive.kernel.operations as operations_package
    import beadhive.kernel.operations._registry as registry_module
    import beadhive.kernel.operations.executor as executor_module

    def recorded_name(executor_type: type[OperationExecutor]) -> str | None:
        sink = RecordingTelemetrySink()
        executor_type(_telemetry(sink, ["start", "end"], [1.0, 1.1])).execute(
            _spec("work.issue"), lambda: None
        )
        return sink.events[0].to_document()["attributes"].get("operation.name")

    cached_facade = operations_package.OperationExecutor
    assert recorded_name(cached_facade) == "work.issue"

    context = processes.process_context()
    results = context.Queue()
    child = context.Process(target=_record_registered_name_in_fresh_process, args=(results,))
    child.start()
    assert results.get(timeout=10) == ("application-ok", "work.issue")
    child.join(10)
    assert child.exitcode == 0

    reloaded_executor = importlib.reload(executor_module)
    assert recorded_name(cached_facade) == "work.issue"
    assert recorded_name(reloaded_executor.OperationExecutor) == "work.issue"

    importlib.reload(registry_module)
    reloaded_package = importlib.reload(operations_package)
    assert recorded_name(cached_facade) == "work.issue"
    assert recorded_name(reloaded_package.OperationExecutor) == "work.issue"
