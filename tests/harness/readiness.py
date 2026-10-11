"""Explicit readiness / exit signals for real-process tests (no sleep-polling).

``ReadyFifo`` is a one-shot, portable (Linux + macOS) channel: a child process opens the named
pipe for writing and writes a payload; the parent blocks on the read (bounded by a generous
ceiling) instead of polling for a file.  ``wait_pid_gone`` blocks until a process that is not our
child has exited, using ``pidfd_open`` where the kernel offers it.
"""

from __future__ import annotations

import contextlib
import errno
import os
import select
import threading
import time
from pathlib import Path


class ReadyFifo:
    """A named pipe whose first writer's payload is delivered to ``wait``."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        os.mkfifo(self.path)
        self._payload: str | None = None
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()

    def _read(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as stream:  # blocks until a writer opens
                self._payload = stream.read()
        finally:
            self._done.set()

    def wait(self, timeout: float = 30.0) -> str:
        if not self._done.wait(timeout):
            self.release()
            raise AssertionError(f"process did not signal readiness on {self.path}")
        return (self._payload or "").strip()

    def release(self) -> None:
        """Unblock a reader that never saw a writer (failure path)."""
        with contextlib.suppress(OSError):
            os.close(os.open(self.path, os.O_WRONLY | os.O_NONBLOCK))

    def wait_pid(self, timeout: float = 30.0) -> int:
        return int(self.wait(timeout))


def fifo_write_source(path: Path | str, payload: str) -> str:
    """Python source (for a ``-c`` child) that publishes *payload* on the fifo at *path*."""
    return f"open({str(path)!r}, 'w').write({payload})\n"


def _zombie(pid: int) -> bool:
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[-1].split()[0] == "Z"
    except OSError:
        return False


def wait_pid_gone(pid: int, timeout: float = 30.0) -> bool:
    """Block until *pid* exits (a zombie counts as gone); False when *timeout* elapses."""
    try:
        pidfd = os.pidfd_open(pid)  # Linux 5.3+
    except ProcessLookupError:
        return True
    except (AttributeError, OSError) as exc:
        if isinstance(exc, OSError) and exc.errno not in {errno.ENOSYS, errno.EPERM}:
            raise
        return _wait_gone_portable(pid, timeout)
    try:
        poller = select.poll()
        poller.register(pidfd, select.POLLIN)
        return bool(poller.poll(timeout * 1000))
    finally:
        os.close(pidfd)


def _wait_gone_portable(pid: int, timeout: float) -> bool:
    # No pidfd (macOS / old kernels): a non-child exit has no blocking primitive, so fall back to
    # a bounded liveness probe on an Event rather than a bare sleep.
    deadline = time.monotonic() + timeout
    tick = threading.Event()
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        if _zombie(pid):
            return True
        tick.wait(0.05)
    return False
