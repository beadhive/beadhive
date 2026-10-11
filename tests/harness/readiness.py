"""Explicit readiness / exit signals for real-process tests (no sleep-polling).

``ReadyFifo`` is a one-shot, portable (Linux + macOS) channel: a child process opens the named
pipe for writing and writes a payload; the parent blocks on the read (bounded by a generous
ceiling) instead of polling for a file.  ``wait_pid_gone`` blocks until a process that is not our
child has exited, using ``pidfd_open`` where the kernel offers it.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import os
import select
import threading
import time
from pathlib import Path


class ReadyFifo:
    """A named pipe on which children publish one line each; the parent blocks for them.

    The parent holds the pipe open read-write and non-blocking from construction, so a child's
    ``open(path, "w")`` never blocks, never sees ENXIO, and the read side never reports EOF while
    no child is connected.  ``wait`` / ``await_lines`` then block on readability (no sleep loop)
    up to a generous ceiling.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        os.mkfifo(self.path)
        self._fd = os.open(self.path, os.O_RDWR | os.O_NONBLOCK)
        self._buffer = b""

    def __del__(self) -> None:
        self.close()

    def close(self) -> None:
        fd, self._fd = getattr(self, "_fd", -1), -1
        if fd >= 0:
            with contextlib.suppress(OSError):
                os.close(fd)

    def _drain(self) -> None:
        while True:
            try:
                chunk = os.read(self._fd, 4096)
            except BlockingIOError:
                return
            if not chunk:
                return
            self._buffer += chunk

    def _lines(self) -> list[str]:
        return [line for line in self._buffer.decode().splitlines() if line.strip()]

    def wait_lines(self, count: int = 1, timeout: float = 30.0) -> list[str]:
        """Block until *count* lines arrived (a writer may deliver several lines at once)."""
        deadline = time.monotonic() + timeout
        poller = select.poll()
        poller.register(self._fd, select.POLLIN)
        self._drain()
        while len(self._lines()) < count:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not poller.poll(remaining * 1000):
                raise AssertionError(
                    f"expected {count} readiness line(s) on {self.path}, got {self._lines()!r}"
                )
            self._drain()
        return self._lines()

    async def await_lines(self, count: int = 1, timeout: float = 30.0) -> list[str]:
        """Event-loop-friendly ``wait_lines``: readability wakes the loop, nothing sleeps."""
        loop = asyncio.get_running_loop()
        wake = asyncio.Event()
        self._drain()
        if len(self._lines()) < count:
            loop.add_reader(self._fd, wake.set)
        try:
            while len(self._lines()) < count:
                wake.clear()
                try:
                    await asyncio.wait_for(wake.wait(), timeout)
                except TimeoutError:
                    raise AssertionError(
                        f"expected {count} readiness line(s) on {self.path}, got {self._lines()!r}"
                    ) from None
                self._drain()
        finally:
            loop.remove_reader(self._fd)
        return self._lines()

    def wait(self, timeout: float = 30.0) -> str:
        return self.wait_lines(1, timeout)[0]

    async def await_pid(self, timeout: float = 30.0) -> int:
        return int((await self.await_lines(1, timeout))[0])

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
