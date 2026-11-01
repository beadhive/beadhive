"""Opt-in, bounded ``dolt_history_issues`` gap fill for beads with no ``created`` event.

BeadHive-free (``docs/design/backfill-platform-adr.md`` sections 5 and 6). The ``events`` table
(:mod:`bd_history`) cannot say anything about a bead created before the table started or imported
without a ``created`` row; :mod:`bd_timeline` gives such a bead only an inferred, low-confidence
create. Dolt versions every ``issues`` row at every store commit, so the *snapshots* of those
beads can be recovered — at a price this module keeps explicit and bounded.

**Measured shape of the store** (``docs/BACKFILL.md`` "Gap fill" holds the cost table). A
``dolt_history_issues`` read walks every store commit (~10 s on the production store, flat in
the number of beads) and yields one row per commit per bead (~37 k rows for a bead that never
changed), so a naive ``SELECT *`` is both slow and enormous. Two further facts shape the reader:

- ``bd sql`` drops a query whose first result byte takes more than ~10 s, so *blocking* shapes
  (``GROUP BY``, window functions, ``ORDER BY``) fail beyond a few beads and get worse as the
  store grows. The only reliable shape is a streaming scan with a cheap per-row projection.
- ``AS OF '<commit>'`` is a point lookup (~1.6 s for dozens of rows) and needs no history walk.

So each batch of ``batch_size`` beads is two read-only steps, every one a ``SELECT`` through the
injected :data:`~beadlog_sources.runner.Runner` with the configured timeout:

1. **Scan**: ``SELECT id, commit_hash, <formatted commit_date>, <digest of the modeled columns>
   FROM dolt_history_issues WHERE id IN (...)`` as CSV (the digest is computed by the server, so
   a description is never shipped 37 k times), ordered *here* by ``(commit_date, commit_hash)``
   (commit dates are not monotone along the DAG), collapsed to the commits where the digest
   changes. A bead with a single digest state needs nothing more. (``content_hash`` is not the
   digest: bd leaves it unchanged by a status change.)
2. **Fetch**: the modeled columns of only those change commits, in one ``UNION ALL`` of
   ``issues AS OF '<commit>'`` point lookups per chunk, collapsed again on the *projected*
   columns BeadLog models.

**Events.** For a bead with distinct states ``S1..Sk`` (k >= 2) at commits ``T1..Tk``: a ``set``
per modeled field and a ``phase`` for the status wherever the value changes between consecutive
states, plus the value of S1 for every field that changes later (the baseline the changes start
from). ``occurred_at`` is **not known better than** the commit date — a snapshot diff says the
change happened in the commit's write, nothing finer, and S1 only says the value existed *by*
T1 — so ``occurred_at == recorded_at == commit date``, ``provenance`` is ``derived`` and the
confidence is reduced (:data:`DIFF_CONFIDENCE`, :data:`INITIAL_CONFIDENCE`). Evidence names the
dolt commit. The source is ``dolt`` / ``dolt:history/<commit>#<bead>``, so re-extracting the
same history gives the same event ids. :func:`gap_events` runs :func:`consistent_history`, so the
events can never change the fold of the current state.

**Resumable.** After every completed batch the collapsed states are saved through a
:class:`GapfillCursor` (:class:`JsonCursorFile` is the atomic file one), keyed by a fingerprint of
the bead list and batch size; an interrupted run resumes at the next batch and the final state set
is identical to an uninterrupted run's. Not covered (measured, deliberately): labels,
dependencies and metadata — ``dolt_history_labels`` returns ~2.5x the rows of the issues table
for the same walk — so a gap-filled bead still has no label/edge history before the events table.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import tempfile
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from beadlog import BeadEvent, BeadRef, EventKind, Evidence, instant, mint_event

from . import bd_history as _history
from .bd import FIELD_COLUMNS, TIMESTAMP_FIELDS, admissible, bd_timestamp
from .bd_history import BdHistoryError
from .bd_timeline import consistent_history
from .runner import Runner, subprocess_runner

SOURCE_SYSTEM = "dolt"
STRATEGY_VERSION = "0.1.0"
CURSOR_SCHEMA = "bdlg.gapfill-cursor/1"
GAPFILL_ANNOTATION = "gapfill"
# Confidence of a change between two consecutive observed snapshots (the write happened in the
# later commit) and of the first observed snapshot (the value existed *by* that commit).
DIFF_CONFIDENCE = 0.7
INITIAL_CONFIDENCE = 0.3
DEFAULT_BATCH_SIZE = 25
DEFAULT_TIMEOUT_SECONDS = 180.0
# Point lookups per fetch query (each part is ~ms; the bound keeps one statement small).
FETCH_CHUNK = 40

SCAN_COLUMNS = ("id", "commit_hash", "d", "h")
STATE_COLUMNS = (
    *FIELD_COLUMNS.values(),
    "issue_type",
    "status",
    "close_reason",
)
# One short digest per row over exactly the modeled columns (NULL and '' are the same state, as
# they are for BeadLog). `content_hash` is NOT usable: bd leaves it unchanged by a status change
# (measured: bh-9wpd.1 went open -> closed with one content_hash on every commit).
_DIGEST = (
    "LEFT(MD5(CONCAT_WS(CHAR(31), "
    + ", ".join(f"COALESCE({column}, '')" for column in STATE_COLUMNS)
    + ")), 12)"
)
_SCAN_SQL = (
    "SELECT id, commit_hash, DATE_FORMAT(commit_date, '%Y-%m-%dT%H:%i:%s.%fZ') AS d, "
    + _DIGEST
    + " AS h FROM dolt_history_issues WHERE id IN ({ids})"
)
_FETCH_PART = (
    "SELECT '{commit}' AS commit_hash, id, {columns} "
    "FROM issues AS OF '{commit}' WHERE id = '{bead}'"
)
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_SAFE_COMMIT = re.compile(r"^[A-Za-z0-9]{8,64}$")
_PHASE = re.compile(r"^[a-z][a-z0-9_]*$")
_INTEGER_FIELDS = frozenset({"priority", "estimated_minutes"})


class BdGapfillError(BdHistoryError):
    """A gap-fill query failed, timed out, or printed something unreadable."""


# -- the record types ------------------------------------------------------------------------


@dataclass(frozen=True)
class GapState:
    """One distinct snapshot of a bead: the commit that first showed it and the modeled values.

    ``at`` is the commit date as a canonical BeadLog timestamp; ``fields`` holds the BeadLog field
    names (``title`` ... ``spec_id``) plus ``type``; ``phase`` is the status and ``reason`` the
    close reason (only meaningful while closed)."""

    commit: str
    at: str
    fields: Mapping[str, Any]
    phase: str | None
    reason: str | None = None

    def key(self) -> tuple[Any, ...]:
        """What makes two states the same: every modeled value, never the commit or time."""
        return (
            tuple(sorted(self.fields.items())),
            self.phase,
            self.reason if self.phase == "closed" else None,
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "commit": self.commit,
            "at": self.at,
            "fields": dict(self.fields),
            "phase": self.phase,
            "reason": self.reason,
        }

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> GapState:
        return cls(
            str(raw["commit"]),
            str(raw["at"]),
            dict(raw.get("fields") or {}),
            raw.get("phase"),
            raw.get("reason"),
        )


@dataclass(frozen=True)
class GapfillRead:
    """What the scan found: distinct states of the beads that changed (>= 2 states), and the
    counted account of how (``counts``: batches, queries, rows scanned, hash states, ...)."""

    states: Mapping[str, tuple[GapState, ...]] = field(default_factory=dict, hash=False)
    counts: Mapping[str, int] = field(default_factory=dict, hash=False)
    resumed_from: int = 0  # beads already done when this run began (0: a fresh run)


# -- the resumable cursor --------------------------------------------------------------------


class GapfillCursor(Protocol):
    """Where a run keeps its progress: load what a previous run saved, save after each batch,
    clear when the run is complete."""

    def load(self) -> Mapping[str, Any] | None: ...

    def save(self, data: Mapping[str, Any]) -> None: ...

    def clear(self) -> None: ...


class JsonCursorFile:
    """A :class:`GapfillCursor` in one JSON file, replaced atomically (temp file in the same
    directory, ``fsync``, ``os.replace``) so an interrupt never leaves a torn cursor."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def load(self) -> Mapping[str, Any] | None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            return None  # unreadable: start over rather than trust it
        return data if isinstance(data, dict) else None

    def save(self, data: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle, name = tempfile.mkstemp(prefix=self.path.name + ".", dir=self.path.parent)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as out:
                json.dump(data, out, sort_keys=True)
                out.flush()
                os.fsync(out.fileno())
            os.replace(name, self.path)
        except BaseException:
            Path(name).unlink(missing_ok=True)
            raise

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


def fingerprint(ids: Sequence[str], batch_size: int) -> str:
    """Identity of a run: the bead list and batch size (a cursor from another run is stale)."""
    material = json.dumps({"ids": list(ids), "batch_size": batch_size, "v": CURSOR_SCHEMA})
    return "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()


# -- the queries -----------------------------------------------------------------------------


def _quote(value: str) -> str:
    return "'" + value + "'"


def scan_sql(ids: Sequence[str]) -> str:
    """The streaming history scan of ``ids`` (a ``SELECT``; ids must be safe identifiers)."""
    return _SCAN_SQL.format(ids=", ".join(_quote(item) for item in ids))


def fetch_sql(points: Sequence[tuple[str, str]]) -> str:
    """The point lookups of ``(bead, commit)`` pairs as one ``UNION ALL`` of ``AS OF`` selects."""
    columns = ", ".join(STATE_COLUMNS)
    return " UNION ALL ".join(
        _FETCH_PART.format(commit=commit, bead=bead, columns=columns) for bead, commit in points
    )


def _argv(root: Path | str, binary: str, mode: str, sql: str) -> list[str]:
    return [binary, "-C", str(root), "sql", mode, sql]


# -- parsing ---------------------------------------------------------------------------------


def parse_scan(text: str) -> dict[str, list[tuple[str, str, str]]]:
    """The scan's CSV as ``bead -> [(raw date, commit, hash prefix), ...]`` (unsorted).

    Lines before the header (a banner) are skipped; a short or malformed row raises
    :class:`BdGapfillError`, since a half-read history would silently lose states."""
    lines = text.splitlines()
    header = ",".join(SCAN_COLUMNS)
    start = next((n for n, line in enumerate(lines) if line.strip() == header), None)
    if start is None:
        if not text.strip() or all(not line.strip() for line in lines):
            return {}
        raise BdGapfillError("bd sql output holds no gap-fill scan header")
    out: dict[str, list[tuple[str, str, str]]] = {}
    for row in csv.reader(lines[start + 1 :]):
        if not row:
            continue
        if len(row) != len(SCAN_COLUMNS):
            raise BdGapfillError(f"gap-fill scan row has {len(row)} columns, not 4")
        bead, commit, raw, digest = row
        out.setdefault(bead, []).append((raw, commit, digest))
    return out


def _value(name: str, value: Any) -> Any:
    if value is None or value == "":
        return None
    if name in _INTEGER_FIELDS:
        if isinstance(value, bool):
            return admissible(value)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and re.fullmatch(r"-?[0-9]+", value.strip()):
            return int(value.strip())
        return admissible(value)
    if name in TIMESTAMP_FIELDS:
        return bd_timestamp(value) or admissible(value)
    return admissible(value)


def _state(row: Mapping[str, Any], commit: str, at: str) -> GapState:
    fields: dict[str, Any] = {}
    for name, column in FIELD_COLUMNS.items():
        fields[name] = _value(name, row.get(column))
    kind = row.get("issue_type")
    fields["type"] = kind if isinstance(kind, str) and kind else None
    status = row.get("status")
    reason = row.get("close_reason")
    return GapState(
        commit,
        at,
        fields,
        status if isinstance(status, str) and status else None,
        reason if isinstance(reason, str) and reason else None,
    )


def hash_states(rows: Iterable[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    """The change points of one bead's scan rows: ``(raw date, commit, hash)`` ordered by
    ``(commit date, commit hash)`` — never DAG order — keeping the first row of every run of
    equal content hashes (a state that returns later is a new change point)."""
    ordered = sorted(rows, key=lambda row: (row[0], row[1]))
    points: list[tuple[str, str, str]] = []
    previous: str | None = None
    for raw, commit, digest in ordered:
        if digest != previous:
            points.append((raw, commit, digest))
        previous = digest
    return points


def collapse(states: Iterable[GapState]) -> list[GapState]:
    """Drop every state equal (on :meth:`GapState.key`) to the one before it."""
    out: list[GapState] = []
    for state in states:
        if not out or out[-1].key() != state.key():
            out.append(state)
    return out


def _canonical(raw: str) -> str | None:
    return bd_timestamp(raw)


class GapfillReader:
    """Reads the snapshot history of a list of beads, batch by batch, resumably.

    ``runner`` is the injected command seam (default: a subprocess with ``timeout`` seconds per
    query); a fake in tests counts the calls. Nothing it runs writes.
    """

    def __init__(
        self,
        root: Path | str,
        *,
        binary: str = "bd",
        batch_size: int = DEFAULT_BATCH_SIZE,
        timeout: float | None = DEFAULT_TIMEOUT_SECONDS,
        runner: Runner | None = None,
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        self.root = root
        self.binary = binary
        self.batch_size = batch_size
        self._run = runner or subprocess_runner(timeout, BdGapfillError, "bd sql")

    def _fetch(self, points: Sequence[tuple[str, str]], counts: Counter[str]) -> dict[Any, Any]:
        rows: dict[tuple[str, str], Mapping[str, Any]] = {}
        for start in range(0, len(points), FETCH_CHUNK):
            chunk = points[start : start + FETCH_CHUNK]
            counts["queries"] += 1
            decoded = _history.decode_rows(
                self._run(_argv(self.root, self.binary, "--json", fetch_sql(chunk)))
            )
            if not isinstance(decoded, list):
                raise BdGapfillError("bd sql --json did not return a JSON array of rows")
            for row in decoded:
                if isinstance(row, Mapping) and row.get("id") and row.get("commit_hash"):
                    rows[(str(row["id"]), str(row["commit_hash"]))] = row
        return rows

    def batch(self, ids: Sequence[str], counts: Counter[str]) -> dict[str, tuple[GapState, ...]]:
        """The distinct states of the beads of ``ids`` that changed (>= 2 states), by bead."""
        counts["queries"] += 1
        counts["batches"] += 1
        scanned = parse_scan(self._run(_argv(self.root, self.binary, "--csv", scan_sql(ids))))
        points: dict[str, list[tuple[str, str]]] = {}  # bead -> [(commit, raw date)]
        for bead in ids:
            rows = scanned.get(bead, [])
            counts["rows_scanned"] += len(rows)
            if not rows:
                counts["beads_without_history"] += 1
                continue
            changes = hash_states(rows)
            if any(not _SAFE_COMMIT.fullmatch(commit) for _, commit, _ in changes):
                raise BdGapfillError(f"bd sql returned an unexpected commit hash for {bead}")
            counts["hash_states"] += len(changes)
            if len(changes) < 2:
                counts["beads_single_state"] += 1
                continue
            points[bead] = [(commit, raw) for raw, commit, _ in changes]
        wanted = [(bead, commit) for bead, items in points.items() for commit, _ in items]
        fetched = self._fetch(wanted, counts) if wanted else {}
        out: dict[str, tuple[GapState, ...]] = {}
        for bead, items in points.items():
            states: list[GapState] = []
            for commit, raw in items:
                row = fetched.get((bead, commit))
                at = _canonical(raw)
                if row is None or at is None:
                    counts["states_unreadable"] += 1
                    continue
                states.append(_state(row, commit, at))
            distinct = collapse(states)
            counts["projected_states"] += len(distinct)
            if len(distinct) < 2:
                counts["beads_single_projected_state"] += 1
                continue
            counts["beads_changed"] += 1
            out[bead] = tuple(distinct)
        return out

    def read(self, ids: Sequence[str], cursor: GapfillCursor | None = None) -> GapfillRead:
        """Every batch of ``ids`` in order, saving the cursor after each; resumes a saved run
        with the same fingerprint. The cursor is cleared only by the caller, after the events
        derived from the result are safely written."""
        counts: Counter[str] = Counter()
        given = list(ids)
        ids = [item for item in given if _SAFE_ID.fullmatch(item)]
        if len(ids) != len(given):
            counts["beads_unsafe_id"] = len(given) - len(ids)
        run_id = fingerprint(ids, self.batch_size)
        states: dict[str, tuple[GapState, ...]] = {}
        done = 0
        saved = cursor.load() if cursor is not None else None
        if (
            saved
            and saved.get("schema") == CURSOR_SCHEMA
            and saved.get("fingerprint") == run_id
            and isinstance(saved.get("done"), int)
            and 0 <= saved["done"] <= len(ids)
        ):
            done = saved["done"]
            counts.update({str(k): int(v) for k, v in (saved.get("counts") or {}).items()})
            for bead, items in (saved.get("states") or {}).items():
                states[str(bead)] = tuple(GapState.from_json(item) for item in items)
        resumed = done
        while done < len(ids):
            chunk = ids[done : done + self.batch_size]
            states.update(self.batch(chunk, counts))
            done += len(chunk)
            if cursor is not None:
                cursor.save(
                    {
                        "schema": CURSOR_SCHEMA,
                        "fingerprint": run_id,
                        "done": done,
                        "total": len(ids),
                        "counts": dict(sorted(counts.items())),
                        "states": {
                            bead: [s.to_json() for s in items]
                            for bead, items in sorted(states.items())
                        },
                    }
                )
        counts["beads_queried"] = len(ids)
        return GapfillRead(dict(sorted(states.items())), dict(sorted(counts.items())), resumed)


# -- states -> events ------------------------------------------------------------------------


def lacking_created(history_events: Iterable[BeadEvent]) -> list[str]:
    """The ids of the beads with no ``created`` row in the events table: exactly those the
    history conversion gave an inferred create (``bd:inferred/<id>#create``)."""
    return sorted(
        {
            event.bead.id
            for event in history_events
            if event.kind is EventKind.CREATE and event.source.ref.startswith("bd:inferred/")
        }
    )


def source_ref(commit: str, bead: str) -> str:
    """The stable source reference of a snapshot: ``dolt:history/<commit>#<bead>``."""
    return f"dolt:history/{commit}#{bead}"


def _phase_ok(phase: str | None) -> bool:
    return (
        phase is not None
        and _PHASE.fullmatch(phase) is not None
        and phase
        not in (
            "retired",
            "deleted",
        )
    )


def bead_gap_events(
    bead: BeadRef, states: Sequence[GapState], *, extracted_at: str
) -> list[BeadEvent]:
    """The events of one bead's distinct states (module doc): sets and phases at the commits
    where a value changes, plus the starting value of every field that changes later."""
    out: list[BeadEvent] = []

    def mint(kind: EventKind, data: Mapping[str, Any], state: GapState, previous: GapState | None):
        initial = previous is None
        note = (
            "the first snapshot that holds the bead; the value existed by this commit, when it "
            "was set is not recorded"
            if initial
            else f"differs from the snapshot of dolt commit {previous.commit}; the change was "
            "written by this commit (commit date, not a finer valid time)"
        )
        out.append(
            mint_event(
                str(kind),
                bead,
                data,
                occurred_at=state.at,
                recorded_at=state.at,
                extracted_at=extracted_at,
                source=(SOURCE_SYSTEM, source_ref(state.commit, bead.id)),
                provenance="derived",
                confidence=INITIAL_CONFIDENCE if initial else DIFF_CONFIDENCE,
                evidence=(Evidence("dolt-commit", state.commit, note),),
            )
        )

    ordered = sorted(states, key=lambda state: (instant(state.at), state.commit))
    names = sorted({name for state in ordered for name in state.fields})
    changing = {name for name in names if len({_hashable(s.fields.get(name)) for s in ordered}) > 1}
    phases = {state.phase for state in ordered}
    for index, state in enumerate(ordered):
        previous = ordered[index - 1] if index else None
        for name in names:
            value = state.fields.get(name)
            if previous is None:
                if name not in changing:
                    continue
            elif _hashable(previous.fields.get(name)) == _hashable(value):
                continue
            mint(EventKind.SET, {"field": name, "value": value}, state, previous)
        if _phase_ok(state.phase) and (
            (previous is None and len(phases) > 1)
            or (previous is not None and previous.phase != state.phase)
        ):
            data: dict[str, Any] = {"phase": state.phase}
            if state.phase == "closed" and state.reason:
                data["reason"] = state.reason
            mint(EventKind.PHASE, data, state, previous)
    return out


def _hashable(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


@dataclass(frozen=True)
class GapfillEvents:
    """The gap-fill events and the counted account (``counts`` from the read plus
    ``events``/``suppressed``; ``resumed_from`` > 0 when the run continued a saved cursor)."""

    events: tuple[BeadEvent, ...] = ()
    counts: Mapping[str, int] = field(default_factory=dict, hash=False)
    suppressed: Mapping[str, int] = field(default_factory=dict, hash=False)
    resumed_from: int = 0
    beads_queried: int = 0


def gap_events(
    read: GapfillRead,
    baseline: Sequence[BeadEvent],
    *,
    group: str,
    extracted_at: str,
    queried: Iterable[str] = (),
) -> GapfillEvents:
    """Turn a :class:`GapfillRead` into events that leave the fold of ``baseline`` unchanged.

    ``baseline`` is everything already in the archive for the beads (current state and the
    events-table history); events of beads it does not hold are dropped, and so is any event that
    would change a bead's folded current state (counted ``inconsistent:*`` in ``suppressed``).
    Only the beads of ``queried`` (those lacking a created event) may receive events.
    """
    allowed = set(queried) or set(read.states)
    raw: list[BeadEvent] = []
    for bead_id in sorted(read.states):
        if bead_id in allowed:
            raw.extend(
                bead_gap_events(
                    BeadRef(group, bead_id), read.states[bead_id], extracted_at=extracted_at
                )
            )
    kept, dropped = consistent_history(tuple(baseline), raw)
    counts = dict(sorted({**read.counts, "events": len(kept)}.items()))
    return GapfillEvents(
        tuple(kept),
        counts,
        dict(sorted(dropped.items())),
        read.resumed_from,
        int(read.counts.get("beads_queried", 0)),
    )


def gapfill_annotation(result: GapfillEvents, *, batch_size: int) -> dict[str, Any]:
    """The ``gapfill`` member of the ``bdlg.history`` manifest annotation."""
    return {
        "enabled": True,
        "source": "dolt_history_issues",
        "batch_size": batch_size,
        "beads_queried": result.beads_queried,
        "events": len(result.events),
        "resumed_from": result.resumed_from,
        "counts": dict(result.counts),
        "suppressed": dict(result.suppressed),
        "time_semantics": (
            "occurred_at and recorded_at are the dolt commit date (snapshot diffs carry no finer "
            "valid time); provenance is derived, confidence "
            f"{DIFF_CONFIDENCE} for a change between two snapshots and {INITIAL_CONFIDENCE} for "
            "the first snapshot of a bead. Labels, dependencies and metadata are not recovered."
        ),
    }


__all__ = [
    "CURSOR_SCHEMA",
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_TIMEOUT_SECONDS",
    "DIFF_CONFIDENCE",
    "GAPFILL_ANNOTATION",
    "INITIAL_CONFIDENCE",
    "BdGapfillError",
    "GapState",
    "GapfillCursor",
    "GapfillEvents",
    "GapfillRead",
    "GapfillReader",
    "JsonCursorFile",
    "bead_gap_events",
    "collapse",
    "fetch_sql",
    "fingerprint",
    "gap_events",
    "gapfill_annotation",
    "hash_states",
    "lacking_created",
    "parse_scan",
    "scan_sql",
    "source_ref",
]
