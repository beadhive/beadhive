"""Run one command and report its memory high-water mark (bh-jg7fy).

This file is executed as a script (``python -I validation_memory_exec.py``) as the innermost
launcher layer of a validation run, *inside* the run's systemd scope when one exists.  It must
therefore import nothing from :mod:`beadhive`: it is stdlib-only, starts quickly, and survives a
broken or partially-installed package.

Usage::

    validation_memory_exec.py --report <path> [--scoped] -- <command> [args...]

The child runs to completion with its signals forwarded.  Afterwards, while this process is still
inside the scope, the report records the scope cgroup's ``memory.peak`` and the number of kernel
OOM kills from ``memory.events`` (``--scoped``), or the largest waited-for descendant's
``ru_maxrss`` otherwise.  The child's exit status is reproduced exactly — a signal death is
re-raised on this process — so wrapping never changes how the caller classifies the result.
"""

from __future__ import annotations

import json
import os
import resource
import signal
import subprocess
import sys
from pathlib import Path

_FORWARDED = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT)


def _own_cgroup() -> Path | None:
    """The cgroup v2 directory of this process, or None when not on a unified hierarchy."""
    try:
        lines = Path("/proc/self/cgroup").read_text().splitlines()
    except OSError:
        return None
    for line in lines:
        if line.startswith("0::"):
            directory = Path("/sys/fs/cgroup") / line[3:].lstrip("/")
            return directory if directory.is_dir() else None
    return None


def _read_int(path: Path) -> int | None:
    try:
        raw = path.read_text().strip()
    except OSError:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _events(path: Path) -> dict[str, int]:
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return {}
    events: dict[str, int] = {}
    for line in lines:
        key, _, value = line.partition(" ")
        try:
            events[key] = int(value)
        except ValueError:
            continue
    return events


def _measure(scoped: bool) -> dict:
    if scoped:
        cgroup = _own_cgroup()
        if cgroup is not None:
            peak = _read_int(cgroup / "memory.peak")
            if peak is not None:
                events = _events(cgroup / "memory.events")
                return {
                    "peak_bytes": peak,
                    "peak_source": "cgroup.memory.peak",
                    "oom_kills": events.get("oom_kill", 0),
                    "memory_max_events": events.get("max", 0),
                    "memory_high_events": events.get("high", 0),
                }
    # ru_maxrss is KiB on Linux: the largest single waited-for descendant, not a sum.
    maxrss = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    return {
        "peak_bytes": int(maxrss) * 1024,
        "peak_source": "rusage.children.maxrss",
        "oom_kills": None,
    }


def _write_report(path: Path, report: dict) -> None:
    try:
        partial = path.with_name(path.name + ".partial")
        partial.write_text(json.dumps(report, sort_keys=True))
        partial.replace(path)
    except OSError:
        pass  # measurement is evidence, never a reason to change the run's outcome


def _parse(argv: list[str]) -> tuple[Path | None, bool, list[str]]:
    report: Path | None = None
    scoped = False
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--":
            return report, scoped, argv[index + 1 :]
        if arg == "--scoped":
            scoped = True
        elif arg == "--report" and index + 1 < len(argv):
            index += 1
            report = Path(argv[index])
        else:
            break
        index += 1
    raise SystemExit("usage: validation_memory_exec.py --report PATH [--scoped] -- COMMAND...")


def main(argv: list[str]) -> int:
    report_path, scoped, command = _parse(argv)
    if not command:
        raise SystemExit("validation_memory_exec.py: no command given")
    try:
        child = subprocess.Popen(command)
    except FileNotFoundError:
        print(f"validation_memory_exec.py: {command[0]}: command not found", file=sys.stderr)
        return 127
    except PermissionError:
        print(f"validation_memory_exec.py: {command[0]}: permission denied", file=sys.stderr)
        return 126

    def _forward(signum, _frame):
        try:
            child.send_signal(signum)
        except OSError:
            pass

    for signum in _FORWARDED:
        signal.signal(signum, _forward)
    returncode = child.wait()
    for signum in _FORWARDED:
        signal.signal(signum, signal.SIG_DFL)
    if report_path is not None:
        _write_report(report_path, {"schema": 1, "returncode": returncode, **_measure(scoped)})
    if returncode < 0:
        os.kill(os.getpid(), -returncode)  # reproduce the child's signal death for the caller
        return 128 - returncode  # only reached when the signal is ignored or blocked
    return returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
