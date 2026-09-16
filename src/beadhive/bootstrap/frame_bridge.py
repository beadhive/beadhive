"""Installed Beadhive Frame Bridge composition root."""

from __future__ import annotations

from .. import frame_bridge_runtime


def main() -> None:
    """Compose and run the loopback Frame Bridge for one sealed DEV network profile."""
    frame_bridge_runtime.main()
