"""The host and plugin-kernel versions every composition root discovers manifests with.

Discovery checks each manifest's ``compatibility.beadhive`` and ``compatibility.plugin_kernel``
ranges against these two values. They used to be copied into every composition root (the legacy
facade, ``bootstrap.impact``, ``bootstrap.build_verify``); one definition here means a new root
(``bootstrap.tracker``) cannot drift from the others (tracker-adapter-plugin-adr.md, gap P5).
"""

from __future__ import annotations

#: The Beadhive version manifests are checked against (``compatibility.beadhive``).
BEADHIVE_COMPATIBILITY_VERSION = "0.15.1"

#: The plugin-kernel version manifests are checked against (``compatibility.plugin_kernel``).
PLUGIN_KERNEL_VERSION = "1.0.0"

__all__ = ["BEADHIVE_COMPATIBILITY_VERSION", "PLUGIN_KERNEL_VERSION"]
