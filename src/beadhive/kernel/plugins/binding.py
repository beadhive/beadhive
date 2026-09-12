"""Typed capability binding performed only by a bootstrap composition root."""

from __future__ import annotations

from collections.abc import Iterable
from typing import cast

from .contracts import (
    CapabilityBindingError,
    CapabilityKey,
    DiscoveryResult,
    PortT,
    ProviderBinding,
    ProviderKey,
)


def bind_application_port(
    request: CapabilityKey[PortT],
    discovery: DiscoveryResult,
    providers: Iterable[ProviderBinding[object]],
) -> PortT:
    """Return one selected, runtime-conforming port for explicit consumer injection.

    This function is deliberately not a process-global registry.  Bootstrap passes the finite
    discovery result and provider objects, obtains a typed port, and injects that port into the
    application consumer.  Domain and application code therefore have nothing to query.
    """

    owner = discovery.owner_of(request.capability)
    if owner is None:
        raise CapabilityBindingError(
            f"capability {request.capability.render()} has no selected provider"
        )
    wanted = ProviderKey(owner, request.capability)
    matches = tuple(binding for binding in providers if binding.key == wanted)
    if not matches:
        raise CapabilityBindingError(
            f"selected provider {owner!r} has no binding for {request.capability.render()}"
        )
    if len(matches) > 1:
        raise CapabilityBindingError(
            f"selected provider {owner!r} has duplicate bindings for {request.capability.render()}"
        )
    binding = matches[0]
    try:
        conforms = isinstance(binding.port, request.port_type)
    except TypeError as exc:
        raise CapabilityBindingError(
            f"port type for {request.capability.render()} is not runtime-checkable"
        ) from exc
    if not conforms:
        raise CapabilityBindingError(
            f"provider {owner!r} for {request.capability.render()} does not implement "
            f"{request.port_type.__name__}"
        )
    return cast(PortT, binding.port)
