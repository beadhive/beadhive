"""Prototype of the root composition step ``bootstrap/tracker.py`` would own.

It reuses the kernel UNCHANGED: ``discover_plugins`` for manifest selection and
``bind_application_port`` for the typed bind. The only new logic is (1) folding each tracker
plugin's own ``enabled`` flag into the kernel's enablement map, exactly as the legacy facade's
``_compose`` does for orca/hitch/repowise, and (2) a provider row with a lazy loader, exactly as
``bootstrap/impact.py`` does for pants.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bh-gj1hb"))

from credentials_proto import TokenSource  # noqa: E402
from tracker_contracts import TrackerAdapter  # noqa: E402

from beadhive.kernel.plugins import (  # noqa: E402
    DiscoveryResult,
    bind_application_port,
    discover_plugins,
)
from beadhive_plugins.contracts import (  # noqa: E402
    CapabilityBindingError,
    CapabilityKey,
    CapabilityRef,
    DiagnosticCode,
    ManifestSource,
    PluginDiagnostic,
    ProviderBinding,
    ProviderKey,
)

TRACKER_ADAPTER = CapabilityRef("tracker.adapter", 1)
TRACKER_ADAPTER_PORT = CapabilityKey(TRACKER_ADAPTER, TrackerAdapter)


@dataclass(frozen=True)
class TrackerAdapterProvider:
    """A first-party ``tracker.adapter`` runtime binding. ``load`` imports the plugin package
    lazily and is called only after discovery selected ``plugin_id`` (the pants precedent)."""

    plugin_id: str
    loader: Callable[[Mapping, TokenSource], object] = field(repr=False, compare=False)


@dataclass(frozen=True)
class TrackerComposition:
    state: str  # disabled | conflict | ready
    port: TrackerAdapter | None
    owner: str | None
    discovery: DiscoveryResult
    diagnostics: tuple[PluginDiagnostic, ...] = ()


def _own_enabled(config: Mapping) -> bool:
    return config.get("enabled", False) is True  # exact bool; default OFF


def compose_tracker_adapter(
    plugin_configs: Mapping[str, Mapping],
    *,
    sources: Sequence[ManifestSource],
    providers: Sequence[TrackerAdapterProvider],
    token_source: TokenSource,
    kernel: Mapping[str, object] | None = None,
) -> TrackerComposition:
    kernel = dict(kernel or {})
    enabled = dict(kernel.get("enabled", {}))  # type: ignore[arg-type]
    for provider in providers:
        own = _own_enabled(plugin_configs.get(provider.plugin_id, {}))
        enabled[provider.plugin_id] = own and enabled.get(provider.plugin_id, True)
    discovery = discover_plugins(
        list(sources),
        config={**kernel, "enabled": enabled},
        beadhive_version="0.15.1",
        kernel_version="1.0.0",
        host_executables={},
    )
    if any(d.code is DiagnosticCode.DUPLICATE_CAPABILITY for d in discovery.errors):
        return TrackerComposition("conflict", None, None, discovery, discovery.errors)
    owner = discovery.owner_of(TRACKER_ADAPTER)
    provider = next((p for p in providers if p.plugin_id == owner), None)
    if owner is None or provider is None:
        return TrackerComposition("disabled", None, None, discovery, discovery.errors)
    adapter = provider.loader(plugin_configs[owner], token_source)
    binding = ProviderBinding(ProviderKey(owner, TRACKER_ADAPTER), adapter)
    port = bind_application_port(TRACKER_ADAPTER_PORT, discovery, (binding,))
    return TrackerComposition("ready", port, owner, discovery, discovery.errors)


__all__ = [
    "CapabilityBindingError",
    "TRACKER_ADAPTER",
    "TRACKER_ADAPTER_PORT",
    "TrackerAdapterProvider",
    "TrackerComposition",
    "compose_tracker_adapter",
]
