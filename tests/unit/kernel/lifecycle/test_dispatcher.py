from __future__ import annotations

import asyncio

import pytest

from beadhive.kernel.lifecycle import (
    ALL_LIFECYCLE_EVENTS,
    CompensationMode,
    CompensationPolicy,
    Criticality,
    DeliveryPolicy,
    DeliveryStatus,
    HostLifecycleContext,
    Idempotency,
    LifecycleDeliveryError,
    LifecycleDispatcher,
    LifecycleEvent,
    PluginLifecycleContext,
    RetryPolicy,
    SubscriberBinding,
)
from beadhive.kernel.telemetry import (
    EventIdentity,
    Outcome,
    RecordingTelemetrySink,
    SemanticTelemetry,
    current_telemetry_context,
)

EVENT = ALL_LIFECYCLE_EVENTS[0]
CONTEXT = PluginLifecycleContext(plugin_id="owner", correlation_id="corr-1")
_ACTIVATION_FAULTS = ("call", "none", "non-context", "enter", "exit")


class _TelemetryControlFlow(BaseException):
    pass


class _ApplicationControlFlow(BaseException):
    pass


_TELEMETRY_EXCEPTIONS = (asyncio.CancelledError, _TelemetryControlFlow)


def _binding(plugin_id, subscription_id, subscriber, *, policy=None, compensator=None):
    return SubscriberBinding(
        plugin_id,
        subscription_id,
        EVENT,
        subscriber,
        policy or DeliveryPolicy(),
        compensator,
    )


def _telemetry() -> SemanticTelemetry:
    ids = iter([f"event-{number}" for number in range(20)])
    times = iter(float(number) for number in range(20))
    return SemanticTelemetry(
        sink=RecordingTelemetrySink(),
        identity=EventIdentity(service="bh", instance_id="instance-1"),
        event_id_factory=lambda: next(ids),
        occurred_at_factory=lambda: "2026-09-10T02:00:00Z",
        monotonic=lambda: next(times),
    )


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
def test_port_control_flow_failure_preserves_successful_delivery(
    monkeypatch, seam: str, exception_type: type[BaseException]
) -> None:
    telemetry = _telemetry()
    _install_port_fault(monkeypatch, telemetry, seam, exception_type)
    calls: list[str] = []

    async def subscriber(_context) -> None:
        calls.append("ran")

    report = asyncio.run(
        LifecycleDispatcher(
            (_binding("owner", "owner.control-flow", subscriber),), telemetry=telemetry
        ).dispatch(EVENT, CONTEXT)
    )

    assert calls == ["ran"]
    assert report.deliveries[-1].status is DeliveryStatus.SUCCEEDED
    assert current_telemetry_context() is None


@pytest.mark.parametrize("exception_type", _TELEMETRY_EXCEPTIONS)
@pytest.mark.parametrize("fault", ["call", "enter", "exit"])
def test_activation_control_flow_failure_preserves_successful_delivery(
    monkeypatch, fault: str, exception_type: type[BaseException]
) -> None:
    telemetry = _telemetry()
    _install_activation_fault(monkeypatch, telemetry, fault, exception_type)
    calls: list[str] = []

    async def subscriber(_context) -> None:
        calls.append("ran")

    report = asyncio.run(
        LifecycleDispatcher(
            (_binding("owner", "owner.control-flow", subscriber),), telemetry=telemetry
        ).dispatch(EVENT, CONTEXT)
    )

    assert calls == ["ran"]
    assert report.deliveries[-1].status is DeliveryStatus.SUCCEEDED
    assert current_telemetry_context() is None


@pytest.mark.parametrize("fault", _ACTIVATION_FAULTS)
def test_activation_failures_never_prevent_lifecycle_delivery(monkeypatch, fault: str) -> None:
    sink = RecordingTelemetrySink()
    ids = iter(["start", "end"])
    times = iter([1.0, 1.25])
    telemetry = SemanticTelemetry(
        sink=sink,
        identity=EventIdentity(service="bh", instance_id="instance-1"),
        event_id_factory=lambda: next(ids),
        occurred_at_factory=lambda: "2026-09-10T02:00:00Z",
        monotonic=lambda: next(times),
    )
    _install_activation_fault(monkeypatch, telemetry, fault)
    calls: list[str] = []

    async def subscriber(_context) -> None:
        calls.append("ran")

    report = asyncio.run(
        LifecycleDispatcher(
            (_binding("owner", "owner.activation", subscriber),), telemetry=telemetry
        ).dispatch(EVENT, CONTEXT)
    )

    assert calls == ["ran"]
    assert report.deliveries[-1].status is DeliveryStatus.SUCCEEDED
    assert current_telemetry_context() is None
    assert sink.events[-1].outcome is Outcome.SUCCEEDED


def test_activation_exit_failure_preserves_lifecycle_application_failure(monkeypatch) -> None:
    sink = RecordingTelemetrySink()
    ids = iter(["start", "end"])
    times = iter([1.0, 1.25])
    telemetry = SemanticTelemetry(
        sink=sink,
        identity=EventIdentity(service="bh", instance_id="instance-1"),
        event_id_factory=lambda: next(ids),
        occurred_at_factory=lambda: "2026-09-10T02:00:00Z",
        monotonic=lambda: next(times),
    )
    _install_activation_fault(monkeypatch, telemetry, "exit")

    async def subscriber(_context) -> None:
        raise LookupError("application failure")

    report = asyncio.run(
        LifecycleDispatcher(
            (_binding("owner", "owner.activation", subscriber),), telemetry=telemetry
        ).dispatch(EVENT, CONTEXT)
    )

    attempt = report.deliveries[-1].attempts[-1]
    assert attempt.status is DeliveryStatus.FAILED
    assert attempt.error == "LookupError: application failure"
    assert current_telemetry_context() is None
    assert sink.events[-1].outcome is Outcome.FAILED


def test_activation_exit_failure_preserves_lifecycle_timeout(monkeypatch) -> None:
    sink = RecordingTelemetrySink()
    ids = iter(["start", "end"])
    times = iter([1.0, 1.25])
    telemetry = SemanticTelemetry(
        sink=sink,
        identity=EventIdentity(service="bh", instance_id="instance-1"),
        event_id_factory=lambda: next(ids),
        occurred_at_factory=lambda: "2026-09-10T02:00:00Z",
        monotonic=lambda: next(times),
    )
    _install_activation_fault(monkeypatch, telemetry, "exit")

    async def subscriber(_context) -> None:
        await asyncio.sleep(60)

    report = asyncio.run(
        LifecycleDispatcher(
            (
                _binding(
                    "owner",
                    "owner.activation",
                    subscriber,
                    policy=DeliveryPolicy(timeout_seconds=0.001),
                ),
            ),
            telemetry=telemetry,
        ).dispatch(EVENT, CONTEXT)
    )

    assert report.deliveries[-1].status is DeliveryStatus.TIMED_OUT
    assert current_telemetry_context() is None
    assert sink.events[-1].outcome is Outcome.TIMED_OUT


def test_activation_exit_failure_preserves_lifecycle_cancellation(monkeypatch) -> None:
    sink = RecordingTelemetrySink()
    ids = iter(["start", "end"])
    times = iter([1.0, 1.25])
    telemetry = SemanticTelemetry(
        sink=sink,
        identity=EventIdentity(service="bh", instance_id="instance-1"),
        event_id_factory=lambda: next(ids),
        occurred_at_factory=lambda: "2026-09-10T02:00:00Z",
        monotonic=lambda: next(times),
    )
    _install_activation_fault(monkeypatch, telemetry, "exit")
    entered = asyncio.Event()

    async def subscriber(_context) -> None:
        entered.set()
        await asyncio.sleep(60)

    async def scenario() -> None:
        task = asyncio.create_task(
            LifecycleDispatcher(
                (_binding("owner", "owner.activation", subscriber),), telemetry=telemetry
            ).dispatch(EVENT, CONTEXT)
        )
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    assert current_telemetry_context() is None
    assert sink.events[-1].outcome is Outcome.CANCELLED


@pytest.mark.parametrize("exception_type", _TELEMETRY_EXCEPTIONS)
def test_complete_control_flow_failure_preserves_retries_and_compensation(
    monkeypatch, exception_type: type[BaseException]
) -> None:
    telemetry = _telemetry()
    _install_port_fault(monkeypatch, telemetry, "complete", exception_type)
    observed: list[str] = []

    async def first(_context) -> None:
        observed.append("first")

    async def undo_first(_context) -> None:
        observed.append("undo:first")

    async def fail(_context) -> None:
        observed.append("fail")
        raise LookupError("application failure")

    dispatcher = LifecycleDispatcher(
        (
            _binding(
                "alpha",
                "alpha.first",
                first,
                policy=DeliveryPolicy(
                    compensation=CompensationPolicy(CompensationMode.REQUIRED, "alpha.undo")
                ),
                compensator=undo_first,
            ),
            _binding(
                "omega",
                "omega.fail",
                fail,
                policy=DeliveryPolicy(
                    criticality=Criticality.BLOCKING,
                    idempotency=Idempotency.REQUIRED,
                    retry=RetryPolicy(max_attempts=2),
                ),
            ),
        ),
        telemetry=telemetry,
    )

    with pytest.raises(LifecycleDeliveryError) as caught:
        asyncio.run(dispatcher.dispatch(EVENT, CONTEXT))

    assert observed == ["first", "fail", "fail", "undo:first"]
    report = caught.value.report
    assert [attempt.status for attempt in report.deliveries[-1].attempts] == [
        DeliveryStatus.FAILED,
        DeliveryStatus.FAILED,
    ]
    assert report.deliveries[-1].attempts[-1].error == "LookupError: application failure"
    assert report.compensations[-1].status is DeliveryStatus.SUCCEEDED
    assert current_telemetry_context() is None


@pytest.mark.parametrize("exception_type", _TELEMETRY_EXCEPTIONS)
def test_complete_control_flow_failure_preserves_timeout_report(
    monkeypatch, exception_type: type[BaseException]
) -> None:
    telemetry = _telemetry()
    _install_port_fault(monkeypatch, telemetry, "complete", exception_type)

    async def subscriber(_context) -> None:
        await asyncio.sleep(60)

    report = asyncio.run(
        LifecycleDispatcher(
            (
                _binding(
                    "owner",
                    "owner.timeout",
                    subscriber,
                    policy=DeliveryPolicy(timeout_seconds=0.001),
                ),
            ),
            telemetry=telemetry,
        ).dispatch(EVENT, CONTEXT)
    )

    assert report.deliveries[-1].status is DeliveryStatus.TIMED_OUT
    assert current_telemetry_context() is None


@pytest.mark.parametrize("exception_type", _TELEMETRY_EXCEPTIONS)
def test_complete_control_flow_failure_preserves_exact_lifecycle_cancellation(
    monkeypatch, exception_type: type[BaseException]
) -> None:
    telemetry = _telemetry()
    _install_port_fault(monkeypatch, telemetry, "complete", exception_type)
    entered = asyncio.Event()
    application_cancellation: list[asyncio.CancelledError] = []

    async def subscriber(_context) -> None:
        entered.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError as error:
            application_cancellation.append(error)
            raise

    async def scenario() -> asyncio.CancelledError:
        task = asyncio.create_task(
            LifecycleDispatcher(
                (_binding("owner", "owner.cancel", subscriber),), telemetry=telemetry
            ).dispatch(EVENT, CONTEXT)
        )
        await entered.wait()
        task.cancel("application cancellation")
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        return caught.value

    caught = asyncio.run(scenario())

    assert caught is application_cancellation[0]
    assert current_telemetry_context() is None


@pytest.mark.parametrize("exception_type", _TELEMETRY_EXCEPTIONS)
def test_activation_control_flow_failure_never_swallows_application_base_exception(
    monkeypatch, exception_type: type[BaseException]
) -> None:
    telemetry = _telemetry()
    _install_activation_fault(monkeypatch, telemetry, "exit", exception_type)
    failure = _ApplicationControlFlow("application control flow")

    async def subscriber(_context) -> None:
        raise failure

    with pytest.raises(_ApplicationControlFlow) as caught:
        asyncio.run(
            LifecycleDispatcher(
                (_binding("owner", "owner.control-flow", subscriber),), telemetry=telemetry
            ).dispatch(EVENT, CONTEXT)
        )

    assert caught.value is failure
    assert current_telemetry_context() is None


def test_delivery_order_is_policy_then_plugin_then_subscription_not_input_order() -> None:
    observed: list[str] = []

    def subscriber(name):
        async def call(_context):
            observed.append(name)

        return call

    bindings = (
        _binding("zeta", "zeta.second", subscriber("zeta")),
        _binding("alpha", "alpha.second", subscriber("alpha-second")),
        _binding("alpha", "alpha.first", subscriber("alpha-first")),
        _binding(
            "late",
            "late.first",
            subscriber("late"),
            policy=DeliveryPolicy(order=10),
        ),
    )
    asyncio.run(LifecycleDispatcher(bindings).dispatch(EVENT, CONTEXT))
    assert observed == ["alpha-first", "alpha-second", "zeta", "late"]


@pytest.mark.parametrize(
    ("criticality", "raises", "expected"),
    [
        (Criticality.BEST_EFFORT, False, ["fail", "later"]),
        (Criticality.BLOCKING, True, ["fail"]),
    ],
)
def test_failure_matrix_preserves_best_effort_and_blocks_critical(
    criticality, raises, expected
) -> None:
    observed: list[str] = []

    async def fail(_context):
        observed.append("fail")
        raise RuntimeError("boom")

    async def later(_context):
        observed.append("later")

    dispatcher = LifecycleDispatcher(
        (
            _binding(
                "alpha",
                "alpha.fail",
                fail,
                policy=DeliveryPolicy(criticality=criticality),
            ),
            _binding("beta", "beta.later", later),
        )
    )
    if raises:
        with pytest.raises(LifecycleDeliveryError) as caught:
            asyncio.run(dispatcher.dispatch(EVENT, CONTEXT))
        assert caught.value.report.deliveries[-1].status is DeliveryStatus.FAILED
    else:
        report = asyncio.run(dispatcher.dispatch(EVENT, CONTEXT))
        assert [result.status for result in report.deliveries] == [
            DeliveryStatus.FAILED,
            DeliveryStatus.SUCCEEDED,
        ]
    assert observed == expected


def test_timeout_cancels_attempt_and_best_effort_continues() -> None:
    cancelled = asyncio.Event()
    observed: list[str] = []

    async def slow(_context):
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def later(_context):
        observed.append("later")

    dispatcher = LifecycleDispatcher(
        (
            _binding(
                "alpha",
                "alpha.slow",
                slow,
                policy=DeliveryPolicy(timeout_seconds=0.01),
            ),
            _binding("beta", "beta.later", later),
        )
    )
    report = asyncio.run(dispatcher.dispatch(EVENT, CONTEXT))
    assert cancelled.is_set()
    assert report.deliveries[0].status is DeliveryStatus.TIMED_OUT
    assert observed == ["later"]


def test_idempotent_retry_succeeds_on_the_declared_attempt() -> None:
    attempts = 0

    async def flaky(_context):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise RuntimeError("transient")

    binding = _binding(
        "alpha",
        "alpha.flaky",
        flaky,
        policy=DeliveryPolicy(
            idempotency=Idempotency.REQUIRED,
            retry=RetryPolicy(max_attempts=3),
        ),
    )
    report = asyncio.run(LifecycleDispatcher((binding,)).dispatch(EVENT, CONTEXT))
    assert report.deliveries[0].status is DeliveryStatus.SUCCEEDED
    assert [attempt.status for attempt in report.deliveries[0].attempts] == [
        DeliveryStatus.FAILED,
        DeliveryStatus.FAILED,
        DeliveryStatus.SUCCEEDED,
    ]


def test_blocking_failure_compensates_completed_participants_in_reverse_order() -> None:
    observed: list[str] = []

    def succeeds(name):
        async def call(_context):
            observed.append(name)

        return call

    def compensates(name):
        async def call(_context):
            observed.append(f"undo:{name}")

        return call

    async def fail(_context):
        observed.append("fail")
        raise RuntimeError("commit refused")

    policy = DeliveryPolicy(
        compensation=CompensationPolicy(CompensationMode.REQUIRED, "owner.undo")
    )
    dispatcher = LifecycleDispatcher(
        (
            _binding(
                "alpha",
                "alpha.one",
                succeeds("one"),
                policy=policy,
                compensator=compensates("one"),
            ),
            _binding(
                "beta",
                "beta.two",
                succeeds("two"),
                policy=policy,
                compensator=compensates("two"),
            ),
            _binding(
                "omega",
                "omega.fail",
                fail,
                policy=DeliveryPolicy(criticality=Criticality.BLOCKING),
            ),
        )
    )
    with pytest.raises(LifecycleDeliveryError) as caught:
        asyncio.run(dispatcher.dispatch(EVENT, CONTEXT))
    assert observed == ["one", "two", "fail", "undo:two", "undo:one"]
    assert [result.status for result in caught.value.report.compensations] == [
        DeliveryStatus.SUCCEEDED,
        DeliveryStatus.SUCCEEDED,
    ]


def test_external_cancellation_is_never_downgraded_to_best_effort() -> None:
    cancelled = asyncio.Event()

    async def slow(_context):
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def scenario():
        task = asyncio.create_task(
            LifecycleDispatcher((_binding("alpha", "alpha.slow", slow),)).dispatch(EVENT, CONTEXT)
        )
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert cancelled.is_set()


def test_dispatch_rejects_context_from_another_lifecycle_family() -> None:
    with pytest.raises(TypeError, match="PluginLifecycleContext"):
        asyncio.run(
            LifecycleDispatcher().dispatch(
                EVENT,
                HostLifecycleContext("host-1", "corr-1"),
            )
        )


def test_dispatch_rejects_forged_catalog_id_before_invoking_subscriber() -> None:
    observed: list[str] = []

    async def subscriber(_context) -> None:
        observed.append("invoked")

    forged = LifecycleEvent(
        family=EVENT.family,
        phase=EVENT.phase,
        order=EVENT.order,
        context_type=HostLifecycleContext,
    )
    dispatcher = LifecycleDispatcher((_binding("owner", "owner.discovered", subscriber),))

    with pytest.raises(ValueError, match="kernel-owned lifecycle event contract"):
        asyncio.run(dispatcher.dispatch(forged, HostLifecycleContext("host-1", "corr-1")))

    assert observed == []


def test_dispatch_accepts_an_equivalent_public_event_contract() -> None:
    observed: list[str] = []

    async def subscriber(_context) -> None:
        observed.append("invoked")

    equivalent = LifecycleEvent(
        family=EVENT.family,
        phase=EVENT.phase,
        order=EVENT.order,
        context_type=EVENT.context_type,
    )
    dispatcher = LifecycleDispatcher((_binding("owner", "owner.discovered", subscriber),))

    report = asyncio.run(dispatcher.dispatch(equivalent, CONTEXT))

    assert report.event_id == EVENT.id
    assert observed == ["invoked"]


def test_lifecycle_delivery_emits_versioned_correlated_semantics() -> None:
    sink = RecordingTelemetrySink()
    ids = iter(["lifecycle-start", "operation-start", "operation-end", "lifecycle-end"])
    times = iter([1.0, 1.1, 1.2, 1.4])
    telemetry = SemanticTelemetry(
        sink=sink,
        identity=EventIdentity(service="bh", instance_id="instance-1"),
        event_id_factory=lambda: next(ids),
        occurred_at_factory=lambda: "2026-09-10T02:00:00Z",
        monotonic=lambda: next(times),
    )

    async def subscriber(_context) -> None:
        from beadhive.kernel.operations import OperationExecutor, OperationSpec

        operation = OperationSpec(
            name="work.issue",
            parameters=(),
            result_schema="urn:test:result",
            kind="read-resource",
            privilege="unprivileged-read",
            constraints={},
            surfaces={},
        )
        await OperationExecutor(telemetry).execute_async(operation, _completed)

    async def _completed() -> None:
        return None

    dispatcher = LifecycleDispatcher(
        (_binding("owner", "owner.discovered", subscriber),), telemetry=telemetry
    )
    report = asyncio.run(dispatcher.dispatch(EVENT, CONTEXT))

    assert report.deliveries[0].status is DeliveryStatus.SUCCEEDED
    assert [event.event_name.value for event in sink.events] == [
        "beadhive.lifecycle.delivery",
        "beadhive.operation.execution",
        "beadhive.operation.execution",
        "beadhive.lifecycle.delivery",
    ]
    assert {event.correlation_id for event in sink.events} == {CONTEXT.correlation_id}
    assert [event.causation_id for event in sink.events] == [
        None,
        "lifecycle-start",
        "operation-start",
        "lifecycle-start",
    ]
    assert sink.events[-1].outcome is Outcome.SUCCEEDED
    assert sink.events[0].event_version == sink.events[-1].event_version == 1
    assert sink.events[0].identity.plugin_id == "owner"
    assert sink.events[0].to_document()["attributes"] == {
        "lifecycle.event": EVENT.id,
        "operation.kind": "lifecycle",
        "retry.count": 0,
        "surface": "internal",
    }


def test_compensation_telemetry_omits_many_plugin_action_ids_and_keeps_canonical_event() -> None:
    sink = RecordingTelemetrySink()
    ids = iter([f"telemetry-event-{number}" for number in range(100)])
    times = iter(number / 10 for number in range(100))
    telemetry = SemanticTelemetry(
        sink=sink,
        identity=EventIdentity(service="bh", instance_id="instance-1"),
        event_id_factory=lambda: next(ids),
        occurred_at_factory=lambda: "2026-09-10T02:00:00Z",
        monotonic=lambda: next(times),
    )
    observed: list[str] = []
    action_ids = tuple(f"tenant-{number:02d}.customersecret.undo" for number in range(12))

    def succeeds(name: str):
        async def call(_context) -> None:
            observed.append(name)

        return call

    def compensates(name: str):
        async def call(_context) -> None:
            observed.append(f"undo:{name}")

        return call

    successful_bindings = tuple(
        _binding(
            f"tenant-{number:02d}",
            f"tenant-{number:02d}.success",
            succeeds(str(number)),
            policy=DeliveryPolicy(
                compensation=CompensationPolicy(CompensationMode.REQUIRED, action_id)
            ),
            compensator=compensates(str(number)),
        )
        for number, action_id in enumerate(action_ids)
    )

    async def fail(_context) -> None:
        observed.append("fail")
        raise RuntimeError("application failure")

    failing_binding = _binding(
        "zeta",
        "zeta.fail",
        fail,
        policy=DeliveryPolicy(
            criticality=Criticality.BLOCKING,
            idempotency=Idempotency.REQUIRED,
            retry=RetryPolicy(max_attempts=2),
        ),
    )

    with pytest.raises(LifecycleDeliveryError) as caught:
        asyncio.run(
            LifecycleDispatcher(
                successful_bindings + (failing_binding,), telemetry=telemetry
            ).dispatch(EVENT, CONTEXT)
        )

    report = caught.value.report
    assert [attempt.status for attempt in report.deliveries[-1].attempts] == [
        DeliveryStatus.FAILED,
        DeliveryStatus.FAILED,
    ]
    assert len(report.compensations) == len(action_ids)
    assert all(result.status is DeliveryStatus.SUCCEEDED for result in report.compensations)
    assert observed == [
        *(str(number) for number in range(12)),
        "fail",
        "fail",
        *(f"undo:{number}" for number in reversed(range(12))),
    ]
    assert [event.outcome for event in sink.events if event.outcome is not None] == [
        *([Outcome.SUCCEEDED] * 12),
        Outcome.FAILED,
        Outcome.FAILED,
        *([Outcome.SUCCEEDED] * 12),
    ]
    for event in sink.events:
        document = event.to_document()
        assert event.event_name.value == "beadhive.lifecycle.delivery"
        assert document["attributes"] == {
            "lifecycle.event": EVENT.id,
            "operation.kind": "lifecycle",
            "retry.count": document["attributes"]["retry.count"],
            "surface": "internal",
        }
        assert not any(action_id in repr(document) for action_id in action_ids)
        assert "customersecret" not in repr(document)


def test_lifecycle_cancellation_is_observed_without_becoming_best_effort() -> None:
    sink = RecordingTelemetrySink()
    ids = iter(["start", "end"])
    times = iter([1.0, 1.25])
    telemetry = SemanticTelemetry(
        sink=sink,
        identity=EventIdentity(service="bh", instance_id="instance-1"),
        event_id_factory=lambda: next(ids),
        occurred_at_factory=lambda: "2026-09-10T02:00:00Z",
        monotonic=lambda: next(times),
    )
    entered = asyncio.Event()

    async def slow(_context) -> None:
        entered.set()
        await asyncio.sleep(60)

    async def scenario() -> None:
        task = asyncio.create_task(
            LifecycleDispatcher(
                (_binding("owner", "owner.slow", slow),), telemetry=telemetry
            ).dispatch(EVENT, CONTEXT)
        )
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    assert sink.events[-1].outcome is Outcome.CANCELLED
    assert sink.events[-1].error is not None
    assert sink.events[-1].error.code == "lifecycle.cancelled"
    assert "slow" not in repr(sink.events[-1].to_document())
    assert current_telemetry_context() is None


def test_lifecycle_timeout_has_bounded_error_and_preserves_delivery_behavior() -> None:
    sink = RecordingTelemetrySink()
    ids = iter(["start", "end"])
    times = iter([1.0, 1.25])
    telemetry = SemanticTelemetry(
        sink=sink,
        identity=EventIdentity(service="bh", instance_id="instance-1"),
        event_id_factory=lambda: next(ids),
        occurred_at_factory=lambda: "2026-09-10T02:00:00Z",
        monotonic=lambda: next(times),
    )

    async def slow(_context) -> None:
        await asyncio.sleep(60)

    binding = _binding(
        "owner",
        "owner.slow",
        slow,
        policy=DeliveryPolicy(timeout_seconds=0.001),
    )
    report = asyncio.run(
        LifecycleDispatcher((binding,), telemetry=telemetry).dispatch(EVENT, CONTEXT)
    )

    assert report.deliveries[-1].status is DeliveryStatus.TIMED_OUT
    assert sink.events[-1].outcome is Outcome.TIMED_OUT
    assert sink.events[-1].error is not None
    assert sink.events[-1].error.code == "lifecycle.timeout"
    assert "TimeoutError" not in repr(sink.events[-1].to_document())


def test_lifecycle_failure_is_redacted_while_existing_delivery_result_is_preserved() -> None:
    sink = RecordingTelemetrySink()
    ids = iter(["start", "end"])
    times = iter([1.0, 1.25])
    telemetry = SemanticTelemetry(
        sink=sink,
        identity=EventIdentity(service="bh", instance_id="instance-1"),
        event_id_factory=lambda: next(ids),
        occurred_at_factory=lambda: "2026-09-10T02:00:00Z",
        monotonic=lambda: next(times),
    )

    async def fail(_context) -> None:
        raise RuntimeError("token=secret /home/operator/private")

    report = asyncio.run(
        LifecycleDispatcher((_binding("owner", "owner.fail", fail),), telemetry=telemetry).dispatch(
            EVENT, CONTEXT
        )
    )

    assert report.deliveries[-1].status is DeliveryStatus.FAILED
    assert "RuntimeError" in report.deliveries[-1].attempts[-1].error
    assert sink.events[-1].outcome is Outcome.FAILED
    assert sink.events[-1].error is not None
    assert sink.events[-1].error.code == "lifecycle.failed"
    assert "secret" not in repr(sink.events[-1].to_document())


def test_disabled_lifecycle_telemetry_allocates_nothing_and_changes_nothing() -> None:
    from beadhive.kernel.telemetry import NoOpTelemetrySink

    def forbidden():
        raise AssertionError("disabled telemetry allocated an observation")

    observed: list[str] = []

    async def subscriber(_context) -> None:
        observed.append("called")

    telemetry = SemanticTelemetry(
        sink=NoOpTelemetrySink(),
        identity=EventIdentity(service="bh", instance_id="instance-1"),
        event_id_factory=forbidden,
        occurred_at_factory=forbidden,
        monotonic=forbidden,
    )
    report = asyncio.run(
        LifecycleDispatcher(
            (_binding("owner", "owner.disabled", subscriber),), telemetry=telemetry
        ).dispatch(EVENT, CONTEXT)
    )

    assert report.deliveries[-1].status is DeliveryStatus.SUCCEEDED
    assert observed == ["called"]
