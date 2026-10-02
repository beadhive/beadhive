"""Bound local custody waits by the same monotonic SQL operation budget."""

from __future__ import annotations

import fcntl
import time


def flock_until(file, deadline: float) -> None:
    while True:
        if time.monotonic() >= deadline:
            raise TimeoutError("HQ local custody lock deadline exceeded")
        try:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            time.sleep(min(0.02, max(0, deadline - time.monotonic())))
