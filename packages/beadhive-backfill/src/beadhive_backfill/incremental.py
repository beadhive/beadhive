"""Per-pass cursors and the incremental-run contract (``bh backfill ... --since-cursor``).

A **cursor** is where a pass last read to: the first-parent head it saw (``land_sha``) and the
newest bd ``events`` row it saw (``events_at`` / ``events_row_id``). The next incremental run
reads only what moved past it. Cursors are kept in one small versioned JSON file per hive, beside
the coverage cache, and are written only after an applied run (``plan_hash`` and ``audit`` name
the run they came from), never by a dry run.

**A cursor is a hint, never an authority.** Everything a cursor lets a pass skip is something a
full run would have found already present, so a lost or wrong cursor costs time, not
correctness: a missing file means "no cursor"; a file that cannot be trusted (not JSON, another
schema, a failed digest, a malformed entry) or a cursor whose commit is no longer in the history
reads as no cursor *with a warning*, and the pass runs in full.

**Rows are not ordered by id.** The ``events`` table's ``id`` is a UUID and ``created_at`` has
second resolution, so "after the cursor" means ``created_at >= events_at`` (inclusive: a row
written in the same second as the cursor is seen again, harmlessly, because every proposal is
content-addressed and a repeat is PRESENT). ``events_row_id`` records which row set the mark.

Neutral file handling only: nothing here reads a store or imports ``beadhive``. The readers an
incremental pass needs are the :class:`IncrementalReaders` protocol; ``PathReaders`` implements it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from beadlog import check_timestamp
from beadlog_sources import HistoryRead

from .merges import MergeScan

CURSOR_SCHEMA = "1"
CURSOR_FILE = "cursors.json"
MODE_FULL = "full"
MODE_INCREMENTAL = "incremental"
MODE_SCOPED = "scoped"
INCREMENTAL_KEY = "incremental"

_SHA = re.compile(r"^[0-9a-f]{7,64}$")


@dataclass(frozen=True)
class PassCursor:
    """Where one pass last read to (module doc). Every position is optional: a pass records the
    ones it reads (``commits`` a sha only; ``timeline`` a sha and an events mark)."""

    pass_name: str
    land_sha: str | None = None
    events_at: str | None = None
    events_row_id: str | None = None
    recorded_at: str | None = None
    plan_hash: str | None = None
    audit: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "pass": self.pass_name,
            "land_sha": self.land_sha,
            "events_at": self.events_at,
            "events_row_id": self.events_row_id,
            "recorded_at": self.recorded_at,
            "plan_hash": self.plan_hash,
            "audit": self.audit,
        }

    @classmethod
    def from_json(cls, data: Any) -> PassCursor:
        """The cursor of one file entry; a malformed entry raises ``ValueError``."""
        if not isinstance(data, Mapping):
            raise ValueError("a cursor is not an object")
        name = data.get("pass")
        if not isinstance(name, str) or not name:
            raise ValueError("a cursor has no pass name")
        sha = data.get("land_sha")
        if sha is not None and not (isinstance(sha, str) and _SHA.match(sha)):
            raise ValueError(f"{name}: land_sha {sha!r} is not a commit sha")
        for key in ("events_at", "recorded_at"):
            value = data.get(key)
            if value is not None:
                try:
                    check_timestamp(value)
                except ValueError as exc:
                    raise ValueError(f"{name}: {key} {value!r} is not a timestamp") from exc
        for key in ("events_row_id", "plan_hash", "audit"):
            value = data.get(key)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"{name}: {key} is not text")
        return cls(
            name,
            sha,
            data.get("events_at"),
            data.get("events_row_id"),
            data.get("recorded_at"),
            data.get("plan_hash"),
            data.get("audit"),
        )


@dataclass(frozen=True)
class CursorRead:
    """One pass's cursor from the file (``None`` when there is none) and, when the file could
    not be trusted, why. A warning means the caller runs in full."""

    cursor: PassCursor | None = None
    warning: str | None = None


def _digest(passes: Mapping[str, Any]) -> str:
    body = json.dumps({"schema": CURSOR_SCHEMA, "passes": passes}, sort_keys=True)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def read_cursors(path: Path | str) -> tuple[dict[str, PassCursor], str | None]:
    """Every cursor in the file at ``path`` and a warning when the file cannot be trusted.

    A missing file is not an error (no cursors, no warning). Any other problem returns no cursors
    and one line saying why, so the caller falls back to a full run."""
    source = Path(path)
    try:
        text = source.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, None
    except (OSError, UnicodeDecodeError) as exc:
        return {}, f"cursor file {source} is unreadable ({exc})"
    try:
        data = json.loads(text)
        if not isinstance(data, Mapping) or data.get("schema") != CURSOR_SCHEMA:
            raise ValueError(f"schema is not {CURSOR_SCHEMA!r}")
        passes = data.get("passes")
        if not isinstance(passes, Mapping):
            raise ValueError("no passes object")
        if data.get("digest") != _digest(passes):
            raise ValueError("digest mismatch (edited or truncated)")
        cursors = {name: PassCursor.from_json(item) for name, item in passes.items()}
        for name, cursor in cursors.items():
            if cursor.pass_name != name:
                raise ValueError(f"entry {name!r} names pass {cursor.pass_name!r}")
    except (ValueError, TypeError) as exc:
        return {}, f"cursor file {source} is corrupt ({exc})"
    return cursors, None


def read_cursor(path: Path | str, pass_name: str) -> CursorRead:
    """The cursor of ``pass_name`` (:func:`read_cursors`)."""
    cursors, warning = read_cursors(path)
    return CursorRead(cursors.get(pass_name), warning)


def write_cursor(path: Path | str, cursor: PassCursor) -> None:
    """Record ``cursor`` for its pass, keeping the other passes' cursors.

    An untrustworthy existing file is replaced (its other cursors were already ignored). The file
    is written to a sibling and renamed, so a reader never sees half of it."""
    target = Path(path)
    cursors, _warning = read_cursors(target)
    cursors[cursor.pass_name] = cursor
    passes = {name: item.to_json() for name, item in sorted(cursors.items())}
    document = {"schema": CURSOR_SCHEMA, "passes": passes, "digest": _digest(passes)}
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=target.parent,
        prefix=f".{target.name}.",
        delete=False,
    ) as handle:
        json.dump(document, handle, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    temporary.replace(target)


def cursor_to_record(plan_report: Mapping[str, Any], pass_name: str) -> PassCursor | None:
    """The cursor an applied plan earns: the ``cursor_next`` its report recorded (``None`` for a
    plan that was not an incremental run, or a scoped run that must not move the mark)."""
    info = plan_report.get(INCREMENTAL_KEY)
    if not isinstance(info, Mapping):
        return None
    nxt = info.get("cursor_next")
    if not isinstance(nxt, Mapping):
        return None
    return PassCursor.from_json({**nxt, "pass": pass_name})


def incremental_info(
    mode: str,
    *,
    cursor_in: PassCursor | None,
    cursor_next: PassCursor | None,
    warnings: Sequence[str] = (),
    scope: Sequence[str] | None = None,
) -> dict[str, Any]:
    """The ``incremental`` section of a plan report (JSON-serializable, additive)."""
    info: dict[str, Any] = {
        "mode": mode,
        "cursor_in": None if cursor_in is None else cursor_in.to_json(),
        "cursor_next": None if cursor_next is None else cursor_next.to_json(),
        "warnings": list(warnings),
    }
    if scope is not None:
        info["scope"] = sorted(scope)
    return info


@runtime_checkable
class IncrementalReaders(Protocol):
    """The narrow reads an incremental timeline needs on top of ``TimelineReaders``. Each costs
    a fixed number of store or git calls; none loads the whole corpus."""

    def head(self) -> str | None:
        """The integration branch's tip sha (one ``git rev-parse``), or ``None``."""
        ...

    def reaches(self, sha: str) -> bool:
        """Whether ``sha`` is a commit in the integration branch's history."""
        ...

    def history_since(self, at: str) -> HistoryRead:
        """The ``events`` rows created at or after about ``at`` (a superset; callers filter)."""
        ...

    def history_of(self, ids: Sequence[str]) -> HistoryRead:
        """Every ``events`` row of the beads ``ids``."""
        ...

    def lands_since(self, sha: str) -> MergeScan:
        """The first-parent lands after ``sha`` (``git log --first-parent sha..ref``)."""
        ...


__all__ = [
    "CURSOR_FILE",
    "CURSOR_SCHEMA",
    "INCREMENTAL_KEY",
    "MODE_FULL",
    "MODE_INCREMENTAL",
    "MODE_SCOPED",
    "CursorRead",
    "IncrementalReaders",
    "PassCursor",
    "cursor_to_record",
    "incremental_info",
    "read_cursor",
    "read_cursors",
    "write_cursor",
]
