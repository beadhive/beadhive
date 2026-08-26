"""Tiny owner-death watchdog for a heavyweight subprocess process group.

The watcher intentionally runs outside the owner's process group. If the owner disappears it
terminates the validation group, including descendants that a direct PDEATHSIG cannot reach.
It is an internal subprocess entry point, not a state store or public CLI.
"""

from __future__ import annotations

import contextlib
import os
import signal
import sys
import time


def _start_token(pid: int) -> str:
    """Linux process-start token, empty on unsupported/unreadable systems."""
    try:
        return open(f"/proc/{pid}/stat").read().rsplit(")", 1)[-1].split()[19]  # noqa: SIM115
    except (OSError, IndexError):
        return ""


def _owner_alive(pid: int, token: str) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    current = _start_token(pid)
    return not (token and current and current != token)


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except OSError:
        return False
    return True


def watch(owner_pid: int, owner_token: str, pgid: int) -> None:
    while _owner_alive(owner_pid, owner_token):
        if not _group_alive(pgid):
            return
        time.sleep(0.05)
    for sig, grace in ((signal.SIGTERM, 2.0), (signal.SIGKILL, 2.0)):
        with contextlib.suppress(OSError):
            os.killpg(pgid, sig)
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            if not _group_alive(pgid):
                return
            time.sleep(0.05)


def main() -> None:
    if len(sys.argv) != 4:
        raise SystemExit(2)
    watch(int(sys.argv[1]), sys.argv[2], int(sys.argv[3]))


if __name__ == "__main__":
    main()
