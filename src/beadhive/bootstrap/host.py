"""Installed operator-API and host-daemon composition root."""

from __future__ import annotations

from .. import host_daemon_entrypoint


def main() -> None:
    """Compose and run the one daemon-owned HTTP/MCP listener."""
    host_daemon_entrypoint.main()
