"""Installed Beadhive Frame Bridge composition root."""

from __future__ import annotations

import argparse

from .. import frame_bridge_runtime


def main() -> None:
    """Compose and run the loopback Frame Bridge for one sealed DEV network profile."""
    argparse.ArgumentParser(description="Run the Beadhive Frame Bridge").parse_args()
    frame_bridge_runtime.main()
