"""The neutral ``bd`` history reader: the ``events`` table in one bounded ``SELECT``.

BeadHive-free (``docs/design/backfill-platform-adr.md`` sections 2 and 5). ``bd history <id>``
costs ~20 s per bead and omits labels, dependencies and metadata, so a backfill never loops it;
the ``events`` table is the primary lifecycle source and one call reads all of it:

``bd -C <root> sql --json 'SELECT issue_id, event_type, actor, old_value, new_value, comment,
created_at, id FROM events'`` (SELECT only; the real call is ~13 s for ~36k rows, so the timeout
is configurable and generous).

Readers return plain typed records (:class:`HistoryEvent`), *not* BeadLog events: turning them into
events is the timeline pass's job. The table's ``id`` is a UUID, not a sequence; ``created_at`` has
second resolution, so ties order by ``(created_at, id)`` — deterministic, not causal.

**Row shapes** (measured on the production store, 36,979 rows). ``old_value`` / ``new_value`` are
mixed: for ``claimed`` / ``updated`` / ``status_changed`` / ``reopened`` they are JSON (``old`` the
whole prior issue row, ``new`` just the changed fields); ``closed`` and ``reopened`` carry the
reason as plain text in ``new_value``; ``status_changed`` by the defer-wake daemon is plain text
(``deferred`` -> ``open``); ``label_added`` / ``label_removed`` put ``Added label: X`` /
``Removed label: X`` in ``comment`` (or the bare label in ``new_value`` / ``old_value``);
``dependency_added`` is ``Added dependency: <issue> <type> <target>`` and ``dependency_removed`` is
``Removed dependency on <target>`` (no type), both in ``new_value``; ``commented`` has only
``comment``; ``lease_reclaimed`` has the reclaimed-from assignee in ``old_value``; ``created`` has
no payload.

**Defensive.** A malformed row is skipped and counted by reason in :attr:`HistoryRead.skipped`; an
event type this reader does not know is counted in :attr:`HistoryRead.unknown_types`. Neither is
fatal. Only a failed or unparseable *query* raises :class:`BdHistoryError`.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .bd import BdReadError, bd_timestamp
from .runner import DEFAULT_TIMEOUT_SECONDS, Runner, subprocess_runner

COLUMNS = (
    "issue_id",
    "event_type",
    "actor",
    "old_value",
    "new_value",
    "comment",
    "created_at",
    "id",
)
HISTORY_SQL = f"SELECT {', '.join(COLUMNS)} FROM events"

CREATED = "created"
UPDATED = "updated"
STATUS_CHANGED = "status_changed"
CLAIMED = "claimed"
CLOSED = "closed"
REOPENED = "reopened"
LABEL_ADDED = "label_added"
LABEL_REMOVED = "label_removed"
DEPENDENCY_ADDED = "dependency_added"
DEPENDENCY_REMOVED = "dependency_removed"
COMMENTED = "commented"
LEASE_RECLAIMED = "lease_reclaimed"
EVENT_TYPES = frozenset(
    {
        CREATED,
        UPDATED,
        STATUS_CHANGED,
        CLAIMED,
        CLOSED,
        REOPENED,
        LABEL_ADDED,
        LABEL_REMOVED,
        DEPENDENCY_ADDED,
        DEPENDENCY_REMOVED,
        COMMENTED,
        LEASE_RECLAIMED,
    }
)

_ADDED_LABEL = re.compile(r"^\s*Added label:\s*(?P<label>\S.*?)\s*$", re.DOTALL)
_REMOVED_LABEL = re.compile(r"^\s*Removed label:\s*(?P<label>\S.*?)\s*$", re.DOTALL)
_ADDED_DEP = re.compile(
    r"^\s*Added dependency:\s*(?P<issue>\S+)\s+(?P<type>\S+)\s+(?P<target>\S+)\s*$"
)
_REMOVED_DEP = re.compile(r"^\s*Removed dependency on\s+(?P<target>\S+)\s*$")


class BdHistoryError(BdReadError):
    """The events query failed, timed out, or printed something that is not a JSON row array."""


@dataclass(frozen=True)
class HistoryEvent:
    """One parsed ``events`` row. Only the members meaningful for ``kind`` are set.

    ``at`` is the row's ``created_at`` as a canonical BeadLog timestamp (second resolution);
    ``row_id`` is the table's UUID, the deterministic tiebreak after ``at``.
    """

    row_id: str
    bead: str
    kind: str
    at: str
    actor: str | None = None
    old_status: str | None = None  # status_changed / claimed / closed / reopened
    new_status: str | None = None
    assignee: str | None = None  # claimed: the claimant; lease_reclaimed: who lost the lease
    reason: str | None = None  # closed / reopened reason; commented text
    label: str | None = None  # label_added / label_removed
    dep_type: str | None = None  # dependency_added (the removal text omits it)
    dep_target: str | None = None  # dependency_added / dependency_removed
    changes: Mapping[str, Any] = field(default_factory=dict, hash=False)  # the new_value JSON

    @property
    def sort_key(self) -> tuple[str, str]:
        return (self.at, self.row_id)


@dataclass(frozen=True)
class HistoryRead:
    """A whole-hive read: events in ``(at, row_id)`` order plus the counted warnings."""

    events: tuple[HistoryEvent, ...] = ()
    skipped: Counter[str] = field(default_factory=Counter, hash=False)
    unknown_types: Counter[str] = field(default_factory=Counter, hash=False)
    rows: int = 0

    @property
    def warnings(self) -> tuple[str, ...]:
        """One human line per counted problem (skipped reasons, then unknown event types)."""
        out = [f"skipped {n} row(s): {reason}" for reason, n in sorted(self.skipped.items())]
        out += [
            f"unknown event type {kind!r}: {n} row(s)"
            for kind, n in sorted(self.unknown_types.items())
        ]
        return tuple(out)


class _Skip(Exception):
    """Internal: this row is malformed for ``reason``."""


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _json_object(value: Any, reason: str) -> dict[str, Any] | None:
    """``value`` as a JSON object when it is JSON-looking text; ``None`` when empty or plain text.

    A value that starts with ``{`` but does not parse to an object is malformed (``reason``)."""
    if not isinstance(value, str) or not value.lstrip().startswith("{"):
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise _Skip(reason) from exc
    if not isinstance(parsed, dict):
        raise _Skip(reason)
    return parsed


def _status(payload: Mapping[str, Any] | None) -> str | None:
    return _text(payload.get("status")) if payload else None


def _label(pattern: re.Pattern[str], row: Mapping[str, Any], fallback: Any) -> str:
    comment = row.get("comment")
    match = pattern.match(comment) if isinstance(comment, str) else None
    label = match.group("label") if match else _text(fallback)
    if not label:
        raise _Skip("label_text")
    return label


def _parse(row: Mapping[str, Any], kind: str) -> dict[str, Any]:
    """The kind-specific members of one row, or :class:`_Skip`."""
    old, new = row.get("old_value"), row.get("new_value")
    if kind == CREATED:
        return {}
    if kind == COMMENTED:
        text = _text(row.get("comment")) or _text(new)
        if text is None:
            raise _Skip("comment_text")
        return {"reason": text}
    if kind == LABEL_ADDED:
        return {"label": _label(_ADDED_LABEL, row, new)}
    if kind == LABEL_REMOVED:
        return {"label": _label(_REMOVED_LABEL, row, old)}
    if kind in (DEPENDENCY_ADDED, DEPENDENCY_REMOVED):
        pattern = _ADDED_DEP if kind == DEPENDENCY_ADDED else _REMOVED_DEP
        for source in (new, row.get("comment")):
            match = pattern.match(source) if isinstance(source, str) else None
            if match:
                groups = match.groupdict()
                return {"dep_type": groups.get("type"), "dep_target": groups["target"]}
        raise _Skip("dependency_text")
    if kind == LEASE_RECLAIMED:
        return {"assignee": _text(old)}
    # The JSON-bearing kinds: updated, status_changed, claimed, closed, reopened.
    old_json = _json_object(old, "bad_json_old_value")
    new_json = _json_object(new, "bad_json_new_value")
    changes = new_json or {}
    out: dict[str, Any] = {"changes": changes}
    if kind == UPDATED:
        return out
    if kind == CLAIMED:
        assignee = _text(changes.get("assignee"))
        if assignee is None:
            raise _Skip("claim_payload")
        return {
            **out,
            "assignee": assignee,
            "old_status": _status(old_json),
            "new_status": _status(changes),
        }
    if kind == STATUS_CHANGED:
        # The defer-wake daemon writes plain text: old_value "deferred", new_value "open".
        old_status = _status(old_json) or (None if old_json else _text(old))
        new_status = _status(changes) or (None if new_json else _text(new))
        return {**out, "old_status": old_status, "new_status": new_status}
    # closed / reopened: the reason is plain text in new_value, or notes inside a JSON payload.
    reason = None if new_json is not None else _text(new)
    if reason is None and new_json is not None:
        reason = _text(changes.get("close_reason")) or _text(changes.get("notes"))
    default = "closed" if kind == CLOSED else "open"
    return {
        **out,
        "old_status": _status(old_json),
        "new_status": _status(changes) or default,
        "reason": reason,
    }


def parse_event_row(row: Any) -> HistoryEvent | str:
    """One decoded ``events`` row as a :class:`HistoryEvent`, or the *reason* it is malformed.

    An unknown event type returns ``"unknown_type:<type>"``."""
    if not isinstance(row, Mapping):
        return "not_an_object"
    row_id, bead = _text(row.get("id")), _text(row.get("issue_id"))
    if row_id is None:
        return "missing_row_id"
    if bead is None:
        return "missing_issue_id"
    kind = _text(row.get("event_type"))
    if kind is None:
        return "missing_event_type"
    at = bd_timestamp(row.get("created_at"))
    if at is None:
        return "bad_timestamp"
    if kind not in EVENT_TYPES:
        return f"unknown_type:{kind}"
    try:
        members = _parse(row, kind)
    except _Skip as skip:
        return str(skip)
    return HistoryEvent(row_id, bead, kind, at, actor=_text(row.get("actor")), **members)


def parse_history(rows: Any) -> HistoryRead:
    """Parse decoded ``bd sql --json`` rows: valid ones sorted by ``(at, row_id)``, rest counted."""
    if not isinstance(rows, list):
        raise BdHistoryError("bd sql --json did not return a JSON array of rows")
    events: list[HistoryEvent] = []
    skipped: Counter[str] = Counter()
    unknown: Counter[str] = Counter()
    for row in rows:
        result = parse_event_row(row)
        if isinstance(result, HistoryEvent):
            events.append(result)
        elif result.startswith("unknown_type:"):
            unknown[result.split(":", 1)[1]] += 1
        else:
            skipped[result] += 1
    events.sort(key=lambda event: event.sort_key)
    return HistoryRead(tuple(events), skipped, unknown, len(rows))


def decode_rows(text: str) -> Any:
    """The JSON array in ``bd sql --json`` output, tolerating banner lines before it."""
    lines = text.splitlines()
    for number, line in enumerate(lines):
        if line.startswith("["):
            try:
                return json.loads("\n".join(lines[number:]))
            except json.JSONDecodeError as exc:
                raise BdHistoryError(f"bd sql output is not JSON: {exc}") from exc
    raise BdHistoryError("bd sql output holds no JSON array")


def history_argv(root: Path | str, *, binary: str = "bd") -> list[str]:
    """The one bulk read: ``bd -C <root> sql --json <HISTORY_SQL>`` (a SELECT, never a write)."""
    return [binary, "-C", str(root), "sql", "--json", HISTORY_SQL]


def read_history(
    root: Path | str,
    *,
    binary: str = "bd",
    timeout: float | None = DEFAULT_TIMEOUT_SECONDS,
    runner: Runner | None = None,
) -> HistoryRead:
    """Read the whole hive's ``events`` table with exactly one store call and parse it."""
    run = runner or subprocess_runner(timeout, BdHistoryError, "bd sql")
    return parse_history(decode_rows(run(history_argv(root, binary=binary))))


__all__ = [
    "CLAIMED",
    "CLOSED",
    "COLUMNS",
    "COMMENTED",
    "CREATED",
    "DEPENDENCY_ADDED",
    "DEPENDENCY_REMOVED",
    "EVENT_TYPES",
    "HISTORY_SQL",
    "LABEL_ADDED",
    "LABEL_REMOVED",
    "LEASE_RECLAIMED",
    "REOPENED",
    "STATUS_CHANGED",
    "UPDATED",
    "BdHistoryError",
    "HistoryEvent",
    "HistoryRead",
    "decode_rows",
    "history_argv",
    "parse_event_row",
    "parse_history",
    "read_history",
]
