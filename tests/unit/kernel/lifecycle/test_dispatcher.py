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

EVENT = ALL_LIFECYCLE_EVENTS[0]
CONTEXT = PluginLifecycleContext(plugin_id="owner", correlation_id="corr-1")


def _binding(plugin_id, subscription_id, subscriber, *, policy=None, compensator=None):
    return SubscriberBinding(
        plugin_id,
        subscription_id,
        EVENT,
        subscriber,
        policy or DeliveryPolicy(),
        compensator,
    )


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
