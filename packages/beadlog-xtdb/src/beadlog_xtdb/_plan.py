"""Load planning for the XTDB BeadLog loader: events in, transactions and a loss report out.

Pure (no connection), so every guard and every degradation is decided **before anything is
written** and a dry run reports exactly what the apply would. The rules are the epic design of
``bh-wj8d7`` and ``docs/design/beadlog-format-adr.md`` amendment A1-A5, from the spikes
``bh-w56ts`` (layout (b), write semantics) and ``bh-3jp2g`` (monotone ``SYSTEM_TIME``).

**Op mapping.** Each bead's rows are kept equal to the BeadLog fold of its loaded events, written
as layout (b) versions (``create`` / ``set`` / ``phase`` / ``retire`` / ``delete`` and a seeding
``checkpoint`` give a full ``beads`` version by ``INSERT ... RECORDS`` with ``_valid_from`` the
event's ``occurred_at``; ``delete`` is a version with ``deleted: true``; ``label_add`` /
``edge_add`` an ``INSERT`` of the composite ``_id``; ``label_remove`` / ``edge_remove`` a
``DELETE ... FOR PORTION OF VALID_TIME FROM t TO NULL``; ``comment`` an ``INSERT``; ``tombstone``
an ``ERASE``). A field cleared by ``set`` to null is absent from the next full version, which
replaces the whole row: never ``PATCH``. When an event lands earlier in valid time than versions
already written (a backdated correction, the null-``recorded_at`` tail), the versions it changes
are rewritten with an explicit ``_valid_to`` (the next version's start), so a write never
clobbers later versions (``bh-w56ts`` E7.2, ``bh-3jp2g`` S11). In the common in-order case that
reduces to exactly one statement per event.

**Monotone initial load** (A2, A3). Events with a ``recorded_at`` replay in
``index/recorded.jsonl`` order, ``(recorded_at, id)``; consecutive equal ``recorded_at`` form one
``BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '<recorded_at>')`` transaction, its events
ordered by ``(bead, ordering key)``. The planned transaction times are checked strictly
increasing client-side, and the last one must not be later than the wall clock.

**Same-instant split** (A5). Within a group, each entity's writes that share one ``occurred_at``
are numbered from ``k = 0``; write ``k >= 1`` gets its own transaction at
``recorded_at + k`` µs with ``_valid_from = occurred_at + k`` µs, reported ``x-xtdb.us-split``.
The shifted time must stay below the next distinct ``recorded_at`` or the load is refused. An
entity is a ``(table, _id)``: the bead for ``create`` / ``set`` / ``phase`` / ``retire`` /
``delete``, the label or edge row, the comment. **Checkpoints and the split:** a checkpoint
carries the times of the event it covers, so it always shares an instant with that event. An
*assertion* checkpoint (the covered event, or an earlier change, is in the log) writes no row, so
it never counts toward ``k`` and never shifts anything; it is only journaled. A *seeding*
checkpoint writes the bead's version (and its label and edge rows) at its ``covers_through``
position, which sorts before every change of the bead, so within its group it is always write
``k = 0`` and any same-instant change of the same bead becomes ``k >= 1``.

**Wall-clock phase last** (A2, A4, A1). Events with a null ``recorded_at`` load after the replay
in wall-clock transactions (``recorded-time.null``), each write bounded by an explicit
``_valid_to``; then every ``ERASE``. A tombstone's journal row is part of the replay; its
``ERASE`` statements run in the wall-clock phase and are reported ``erasure.incomplete``
(``erase-logical``). Content of earlier loads that a new tombstone erases is removed by
``ERASE`` of the affected rows, which are then rewritten from the surviving events in a
following wall-clock transaction.

**Incremental mode.** Every event goes in plain wall-clock transactions (``batch_size`` events
each) and reports ``recorded-time.reassigned``; events already in the store (by id) are skipped,
so a reload is idempotent.

**Journal.** Every loaded event is also kept whole, with its original id, in table
``bead_events`` (``_id`` = event id; ``bead_id``, ``kind``, ``event`` = the event's JSON,
``valid_at`` = the instant its rows were written at). The format ADR requires a store that keeps
a loaded event to report it back with its original id (section 4); the journal is how
:meth:`XtdbStoreLoader.export` does that, and how an incremental load knows the store's state.
"""

from __future__ import annotations

import dataclasses
import json
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from beadlog import (
    BeadEvent,
    BeadRef,
    BeadState,
    Edge,
    EventKind,
    OrderKey,
    checkpoint_key,
    erasure,
    fold,
    format_timestamp,
    instant,
    ordering_key,
    union_merge,
)

from ._layout import (
    BEAD_COMMENTS,
    BEAD_EDGES,
    BEAD_LABELS,
    BEADS,
    TABLES,
    Layout,
    LayoutError,
    bead_row,
    comment_row,
    edge_row,
    label_row,
)
from ._report import (
    COLUMN_COERCED,
    COLUMN_REFUSED,
    ERASE_LOGICAL_DETAIL,
    ERASED,
    ID_UNREPRESENTABLE,
    JOURNAL_STORED_AS,
    SUB_MICROSECOND,
    US_SPLIT,
    LossReport,
)
from ._transit import Instant
from ._types import ColumnTypeError, ColumnTypes

JOURNAL = "bead_events"
LOADER_TABLES = (*TABLES, JOURNAL)
DEFAULT_LOAD_BATCH = 500
MICROSECOND = timedelta(microseconds=1)

# Transaction phases, in the order a load runs them.
BACKFILL = "backfill"
SPLIT = "split"
TAIL = "tail"
INCREMENTAL = "incremental"
ERASE = "erase"
REWRITE = "rewrite"

# Field names that cannot be a ``beads`` column: they collide with the layout's own columns.
_STRUCTURAL = frozenset({"group_id", "bead_id", "issue_type", "phase", "retired", "deleted"})
_METADATA_PREFIX = "metadata."
_BEAD_KINDS = frozenset(
    {EventKind.CREATE, EventKind.SET, EventKind.PHASE, EventKind.RETIRE, EventKind.DELETE}
)

Entity = tuple[str, str]
Segment = tuple[datetime, datetime | None, Any]


class LoadMode(StrEnum):
    """``initial``: the monotone recorded-time replay into a fresh database (refused otherwise);
    ``incremental``: wall-clock transactions on top of an earlier load; ``auto``: initial when the
    target is fresh, incremental when it is not."""

    AUTO = "auto"
    INITIAL = "initial"
    INCREMENTAL = "incremental"


class LoadRefused(ValueError):
    """The load was refused before anything was written; the database is untouched."""


class MonotoneOrderError(LoadRefused):
    """Recorded times are not strictly increasing in replay order (a tie or an earlier time)."""


class FutureRecordedTimeError(LoadRefused):
    """A ``recorded_at`` later than the wall clock: XTDB would hide its rows until then (S11e)."""


class SplitPreconditionError(LoadRefused):
    """A same-instant split would reach the next distinct ``recorded_at`` (format ADR A5)."""


class TargetNotFreshError(LoadRefused):
    """An initial load into a database that is not fresh (format ADR A2, target check)."""


class LayoutMismatchError(LoadRefused):
    """The batch needs a different id spelling than the store already uses."""


class LoadAborted(RuntimeError):
    """A transaction failed while the load was being written: discard the database and reload."""


@dataclass(frozen=True)
class DeletePortion:
    """``DELETE FROM <table> FOR PORTION OF VALID_TIME FROM start TO end WHERE _id = entity``."""

    table: str
    entity: str
    start: datetime
    end: datetime | None


@dataclass(frozen=True)
class Erase:
    """``ERASE FROM <table> WHERE <column> IN (values)``; journal erasures keep tombstones."""

    table: str
    column: str
    values: tuple[str, ...]
    keep_tombstones: bool = False


@dataclass
class Transaction:
    """One XTDB transaction: ``system_time`` set (a backfill) or ``None`` (wall clock)."""

    system_time: datetime | None
    phase: str
    inserts: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    deletes: list[DeletePortion] = field(default_factory=list)
    erases: list[Erase] = field(default_factory=list)
    events: list[str] = field(default_factory=list)

    def insert(self, table: str, document: dict[str, Any]) -> None:
        self.inserts.setdefault(table, []).append(document)

    @property
    def empty(self) -> bool:
        return not (self.inserts or self.deletes or self.erases)

    def statement_count(self) -> int:
        return (
            sum(len(docs) for docs in self.inserts.values()) + len(self.deletes) + len(self.erases)
        )


@dataclass(frozen=True)
class LoadPlan:
    """What a load will write, in order, and what it loses."""

    mode: LoadMode
    transactions: tuple[Transaction, ...]
    report: LossReport
    skipped: tuple[str, ...] = ()

    @property
    def system_times(self) -> tuple[datetime, ...]:
        return tuple(tx.system_time for tx in self.transactions if tx.system_time is not None)

    @property
    def first_system_time(self) -> datetime | None:
        times = self.system_times
        return times[0] if times else None


# -- time helpers ---------------------------------------------------------------------------


def truncate_timestamp(value: str | None) -> str | None:
    """``value`` cut to microseconds (XTDB's precision); canonical spelling, ``None`` kept."""
    if value is None or len(value) <= 27 or value[19] != ".":
        return value
    fraction = value[20:-1][:6].rstrip("0")
    return value[:19] + (f".{fraction}" if fraction else "") + "Z"


def _dt(value: str) -> datetime:
    return Instant.parse(value).at


def _iso(moment: datetime) -> str:
    return format_timestamp(moment)


def _key_dt(k1: tuple[int, str, int]) -> datetime:
    if k1[0] != 0:
        raise ValueError("an effective event always has a valid time")
    base = datetime.strptime(k1[1], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)
    return base + timedelta(microseconds=k1[2] // 1000)


def first_recorded_at(events: Iterable[BeadEvent]) -> datetime | None:
    """The earliest ``recorded_at`` (cut to µs) in ``events``: an initial load's first system
    time, which the target check compares with the target's latest transaction."""
    times = [truncate_timestamp(event.recorded_at) for event in events]
    present = [_dt(value) for value in times if value is not None]
    return min(present) if present else None


def _pointer(*tokens: str) -> str:
    return "".join("/" + token.replace("~", "~0").replace("/", "~1") for token in tokens)


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    return value


def column_ok(name: str) -> bool:
    """Whether a BeadLog field has a ``beads`` column (or a ``metadata`` struct key)."""
    if name.startswith(_METADATA_PREFIX):
        return len(name) > len(_METADATA_PREFIX)
    return "." not in name and name not in _STRUCTURAL and not name.startswith("_")


def _lossy(original: Any, coerced: Any) -> bool:
    if isinstance(coerced, Instant):
        return not (isinstance(original, str) and format_timestamp(coerced.at) == original)
    return type(original) is not type(coerced) or original != coerced


# -- the planner ----------------------------------------------------------------------------


@dataclass
class _Timed:
    """A batch event with its µs-cut times."""

    event: BeadEvent
    occurred: str | None
    recorded: str | None


class _Planner:
    def __init__(self, *, layout: Layout, now: datetime, report: LossReport, batch_size: int):
        self.layout = layout
        self.now = now.astimezone(UTC)
        self.report = report
        self.batch_size = batch_size
        self.types = ColumnTypes()
        self.live: dict[BeadRef, dict[str, BeadEvent]] = defaultdict(dict)
        self.timelines: dict[BeadRef, dict[Entity, list[Segment]]] = {}
        self.seeding: frozenset[str] = frozenset()

    # -- entities ---------------------------------------------------------------------------

    def entities(self, event: BeadEvent) -> list[Entity]:
        """The rows one event writes (format ADR A5's "entity"): none for a tombstone or an
        assertion checkpoint."""
        layout, kind, data = self.layout, event.kind, event.data
        if kind in _BEAD_KINDS:
            return [(BEADS, layout.bead_key(event.bead))]
        if kind in (EventKind.LABEL_ADD, EventKind.LABEL_REMOVE):
            return [(BEAD_LABELS, layout.label_id(event.bead, data["label"]))]
        if kind in (EventKind.EDGE_ADD, EventKind.EDGE_REMOVE):
            edge = Edge.from_json(data)
            return [(BEAD_EDGES, layout.edge_id(event.bead, edge.type, edge.target))]
        if kind is EventKind.COMMENT:
            return [(BEAD_COMMENTS, event.id)]
        if kind is EventKind.CHECKPOINT and event.id in self.seeding:
            state = data["state"]
            out = [(BEADS, layout.bead_key(event.bead))]
            out += [(BEAD_LABELS, layout.label_id(event.bead, label)) for label in state["labels"]]
            for raw in state["edges"]:
                edge = Edge.from_json(raw)
                out.append((BEAD_EDGES, layout.edge_id(event.bead, edge.type, edge.target)))
            return out
        return []

    def representable(self, event: BeadEvent) -> str | None:
        """Why the layout cannot spell this event's ids, or ``None``."""
        try:
            self.layout.bead_key(event.bead)
            refs: list[Edge] = []
            if event.kind in (EventKind.EDGE_ADD, EventKind.EDGE_REMOVE):
                refs.append(Edge.from_json(event.data))
            elif event.kind is EventKind.CHECKPOINT:
                refs += [Edge.from_json(raw) for raw in event.data["state"].get("edges") or ()]
            for edge in refs:
                self.layout.edge_id(event.bead, edge.type, edge.target)
        except LayoutError as exc:
            return str(exc)
        return None

    def _bead_values(self, state: BeadState | None) -> dict[Entity, Any]:
        out: dict[Entity, Any] = {}
        if state is None or state.erased:
            return out
        layout, bead = self.layout, state.bead
        if state.exists or state.deleted or state.type or state.phase or state.fields:
            fields: dict[str, Any] = {}
            for name, value in state.fields.items():
                if value is None or not column_ok(name):
                    continue
                plain = _thaw(value)
                if name.startswith(_METADATA_PREFIX):
                    fields[name] = plain
                    continue
                try:
                    fields[name] = self.types.coerce(BEADS, name, plain)
                except ColumnTypeError:
                    continue  # reported as x-xtdb.column-refused on the assigning event
            shaped = dataclasses.replace(
                state,
                fields=fields,
                retired=_thaw(state.retired) if state.retired is not None else None,
            )
            out[(BEADS, layout.bead_key(bead))] = bead_row(
                layout, shaped, self.types, unsupported=[]
            )
        for label in state.labels:
            row = label_row(layout, bead, label)
            out[(BEAD_LABELS, row["_id"])] = row
        for edge in state.edges:
            row = edge_row(layout, bead, edge, self.types)
            out[(BEAD_EDGES, row["_id"])] = row
        for comment in state.comments:
            row = comment_row(layout, bead, comment, self.types)
            row.pop("_valid_from", None)
            out[(BEAD_COMMENTS, comment.id)] = row
        return out

    # -- timelines --------------------------------------------------------------------------

    def recompute(self, bead: BeadRef, start: datetime | None) -> dict[Entity, list[Segment]]:
        """The bead's entity timelines after its live events, recomputed from ``start`` on
        (``None``: from scratch). Earlier segments are kept as written."""
        events = list(self.live.get(bead, {}).values())
        last_key: dict[tuple[int, str, int], OrderKey] = {}
        for event in events:
            keys = [ordering_key(event)]
            if event.kind is EventKind.CHECKPOINT:
                keys.append(checkpoint_key(event))
            for key in keys:
                held = last_key.get(key.k1)
                if held is None or key > held:
                    last_key[key.k1] = key
        points: list[tuple[datetime, dict[Entity, Any]]] = []
        for k1 in sorted(last_key):
            at = _key_dt(k1)
            if start is not None and at < start:
                continue
            state = fold(events, at=last_key[k1]).get(bead)
            points.append((at, self._bead_values(state)))
        old = self.timelines.get(bead, {}) if start is not None else {}
        names = set(old)
        for _, values in points:
            names.update(values)
        new: dict[Entity, list[Segment]] = {}
        for name in sorted(names):
            segments: list[Segment] = []
            for begin, end, value in old.get(name, ()):
                if start is not None and begin < start:
                    segments.append((begin, start if end is None or end > start else end, value))
            for index, (at, values) in enumerate(points):
                end = points[index + 1][0] if index + 1 < len(points) else None
                segments.append((at, end, values.get(name)))
            compressed = _compress(segments)
            if compressed:
                new[name] = compressed
        return new

    def emit(
        self,
        tx: Transaction,
        old: Mapping[Entity, list[Segment]],
        new: Mapping[Entity, list[Segment]],
        *,
        only: set[Entity] | None = None,
    ) -> None:
        for name in sorted(set(old) | set(new)):
            if only is not None and name not in only:
                continue
            table, entity = name
            before = old.get(name, [])
            by_begin = {b: (e, v) for b, e, v in before}
            for begin, end, value in new.get(name, []):
                if value is None:
                    if any(v is not None and _overlaps(b, e, begin, end) for b, e, v in before):
                        tx.deletes.append(DeletePortion(table, entity, begin, end))
                    continue
                held = by_begin.get(begin)  # the common case: the same version, already written
                if held is not None and held[1] == value and _covers(held[0], end):
                    continue
                if any(v == value and b <= begin and _covers(e, end) for b, e, v in before):
                    continue
                document = dict(value)
                document["_valid_from"] = Instant(begin)
                if end is not None:
                    document["_valid_to"] = Instant(end)
                tx.insert(table, document)

    # -- per-event bookkeeping ----------------------------------------------------------------

    def field_pass(self, event: BeadEvent, *, report: bool) -> None:
        """Pin column types in load order and report the field values a column cannot take."""
        kind, data = event.kind, event.data
        if kind is EventKind.CREATE:
            items = [((name,), name, value) for name, value in (data.get("fields") or {}).items()]
            prefix: tuple[str, ...] = ("data", "fields")
        elif kind is EventKind.SET and data.get("field") != "type" and "field" in data:
            items = [((), data["field"], data.get("value"))]
            prefix = ("data", "value")
        elif kind is EventKind.CHECKPOINT and event.id in self.seeding:
            fields = data["state"].get("fields") or {}
            items = [((name,), name, value) for name, value in fields.items()]
            prefix = ("data", "state", "fields")
        else:
            return
        for tail, name, value in items:
            if value is None:
                continue
            member = _pointer(*prefix, *tail)
            if not column_ok(name):
                if report:
                    self.report.add(
                        event,
                        "field.unsupported",
                        "degraded",
                        member=member,
                        stored_as=JOURNAL_STORED_AS,
                        detail=f"field {name!r} has no beads column (layout (b))",
                    )
                continue
            if name.startswith(_METADATA_PREFIX):
                continue
            plain = _thaw(value)
            try:
                coerced = self.types.coerce(BEADS, name, plain)
            except ColumnTypeError as exc:
                if report:
                    self.report.add(
                        event,
                        COLUMN_REFUSED,
                        "degraded",
                        member=member,
                        stored_as=JOURNAL_STORED_AS,
                        detail=str(exc),
                    )
                continue
            if report and _lossy(plain, coerced):
                kind_name = self.types.kind(BEADS, name)
                self.report.add(
                    event,
                    COLUMN_COERCED,
                    "degraded",
                    member=member,
                    stored_as=f"{BEADS}.{name}",
                    detail=f"stored as {kind_name} to keep the column one type",
                )

    def journal_row(self, event: BeadEvent, valid_at: datetime) -> dict[str, Any]:
        return {
            "_id": event.id,
            "bead_id": self.layout.bead_key(event.bead),
            "kind": str(event.kind),
            "event": json.dumps(
                event.to_json(), sort_keys=True, ensure_ascii=False, separators=(",", ":")
            ),
            "valid_at": Instant(valid_at),
        }


def _compress(segments: list[Segment]) -> list[Segment]:
    out: list[Segment] = []
    for begin, end, value in segments:
        if out and out[-1][1] == begin and out[-1][2] == value:
            out[-1] = (out[-1][0], end, value)
        else:
            out.append((begin, end, value))
    while out and out[0][2] is None:
        out.pop(0)
    return out


def _overlaps(b1: datetime, e1: datetime | None, b2: datetime, e2: datetime | None) -> bool:
    return (e1 is None or e1 > b2) and (e2 is None or e2 > b1)


def _covers(held_end: datetime | None, wanted_end: datetime | None) -> bool:
    return held_end is None or (wanted_end is not None and held_end >= wanted_end)


def _seeding(events: Iterable[BeadEvent]) -> frozenset[str]:
    """Ids of the checkpoints that seed their bead (format ADR section 6, as ``fold`` decides)."""
    by_bead: dict[BeadRef, list[BeadEvent]] = defaultdict(list)
    for event in events:
        if event.kind is not EventKind.TOMBSTONE:
            by_bead[event.bead].append(event)
    seeds: set[str] = set()
    for history in by_bead.values():
        changes = [ordering_key(e) for e in history if e.kind is not EventKind.CHECKPOINT]
        first = min(changes) if changes else None
        candidates = [
            e
            for e in history
            if e.kind is EventKind.CHECKPOINT and (first is None or first > checkpoint_key(e))
        ]
        if candidates:
            seeds.add(max(candidates, key=checkpoint_key).id)
    return frozenset(seeds)


def _with_times(event: BeadEvent, occurred: str, recorded: str | None) -> BeadEvent:
    changes: dict[str, Any] = {"occurred_at": occurred, "recorded_at": recorded}
    if event.kind is EventKind.CHECKPOINT:
        data = _thaw(event.data)
        covers = data["covers_through"]
        covers["occurred_at"] = truncate_timestamp(covers.get("occurred_at"))
        covers["recorded_at"] = truncate_timestamp(covers.get("recorded_at"))
        changes["data"] = data
    return dataclasses.replace(event, **changes)


def _refs(events: Iterable[BeadEvent]) -> list[BeadRef]:
    refs: list[BeadRef] = []
    for event in events:
        refs.append(event.bead)
        if event.kind in (EventKind.EDGE_ADD, EventKind.EDGE_REMOVE):
            refs.append(Edge.from_json(event.data).target)
        elif event.kind is EventKind.CHECKPOINT:
            refs += [Edge.from_json(raw).target for raw in event.data["state"].get("edges") or ()]
    return refs


def _replay_key(event: BeadEvent) -> tuple[tuple[int, str, int], str]:
    return (instant(event.recorded_at), event.id)


def plan_load(
    events: Sequence[BeadEvent],
    *,
    mode: LoadMode,
    now: datetime,
    existing: Sequence[tuple[BeadEvent, datetime | None]] = (),
    order: Sequence[str] | None = None,
    multi_group: bool | None = None,
    batch_size: int = DEFAULT_LOAD_BATCH,
) -> LoadPlan:
    """Plan loading ``events`` (module doc) and compute its loss report.

    ``mode`` is :attr:`LoadMode.INITIAL` or :attr:`LoadMode.INCREMENTAL` (the loader resolves
    ``auto`` against the target). ``existing`` is the store's journal: ``(event, valid_at)`` for
    every event already loaded (incremental only). ``order`` is the replay order of event ids
    from ``index/recorded.jsonl``; without it the events are sorted into the same order. ``now``
    is the wall clock (the future-time check and the valid time of an event with neither time).

    Raises a :class:`LoadRefused` subclass when a guard fails, before anything is planned for
    writing.
    """
    if mode is LoadMode.AUTO:
        raise ValueError("resolve LoadMode.AUTO against the target before planning")
    if now.tzinfo is None:
        raise ValueError("now must be an aware datetime")
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    batch = tuple(events)
    report = LossReport(batch)
    if mode is LoadMode.INITIAL and existing:
        raise TargetNotFreshError("an initial load needs a fresh database; the store has events")

    # 1. Merge the batch (an id collision raises), drop what tombstones erase.
    merged = union_merge([batch])
    kept_ids = {event.id for event in merged}
    first_copy: dict[str, BeadEvent] = {}
    for event in batch:
        first_copy.setdefault(event.id, event)
    store_events = [event for event, _ in existing]
    erased_ids, erased_beads = erasure([*store_events, *merged])
    reported: set[str] = set()
    for event_id, event in first_copy.items():
        erased_now = event.kind is not EventKind.TOMBSTONE and (
            event_id in erased_ids or event.bead in erased_beads
        )
        if event_id not in kept_ids or erased_now:
            report.drop(
                event, ERASED, "erased by a tombstone (in this load or the store); never written"
            )
            reported.add(event_id)
    store_ids = {event.id for event in store_events}
    skipped = tuple(sorted(e.id for e in merged if e.id in store_ids and e.id not in reported))
    candidates = [e for e in merged if e.id not in reported and e.id not in store_ids]

    # 2. Id spelling: one layout per store.
    if multi_group is None:
        layout = Layout.for_refs([*_refs(store_events), *_refs(candidates)])
    else:
        layout = Layout(multi_group=multi_group)
    if store_events and Layout.for_refs(_refs(store_events)).multi_group != layout.multi_group:
        raise LayoutMismatchError(
            "the store spells ids for a single group but this batch spans several groups (or "
            "the reverse); load it into a fresh database"
        )
    planner = _Planner(layout=layout, now=now, report=report, batch_size=batch_size)
    pending: list[_Timed] = []
    for event in candidates:
        why = planner.representable(event)
        if why is not None:
            report.drop(event, ID_UNREPRESENTABLE, why, member="/bead")
            continue
        timed = _Timed(
            event, truncate_timestamp(event.occurred_at), truncate_timestamp(event.recorded_at)
        )
        for member, original, cut in (
            ("/occurred_at", event.occurred_at, timed.occurred),
            ("/recorded_at", event.recorded_at, timed.recorded),
        ):
            if original != cut:
                report.add(
                    event,
                    SUB_MICROSECOND,
                    "approximated",
                    member=member,
                    stored_as=cut,
                    detail="XTDB keeps microseconds; the journal keeps the time as written",
                )
        pending.append(timed)
    planner.seeding = _seeding([*store_events, *(t.event for t in pending)])

    # 3. The store as written (incremental): live events, pinned columns, timelines.
    now_iso = _iso(planner.now)
    for event, valid_at in sorted(existing, key=lambda item: _replay_key(item[0])):
        if event.kind is EventKind.TOMBSTONE:
            continue
        occurred = truncate_timestamp(event.occurred_at)
        valid = (
            _iso(valid_at)
            if valid_at is not None
            else (occurred or truncate_timestamp(event.recorded_at) or now_iso)
        )
        planner.live[event.bead][event.id] = _with_times(
            event, valid, truncate_timestamp(event.recorded_at)
        )
        planner.field_pass(event, report=False)
    for bead in sorted(planner.live):
        planner.timelines[bead] = planner.recompute(bead, None)

    # 4. Transactions with their events and effective times.
    plan_txs: list[tuple[Transaction, list[tuple[_Timed, int]]]] = []
    if mode is LoadMode.INITIAL:
        ordered = _replay_order(pending, order, report)
        backfill = [t for t in ordered if t.recorded is not None]
        tail = [t for t in ordered if t.recorded is None]
        plan_txs += _backfill_transactions(planner, backfill)
        plan_txs += _wall_clock_transactions(planner, tail, TAIL)
    else:
        ordered = sorted(pending, key=lambda t: _replay_key(t.event))
        plan_txs += _wall_clock_transactions(planner, ordered, INCREMENTAL)
    _check_monotone(plan_txs, planner.now if mode is LoadMode.INITIAL else None)

    # 5. Simulate each transaction: effective events, journal rows, entity writes, losses.
    transactions: list[Transaction] = []
    loaded: list[BeadEvent] = []
    for tx, members in plan_txs:
        touched: dict[BeadRef, datetime] = {}
        for timed, k in members:
            event = timed.event
            effective, valid = _effective(planner, timed, k, tx)
            planner.field_pass(event, report=True)
            tx.insert(JOURNAL, planner.journal_row(event, valid))
            tx.events.append(event.id)
            loaded.append(event)
            if event.kind is EventKind.TOMBSTONE:
                continue
            planner.live[event.bead][event.id] = effective
            begin = valid
            if event.kind is EventKind.CHECKPOINT:
                begin = min(begin, _key_dt(checkpoint_key(effective).k1))
            held = touched.get(event.bead)
            touched[event.bead] = begin if held is None else min(held, begin)
        for bead, begin in sorted(touched.items()):
            old = planner.timelines.get(bead, {})
            new = planner.recompute(bead, begin)
            planner.emit(tx, old, new)
            planner.timelines[bead] = new
        transactions.append(tx)

    # 6. The wall-clock ERASE phase, then rewrites of rows the erased events had shaped.
    tombstones = [e for e in loaded if e.kind is EventKind.TOMBSTONE]
    if tombstones:
        transactions += _erase_transactions(planner, tombstones, store_events)

    # 7. Losses that depend on the whole load: dangling edge targets.
    with_rows = {
        name[1]
        for timelines in planner.timelines.values()
        for name, segments in timelines.items()
        if name[0] == BEADS and any(value is not None for _, _, value in segments)
    }
    for event in loaded:
        if event.kind is EventKind.EDGE_ADD:
            target = Edge.from_json(event.data).target
            if layout.bead_key(target) not in with_rows:
                report.add(
                    event,
                    "edge.dangling-target",
                    "degraded",
                    member="/data/target",
                    stored_as=f"{BEAD_EDGES}.to_id",
                    detail=f"{target.id} has no beads row in the store after this load",
                )
    return LoadPlan(
        mode=mode,
        transactions=tuple(tx for tx in transactions if not tx.empty or tx.events),
        report=report,
        skipped=skipped,
    )


def _replay_order(
    pending: list[_Timed], order: Sequence[str] | None, report: LossReport
) -> list[_Timed]:
    by_id = {t.event.id: t for t in pending}
    if order is None:
        return sorted(pending, key=lambda t: _replay_key(t.event))
    known = {event.id for event in report.batch}
    ordered: list[_Timed] = []
    previous: tuple[int, str, int] | None = None
    seen: set[str] = set()
    for position, event_id in enumerate(order, start=1):
        if event_id not in known:
            raise MonotoneOrderError(
                f"index/recorded.jsonl line {position} names {event_id}, which is not in the load"
            )
        if event_id in seen:
            raise MonotoneOrderError(f"index/recorded.jsonl lists {event_id} twice")
        seen.add(event_id)
        timed = by_id.get(event_id)
        if timed is None:
            continue  # dropped or already in the store
        current = instant(timed.event.recorded_at)
        if previous is not None and current < previous:
            raise MonotoneOrderError(
                f"index/recorded.jsonl is out of recorded-time order at line {position} "
                f"({event_id}, recorded_at {timed.event.recorded_at}); a backfill transaction "
                "earlier than the previous one would abort and end recorded-time preservation"
            )
        previous = current
        ordered.append(timed)
    missing = sorted(set(by_id) - seen)
    if missing:
        raise MonotoneOrderError(f"index/recorded.jsonl does not list {missing[0]}")
    return ordered


def _group_sort_key(planner: _Planner, timed: _Timed) -> tuple[BeadRef, OrderKey]:
    event = timed.event
    if event.kind is EventKind.CHECKPOINT and event.id in planner.seeding:
        return (event.bead, checkpoint_key(event))
    return (event.bead, ordering_key(event))


def _number(planner: _Planner, members: list[_Timed], base_of: Any) -> list[tuple[_Timed, int]]:
    """``k`` for each event: its position among earlier writes of the same entity at the same
    instant (zero-based, format ADR A5)."""
    counts: Counter[tuple[Entity, datetime]] = Counter()
    out: list[tuple[_Timed, int]] = []
    for timed in members:
        base = base_of(timed)
        names = planner.entities(timed.event)
        k = max((counts[(name, base)] for name in names), default=0)
        for name in names:
            counts[(name, base)] += 1
        out.append((timed, k))
    return out


def _base(planner: _Planner, timed: _Timed) -> datetime:
    value = timed.occurred or timed.recorded
    return _dt(value) if value is not None else planner.now


def _backfill_transactions(
    planner: _Planner, backfill: list[_Timed]
) -> list[tuple[Transaction, list[tuple[_Timed, int]]]]:
    groups: list[tuple[datetime, list[_Timed]]] = []
    for timed in backfill:
        moment = _dt(timed.recorded)  # type: ignore[arg-type]
        if groups and groups[-1][0] == moment:
            groups[-1][1].append(timed)
        else:
            groups.append((moment, [timed]))
    out: list[tuple[Transaction, list[tuple[_Timed, int]]]] = []
    for index, (moment, members) in enumerate(groups):
        members.sort(key=lambda t: _group_sort_key(planner, t))
        numbered = _number(planner, members, lambda t: _base(planner, t))
        depth = max(k for _, k in numbered)
        if depth and index + 1 < len(groups):
            following = groups[index + 1][0]
            if moment + depth * MICROSECOND >= following:
                raise SplitPreconditionError(
                    f"the same-instant split of the recorded_at {_iso(moment)} group needs "
                    f"{depth} µs, which reaches the next recorded_at {_iso(following)}; the "
                    "replay would no longer be strictly increasing (format ADR A5)"
                )
        for k in range(depth + 1):
            tx = Transaction(moment + k * MICROSECOND, BACKFILL if k == 0 else SPLIT)
            out.append((tx, [(t, kk) for t, kk in numbered if kk == k]))
    return out


def _wall_clock_transactions(
    planner: _Planner, members: list[_Timed], phase: str
) -> list[tuple[Transaction, list[tuple[_Timed, int]]]]:
    numbered = _number(planner, members, lambda t: _base(planner, t))
    out = []
    for start in range(0, len(numbered), planner.batch_size):
        out.append((Transaction(None, phase), numbered[start : start + planner.batch_size]))
    return out


def _check_monotone(
    plan_txs: list[tuple[Transaction, list[tuple[_Timed, int]]]], now: datetime | None
) -> None:
    previous: datetime | None = None
    wall_clock = False
    for tx, _ in plan_txs:
        if tx.system_time is None:
            wall_clock = True
            continue
        if wall_clock:
            raise MonotoneOrderError(
                "a backfill transaction is planned after a wall-clock one, which ends backfill"
            )
        if previous is not None and tx.system_time <= previous:
            raise MonotoneOrderError(
                f"backfill transaction at {_iso(tx.system_time)} is not strictly after "
                f"{_iso(previous)}"
            )
        previous = tx.system_time
    if now is not None and previous is not None and previous > now:
        raise FutureRecordedTimeError(
            f"recorded_at {_iso(previous)} is later than the wall clock ({_iso(now)}); XTDB "
            "would hide those rows from default reads until then (bh-3jp2g S11e)"
        )


def _effective(
    planner: _Planner, timed: _Timed, k: int, tx: Transaction
) -> tuple[BeadEvent, datetime]:
    """The event as written (shifted times) and its valid instant; reports the time losses."""
    event, report = timed.event, planner.report
    load_time = timed.occurred is None and timed.recorded is None
    valid = _base(planner, timed) + k * MICROSECOND
    if event.occurred_at is None:
        report.add(
            event,
            "valid-time.missing",
            "reassigned",
            member="/occurred_at",
            stored_as=None if load_time else _iso(valid),
            detail=(
                f"occurred_at is null: valid from the load time (+{k} µs)"
                if load_time
                else "occurred_at is null: valid from recorded_at"
            ),
        )
    if event.occurred_precision is not None:
        report.add(
            event,
            "precision.lost",
            "degraded",
            member="/occurred_precision",
            stored_as=JOURNAL_STORED_AS,
            detail=f"_valid_from is an instant; the {event.occurred_precision} interval is lost",
        )
    if k and event.occurred_at is not None:
        report.add(
            event,
            US_SPLIT,
            "approximated",
            member="/occurred_at",
            stored_as=_iso(valid),
            detail=f"same-instant write {k} of its entity: valid time moved by {k} µs",
        )
    recorded = timed.recorded
    if tx.system_time is not None:
        recorded = _iso(tx.system_time)
        if k:
            report.add(
                event,
                US_SPLIT,
                "approximated",
                member="/recorded_at",
                stored_as=recorded,
                detail=f"same-instant write {k} of its entity: its own transaction {k} µs later",
            )
    elif event.recorded_at is None:
        report.add(
            event,
            "recorded-time.null",
            "reassigned",
            member="/recorded_at",
            stored_as=None,
            detail="recorded_at is null: loaded after the backfill in a wall-clock transaction",
        )
    else:
        report.add(
            event,
            "recorded-time.reassigned",
            "reassigned",
            member="/recorded_at",
            stored_as=None,
            detail=(
                "incremental load: a wall-clock transaction; recorded time is preserved only by "
                "the initial load into a fresh database"
            ),
        )
    return _with_times(event, _iso(valid), recorded), valid


def _erase_transactions(
    planner: _Planner, tombstones: list[BeadEvent], store_events: list[BeadEvent]
) -> list[Transaction]:
    layout = planner.layout
    erase_tx = Transaction(None, ERASE)
    rewrite_tx = Transaction(None, REWRITE)
    erased_ids, erased_beads = erasure(tombstones)
    for tombstone in tombstones:
        if tombstone.data.get("scope") == "bead":
            key = layout.bead_key(tombstone.bead)
            erase_tx.erases += [
                Erase(BEADS, "_id", (key,)),
                Erase(BEAD_LABELS, "bead_id", (key,)),
                Erase(BEAD_EDGES, "from_id", (key,)),
                Erase(BEAD_COMMENTS, "bead_id", (key,)),
                Erase(JOURNAL, "bead_id", (key,), keep_tombstones=True),
            ]
        else:
            ids = tuple(sorted(tombstone.data.get("events") or ()))
            if ids:
                erase_tx.erases += [Erase(BEAD_COMMENTS, "_id", ids), Erase(JOURNAL, "_id", ids)]

    # Content written by earlier loads: erase the rows it shaped, rewrite them without it.
    inbound: Counter[str] = Counter()
    affected: dict[BeadRef, set[Entity]] = defaultdict(set)
    for event in store_events:
        if event.kind is EventKind.TOMBSTONE:
            continue
        if event.bead in erased_beads:
            planner.live.get(event.bead, {}).pop(event.id, None)
        elif event.id in erased_ids:
            affected[event.bead].update(planner.entities(event))
            planner.live.get(event.bead, {}).pop(event.id, None)
    for bead in erased_beads:
        key = layout.bead_key(bead)
        planner.timelines[bead] = {}
        for timelines in planner.timelines.values():
            for (table, entity), segments in timelines.items():
                live_edge = bool(segments) and segments[-1][2] is not None
                if table == BEAD_EDGES and entity.endswith(f"|{key}") and live_edge:
                    inbound[key] += 1
    for bead, names in sorted(affected.items()):
        old = planner.timelines.get(bead, {})
        new = planner.recompute(bead, None)
        for table, entity in sorted(names):
            erase_tx.erases.append(Erase(table, "_id", (entity,)))
        planner.emit(rewrite_tx, {}, new, only=names)
        planner.emit(rewrite_tx, old, new, only=set(new) - names)
        planner.timelines[bead] = new
    rewritten = sum(len(names) for names in affected.values())
    for tombstone in tombstones:
        detail = ERASE_LOGICAL_DETAIL
        if tombstone.data.get("scope") == "bead":
            dangling = inbound[layout.bead_key(tombstone.bead)]
            if dangling:
                detail += (
                    f"; {dangling} inbound edge row(s) of other beads still point at the erased "
                    "bead (edge.dangling-target)"
                )
        if rewritten:
            detail += (
                f"; {rewritten} row(s) shaped by erased events were erased and rewritten at load "
                "time, so their recorded time is not preserved"
            )
        planner.report.add(
            tombstone, "erasure.incomplete", "degraded", stored_as="ERASE (logical)", detail=detail
        )
    return [tx for tx in (erase_tx, rewrite_tx) if not tx.empty]


__all__ = [
    "BACKFILL",
    "DEFAULT_LOAD_BATCH",
    "ERASE",
    "INCREMENTAL",
    "JOURNAL",
    "LOADER_TABLES",
    "REWRITE",
    "SPLIT",
    "TAIL",
    "DeletePortion",
    "Erase",
    "FutureRecordedTimeError",
    "LayoutMismatchError",
    "LoadAborted",
    "LoadMode",
    "LoadPlan",
    "LoadRefused",
    "MonotoneOrderError",
    "SplitPreconditionError",
    "TargetNotFreshError",
    "Transaction",
    "column_ok",
    "first_recorded_at",
    "plan_load",
    "truncate_timestamp",
]
