"""Typed composition-time capability binding tests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import pytest

from beadhive.kernel.plugins import (
    CapabilityBindingError,
    CapabilityKey,
    CapabilityRef,
    CapabilitySelection,
    DiscoveryResult,
    ProviderBinding,
    ProviderKey,
    bind_application_port,
)


@runtime_checkable
class GreetingPort(Protocol):
    def greet(self, name: str) -> str: ...


class GreetingProvider:
    def greet(self, name: str) -> str:
        return f"hello {name}"


@dataclass(frozen=True)
class GreetingApplication:
    port: GreetingPort

    def execute(self, name: str) -> str:
        return self.port.greet(name)


CAPABILITY = CapabilityRef("example.greeting", 1)
REQUEST = CapabilityKey(CAPABILITY, GreetingPort)


def _discovery(owner: str | None = "example") -> DiscoveryResult:
    selections = () if owner is None else (CapabilitySelection(CAPABILITY, owner),)
    return DiscoveryResult((), selections, ())


def test_composition_binds_named_typed_port_for_direct_consumer_injection():
    binding = ProviderBinding(ProviderKey("example", CAPABILITY), GreetingProvider())
    port = bind_application_port(REQUEST, _discovery(), [binding])
    application = GreetingApplication(port)
    assert application.execute("Ada") == "hello Ada"
    assert not hasattr(application, "registry")


def test_missing_selected_provider_is_exact():
    with pytest.raises(
        CapabilityBindingError,
        match=r"capability example\.greeting@1 has no selected provider",
    ):
        bind_application_port(REQUEST, _discovery(None), [])


def test_missing_provider_binding_is_exact():
    with pytest.raises(
        CapabilityBindingError,
        match=r"selected provider 'example' has no binding for example\.greeting@1",
    ):
        bind_application_port(REQUEST, _discovery(), [])


def test_duplicate_provider_binding_is_rejected():
    binding = ProviderBinding(ProviderKey("example", CAPABILITY), GreetingProvider())
    with pytest.raises(CapabilityBindingError, match="duplicate bindings"):
        bind_application_port(REQUEST, _discovery(), [binding, binding])


def test_wrong_provider_type_is_rejected():
    binding = ProviderBinding(ProviderKey("example", CAPABILITY), object())
    with pytest.raises(CapabilityBindingError, match="does not implement GreetingPort"):
        bind_application_port(REQUEST, _discovery(), [binding])


def test_port_contract_must_be_runtime_checkable():
    class StaticOnlyPort(Protocol):
        def greet(self, name: str) -> str: ...

    request = CapabilityKey(CAPABILITY, StaticOnlyPort)
    binding = ProviderBinding(ProviderKey("example", CAPABILITY), GreetingProvider())
    with pytest.raises(CapabilityBindingError, match="port type .* is not runtime-checkable"):
        bind_application_port(request, _discovery(), [binding])
