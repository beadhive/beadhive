"""Current host-runtime catalog facts used only for manifest drift validation.

This is not an installer and deliberately carries no Nix attributes, package-manager commands,
or enablement policy.  It records which optional integrations ship in ``runtime-core`` and the
external executable each may use.  Plugin discovery does not import this module.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, order=True)
class RuntimePluginEntry:
    plugin_id: str
    module: str
    external_executable: str
    delivery: str = "runtime-core"


PLUGIN_RUNTIME_CATALOG = (
    RuntimePluginEntry("herdr", "beadhive.herdr_plugin", "herdr"),
    RuntimePluginEntry("hitch", "beadhive.hitch_plugin", "hitch"),
    RuntimePluginEntry("observaloop", "beadhive.observaloop", "observaloop"),
    RuntimePluginEntry("orca", "beadhive.orca", "orca"),
    RuntimePluginEntry("repowise", "beadhive.repowise_plugin", "repowise"),
)
