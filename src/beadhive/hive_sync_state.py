"""The typed outcomes of ``hive.sync`` background pushes and pulls (bh-midl0).

Trust-profiles ADR section 5, "Outcomes are typed". Every push or pull the host daemon's sync
loop (:mod:`beadhive.hive_sync_loop`) attempts records a :class:`SyncOutcome` in
``$BH_HOME/state/hive-sync.json``, in the ``bh-nc052.2`` contract shape the frame-ref merge
already uses: a status, the attempt time, the target, positive evidence (pending commits, the
frame merges a writer's pull ran) and the error. Per hive the record also keeps what
``bh doctor`` shows: pending commits, the last push, the last error and the next retry.

The record is also the handshake ``--await-sync`` uses: a verb that wants its writes on the
remote calls :func:`request_flush`, which the loop treats as due at once, then waits
(:func:`await_flush`) for a successful push that STARTED after the request — such a push
carries every commit the verb made before asking. Nothing here pushes or blocks a verb by
itself; a verb that never asks never waits.

Typer-free; no bd. Never raises on an unreadable or unwritable record (the loop and doctor keep
going; a waiter simply sees no outcome).
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import log

STATE_FILE = "hive-sync.json"

PUSH = "push"
PULL = "pull"


class Status:
    """A sync attempt's outcome."""

    #: The push landed (or the pull merged).
    OK = "ok"
    #: Nothing was pending: the remote already has every local commit.
    CLEAN = "clean"
    #: The attempt failed; ``error`` says why and a push carries ``next_retry_at``.
    FAILED = "failed"
    #: Not attempted on this host: ``error`` says why (e.g. not the writer).
    SKIPPED = "skipped"

    SUCCESS = frozenset({OK, CLEAN})


@dataclass(frozen=True)
class SyncOutcome:
    """One background push or pull attempt (``bh-nc052.2`` contract shape)."""

    hive: str
    op: str
    status: str
    #: When the attempt started (a push carries every commit made before this).
    attempted_at: float
    finished_at: float = 0.0
    #: ``main``, ``refs/dolt/frame/<id>``, or ``""`` when nothing was targeted.
    target: str = ""
    #: What made it due: ``timer``, ``max_pending``, ``flush`` or ``retry`` (after backoff).
    reason: str = ""
    #: Local commits not yet on the target when the attempt started (``None``: unknown).
    pending: int | None = None
    error: str = ""
    #: Consecutive failed pushes, and when the next one may run (backoff).
    failures: int = 0
    next_retry_at: float | None = None
    #: A writer's pull under ``open``: each frame ref's merge status.
    frame_merges: tuple[Mapping, ...] = field(default_factory=tuple)

    @property
    def succeeded(self) -> bool:
        return self.status in Status.SUCCESS

    def as_dict(self) -> dict:
        return {
            "hive": self.hive,
            "op": self.op,
            "status": self.status,
            "attempted_at": self.attempted_at,
            "finished_at": self.finished_at,
            "target": self.target,
            "reason": self.reason,
            "pending": self.pending,
            "error": self.error,
            "failures": self.failures,
            "next_retry_at": self.next_retry_at,
            "frame_merges": [dict(item) for item in self.frame_merges],
        }

    @classmethod
    def from_dict(cls, data: Mapping) -> SyncOutcome:
        pending = data.get("pending")
        retry = data.get("next_retry_at")
        return cls(
            hive=str(data["hive"]),
            op=str(data["op"]),
            status=str(data["status"]),
            attempted_at=float(data["attempted_at"]),
            finished_at=float(data.get("finished_at") or 0.0),
            target=str(data.get("target") or ""),
            reason=str(data.get("reason") or ""),
            pending=None if pending is None else int(pending),
            error=str(data.get("error") or ""),
            failures=int(data.get("failures") or 0),
            next_retry_at=None if retry is None else float(retry),
            frame_merges=tuple(
                dict(item) for item in data.get("frame_merges") or () if isinstance(item, Mapping)
            ),
        )


@dataclass(frozen=True)
class HiveSyncRecord:
    """What the record holds for one hive."""

    hive: str
    push: SyncOutcome | None = None
    pull: SyncOutcome | None = None
    #: The last push that succeeded (its start time).
    last_push_ok_at: float | None = None
    #: The last pending count and when it was taken.
    pending: int | None = None
    pending_at: float | None = None
    #: The latest ``--await-sync`` request not yet answered by a push.
    flush_requested_at: float | None = None

    @property
    def failures(self) -> int:
        return self.push.failures if self.push is not None else 0

    @property
    def next_retry_at(self) -> float | None:
        return self.push.next_retry_at if self.push is not None else None

    @property
    def last_error(self) -> str:
        """The latest failed attempt's error, push or pull."""
        failed = [o for o in (self.push, self.pull) if o is not None and o.status == Status.FAILED]
        return max(failed, key=lambda o: o.attempted_at).error if failed else ""

    @property
    def flush_pending(self) -> bool:
        """A flush was requested after the latest push attempt started."""
        if self.flush_requested_at is None:
            return False
        return self.push is None or self.push.attempted_at < self.flush_requested_at

    def as_dict(self) -> dict:
        return {
            "push": None if self.push is None else self.push.as_dict(),
            "pull": None if self.pull is None else self.pull.as_dict(),
            "last_push_ok_at": self.last_push_ok_at,
            "pending": self.pending,
            "pending_at": self.pending_at,
            "flush_requested_at": self.flush_requested_at,
        }

    @classmethod
    def from_dict(cls, hive: str, data: Mapping) -> HiveSyncRecord:
        def outcome(key: str) -> SyncOutcome | None:
            value = data.get(key)
            return SyncOutcome.from_dict(value) if isinstance(value, Mapping) else None

        def number(key: str) -> float | None:
            value = data.get(key)
            return None if value is None else float(value)

        pending = data.get("pending")
        return cls(
            hive=hive,
            push=outcome("push"),
            pull=outcome("pull"),
            last_push_ok_at=number("last_push_ok_at"),
            pending=None if pending is None else int(pending),
            pending_at=number("pending_at"),
            flush_requested_at=number("flush_requested_at"),
        )


# =============================================================================================
# The record ($BH_HOME/state/hive-sync.json)
# =============================================================================================


def state_path() -> Path | None:
    try:
        from .config_consumer_ports import daemon_settings as config

        return Path(config.home()) / "state" / STATE_FILE
    except Exception:  # noqa: BLE001 - no home: nothing is recorded
        return None


@contextlib.contextmanager
def _locked(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(path.with_name(f"{path.name}.lock"), "a+", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(path: Path, data: Mapping) -> None:
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def load(*, path: Path | None = None) -> dict[str, HiveSyncRecord]:
    """Every hive's record. Never raises."""
    path = path or state_path()
    if path is None:
        return {}
    out: dict[str, HiveSyncRecord] = {}
    for hive, data in _read(path).items():
        if not isinstance(data, Mapping):
            continue
        try:
            out[str(hive)] = HiveSyncRecord.from_dict(str(hive), data)
        except (KeyError, TypeError, ValueError):
            continue
    return out


def recorded(hive: str, *, path: Path | None = None) -> HiveSyncRecord:
    return load(path=path).get(hive) or HiveSyncRecord(hive)


def update(hive: str, change: Callable[[dict], None], *, path: Path | None = None) -> None:
    """Apply `change` to `hive`'s raw record under the file lock. Never raises."""
    path = path or state_path()
    if path is None:
        return
    try:
        with _locked(path):
            data = _read(path)
            entry = data.get(hive)
            entry = dict(entry) if isinstance(entry, Mapping) else {}
            change(entry)
            data[hive] = entry
            _write(path, data)
    except OSError as exc:
        log.get_logger(__name__).warning("hive_sync_unrecorded", hive=hive, error=str(exc))


def record(outcome: SyncOutcome, *, path: Path | None = None) -> None:
    """Record `outcome` as `outcome.hive`'s latest push or pull."""

    def change(entry: dict) -> None:
        entry[outcome.op] = outcome.as_dict()
        if outcome.op == PUSH:
            if outcome.pending is not None:
                entry["pending"] = 0 if outcome.succeeded else outcome.pending
                entry["pending_at"] = outcome.finished_at or outcome.attempted_at
            if outcome.succeeded:
                entry["last_push_ok_at"] = outcome.attempted_at

    update(outcome.hive, change, path=path)


def record_pending(hive: str, pending: int | None, at: float, *, path: Path | None = None):
    def change(entry: dict) -> None:
        entry["pending"] = pending
        entry["pending_at"] = at

    update(hive, change, path=path)


def request_flush(
    hive: str, *, now: Callable[[], float] = time.time, path: Path | None = None
) -> float:
    """Ask the sync loop to push `hive` now (``--await-sync``); returns the request time."""
    requested = now()

    def change(entry: dict) -> None:
        prior = entry.get("flush_requested_at")
        entry["flush_requested_at"] = max(requested, float(prior or 0.0))

    update(hive, change, path=path)
    return requested


def flushed(hive: str, requested_at: float, *, path: Path | None = None) -> SyncOutcome | None:
    """The push that answers a flush requested at `requested_at`: a successful push of `hive`
    that started at or after it. ``None`` while there is none."""
    push = recorded(hive, path=path).push
    if push is not None and push.succeeded and push.attempted_at >= requested_at:
        return push
    return None


def await_flush(
    hive: str,
    requested_at: float,
    *,
    timeout_s: float,
    wait: Callable[[float], Any],
    clock: Callable[[], float] = time.monotonic,
    poll_s: float = 1.0,
    path: Path | None = None,
) -> SyncOutcome | None:
    """Wait up to `timeout_s` for :func:`flushed`; ``None`` on timeout. `wait(seconds)` is the
    pause between reads (``time.sleep`` in the CLI, a fake clock's advance in tests)."""
    deadline = clock() + timeout_s
    while True:
        outcome = flushed(hive, requested_at, path=path)
        if outcome is not None:
            return outcome
        remaining = deadline - clock()
        if remaining <= 0:
            return None
        wait(min(poll_s, remaining))


# =============================================================================================
# Reads for doctor
# =============================================================================================


def _when(stamp: float | None, now: float) -> str:
    if stamp is None:
        return "never"
    delta = now - stamp
    if delta < 0:
        return f"in {int(-delta)}s"
    return f"{int(delta)}s ago"


def doctor_lines(data: Mapping, *, now: float | None = None) -> list[str]:
    """One line per hive with the sync loop on: pending commits, the last push, the last error
    and the next retry."""
    now = time.time() if now is None else now
    lines: list[str] = []
    for item in data.get("hives", ()):
        if not item.get("active"):
            continue
        pending = item.get("pending")
        retry = item.get("next_retry_at")
        error = item.get("last_error") or ""
        glyph = "⚠" if error else "✓"
        parts = [
            f"{'?' if pending is None else pending} pending",
            f"last push {_when(item.get('last_push_ok_at'), now)}",
        ]
        if retry is not None and retry > now:
            parts.append(f"next retry {_when(retry, now)}")
        lines.append(f"  {glyph} {item['hive']}: hive.sync {', '.join(parts)}")
        if error:
            lines.append(f"      last error: {error}")
    return lines
