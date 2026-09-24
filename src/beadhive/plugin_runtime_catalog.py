"""Stable built-in plugin runtime catalog.

This is neither an installer nor plugin policy.  It records the compatibility registry's exact
runtime order plus the delivery facts checked against built-in manifests.  Keeping module names
as data lets the legacy facade resolve integrations without static imports back into their
implementations.  Discovery itself does not import this module.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, order=True)
class RuntimePluginEntry:
    plugin_id: str
    module: str
    external_executable: str
    delivery: str = "runtime-core"


@dataclass(frozen=True, order=True)
class RuntimeCliEntry:
    """A lazily imported CLI app advertised by a built-in manifest projection."""

    plugin_id: str
    module: str
    object_name: str = "app"


PLUGIN_RUNTIME_CATALOG = (
    RuntimePluginEntry("orca", "beadhive.orca", "orca"),
    RuntimePluginEntry("observaloop", "beadhive.observaloop", "observaloop"),
    RuntimePluginEntry("hitch", "beadhive.hitch_plugin", "hitch"),
    RuntimePluginEntry("herdr", "beadhive.herdr_plugin", "herdr"),
    RuntimePluginEntry("repowise", "beadhive.repowise_plugin", "repowise"),
)

PLUGIN_RUNTIME_MODULES = tuple(entry.module for entry in PLUGIN_RUNTIME_CATALOG)

PLUGIN_CLI_RUNTIME_CATALOG = (RuntimeCliEntry("pants", "beadhive_pants.cli"),)
