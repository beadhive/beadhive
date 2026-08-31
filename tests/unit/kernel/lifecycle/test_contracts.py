from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from beadhive.kernel.lifecycle import (
    ALL_LIFECYCLE_EVENTS,
    CompensationMode,
    CompensationPolicy,
    DeliveryPolicy,
    HostLifecycleContext,
    Idempotency,
    LifecycleFamily,
    PluginLifecycleContext,
    RetryPolicy,
    SubscriberBinding,
)


async def _subscriber(_context: PluginLifecycleContext) -> None:
    pass


async def _compensator(_context: PluginLifecycleContext) -> None:
    pass


def test_event_catalog_has_every_typed_family_and_stable_unique_ids() -> None:
    assert {event.family for event in ALL_LIFECYCLE_EVENTS} == set(LifecycleFamily)
    assert len({event.id for event in ALL_LIFECYCLE_EVENTS}) == len(ALL_LIFECYCLE_EVENTS)
    assert [event.id for event in ALL_LIFECYCLE_EVENTS] == [
        "plugin.discovered",
        "plugin.validated",
        "plugin.configured",
        "plugin.started",
        "plugin.ready",
        "plugin.stopping",
        "plugin.stopped",
        "hive.onboarding",
        "hive.onboarded",
        "hive.retiring",
        "hive.retired",
        "worktree.prepare",
        "worktree.creating",
        "worktree.created",
        "worktree.removing",
        "worktree.removed",
        "agent-launch.prepare",
        "agent-launch.adapter-commit",
        "agent-launch.core-commit",
        "agent-launch.abort",
        "host.configure",
        "host.startup",
        "host.readiness",
        "host.drain",
        "host.shutdown",
        "host.telemetry-flush",
    ]


def test_contexts_are_immutable_and_reject_missing_identity() -> None:
    context = HostLifecycleContext(host_id="host-1", correlation_id="corr-1")
    with pytest.raises(FrozenInstanceError):
        context.host_id = "other"  # type: ignore[misc]
    with pytest.raises(ValueError, match="host_id"):
        HostLifecycleContext(host_id="", correlation_id="corr-1")


def test_retry_requires_explicit_idempotency() -> None:
    with pytest.raises(ValueError, match="idempotent"):
        DeliveryPolicy(retry=RetryPolicy(max_attempts=2))
    policy = DeliveryPolicy(
        idempotency=Idempotency.REQUIRED,
        retry=RetryPolicy(max_attempts=2),
    )
    assert policy.retry.max_attempts == 2


def test_compensation_declaration_and_binding_must_agree() -> None:
    event = ALL_LIFECYCLE_EVENTS[0]
    policy = DeliveryPolicy(
        compensation=CompensationPolicy(
            mode=CompensationMode.REQUIRED,
            action_id="example.undo",
        )
    )
    with pytest.raises(ValueError, match="compensator binding"):
        SubscriberBinding("example", "example.observe", event, _subscriber, policy)
    binding = SubscriberBinding(
        "example",
        "example.observe",
        event,
        _subscriber,
        policy,
        _compensator,
    )
    assert binding.policy.compensation.action_id == "example.undo"
