"""Installed Development-gateway composition root."""

from __future__ import annotations

from .. import remote_gateway_runtime


def main() -> None:
    """Compose and run the loopback gateway behind its external tunnel."""
    remote_gateway_runtime.main()
