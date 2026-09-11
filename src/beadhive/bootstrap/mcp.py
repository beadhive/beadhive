"""Installed FastMCP stdio composition root."""

from __future__ import annotations

from .. import mcp


def main() -> int:
    """Compose and run the stdio server with its bounded telemetry lifecycle."""
    return mcp.main()
