"""Frame heartbeat timing and the local conformance cache (bh-i6ggn, ADR F7 / bh-32379 P-F7).

The beat proves *liveness*; conformance (``hive_ready`` and the other host checks) proves the
frame is fit to work. One process used to do both, so a slow conformance run under load
starved the beat and false-staled a healthy frame (bh-32379 L8). The sender now splits them:

* a **conformance job** runs on its own timer, measures the slow host-local checks and writes
  this cache atomically, stamped with ``measured_at`` (the time the measurement *started*, the
  conservative age basis);
* the **beat** never measures. It reads the newest cache, re-labels each cached check with its
  ``measured_at`` and adds a ``conformance-age`` check that fails once the cache is older than
  :data:`CONFORMANCE_MAX_AGE_SECONDS`. A stalled conformance job therefore ages into a
  non-conformant beat; it never stops the beat.

Only the sender changes. The signed ``HeartbeatLease`` contract and every reader predicate are
untouched: readers already look at ``conformance.status`` and each check's ``status`` only, and
check ids/evidence are free sender data inside the existing schema.

This module is the **one in-tree source** of the beat's TTL. ``heartbeat_report.generate`` and
the installable units (``heartbeat_sender``) both read these constants, and a test pins them.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import tempfile
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

# The beat's lease TTL. 900 s is the HeartbeatLease contract ceiling (``le=900``) and the value
# the factory has run since 2026-10-05 (bh-32379 L8, mitigation 1); every in-tree sender and
# unit derives from this one constant.
LEASE_DURATION_SECONDS = 900
# One beat per minute; the contract requires ``LEASE_DURATION_SECONDS >= 3 * INTERVAL_SECONDS``.
INTERVAL_SECONDS = 60
# The conformance job's own timer.
CONFORMANCE_INTERVAL_SECONDS = 300
# The documented staleness bound: a cached measurement older than one lease window is as stale
# as a lapsed beat, so the beat reports ``conformance-age: fail`` (non-conformant) past it. It
# leaves room for a loaded conformance run (~3.5 min observed, L8) plus the 5-minute timer.
CONFORMANCE_MAX_AGE_SECONDS = LEASE_DURATION_SECONDS
# A measurement stamped further in the future than this is treated as unusable (clock skew).
FUTURE_SKEW_SECONDS = 60

# The slow, host-local checks the conformance job owns. The release digest and host identity
# stay on the beat: they are cheap and must bind to the grant read at signing time.
CACHED_CHECK_IDS = ("host-config-partition", "hives-ready")
AGE_CHECK_ID = "conformance-age"
CACHE_FORMAT = "beadhive/heartbeat-conformance/v1"
_STATUSES = ("pass", "fail")


def _now() -> float:
    return time.time()


def _stamp(at: float) -> str:
    return datetime.fromtimestamp(at, UTC).isoformat()


def cache_path() -> Path:
    from . import config

    return config.home() / "heartbeat" / "conformance.json"


def _lock_path(path: Path) -> Path:
    return path.with_name(path.name + ".lock")


@dataclass(frozen=True)
class CachedConformance:
    measured_at: float
    duration_seconds: float
    checks: tuple[tuple[str, str], ...]  # (id, "pass" | "fail") in CACHED_CHECK_IDS order


@contextlib.contextmanager
def _exclusive(path: Path) -> Iterator[bool]:
    """Non-blocking single-runner lock; yields False when another run holds it."""
    import fcntl

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(_lock_path(path), "a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def write(cached: CachedConformance, path: Path | None = None) -> Path:
    """Atomically replace the cache: a reader sees the old or the new file, never a torn one."""
    path = path or cache_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    document = {
        "format": CACHE_FORMAT,
        "measured_at": _stamp(cached.measured_at),
        "duration_seconds": round(cached.duration_seconds, 3),
        "checks": [{"id": name, "status": status} for name, status in cached.checks],
    }
    fd, tmp = tempfile.mkstemp(prefix=".conformance-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(document, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return path


def read(path: Path | None = None) -> CachedConformance | None:
    """The newest cached measurement, or ``None`` when absent or malformed (fail closed)."""
    path = path or cache_path()
    try:
        document = json.loads(path.read_text())
        if document.get("format") != CACHE_FORMAT:
            return None
        measured = datetime.fromisoformat(document["measured_at"])
        if measured.tzinfo is None:
            return None
        checks = tuple((item["id"], item["status"]) for item in document["checks"])
        if tuple(name for name, _ in checks) != CACHED_CHECK_IDS or any(
            status not in _STATUSES for _, status in checks
        ):
            return None
        duration = float(document.get("duration_seconds", 0.0))
        if not math.isfinite(duration):
            return None
        return CachedConformance(measured.timestamp(), duration, checks)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def refresh(
    measure: Callable[[], list[dict]], *, path: Path | None = None, clock=None
) -> CachedConformance | None:
    """Run one conformance measurement and publish it to the cache.

    ``measure`` returns check dicts for exactly :data:`CACHED_CHECK_IDS`. Returns ``None``
    without measuring when another conformance run already holds the lock, so overlapping
    timer firings never stack up.
    """
    path = path or cache_path()
    clock = clock or _now
    with _exclusive(path) as acquired:
        if not acquired:
            return None
        started = clock()
        checks = measure()
        finished = clock()
        statuses = {item["id"]: item["status"] for item in checks}
        cached = CachedConformance(
            measured_at=started,
            duration_seconds=max(0.0, finished - started),
            checks=tuple(
                (name, "pass" if statuses.get(name) == "pass" else "fail")
                for name in CACHED_CHECK_IDS
            ),
        )
        write(cached, path)
        return cached


def beat_checks(
    cached: CachedConformance | None,
    *,
    now: float,
    max_age: float = CONFORMANCE_MAX_AGE_SECONDS,
) -> dict[str, dict]:
    """Project the cache into signed checks for one beat, keyed by check id.

    Evidence is fixed-format text (timestamps and integers only), never subprocess output.
    """
    if cached is None:
        missing = {
            name: {
                "id": name,
                "status": "fail",
                "evidence": "no cached conformance measurement",
            }
            for name in CACHED_CHECK_IDS
        }
        missing[AGE_CHECK_ID] = {
            "id": AGE_CHECK_ID,
            "status": "fail",
            "evidence": f"no cached conformance measurement; bound {int(max_age)}s",
        }
        return missing
    measured = _stamp(cached.measured_at)
    out = {
        name: {
            "id": name,
            "status": status,
            "evidence": f"measured check {'passed' if status == 'pass' else 'failed'}"
            f" at {measured}",
        }
        for name, status in cached.checks
    }
    age = now - cached.measured_at
    fresh = -FUTURE_SKEW_SECONDS <= age <= max_age
    out[AGE_CHECK_ID] = {
        "id": AGE_CHECK_ID,
        "status": "pass" if fresh else "fail",
        "evidence": f"cached conformance measured at {measured}; age {math.floor(age)}s;"
        f" bound {int(max_age)}s",
    }
    return out
