"""Executable sample for discovery, binding, disablement, and bounded failures."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast

import pytest

from beadhive.kernel.lifecycle import (
    EVENTS_BY_ID,
    Criticality,
    DeliveryPolicy,
    DeliveryStatus,
    HostLifecycleContext,
    LifecycleDispatcher,
    LifecycleEvent,
    SubscriberBinding,
)
from beadhive.kernel.plugins import (
    BuiltInManifestSource,
    CapabilityBindingError,
    CapabilityKey,
    CapabilityRef,
    DiagnosticCode,
    ManifestDocument,
    ManifestProvenance,
    ProviderBinding,
    ProviderKey,
    bind_application_port,
    discover_plugins,
)
from harness.sample_plugin_v1 import (
    GreetingPort,
    GreetingProvider,
    crashing_observer,
    readiness_observer,
    slow_observer,
)

MANIFEST = Path(__file__).parents[1] / "harness" / "sample-plugin-v1.json"
CAPABILITY = CapabilityRef("sample.greeting", 1)


def _document(payload: bytes | None = None) -> ManifestDocument:
    return ManifestDocument(
        ManifestProvenance("built-in", "tests/harness/sample-plugin-v1.json"),
        payload if payload is not None else MANIFEST.read_bytes(),
    )


def _discover(*documents: ManifestDocument, config=None):
    return discover_plugins(
        [BuiltInManifestSource(documents or (_document(),))],
        config=config,
        beadhive_version="0.15.1",
        kernel_version="1.0.0",
    )


def test_sample_discovers_and_binds_one_named_application_port():
    result = _discover()
    binding = ProviderBinding(ProviderKey("sample", CAPABILITY), GreetingProvider())
    port = bind_application_port(CapabilityKey(CAPABILITY, GreetingPort), result, [binding])
    assert port.greet("plugin author") == "hello plugin author"


def test_sample_config_validation_and_disablement_precede_composition():
    invalid = _discover(config={"allow_external_entry_points": "false"})
    assert invalid.plugins == ()
    assert [diagnostic.code for diagnostic in invalid.diagnostics] == [
        DiagnosticCode.INVALID_CONFIG
    ]

    disabled = _discover(config={"enabled": {"sample": False}})
    assert disabled.plugins == ()
    assert [diagnostic.code for diagnostic in disabled.diagnostics] == [
        DiagnosticCode.DISABLED_PLUGIN
    ]


def test_invalid_optional_plugin_does_not_hide_valid_read_only_capabilities():
    result = _discover(_document(b"not-json"), _document())
    assert [plugin.manifest.plugin_id for plugin in result.plugins] == ["sample"]
    assert DiagnosticCode.INVALID_MANIFEST in {diagnostic.code for diagnostic in result.diagnostics}


def test_best_effort_crash_and_timeout_are_bounded_and_later_observer_runs():
    event = cast(LifecycleEvent[HostLifecycleContext], EVENTS_BY_ID["host.readiness"])
    calls: list[str] = []

    async def after(context: HostLifecycleContext) -> None:
        await readiness_observer(context)
        calls.append("after")

    bindings = (
        SubscriberBinding(
            "sample",
            "sample.crashing-observer",
            event,
            crashing_observer,
            DeliveryPolicy(order=10, criticality=Criticality.BEST_EFFORT),
        ),
        SubscriberBinding(
            "sample",
            "sample.slow-observer",
            event,
            slow_observer,
            DeliveryPolicy(
                order=20,
                timeout_seconds=0.01,
                criticality=Criticality.BEST_EFFORT,
            ),
        ),
        SubscriberBinding(
            "sample",
            "sample.after-observer",
            event,
            after,
            DeliveryPolicy(order=30, criticality=Criticality.BEST_EFFORT),
        ),
    )
    report = asyncio.run(
        LifecycleDispatcher(bindings).dispatch(
            event,
            HostLifecycleContext("read-only-host", "sample-readiness"),
        )
    )
    assert [delivery.status for delivery in report.deliveries] == [
        DeliveryStatus.FAILED,
        DeliveryStatus.TIMED_OUT,
        DeliveryStatus.SUCCEEDED,
    ]
    assert calls == ["after"]


def test_a_required_capability_without_a_provider_blocks_only_that_composition():
    result = _discover(config={"enabled": {"sample": False}})
    with pytest.raises(CapabilityBindingError, match="has no selected provider"):
        bind_application_port(CapabilityKey(CAPABILITY, GreetingPort), result, [])
