"""Memory bounds, memory-aware admission, and peak accounting for validation runs (bh-jg7fy).

With no swap, a host whose validation lanes exhaust RAM livelocks in reclaim instead of
OOM-killing the offender.  This module supplies the three host-side defences:

* ``scope_properties`` — ``MemoryHigh``/``MemoryMax``/``MemorySwapMax`` for the validation run's
  user systemd scope, so the kernel OOM-kills *inside the scope* (resolved from
  ``work.validation_memory``; percentages are of physical RAM);
* ``wait_for_floor`` — admission waits, without starting, until ``MemAvailable`` is above a
  configurable floor, and returns an auditable record of the wait;
* ``read_report`` — the peak/oom evidence written by :mod:`beadhive.validation_memory_exec`,
  the innermost launcher layer, for the run manifest.

Everything is configurable and opt-out-able (``work.validation_memory.enabled``, or
``BH_VALIDATION_MEMORY=false`` for one invocation).  Where a user systemd manager or the cgroup v2
memory controller is unavailable the bounds are skipped with a recorded note; the floor still
applies wherever ``/proc/meminfo`` exists.

The same guard is a CLI for ad-hoc test runs in :mod:`beadhive.validation_memory_cli` (no host
slot is taken — never nest it around ``bh work check``/``submit``, which admit themselves)::

    uv run python -m beadhive.validation_memory_cli -- uv run pytest -n 2 tests/test_x.py
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

MEMINFO_PATH = Path("/proc/meminfo")
EXEC_SCRIPT = Path(__file__).with_name("validation_memory_exec.py")
# Re-announce a continuing wait at this cadence so a queued gate never looks hung.
ANNOUNCE_SECONDS = 60.0

_SIZE = re.compile(r"^(?P<number>\d+(?:\.\d+)?)(?P<unit>%|[KMGT])?$", re.IGNORECASE)
_UNITS = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
_OFF = {"", "none", "infinity", "off"}


class MemoryAdmissionTimeout(RuntimeError):
    """MemAvailable stayed below the admission floor for the configured timeout."""

    def __init__(self, record: dict):
        super().__init__(
            f"host MemAvailable stayed below the {_human(record.get('floor_bytes'))} validation "
            f"admission floor for {record.get('wait_seconds', 0):.0f}s; not started"
        )
        self.record = record


@dataclass(frozen=True)
class MemorySettings:
    enabled: bool = True
    memory_high: str = "50%"
    memory_max: str = "60%"
    memory_swap_max: str = "0"
    admission_floor: str = "10%"
    admission_timeout_seconds: float = 1800.0
    admission_poll_seconds: float = 5.0


def _bool(value: object, *, source: str) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{source} must be a boolean")


def parse_size(value: object, total_bytes: int | None) -> int | None:
    """Resolve a systemd-style size to bytes; None means unbounded/disabled.

    Accepts bytes, a binary K/M/G/T suffix, or a percentage of ``total_bytes``.  A percentage
    with no known total also resolves to None (the caller records why it was skipped).
    """
    if isinstance(value, bool):
        raise ValueError(f"invalid memory size: {value!r}")
    text = str(value).strip()
    if text.lower() in _OFF:
        return None
    match = _SIZE.match(text)
    if not match:
        raise ValueError(f"invalid memory size: {value!r}")
    number = float(match["number"])
    unit = (match["unit"] or "").upper()
    if unit == "%":
        if not 0 < number <= 100:
            raise ValueError(f"memory percentage must be in (0, 100]: {value!r}")
        return int(total_bytes * number / 100) if total_bytes else None
    return int(number * _UNITS[unit])


def settings(cfg: dict) -> MemorySettings:
    """Resolve host-owned ``work.validation_memory`` plus the per-invocation env overrides."""
    work = cfg.get("work") if isinstance(cfg, dict) else None
    raw = work.get("validation_memory", {}) if isinstance(work, dict) else {}
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("work.validation_memory must be a mapping")
    defaults = MemorySettings()
    unknown = set(raw) - set(defaults.__dataclass_fields__)
    if unknown:
        raise ValueError("unknown work.validation_memory setting(s): " + ", ".join(sorted(unknown)))
    resolved = replace(
        defaults,
        enabled=_bool(raw.get("enabled", True), source="validation memory enabled"),
        memory_high=str(raw.get("memory_high", defaults.memory_high)),
        memory_max=str(raw.get("memory_max", defaults.memory_max)),
        memory_swap_max=str(raw.get("memory_swap_max", defaults.memory_swap_max)),
        admission_floor=str(raw.get("admission_floor", defaults.admission_floor)),
        admission_timeout_seconds=float(
            raw.get("admission_timeout_seconds", defaults.admission_timeout_seconds)
        ),
        admission_poll_seconds=float(
            raw.get("admission_poll_seconds", defaults.admission_poll_seconds)
        ),
    )
    override = os.environ.get("BH_VALIDATION_MEMORY")
    if override is not None:
        resolved = replace(resolved, enabled=_bool(override, source="BH_VALIDATION_MEMORY"))
    floor = os.environ.get("BH_VALIDATION_MEMORY_FLOOR")
    if floor is not None:
        resolved = replace(resolved, admission_floor=floor)
    for name in ("memory_high", "memory_max", "memory_swap_max", "admission_floor"):
        parse_size(getattr(resolved, name), 1)  # fail fast on a malformed value
    if resolved.admission_timeout_seconds < 0 or resolved.admission_poll_seconds <= 0:
        raise ValueError("validation memory admission timeout/poll must be non-negative/positive")
    return resolved


def read_meminfo(path: Path | None = None) -> dict[str, int]:
    """``/proc/meminfo`` in bytes; empty when the host has none (e.g. macOS)."""
    try:
        lines = (MEMINFO_PATH if path is None else path).read_text().splitlines()
    except OSError:
        return {}
    values: dict[str, int] = {}
    for line in lines:
        key, _, rest = line.partition(":")
        fields = rest.split()
        if not fields:
            continue
        try:
            number = int(fields[0])
        except ValueError:
            continue
        values[key.strip()] = number * 1024 if fields[1:2] == ["kB"] else number
    return values


def _human(value: object) -> str:
    if not isinstance(value, int) or isinstance(value, bool):
        return "unknown"
    for unit, scale in (("G", 1024**3), ("M", 1024**2), ("K", 1024)):
        if value >= scale:
            return f"{value / scale:.1f}{unit}"
    return f"{value}B"


def wait_for_floor(
    policy: MemorySettings,
    *,
    echo: Callable[[str], object] = print,
    sleep: Callable[[float], object] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict:
    """Block, without starting anything, until MemAvailable reaches the admission floor.

    Returns the admission record for the run manifest.  Raises :class:`MemoryAdmissionTimeout`
    when ``admission_timeout_seconds`` (non-zero) elapses first.
    """
    info = read_meminfo()
    total = info.get("MemTotal")
    record: dict = {
        "enabled": policy.enabled,
        "floor": policy.admission_floor,
        "floor_bytes": None,
        "available_bytes": info.get("MemAvailable"),
        "total_bytes": total,
        "waited": False,
        "wait_seconds": 0.0,
        "note": None,
    }
    if not policy.enabled:
        return record
    floor = parse_size(policy.admission_floor, total)
    record["floor_bytes"] = floor
    if not floor:
        return record
    if "MemAvailable" not in info:
        record["note"] = "MemAvailable unreadable; admission floor not enforced"
        return record
    started = clock()
    announced: float | None = None
    while True:
        available = read_meminfo().get("MemAvailable")
        record["available_bytes"] = available
        record["wait_seconds"] = round(clock() - started, 3)
        if available is None or available >= floor:
            if available is None:
                record["note"] = "MemAvailable unreadable; admission floor not enforced"
            if record["waited"]:
                echo(
                    f"  → MemAvailable {_human(available)} reached the {_human(floor)} "
                    f"admission floor after {record['wait_seconds']:.1f}s"
                )
            return record
        record["waited"] = True
        timeout = policy.admission_timeout_seconds
        if timeout and record["wait_seconds"] >= timeout:
            record["timed_out"] = True
            raise MemoryAdmissionTimeout(record)
        now = clock()
        if announced is None or now - announced >= ANNOUNCE_SECONDS:
            echo(
                f"  → waiting for memory: MemAvailable {_human(available)} is below the "
                f"{_human(floor)} validation admission floor (work.validation_memory."
                "admission_floor; BH_VALIDATION_MEMORY_FLOOR=0 skips the wait)"
            )
            announced = now
        sleep(policy.admission_poll_seconds)


def memory_controller_available() -> bool:
    """Whether the user manager can delegate the cgroup v2 memory controller to its scopes."""
    uid = os.getuid()
    manager = Path(f"/sys/fs/cgroup/user.slice/user-{uid}.slice/user@{uid}.service")
    try:
        controllers = (manager / "cgroup.subtree_control").read_text().split()
    except OSError:
        return False
    return "memory" in controllers


def scope_properties(policy: MemorySettings) -> tuple[list[str], dict]:
    """``systemd-run --property`` arguments and the planned memory record for the manifest."""
    total = read_meminfo().get("MemTotal")
    record: dict = {
        "enabled": policy.enabled,
        "applied": False,
        "mechanism": "disabled" if not policy.enabled else "unavailable",
        "memory_high": policy.memory_high,
        "memory_max": policy.memory_max,
        "memory_swap_max": policy.memory_swap_max,
        "memory_high_bytes": None,
        "memory_max_bytes": None,
        "memory_swap_max_bytes": None,
        "total_bytes": total,
        "note": None,
        "peak_bytes": None,
        "peak_source": None,
        "oom_kills": None,
    }
    if not policy.enabled:
        return [], record
    properties: list[str] = []
    for key, prop in (
        ("memory_high", "MemoryHigh"),
        ("memory_max", "MemoryMax"),
        ("memory_swap_max", "MemorySwapMax"),
    ):
        value = parse_size(getattr(policy, key), total)
        record[f"{key}_bytes"] = value
        if value is not None:
            properties.append(f"--property={prop}={value}")
    if not properties:
        record["note"] = "no memory bound configured"
    elif not memory_controller_available():
        record["note"] = (
            "cgroup v2 memory controller is not delegated to the user systemd manager; "
            "memory bounds skipped"
        )
        properties = []
    return properties, record


def exec_prefix(report_path: Path, *, scoped: bool) -> list[str]:
    """The innermost launcher layer that measures the run (see validation_memory_exec)."""
    return [
        sys.executable,
        "-I",
        str(EXEC_SCRIPT),
        "--report",
        str(report_path),
        *(["--scoped"] if scoped else []),
        "--",
    ]


def read_report(report_path: Path | None) -> dict | None:
    if report_path is None:
        return None
    try:
        value = json.loads(Path(report_path).read_text())
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def finalize(record: dict | None, report: dict | None) -> dict | None:
    """Fold the exec report's peak/oom evidence into the planned memory record."""
    if record is None:
        return None
    merged = dict(record)
    if report:
        for key in ("peak_bytes", "peak_source", "oom_kills"):
            merged[key] = report.get(key)
    return merged


def oom_killed(record: dict | None) -> bool:
    kills = (record or {}).get("oom_kills")
    return isinstance(kills, int) and not isinstance(kills, bool) and kills > 0


def outcome_line(record: dict | None) -> str | None:
    """The one-line operator explanation of an over-limit run, else None."""
    if not oom_killed(record):
        return None
    assert record is not None
    return (
        f"✗ validation exceeded its memory bound (MemoryMax "
        f"{_human(record.get('memory_max_bytes'))}, peak {_human(record.get('peak_bytes'))}): the "
        f"kernel OOM-killed {record['oom_kills']} process(es) inside the validation scope. "
        "Reported red; tune work.validation_memory or reduce test parallelism."
    )
