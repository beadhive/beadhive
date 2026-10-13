"""Host adapters for the local engine's probe ports: availability, disk space and liveness.

Stdlib only (no psutil): memory comes from ``/proc/meminfo`` and CPUs from the scheduler
affinity mask. Both readers take their source as a parameter, so tests feed a file they wrote
and never read the real host. A host without ``/proc/meminfo`` (macOS) makes the availability
probe raise, which the engine selection treats as "local engine unavailable" and falls back to
the slots engine, loudly.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from ._types import Capacity, Owner

__all__ = [
    "MeminfoAvailability",
    "current_owner",
    "host_cpu_count",
    "pid_alive",
    "statvfs_free",
]

MEMINFO = Path("/proc/meminfo")
_PROC = Path("/proc")


def host_cpu_count() -> int:
    """CPUs this process may run on (the affinity mask where the platform has one)."""
    try:
        return len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        return os.cpu_count() or 1


class MeminfoAvailability:
    """An :data:`~beadhive_validation_broker.Availability` over ``/proc/meminfo``.

    ``memory_bytes`` is ``MemAvailable``; ``cpu`` is the CPU count (a capacity, not a load
    reading); no pool is measured, so pools come from the configured budget alone.
    """

    def __init__(
        self, path: Path = MEMINFO, *, cpu_count: Callable[[], int] = host_cpu_count
    ) -> None:
        self._path = Path(path)
        self._cpu_count = cpu_count

    def read(self) -> dict[str, int]:
        """``/proc/meminfo`` in bytes. Raises ``OSError`` when the file is missing."""
        values: dict[str, int] = {}
        for line in self._path.read_text(encoding="ascii").splitlines():
            name, _, rest = line.partition(":")
            fields = rest.split()
            if not fields or not fields[0].isdigit():
                continue
            scale = 1024 if len(fields) > 1 and fields[1].lower() == "kb" else 1
            values[name.strip()] = int(fields[0]) * scale
        return values

    def total_memory_bytes(self) -> int:
        return self._required("MemTotal")

    def __call__(self) -> Capacity:
        return Capacity(float(self._cpu_count()), self._required("MemAvailable"))

    def _required(self, name: str) -> int:
        value = self.read().get(name)
        if value is None:
            raise OSError(f"{self._path} has no {name} line")
        return value


def statvfs_free(path: str) -> int:
    """A :data:`~beadhive_validation_broker.DiskSpace`: bytes an unprivileged writer may still use
    on the filesystem holding ``path`` (``f_bavail * f_frsize``). Raises ``OSError`` when the
    path cannot be read, which makes that root count as full."""
    st = os.statvfs(path)
    return int(st.f_bavail) * int(st.f_frsize)


def _start_token(pid: int, proc: Path) -> str | None:
    """The process start time in clock ticks (``/proc/<pid>/stat`` field 22), if readable."""
    try:
        stat = (proc / str(pid) / "stat").read_text(encoding="ascii")
    except OSError:
        return None
    # The command name (field 2) may hold spaces and parentheses; split after its last ")".
    fields = stat.rpartition(")")[2].split()
    return fields[19] if len(fields) > 19 else None


def current_owner(*, proc: Path = _PROC) -> Owner:
    """This process as a lease or run-directory :class:`Owner` that :func:`pid_alive` can probe:
    its pid, its start token (so a recycled pid never reads as alive) and this host's name."""
    pid = os.getpid()
    return Owner(pid=pid, start_token=_start_token(pid, proc), host=os.uname().nodename)


def pid_alive(owner: Owner, *, proc: Path = _PROC, hostname: str | None = None) -> bool:
    """A :data:`~beadhive_validation_broker.ProcessLiveness`: pid alive with the same start token.

    An owner on another host cannot be probed and counts as alive (never reclaim what you
    cannot see). A start token that cannot be read is not evidence of death.
    """
    if owner.host is not None and owner.host != (hostname or os.uname().nodename):
        return True
    try:
        os.kill(owner.pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    if owner.start_token is None:
        return True
    current = _start_token(owner.pid, proc)
    return current is None or current == owner.start_token
