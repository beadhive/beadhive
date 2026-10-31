"""The neutral bd ``events`` table -> BeadLog history events (the archival half of a backfill).

BeadHive-free (``docs/design/backfill-platform-adr.md`` sections 2 and 5). :mod:`bd_history` reads
the whole ``events`` table in one call as typed records; this module turns those records into
BeadLog events — ``create``, ``phase``, ``set``, ``label_add`` / ``label_remove``, ``edge_add`` /
``edge_remove`` and ``comment`` — that an archive can keep long after the store compacts them.
Hive lifecycle *conventions* (a land commit meaning "merged", ``review:pending`` meaning
"submitted") are not here: they belong to the hive pass.

**Provenance and time.** Every event read from the table is ``observed`` with the stable source
``bd`` / ``bd:events/<row uuid>``, so re-extracting the same table gives the same ids. The table
has one clock: a row's ``created_at`` is when bd recorded the event and the best estimate of when
it happened, so ``occurred_at`` and ``recorded_at`` are both that instant (bd records an event as
it happens). The two are kept apart where the sources really differ:

- a ``created`` row is stamped with the wall clock of the *recording*, which for a bead imported
  later can be long after the bead was created. When the issues row's ``created_at`` is earlier,
  that is ``occurred_at``, the table's time stays ``recorded_at``, and the evidence says so. (On
  the production store this never happens: every ``created`` row is within a second of the row,
  and never later than it. The rule exists for imports that do stamp the wall clock.)
- a bead with no ``created`` row at all (everything created before the table started, and every
  bead imported without events) gets an ``inferred`` create built from the issues row, with
  ``recorded_at`` null (the recording was never made) and a low confidence (:data:`PRE_TABLE`
  below the table's first row, :data:`IMPORTED` after it).

**Revert artifacts** are not history. Rows that repeat verbatim collapse, and a phase change
that is undone in the same instant (``open -> in_progress`` then ``in_progress -> open`` within
one second) cancels. A reopen exists only if a ``reopened`` row says so: a ``closed -> open ->
closed`` visible only in snapshots (Dolt history, repeated exports) produces no reopen.

**Never changes current state.** History is the *past* of the exported baseline, so
:func:`consistent_history` drops every history event that would make the fold of
``baseline + history`` differ from the fold of the baseline alone (a transient lease holder, a
deleted comment, a value the table never saw change), and counts it. Events of beads the baseline
does not hold are dropped too, since they would resurrect them.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from beadlog import (
    BeadEvent,
    BeadRef,
    Edge,
    EventKind,
    Evidence,
    changed_facets,
    fold,
    instant,
    mint_event,
)

from . import bd_history as _history
from .bd import (
    FIELD_COLUMNS,
    SOURCE_SYSTEM,
    TIMESTAMP_FIELDS,
    admissible,
    baseline_events,
    bd_timestamp,
    read_rows,
)
from .bd_history import HistoryEvent, HistoryRead
from .contracts import ExtractRequest
from .runner import Runner, subprocess_runner

STRATEGY_NAME = "bd-history"
STRATEGY_VERSION = "0.1.0"
# The manifest annotation an archive with history carries (bdlg.-prefixed keys are the format's).
HISTORY_ANNOTATION = "bdlg.history"
HISTORY_SCHEMA = "bdlg.history/1"
# Confidence of an inferred create (documented in docs/BACKFILL.md): lower when the bead predates
# the events table, so its creation could never have been recorded; higher when the table existed
# and simply holds no row for it (an import).
PRE_TABLE = 0.3
IMPORTED = 0.5
STORE_START_SQL = "SELECT MIN(date) AS first FROM dolt_log"

INTEGER_FIELDS = frozenset({"priority", "estimated_minutes"})
_METADATA_KEY = re.compile(r"^[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)*$")
_EDGE_TYPE = re.compile(r"^[a-z][a-z0-9-]*$")
_PHASE = re.compile(r"^[a-z][a-z0-9_]*$")
# Fields a `changes` payload may carry that are handled elsewhere or are bd bookkeeping.
_HANDLED = frozenset({"status", "metadata", "close_reason", "closed_at", "started_at"})
_PHASE_KINDS = frozenset(
    {_history.CLAIMED, _history.CLOSED, _history.REOPENED, _history.STATUS_CHANGED}
)


# -- the bead facts the conversion needs ----------------------------------------------------


@dataclass(frozen=True)
class BeadRow:
    """What the conversion needs of one baseline bead: its type, title, creation time (the
    earliest ``create`` occurrence the baseline holds) and current edges."""

    bead: BeadRef
    type: str
    title: str
    created_at: str | None
    closed: bool = False
    edges: frozenset[Edge] = frozenset()


def bead_rows(baseline: Iterable[BeadEvent]) -> dict[str, BeadRow]:
    """The live beads of a baseline log by bead id (deleted and erased beads are absent)."""
    events = tuple(baseline)
    created: dict[BeadRef, str] = {}
    for event in events:
        if event.kind in (EventKind.CREATE, EventKind.CHECKPOINT) and event.occurred_at:
            known = created.get(event.bead)
            if known is None or instant(event.occurred_at) < instant(known):
                created[event.bead] = event.occurred_at
    rows: dict[str, BeadRow] = {}
    for bead, state in fold(events).items():
        if not state.exists or state.deleted or state.erased:
            continue
        title = state.fields.get("title")
        rows[bead.id] = BeadRow(
            bead,
            state.type or "task",
            title if isinstance(title, str) and title else bead.id,
            created.get(bead),
            closed=state.phase == "closed",
            edges=frozenset(state.edges),
        )
    return rows


# -- revert-artifact suppression -------------------------------------------------------------


def _signature(event: HistoryEvent) -> tuple[Any, ...]:
    return (
        event.bead,
        event.kind,
        event.at,
        event.actor,
        event.old_status,
        event.new_status,
        event.assignee,
        event.reason,
        event.label,
        event.dep_type,
        event.dep_target,
        json.dumps(event.changes, sort_keys=True, default=str),
    )


def suppress_artifacts(
    events: Iterable[HistoryEvent],
) -> tuple[tuple[HistoryEvent, ...], Counter[str]]:
    """``events`` without revert artifacts, and what was dropped, counted by reason.

    - ``duplicate_row``: a row that repeats an earlier one verbatim (same bead, kind, instant,
      actor and payload);
    - ``phase_flip_flop``: two status-changing rows of one bead in the same second whose second
      returns to the phase before the first (``open -> in_progress`` then ``in_progress ->
      open``): the state never changed, so neither row is history.

    Input order is irrelevant; the result is in ``(at, row_id)`` order.
    """
    dropped: Counter[str] = Counter()
    seen: set[tuple[Any, ...]] = set()
    unique: list[HistoryEvent] = []
    for event in sorted(events, key=lambda item: item.sort_key):
        signature = _signature(event)
        if signature in seen:
            dropped["duplicate_row"] += 1
            continue
        seen.add(signature)
        unique.append(event)

    by_bead: dict[str, list[HistoryEvent]] = {}
    for event in unique:
        by_bead.setdefault(event.bead, []).append(event)
    cancelled: set[str] = set()
    for rows in by_bead.values():
        phase: str | None = None
        previous: tuple[HistoryEvent, str | None] | None = None  # (event, phase before it)
        for event in rows:
            if event.kind not in _PHASE_KINDS or event.new_status is None:
                continue
            before = event.old_status if event.old_status is not None else phase
            if (
                previous is not None
                and previous[0].at == event.at
                and previous[0].row_id not in cancelled
                and previous[1] is not None
                and event.new_status == previous[1]
                and previous[0].new_status != previous[1]
            ):
                cancelled.update({previous[0].row_id, event.row_id})
                dropped["phase_flip_flop"] += 2
                phase = previous[1]
                previous = None
                continue
            phase = event.new_status
            previous = (event, before)
    kept = tuple(event for event in unique if event.row_id not in cancelled)
    return kept, dropped


# -- conversion ------------------------------------------------------------------------------


def _field_value(name: str, value: Any) -> Any:
    if value is None or value == "":
        return None
    if name in INTEGER_FIELDS:
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


def _metadata(value: Any) -> Mapping[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {}
    return value if isinstance(value, Mapping) else {}


def source_ref(event: HistoryEvent) -> str:
    """The stable source reference of a table row: ``bd:events/<row uuid>``."""
    return f"bd:events/{event.row_id}"


class _Converter:
    def __init__(
        self,
        rows: Mapping[str, BeadRow],
        extracted_at: str,
        first_row: str | None,
    ) -> None:
        self.rows = rows
        self.extracted_at = extracted_at
        self.first_row = first_row
        self.out: list[BeadEvent] = []
        self.counts: Counter[str] = Counter()
        self.ignored: Counter[str] = Counter()
        self.edge_types: dict[tuple[str, str], set[str]] = {}

    def mint(
        self,
        kind: EventKind,
        bead: BeadRef,
        data: Mapping[str, Any],
        *,
        occurred_at: str | None,
        recorded_at: str | None,
        ref: str,
        actor: str | None = None,
        provenance: str = "observed",
        confidence: float | None = None,
        evidence: Sequence[Evidence] = (),
    ) -> BeadEvent:
        return mint_event(
            str(kind),
            bead,
            data,
            occurred_at=occurred_at,
            recorded_at=recorded_at,
            extracted_at=self.extracted_at,
            source=(SOURCE_SYSTEM, ref),
            provenance=provenance,
            confidence=confidence,
            actor=actor,
            evidence=evidence,
        )

    def add(self, *args: Any, **kwargs: Any) -> None:
        self.out.append(self.mint(*args, **kwargs))

    def sets(self, event: HistoryEvent, row: BeadRow) -> None:
        """One ``set`` per changed field of a payload and per changed metadata key."""
        ref, bead = source_ref(event), row.bead
        changes = event.changes
        for name, column in FIELD_COLUMNS.items():
            if column in changes:
                self.add(
                    EventKind.SET,
                    bead,
                    {"field": name, "value": _field_value(name, changes[column])},
                    occurred_at=event.at,
                    recorded_at=event.at,
                    ref=ref,
                    actor=event.actor,
                )
        if "issue_type" in changes and isinstance(changes["issue_type"], str):
            self.add(
                EventKind.SET,
                bead,
                {"field": "type", "value": changes["issue_type"]},
                occurred_at=event.at,
                recorded_at=event.at,
                ref=ref,
                actor=event.actor,
            )
        for key, value in sorted(_metadata(changes.get("metadata")).items()):
            if key == "bdlg" or not _METADATA_KEY.fullmatch(str(key)):
                self.ignored["metadata_key"] += 1
                continue
            self.add(
                EventKind.SET,
                bead,
                {"field": f"metadata.{key}", "value": None if value is None else admissible(value)},
                occurred_at=event.at,
                recorded_at=event.at,
                ref=ref,
                actor=event.actor,
            )
        known = set(FIELD_COLUMNS.values()) | _HANDLED | {"issue_type"}
        for key in changes:
            if key not in known:
                self.ignored[f"field:{key}"] += 1

    def phase(self, event: HistoryEvent, row: BeadRow, phase: str | None) -> None:
        if phase is None:
            return
        if not _PHASE.fullmatch(phase) or phase in ("retired", "deleted"):
            self.ignored["bad_phase"] += 1
            return
        data: dict[str, Any] = {"phase": phase}
        if event.kind in (_history.CLOSED, _history.REOPENED) and event.reason:
            data["reason"] = event.reason
        self.add(
            EventKind.PHASE,
            row.bead,
            data,
            occurred_at=event.at,
            recorded_at=event.at,
            ref=source_ref(event),
            actor=event.actor,
        )

    def edge(self, event: HistoryEvent, row: BeadRow, group: str) -> None:
        target = event.dep_target
        if not target:
            return
        key = (event.bead, target)
        if event.kind == _history.DEPENDENCY_ADDED:
            kinds = [event.dep_type] if event.dep_type else []
            for kind in kinds:
                self.edge_types.setdefault(key, set()).add(kind)
        else:
            known = self.edge_types.pop(key, set())
            kinds = sorted(known) or sorted(
                edge.type for edge in row.edges if edge.target.id == target
            )
            if not kinds:
                self.counts["dependency_removed_untyped"] += 1
                return
        for kind in kinds:
            if not _EDGE_TYPE.fullmatch(kind):
                self.ignored["bad_edge_type"] += 1
                continue
            edge = Edge(kind, BeadRef(group, target))
            self.add(
                EventKind.EDGE_ADD
                if event.kind == _history.DEPENDENCY_ADDED
                else EventKind.EDGE_REMOVE,
                row.bead,
                edge.to_json(),
                occurred_at=event.at,
                recorded_at=event.at,
                ref=source_ref(event),
                actor=event.actor,
            )

    def create(self, event: HistoryEvent, row: BeadRow) -> None:
        occurred, note = event.at, None
        if row.created_at is not None and instant(row.created_at) < instant(event.at):
            occurred = row.created_at
            note = (
                "occurred_at is the issues row's created_at, earlier than the events created "
                f"row ({event.at}), which carries the recording wall clock; the row's time is "
                "recorded_at"
            )
            self.counts["created_before_events_row"] += 1
        evidence = (Evidence("bd-row", f"issues/{row.bead.id}", note),) if note else ()
        self.add(
            EventKind.CREATE,
            row.bead,
            {"type": row.type, "fields": {"title": row.title}},
            occurred_at=occurred,
            recorded_at=event.at,
            ref=source_ref(event),
            actor=event.actor,
            evidence=evidence,
        )

    def inferred_create(self, row: BeadRow) -> None:
        if row.created_at is None:
            self.counts["no_created_at"] += 1
            return
        before = self.first_row is None or instant(row.created_at) < instant(self.first_row)
        note = "no events-table created row; built from the issues row (created_at); the " + (
            "bead predates the table, so its creation could not have been recorded"
            if before
            else "bead was imported without a created event"
        )
        self.add(
            EventKind.CREATE,
            row.bead,
            {"type": row.type, "fields": {"title": row.title}},
            occurred_at=row.created_at,
            recorded_at=None,
            ref=f"bd:inferred/{row.bead.id}#create",
            provenance="inferred",
            confidence=PRE_TABLE if before else IMPORTED,
            evidence=(Evidence("bd-row", f"issues/{row.bead.id}", note),),
        )
        self.counts["inferred_create"] += 1

    def convert(self, event: HistoryEvent, row: BeadRow, group: str) -> None:
        kind = event.kind
        if kind == _history.CREATED:
            self.create(event, row)
        elif kind == _history.COMMENTED:
            self.add(
                EventKind.COMMENT,
                row.bead,
                {"body": event.reason or ""},
                occurred_at=event.at,
                recorded_at=event.at,
                ref=source_ref(event),
                actor=event.actor,
            )
        elif kind in (_history.LABEL_ADDED, _history.LABEL_REMOVED):
            self.add(
                EventKind.LABEL_ADD if kind == _history.LABEL_ADDED else EventKind.LABEL_REMOVE,
                row.bead,
                {"label": event.label},
                occurred_at=event.at,
                recorded_at=event.at,
                ref=source_ref(event),
                actor=event.actor,
            )
        elif kind in (_history.DEPENDENCY_ADDED, _history.DEPENDENCY_REMOVED):
            self.edge(event, row, group)
        elif kind == _history.LEASE_RECLAIMED:
            self.ignored["lease_reclaimed"] += 1
        else:
            if kind == _history.CLAIMED:
                self.add(
                    EventKind.SET,
                    row.bead,
                    {"field": "assignee", "value": event.assignee},
                    occurred_at=event.at,
                    recorded_at=event.at,
                    ref=source_ref(event),
                    actor=event.actor,
                )
                self.phase(event, row, event.new_status or "in_progress")
                rest = {k: v for k, v in event.changes.items() if k != "assignee"}
                self.sets(_without(event, rest), row)
                return
            if kind in (_history.CLOSED, _history.REOPENED, _history.STATUS_CHANGED):
                self.phase(event, row, event.new_status)
            elif "status" in event.changes:
                self.phase(event, row, str(event.changes["status"]))
            self.sets(event, row)


def _without(event: HistoryEvent, changes: Mapping[str, Any]) -> HistoryEvent:
    return replace(event, changes=changes)


# -- the consistency guard -------------------------------------------------------------------


def _facets(event: BeadEvent) -> frozenset[str]:
    """The ``changed_facets`` names an event can decide."""
    kind, data = event.kind, event.data
    if kind is EventKind.CREATE:
        return frozenset(
            {"exists", "type", "phase", *(f"fields.{name}" for name in data.get("fields") or ())}
        )
    if kind is EventKind.SET:
        return frozenset({"type" if data["field"] == "type" else f"fields.{data['field']}"})
    if kind in (EventKind.LABEL_ADD, EventKind.LABEL_REMOVE):
        return frozenset({f"label:{data['label']}"})
    if kind in (EventKind.EDGE_ADD, EventKind.EDGE_REMOVE):
        edge = Edge.from_json(data)
        return frozenset({f"edge:{edge.type}:{edge.target.group}/{edge.target.id}"})
    if kind is EventKind.PHASE:
        return frozenset({"phase"})
    if kind is EventKind.COMMENT:
        return frozenset({f"comment:{event.id}"})
    return frozenset()


def _family(event: BeadEvent) -> str:
    """The reason label of a dropped event: its kind, with add/remove pairs folded together."""
    return str(event.kind).split("_", 1)[0]


def consistent_history(
    baseline: Sequence[BeadEvent], history: Iterable[BeadEvent]
) -> tuple[tuple[BeadEvent, ...], Counter[str]]:
    """The part of ``history`` that leaves the fold of ``baseline`` unchanged, and what was
    dropped, counted as ``inconsistent:<facet family>`` (and ``orphan_bead`` for events of beads
    the baseline does not hold).

    A history event is dropped when it decides a facet whose folded value would differ from the
    baseline's. The rest is kept verbatim, so the fold of ``baseline + kept`` equals the fold of
    ``baseline`` for every bead.
    """
    dropped: Counter[str] = Counter()
    base_by_bead: dict[BeadRef, list[BeadEvent]] = {}
    for event in baseline:
        base_by_bead.setdefault(event.bead, []).append(event)
    hist_by_bead: dict[BeadRef, list[BeadEvent]] = {}
    for event in history:
        hist_by_bead.setdefault(event.bead, []).append(event)
    kept: list[BeadEvent] = []
    for bead in sorted(hist_by_bead):
        mine = hist_by_bead[bead]
        base = base_by_bead.get(bead)
        if base is None:
            dropped["orphan_bead"] += len(mine)
            continue
        before = fold(base).get(bead)
        current = list(mine)
        for _ in range(5):
            changed = set(changed_facets(before, fold([*base, *current]).get(bead)))
            if not changed:
                break
            gone = {event.id for event in current if _facets(event) & changed}
            if not gone:  # cannot be explained by one facet: leave the bead's past out
                gone = {event.id for event in current}
            for event in current:
                if event.id in gone:
                    dropped[f"inconsistent:{_family(event)}"] += 1
            current = [event for event in current if event.id not in gone]
        if current and changed_facets(before, fold([*base, *current]).get(bead)):
            dropped["inconsistent:bead"] += len(current)
            current = []
        kept.extend(current)
    return tuple(kept), dropped


# -- the public conversion -------------------------------------------------------------------


@dataclass(frozen=True)
class HistoryEvents:
    """The history events of a store and the counted account of how they came to be.

    ``events`` leave the fold of the baseline unchanged. ``read`` mirrors the reader's own
    counters; ``suppressed`` are revert artifacts and events dropped to keep current state
    (``duplicate_row``, ``phase_flip_flop``, ``inconsistent:*``, ``orphan_bead``); ``ignored``
    are row parts bd records that BeadLog has no place for; ``counts`` the conversion's tallies.
    """

    events: tuple[BeadEvent, ...] = ()
    rows: int = 0
    skipped: Mapping[str, int] = field(default_factory=dict, hash=False)
    unknown_types: Mapping[str, int] = field(default_factory=dict, hash=False)
    suppressed: Mapping[str, int] = field(default_factory=dict, hash=False)
    ignored: Mapping[str, int] = field(default_factory=dict, hash=False)
    counts: Mapping[str, int] = field(default_factory=dict, hash=False)
    beads: int = 0
    first_row: str | None = None
    last_row: str | None = None

    def earliest_recorded(self) -> str | None:
        """The earliest ``recorded_at`` of any row the events table holds, including rows of
        beads the baseline no longer has (``None`` for an empty table): where recorded history
        begins."""
        return self.first_row


def history_events(
    history: HistoryRead | Sequence[HistoryEvent],
    baseline: Sequence[BeadEvent],
    *,
    group: str,
    extracted_at: str,
) -> HistoryEvents:
    """Turn a whole-hive ``events`` read into BeadLog history events (module doc).

    ``baseline`` is the store's current state as events (``bd`` strategy output): it supplies
    each bead's type, title, creation time and edges, and is what the result must not change.
    Deterministic: the same table and baseline give the same event ids.
    """
    read = history if isinstance(history, HistoryRead) else HistoryRead(tuple(history))
    baseline = tuple(baseline)
    rows = bead_rows(baseline)
    live = [event for event in read.events if event.bead in rows]
    orphans = Counter(event.bead for event in read.events if event.bead not in rows)
    usable, artifacts = suppress_artifacts(live)
    first_row = min((event.at for event in read.events), key=instant, default=None)
    converter = _Converter(rows, extracted_at, first_row)
    created = {event.bead for event in usable if event.kind == _history.CREATED}
    for event in usable:
        converter.convert(event, rows[event.bead], group)
    for bead_id in sorted(set(rows) - created):
        converter.inferred_create(rows[bead_id])
    kept, inconsistent = consistent_history(baseline, converter.out)
    suppressed = artifacts + inconsistent
    if orphans:
        suppressed["orphan_row"] += sum(orphans.values())
    counts = Counter(converter.counts)
    counts["orphan_beads"] = len(orphans)
    return HistoryEvents(
        events=kept,
        rows=read.rows,
        skipped=dict(sorted(read.skipped.items())),
        unknown_types=dict(sorted(read.unknown_types.items())),
        suppressed=dict(sorted(suppressed.items())),
        ignored=dict(sorted(converter.ignored.items())),
        counts=dict(sorted(counts.items())),
        beads=len({event.bead for event in kept}),
        first_row=first_row,
        last_row=max((event.at for event in read.events), key=instant, default=None),
    )


# -- the archive account ---------------------------------------------------------------------


def source_family(event: BeadEvent) -> str:
    """Which part of a source an event came from: ``bd:issues`` (current state), ``bd:events``
    (the events table), ``bd:inferred`` (a create built from an issues row), or the bare system
    (``git``) when the reference has no path."""
    system = event.source.system
    ref = event.source.ref.removeprefix(f"{system}:")
    return f"{system}:{ref.split('/', 1)[0]}" if "/" in ref else system


def read_store_start(
    root: Path | str,
    *,
    binary: str = "bd",
    timeout: float | None = 300.0,
    runner: Runner | None = None,
) -> str | None:
    """The first Dolt commit date of the store (one ``SELECT``), or ``None`` when it cannot be
    read: the date is context for a pre-retention note, never a reason to fail an export."""
    run = runner or subprocess_runner(timeout, _history.BdHistoryError, "bd sql")
    try:
        rows = _history.decode_rows(
            run([binary, "-C", str(root), "sql", "--json", STORE_START_SQL])
        )
    except _history.BdHistoryError:
        return None
    if isinstance(rows, list) and rows and isinstance(rows[0], Mapping):
        return bd_timestamp(rows[0].get("first"))
    return None


def history_annotation(
    events: Sequence[BeadEvent],
    history: HistoryEvents,
    baseline: Sequence[BeadEvent],
    *,
    store_start: str | None = None,
) -> dict[str, Any]:
    """The manifest annotation (:data:`HISTORY_ANNOTATION`) of an archive with history.

    Reports the event count of each source family, what the conversion read, skipped and
    suppressed, the earliest recorded history date, and the pre-retention gap computed from the
    data: beads created before the events table's first row (or the store's first commit) have
    no recorded history, only the inferred create and the exported current state.
    """
    families = Counter(source_family(event) for event in events)
    earliest = history.earliest_recorded()
    created = [
        event.occurred_at
        for event in baseline
        if event.kind is EventKind.CREATE and event.occurred_at
    ]
    first_bead = min(created, key=instant) if created else None
    pre_table = history.counts.get("inferred_create", 0)
    gap: dict[str, Any] = {
        "first_bead_created": first_bead,
        "first_store_commit": store_start,
        "first_event_recorded": earliest,
        "beads_without_recorded_creation": pre_table,
    }
    parts = []
    if first_bead and earliest and instant(first_bead) < instant(earliest):
        parts.append(f"beads date from {first_bead[:10]}")
    if store_start:
        parts.append(f"store commits from {store_start[:10]}")
    if earliest:
        parts.append(f"the events table from {earliest[:10]}")
    gap["note"] = (
        (
            "Pre-retention gap: " + ", ".join(parts) + "; history before the events table is "
            f"unrecorded, so {pre_table} bead(s) carry an inferred, low-confidence create from "
            "the issues row and no other recorded history."
        )
        if parts
        else "No history was read."
    )
    return {
        HISTORY_ANNOTATION: {
            "schema_version": HISTORY_SCHEMA,
            "time_semantics": (
                "occurred_at is when the fact happened; recorded_at is when the source recorded "
                "it. The bd events table has one clock, so for its facts both are the row's "
                "created_at, except a created row, whose occurred_at is the issues row's "
                "created_at when that is earlier. Inferred creates have recorded_at null."
            ),
            "sources": dict(sorted(families.items())),
            "earliest_recorded": earliest,
            "latest_recorded": history.last_row,
            "events_table_rows": history.rows,
            "history_events": len(history.events),
            "skipped_rows": dict(history.skipped),
            "unknown_event_types": dict(history.unknown_types),
            "suppressed": dict(history.suppressed),
            "not_modeled": dict(history.ignored),
            "counts": dict(history.counts),
            "gap": gap,
        }
    }


# -- the strategy ----------------------------------------------------------------------------


class BdHistoryStrategy:
    """Source strategy ``bd-history``: the bd events table of the store at ``request.root`` as
    BeadLog history events (the issues rows it needs are read with one ``bd export``).

    Options: ``binary`` (default ``bd``), ``timeout`` seconds for the events query.
    ``rows_runner`` / ``history_runner`` replace the subprocess calls in tests.
    """

    name = STRATEGY_NAME
    version = STRATEGY_VERSION

    def __init__(
        self,
        rows_runner: Callable[[Sequence[str]], str] | None = None,
        history_runner: Runner | None = None,
    ) -> None:
        self._rows = rows_runner
        self._history = history_runner

    def extract(self, request: ExtractRequest) -> tuple[BeadEvent, ...]:
        options = request.options
        binary = str(options.get("binary", "bd"))
        rows = read_rows(request.root, binary=binary, runner=self._rows)
        baseline = baseline_events(rows, group=request.group, extracted_at=request.extracted_at)
        timeout = options.get("timeout")
        read = _history.read_history(
            request.root,
            binary=binary,
            **({"timeout": float(timeout)} if timeout else {}),
            runner=self._history,
        )
        return history_events(
            read, baseline, group=request.group, extracted_at=request.extracted_at
        ).events


__all__ = [
    "HISTORY_ANNOTATION",
    "HISTORY_SCHEMA",
    "IMPORTED",
    "PRE_TABLE",
    "STORE_START_SQL",
    "STRATEGY_NAME",
    "STRATEGY_VERSION",
    "BdHistoryStrategy",
    "BeadRow",
    "HistoryEvents",
    "bead_rows",
    "consistent_history",
    "history_annotation",
    "history_events",
    "read_store_start",
    "source_family",
    "source_ref",
    "suppress_artifacts",
]
