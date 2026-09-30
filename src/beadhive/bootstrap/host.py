"""Installed operator-API and host-daemon composition root."""

from __future__ import annotations

import argparse

from .. import host_daemon_entrypoint


def main() -> None:
    """Compose and run the one daemon-owned HTTP/MCP listener."""
    argparse.ArgumentParser(description="Run the Beadhive host daemon").parse_args()
    host_daemon_entrypoint.main()
