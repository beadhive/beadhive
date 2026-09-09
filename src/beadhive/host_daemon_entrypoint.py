"""Installed host-daemon composition root."""

from __future__ import annotations

import sys

from . import daemon_state_broker, host_daemon


def main() -> None:
    """Run the daemon with the authoritative MCP server factory injected explicitly."""
    try:
        host_daemon.serve(
            state_broker_factory=daemon_state_broker.DaemonStateBroker.for_host,
        )
    except (host_daemon.DaemonError, FileNotFoundError, KeyError, ValueError) as exc:
        print(f"\u2717 {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
