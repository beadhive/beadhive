"""Installed Beadhive Frame Bridge composition root."""

from __future__ import annotations

from .. import frame_bridge_runtime


def main() -> None:
    """Compose and run the loopback Frame Bridge behind its external tunnel."""
    frame_bridge_runtime.main()
