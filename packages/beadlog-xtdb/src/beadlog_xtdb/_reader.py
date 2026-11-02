"""The as-of reader: layout (b) in XTDB back out as BeadLog events (format ADR amendment A6).

The reader is the inverse of the loader. It reads the four layout (b) tables at a cut, or their
whole history, and emits BeadLog events with ``provenance: observed`` and ``source.system:
xtdb``, so a store can be a ``reconcile`` baseline, be exported as a ``.bdlg``, or prove a
round trip by folding to the same state the loaded log folds to.

Two reads, both through plain ``SELECT``\\ s that need no loader state:

- :func:`read_state` ``FOR VALID_TIME AS OF`` a valid-time cut and/or ``FOR SYSTEM_TIME AS OF`` a
  recorded-time cut (the current belief when neither is given). Every bead present at the cut
  becomes one ``create`` carrying its whole document, plus the ``phase`` / ``retire`` / ``delete``
  that document implies, a ``label_add`` per held label, an ``edge_add`` per held edge and a
  ``comment`` per comment.
- :func:`read_history` ``FOR ALL SYSTEM_TIME FOR ALL VALID_TIME``, reduced to what the store
  currently believes (or believed at ``system_at``): each bead document version is diffed against
  the previous one into ``create`` / ``set`` / ``phase`` / ``retire`` / ``delete``, each label and
  edge period into an add and, when it ended, a remove.

Times. ``occurred_at`` is the row's ``_valid_from`` (a remove's is the period's ``_valid_to``);
``recorded_at`` is its ``_system_from``, the instant the store first believed it. A removal is
recorded when the ``[_valid_to, inf)`` rectangle it closed stopped being believed
(``_system_to``). A time XTDB does not hold stays ``null``; the reader never invents one.

Same-instant events. A ``create`` and the ``phase`` / ``retire`` / ``delete`` of the same document
version share its ``_valid_from`` and a facet, and the fold breaks such ties by event id, which is
not causal. The reader knows the causal order, so those follow the ``create`` by one microsecond
(the shape of the loader's same-instant split, A5), the stored time is kept in the event's
``annotations`` and ``x-xtdb.us-split`` is reported. Nothing else is shifted.

What a read cannot give back is reported in :attr:`ReadResult.notes`, never dropped silently:
a comment's original event id (a comment is an event whose id hashes its source, so a re-read
comment has a new id; the stored id is kept in ``annotations``), a comment without a body, a
field whose value BeadLog cannot hold (a non-integer number) or whose name is not a BeadLog
field name, an edge's ``metadata``, and orphans (a label or edge whose bead is not in the read).

The layout is read, not guessed: a store loaded over several groups spells every key
``<group_id>:<bead_id>`` (:class:`~beadlog_xtdb.Layout`), and the reader detects that from the
bead rows. The bead's own ``group_id`` column carries its group; a single-group store spells an
edge target with the source bead's group.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from importlib import metadata
from typing import Any

from beadlog import (
    MAX_SAFE_INTEGER,
    BeadEvent,
    BeadRef,
    Generator,
    Group,
    Manifest,
    TimestampError,
    fold,
    format_timestamp,
    mint_event,
    ordering_key,
    write_archive,
)

from ._connect import check_table
from ._layout import (
    BEAD_COMMENTS,
    BEAD_EDGES,
    BEAD_LABELS,
    BEADS,
    DecodedBead,
    Layout,
    LayoutError,
    decode_bead_row,
    split_key,
)
from ._transit import Instant, TransitError

SOURCE_SYSTEM = "xtdb"
GENERATOR_NAME = "beadlog-xtdb"
ALL_SYSTEM_TIME = "FOR ALL SYSTEM_TIME"
ALL_VALID_TIME = "FOR ALL VALID_TIME"
# Period and system columns appended to ``*`` (``SELECT *`` omits them).
PERIOD_COLUMNS = ("_valid_from", "_valid_to", "_system_from", "_system_to")
SELECT_LIST = "*, " + ", ".join(PERIOD_COLUMNS)

_FIELD_NAME = re.compile(r"^[a-z][a-z0-9_]*(\.[A-Za-z0-9_-]+)*$")
_RESERVED_FIELDS = frozenset(
    {"id", "status", "phase", "labels", "dependencies", "edges", "comments", "metadata"}
    | {"retired", "deleted"}
)
_GROUP_DIR = re.compile(r"[^a-z0-9._-]+")
_OPEN_PHASE = "open"


class ReaderError(ValueError):
    """A read that cannot produce a faithful BeadLog (an unreadable cut or a malformed row)."""


@dataclass(frozen=True)
class ReadNote:
    """One thing a read could not give back exactly, as a loss-report-shaped entry."""

    code: str
    detail: str
    bead: BeadRef | None = None

    def __str__(self) -> str:
        where = f" [{self.bead.group}/{self.bead.id}]" if self.bead is not None else ""
        return f"{self.code}{where}: {self.detail}"


@dataclass(frozen=True)
class ReadResult:
    """The events (and, optionally, derived checkpoints) of one read, plus what it lost."""

    mode: str
    events: tuple[BeadEvent, ...]
    checkpoints: tuple[BeadEvent, ...] = ()
    notes: tuple[ReadNote, ...] = ()
    multi_group: bool = False
    extracted_at: str = ""

    @property
    def all_events(self) -> tuple[BeadEvent, ...]:
        """Events then checkpoints: what :func:`write_baseline` archives."""
        return (*self.events, *self.checkpoints)

    @property
    def groups(self) -> tuple[str, ...]:
        """Every group id a bead of the read belongs to, sorted."""
        return tuple(sorted({event.bead.group for event in self.all_events}))

    def note_codes(self) -> dict[str, int]:
        """``{code: count}`` of :attr:`notes`."""
        counts: dict[str, int] = defaultdict(int)
        for note in self.notes:
            counts[note.code] += 1
        return dict(sorted(counts.items()))


# -- reading rows --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Row:
    """One stored row: its document and its valid and system periods."""

    key: str
    doc: Mapping[str, Any]
    valid_from: datetime
    valid_to: datetime | None
    system_from: datetime | None
    system_to: datetime | None


def as_datetime(value: str | datetime | Instant) -> datetime:
    """An aware ``datetime`` for a cut given as a canonical BeadLog timestamp or a datetime."""
    if isinstance(value, Instant):
        return value.at
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ReaderError(f"a cut needs an aware datetime, got naive {value!r}")
        return value
    try:
        return Instant.parse(value).at
    except TransitError as exc:
        raise ReaderError(f"not a canonical timestamp: {value!r}") from exc


def select_statement(
    table: str, *, valid_at: datetime | None, system_at: datetime | None, history: bool
) -> tuple[str, list[datetime]]:
    """The ``SELECT`` for one table and its parameters.

    ``history`` reads every version on both axes; otherwise the temporal clauses are the ones
    given (``FOR SYSTEM_TIME AS OF`` then ``FOR VALID_TIME AS OF``, the order XTDB 2.2 accepts),
    and none at all reads the current belief.
    """
    clauses: list[str] = []
    params: list[datetime] = []
    if history:
        clauses += [ALL_SYSTEM_TIME, ALL_VALID_TIME]
    else:
        if system_at is not None:
            clauses.append("FOR SYSTEM_TIME AS OF %s")
            params.append(system_at)
        if valid_at is not None:
            clauses.append("FOR VALID_TIME AS OF %s")
            params.append(valid_at)
    text = f"SELECT {SELECT_LIST} FROM {check_table(table)}"
    return (text + " " + " ".join(clauses)).rstrip(), params


def _fetch(connection: Any, table: str, **cut: Any) -> list[_Row]:
    sql, params = select_statement(table, **cut)
    cursor = connection.execute(sql, params)
    names = [column.name for column in cursor.description]
    rows: list[_Row] = []
    for values in cursor.fetchall():
        record = dict(zip(names, values, strict=True))
        period = {name: record.pop(name, None) for name in PERIOD_COLUMNS}
        key = record.pop("_id", None)
        if not isinstance(key, str) or period["_valid_from"] is None:
            raise ReaderError(f"{table}: a row needs _id and _valid_from, got {sorted(record)}")
        rows.append(
            _Row(
                key,
                record,
                period["_valid_from"],
                period["_valid_to"],
                period["_system_from"],
                period["_system_to"],
            )
        )
    return rows


def _belief(rows: Iterable[_Row], system_at: datetime | None) -> list[_Row]:
    """The rows the store believes now, or believed at ``system_at``."""
    if system_at is None:
        return [row for row in rows if row.system_to is None]
    return [
        row
        for row in rows
        if row.system_from is not None
        and row.system_from <= system_at
        and (row.system_to is None or row.system_to > system_at)
    ]


def _closers(
    rows: Iterable[_Row], system_at: datetime | None
) -> dict[tuple[str, datetime], datetime]:
    """``{(key, t): when}``: a period ended at ``t`` was recorded at ``when``.

    A removal (``DELETE ... FOR PORTION OF VALID_TIME FROM t TO NULL``) leaves the believed
    period ``[from, t)`` with its original ``_system_from`` and the rectangle ``[t, inf)``
    closed in system time; that rectangle's ``_system_to`` is the removal's recorded time.
    """
    found: dict[tuple[str, datetime], datetime] = {}
    for row in rows:
        if row.valid_to is not None or row.system_to is None:
            continue
        if system_at is not None and row.system_to > system_at:
            continue
        slot = (row.key, row.valid_from)
        if slot not in found or row.system_to < found[slot]:
            found[slot] = row.system_to
    return found


# -- values --------------------------------------------------------------------------------


def _json_value(value: Any) -> Any:
    """``value`` as BeadLog's admissible JSON domain, or raise ``ValueError``.

    A timestamp becomes its canonical string, a set a sorted list; a non-integer number, an
    integer outside the I-JSON safe range and anything exotic are refused (the caller reports it).
    """
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        if abs(value) > MAX_SAFE_INTEGER:
            raise ValueError(f"integer {value} is outside the I-JSON safe range")
        return value
    if isinstance(value, float):
        raise ValueError(f"non-integer number {value!r}")
    if isinstance(value, datetime):
        return format_timestamp(value)
    if isinstance(value, (set, frozenset)):
        items = [_json_value(item) for item in value]
        return sorted(items, key=repr)
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, Mapping):
        return {str(name): _json_value(item) for name, item in value.items()}
    raise ValueError(f"{type(value).__name__} has no JSON form")


def _stamp(moment: datetime | None, shift: int = 0) -> str | None:
    if moment is None:
        return None
    try:
        return format_timestamp(moment + timedelta(microseconds=shift) if shift else moment)
    except TimestampError as exc:  # a naive datetime; XTDB sends aware ones
        raise ReaderError(str(exc)) from exc


# -- the reader ----------------------------------------------------------------------------


class _Emitter:
    """Mints the events of one read and collects its notes."""

    def __init__(
        self,
        extracted_at: str,
        layout: Layout,
        group: str | None,
        groups: frozenset[str] | None,
    ) -> None:
        self.extracted_at = extracted_at
        self.layout = layout
        self.group = group
        self.groups = groups
        self.events: list[BeadEvent] = []
        self.notes: list[ReadNote] = []
        self.refs: dict[str, BeadRef] = {}

    # -- identity

    def ref_for(self, key: str, *, sibling: BeadRef | None = None) -> BeadRef | None:
        """The bead a layout key names: from a bead row if read, else from the key's spelling."""
        known = self.refs.get(key)
        if known is not None:
            return known
        if self.layout.multi_group:
            group, ident = split_key(key)
            group = group or (sibling.group if sibling else self.group)
        else:
            group, ident = (sibling.group if sibling else self.group), key
        return BeadRef(group, ident) if group else None

    def note(self, code: str, detail: str, bead: BeadRef | None = None) -> None:
        self.notes.append(ReadNote(code, detail, bead))

    # -- minting

    def mint(
        self,
        kind: str,
        bead: BeadRef,
        data: Mapping[str, Any],
        *,
        occurred: datetime | None,
        recorded: datetime | None,
        ref: str,
        actor: str | None = None,
        shift: int = 0,
        annotations: Mapping[str, Any] | None = None,
    ) -> BeadEvent:
        notes: dict[str, Any] = dict(annotations or {})
        if shift and occurred is not None:
            notes["us_split"] = shift
            notes["valid_from"] = _stamp(occurred)
            self.note(
                "x-xtdb.us-split",
                f"{kind} at {_stamp(occurred)} read as {_stamp(occurred, shift)} so it orders "
                f"after the {shift} same-instant event(s) before it",
                bead,
            )
        event = mint_event(
            kind,
            bead,
            data,
            occurred_at=_stamp(occurred, shift),
            recorded_at=_stamp(recorded),
            extracted_at=self.extracted_at,
            source=(SOURCE_SYSTEM, ref),
            provenance="observed",
            actor=actor,
            annotations={SOURCE_SYSTEM: notes} if notes else None,
        )
        self.events.append(event)
        return event

    # -- beads

    def decode(self, row: _Row) -> DecodedBead | None:
        """The row as bead state with a BeadLog-clean field set, or ``None`` if unusable."""
        try:
            decoded = decode_bead_row({"_id": row.key, **row.doc})
        except LayoutError as exc:
            raise ReaderError(f"beads/{row.key}: {exc}") from exc
        if self.groups is not None and decoded.bead.group not in self.groups:
            return None
        self.refs[row.key] = bead = decoded.bead
        clean: dict[str, Any] = {}
        for name, value in decoded.fields.items():
            if not _FIELD_NAME.fullmatch(name) or name in _RESERVED_FIELDS:
                self.note("field.unsupported", f"{name!r} is not a BeadLog field name", bead)
                continue
            try:
                clean[name] = _json_value(value)
            except ValueError as exc:
                self.note("field.unsupported", f"{name!r}: {exc}", decoded.bead)
        retired = decoded.retired
        if retired is not None:
            try:
                retired = _json_value(retired)
            except ValueError as exc:
                self.note("field.unsupported", f"retired: {exc}", decoded.bead)
                retired = {}
        return DecodedBead(
            bead=decoded.bead,
            type=decoded.type,
            phase=decoded.phase,
            fields=clean,
            retired=retired,
            deleted=decoded.deleted,
        )

    def version(self, row: _Row, previous: DecodedBead | None, current: DecodedBead) -> None:
        """Events that take ``previous`` (``None``: the bead did not exist) to ``current``."""
        bead = current.bead
        when, recorded = row.valid_from, row.system_from
        stem = f"{BEADS}/{row.key}/{_stamp(when)}"

        def tie(kind: str, data: Mapping[str, Any], suffix: str) -> None:
            # Only a (re)create shares a facet with the events that follow it (existence and
            # phase), so those and only those are moved one microsecond later.
            self.mint(
                kind,
                bead,
                data,
                occurred=when,
                recorded=recorded,
                ref=f"{stem}/{suffix}",
                shift=1 if creating and kind != "create" else 0,
            )

        creating = previous is None or (previous.deleted and not current.deleted)
        if creating:
            if current.type is None:
                self.note("create.type-missing", "the bead row has no issue_type", bead)
            else:
                if not current.fields.get("title"):
                    self.note("create.title-missing", "the bead row has no title", bead)
                tie("create", {"type": current.type, "fields": dict(current.fields)}, "create")
            self._lifecycle(tie, None, current)
        else:
            assert previous is not None
            if current.type != previous.type and current.type is not None:
                self.mint(
                    "set",
                    bead,
                    {"field": "type", "value": current.type},
                    occurred=when,
                    recorded=recorded,
                    ref=f"{stem}/set:type",
                )
            for name in sorted(set(previous.fields) | set(current.fields)):
                before, after = previous.fields.get(name), current.fields.get(name)
                if before != after:
                    self.mint(
                        "set",
                        bead,
                        {"field": name, "value": after},
                        occurred=when,
                        recorded=recorded,
                        ref=f"{stem}/set:{name}",
                    )
            self._lifecycle(tie, previous, current)
        if current.deleted and (previous is None or not previous.deleted):
            tie("delete", {}, "delete")

    @staticmethod
    def _lifecycle(tie: Any, previous: DecodedBead | None, current: DecodedBead) -> None:
        """The phase or retire event a version implies (a ``create`` already means ``open``)."""
        was_phase = previous.phase if previous is not None else _OPEN_PHASE
        was_retired = previous.retired if previous is not None else None
        if current.retired is not None:
            if previous is None or current.retired != was_retired or was_phase != "retired":
                tie("retire", dict(current.retired), "retire")
        elif current.phase is not None and current.phase != was_phase:
            tie("phase", {"phase": current.phase}, "phase")

    def vanished(self, bead: BeadRef, key: str, at: datetime, recorded: datetime | None) -> None:
        """A bead document whose valid period ended: it no longer exists from ``at``."""
        self.mint(
            "delete",
            bead,
            {},
            occurred=at,
            recorded=recorded,
            ref=f"{BEADS}/{key}/{_stamp(at)}/vanish",
        )

    # -- labels, edges, comments

    def label(self, row: _Row, *, add: bool, at: datetime, recorded: datetime | None) -> None:
        owner = self._owner(BEAD_LABELS, row)
        label = row.doc.get("label")
        if owner is None or not isinstance(label, str):
            return
        kind = "label_add" if add else "label_remove"
        self.mint(
            kind,
            owner,
            {"label": label},
            occurred=at,
            recorded=recorded,
            ref=f"{BEAD_LABELS}/{row.key}/{_stamp(at)}/{kind}",
        )

    def edge(self, row: _Row, *, add: bool, at: datetime, recorded: datetime | None) -> None:
        source = self._owner(BEAD_EDGES, row, column="from_id")
        kind, target_key = row.doc.get("type"), row.doc.get("to_id")
        if source is None or not isinstance(kind, str) or not isinstance(target_key, str):
            return
        target = self.ref_for(target_key, sibling=source)
        if target is None:
            self.note("edge.orphan", f"{row.key}: no group for target {target_key!r}", source)
            return
        if add and row.doc.get("metadata") is not None:
            self.note("x-xtdb.edge-metadata", f"{row.key}: edge metadata has no event form", source)
        actor = row.doc.get("created_by") if add else None
        self.mint(
            "edge_add" if add else "edge_remove",
            source,
            {"type": kind, "target": target.to_json()},
            occurred=at,
            recorded=recorded,
            ref=f"{BEAD_EDGES}/{row.key}/{_stamp(at)}/{'add' if add else 'remove'}",
            actor=actor if isinstance(actor, str) and actor else None,
        )

    def comment(self, row: _Row) -> None:
        owner = self._owner(BEAD_COMMENTS, row)
        if owner is None:
            return
        text = row.doc.get("text")
        if not isinstance(text, str):
            self.note("comment.body-missing", f"{row.key}: a comment without a body", owner)
            return
        author = row.doc.get("author")
        self.mint(
            "comment",
            owner,
            {"body": text},
            occurred=row.valid_from,
            recorded=row.system_from,
            ref=f"{BEAD_COMMENTS}/{row.key}",
            actor=author if isinstance(author, str) and author else None,
            annotations={"comment_id": row.key},
        )

    def _owner(self, table: str, row: _Row, *, column: str = "bead_id") -> BeadRef | None:
        key = row.doc.get(column)
        owner = self.refs.get(key) if isinstance(key, str) else None
        if owner is None and self.groups is None:
            self.note("row.orphan", f"{table}/{row.key}: its bead {key!r} is not in the read")
        return owner


def detect_layout(rows: Iterable[_Row]) -> Layout:
    """Multi-group when any bead row's ``_id`` differs from its plain ``bead_id``."""
    return Layout(multi_group=any(row.doc.get("bead_id") not in (None, row.key) for row in rows))


def _beads_by_key(rows: Iterable[_Row]) -> dict[str, list[_Row]]:
    grouped: dict[str, list[_Row]] = defaultdict(list)
    for row in rows:
        grouped[row.key].append(row)
    for versions in grouped.values():
        versions.sort(key=lambda item: item.valid_from)
    return grouped


def _periods(rows: Sequence[_Row]) -> list[tuple[_Row, datetime | None]]:
    """A key's believed rows as ``(first row, end)`` periods; touching rows are one period."""
    ordered = sorted(rows, key=lambda item: item.valid_from)
    periods: list[tuple[_Row, datetime | None]] = []
    for row in ordered:
        if periods and periods[-1][1] is not None and periods[-1][1] == row.valid_from:
            periods[-1] = (periods[-1][0], row.valid_to)
        else:
            periods.append((row, row.valid_to))
    return periods


def _group_set(groups: Iterable[str] | None) -> frozenset[str] | None:
    return frozenset(groups) if groups is not None else None


def read_state(
    connection: Any,
    *,
    extracted_at: str,
    valid_at: str | datetime | Instant | None = None,
    system_at: str | datetime | Instant | None = None,
    group: str | None = None,
    groups: Iterable[str] | None = None,
    layout: Layout | None = None,
    checkpoints: bool = False,
) -> ReadResult:
    """The store's state at a cut, as events that fold to that state.

    ``valid_at`` and ``system_at`` are the two ``AS OF`` cuts (either, both or neither: the
    current belief). ``extracted_at`` is the extractor clock stamped on every event; it is a
    parameter so that a read of the same store is byte-for-byte reproducible. ``groups`` keeps only
    the beads of those group ids (and the rows that belong to them; a row of another group is
    skipped without a note); ``group`` is the fallback group for a key no bead row explains;
    ``layout`` overrides the detected id spelling. ``checkpoints=True`` adds one derived
    ``checkpoint`` per bead asserting its state.
    """
    cut = {
        "valid_at": as_datetime(valid_at) if valid_at is not None else None,
        "system_at": as_datetime(system_at) if system_at is not None else None,
        "history": False,
    }
    beads = _fetch(connection, BEADS, **cut)
    emitter = _Emitter(extracted_at, layout or detect_layout(beads), group, _group_set(groups))
    for row in sorted(beads, key=lambda item: (item.key, item.valid_from)):
        current = emitter.decode(row)
        if current is not None:
            emitter.version(row, None, current)
    for row in sorted(_fetch(connection, BEAD_LABELS, **cut), key=lambda item: item.key):
        emitter.label(row, add=True, at=row.valid_from, recorded=row.system_from)
    for row in sorted(_fetch(connection, BEAD_EDGES, **cut), key=lambda item: item.key):
        emitter.edge(row, add=True, at=row.valid_from, recorded=row.system_from)
    for row in sorted(_fetch(connection, BEAD_COMMENTS, **cut), key=lambda item: item.key):
        emitter.comment(row)
    return _result("state", emitter, checkpoints)


def read_history(
    connection: Any,
    *,
    extracted_at: str,
    system_at: str | datetime | Instant | None = None,
    group: str | None = None,
    groups: Iterable[str] | None = None,
    layout: Layout | None = None,
    checkpoints: bool = False,
) -> ReadResult:
    """The store's whole history as events (``FOR ALL SYSTEM_TIME FOR ALL VALID_TIME``).

    What is read is the store's belief (now, or as of ``system_at``): superseded rows serve only
    to date a removal. A bead's document versions are diffed in valid-time order, a document
    whose period ends with no successor is a ``delete`` at its end, and a label or edge period
    is an add and, when it ended, a remove.
    """
    cut_system = as_datetime(system_at) if system_at is not None else None
    cut = {"valid_at": None, "system_at": None, "history": True}
    everything = {
        table: _fetch(connection, table, **cut)
        for table in (BEADS, BEAD_LABELS, BEAD_EDGES, BEAD_COMMENTS)
    }
    believed = {table: _belief(rows, cut_system) for table, rows in everything.items()}
    closers = {table: _closers(rows, cut_system) for table, rows in everything.items()}
    beads = believed[BEADS]
    emitter = _Emitter(extracted_at, layout or detect_layout(beads), group, _group_set(groups))

    for key, versions in sorted(_beads_by_key(beads).items()):
        previous: DecodedBead | None = None
        ended: datetime | None = None
        for row in versions:
            current = emitter.decode(row)
            if current is None:
                continue
            if previous is not None and ended is not None and ended < row.valid_from:
                emitter.vanished(previous.bead, key, ended, closers[BEADS].get((key, ended)))
                previous = None
            emitter.version(row, previous, current)
            previous, ended = current, row.valid_to
        if previous is not None and ended is not None:
            emitter.vanished(previous.bead, key, ended, closers[BEADS].get((key, ended)))

    for table, add_remove in ((BEAD_LABELS, emitter.label), (BEAD_EDGES, emitter.edge)):
        by_key: dict[str, list[_Row]] = defaultdict(list)
        for row in believed[table]:
            by_key[row.key].append(row)
        for key, rows in sorted(by_key.items()):
            for first, end in _periods(rows):
                add_remove(first, add=True, at=first.valid_from, recorded=first.system_from)
                if end is not None:
                    add_remove(first, add=False, at=end, recorded=closers[table].get((key, end)))
    for row in sorted(believed[BEAD_COMMENTS], key=lambda item: item.key):
        emitter.comment(row)
    return _result("history", emitter, checkpoints)


def _result(mode: str, emitter: _Emitter, checkpoints: bool) -> ReadResult:
    unique = {event.id: event for event in emitter.events}.values()
    events = sorted(unique, key=lambda e: (e.bead, ordering_key(e)))
    marks = tuple(_checkpoints(events, emitter)) if checkpoints else ()
    return ReadResult(
        mode=mode,
        events=tuple(events),
        checkpoints=marks,
        notes=tuple(emitter.notes),
        multi_group=emitter.layout.multi_group,
        extracted_at=emitter.extracted_at,
    )


def _checkpoints(events: Sequence[BeadEvent], emitter: _Emitter) -> Iterable[BeadEvent]:
    """One derived checkpoint per bead: its folded state, covering through its last event."""
    states = fold(events)
    last: dict[BeadRef, BeadEvent] = {}
    for event in events:
        if event.bead not in last or ordering_key(event) > ordering_key(last[event.bead]):
            last[event.bead] = event
    for bead, state in sorted(states.items()):
        if state.type is None or state.phase is None or bead not in last:
            continue
        covers = last[bead]
        yield mint_event(
            "checkpoint",
            bead,
            {
                "state": state.to_checkpoint_state(),
                "covers_through": {
                    "occurred_at": covers.occurred_at,
                    "recorded_at": covers.recorded_at,
                    "id": covers.id,
                },
            },
            occurred_at=covers.occurred_at,
            recorded_at=covers.recorded_at,
            extracted_at=emitter.extracted_at,
            source=(SOURCE_SYSTEM, f"checkpoint/{bead.group}/{bead.id}"),
            provenance="derived",
        )


# -- as a baseline .bdlg -------------------------------------------------------------------


def group_dirs(group_ids: Iterable[str]) -> list[Group]:
    """One :class:`~beadlog.Group` per id, with an archive-safe, unique ``dir``."""
    groups: list[Group] = []
    used: set[str] = set()
    for ident in sorted(set(group_ids)):
        stem = _GROUP_DIR.sub("-", ident.lower()).strip("-._") or "group"
        name, number = stem, 1
        while name in used:
            number += 1
            name = f"{stem}-{number}"
        used.add(name)
        groups.append(Group(ident, name))
    return groups


def generator() -> Generator:
    """The :class:`~beadlog.Generator` an exported archive names."""
    try:
        version = metadata.version(GENERATOR_NAME)
    except metadata.PackageNotFoundError:  # not installed (a source checkout on sys.path)
        version = "0"
    return Generator(GENERATOR_NAME, version)


def write_baseline(
    destination: Any,
    result: ReadResult,
    *,
    groups: Sequence[Group] | None = None,
    description: str | None = None,
    annotations: Mapping[str, Any] | None = None,
    index: bool = False,
) -> Manifest:
    """Write ``result`` as a ``.bdlg`` that ``beadlog verify`` accepts and ``reconcile`` can use
    as its baseline (``read_archive`` it, or pass the events straight to ``reconcile``).

    Groups default to one per group id in the read, with generated directory names.
    ``created_at`` is the read's ``extracted_at`` so the bytes depend only on the store's content.
    """
    chosen = list(groups) if groups is not None else group_dirs(result.groups)
    if not chosen:
        raise ReaderError("nothing to write: the read produced no events")
    return write_archive(
        destination,
        result.all_events,
        groups=chosen,
        generator=generator(),
        description=description,
        annotations=annotations,
        created_at=result.extracted_at,
        index=index,
    )


__all__ = [
    "ALL_SYSTEM_TIME",
    "ALL_VALID_TIME",
    "GENERATOR_NAME",
    "PERIOD_COLUMNS",
    "SELECT_LIST",
    "SOURCE_SYSTEM",
    "ReadNote",
    "ReadResult",
    "ReaderError",
    "as_datetime",
    "detect_layout",
    "generator",
    "group_dirs",
    "read_history",
    "read_state",
    "select_statement",
    "write_baseline",
]
