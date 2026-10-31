"""The neutral ``bd`` baseline strategy: one ``bd export`` read -> current-state BeadLog events.

BeadHive-free (``docs/design/backfill-platform-adr.md`` sections 2 and 5): it calls the ``bd``
binary once (``bd -C <root> export --all``, never a ``bd history`` loop) and turns every issue row
into the events whose fold is the row's current state — a ``create`` snapshot, a ``phase`` when
the bead is not open, a ``label_add`` per label, an ``edge_add`` per outgoing dependency, and a
``comment`` per comment. Times come from the row: ``created_at`` / ``closed_at`` / a
dependency's or comment's ``created_at`` as ``occurred_at`` where they say when the fact
happened, and ``updated_at`` as ``recorded_at`` (when the row last recorded the snapshot).

**Re-emitting loaded events.** A bd StoreLoader keeps the BeadLog identity of every event it
applied in the bead's ``metadata.bdlg`` document (format ADR section 9), one entry per facet
(:func:`facet_key`) holding :func:`envelope` — ``event_id``, ``occurred_at``, ``recorded_at`` and
the rest of the identity members (``data`` too, except for a ``set``, whose value is the field's
current value). This reader rebuilds each stored event, keeps it only when its recomputed id
equals the stored ``event_id`` and it agrees with the row, and emits native events for whatever
the stored ones do not explain. So the fold of the output always equals the row's current state,
and an event loaded into bd comes back with its original id.

Values: BeadLog identity material forbids non-integer numbers (format ADR section 4), so a float
in bd metadata is carried as its decimal string; metadata keys that are not BeadLog field-name
segments are skipped. Neither is a write: this module never mutates a store.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from beadlog import (
    MAX_SAFE_INTEGER,
    AdmissibleValueError,
    BeadEvent,
    BeadRef,
    BeadState,
    Edge,
    EventFormatError,
    EventKind,
    TimestampError,
    fold,
    format_timestamp,
    mint_event,
)

from .contracts import ExtractRequest

STRATEGY_NAME = "bd"
STRATEGY_VERSION = "0.1.0"
SOURCE_SYSTEM = "bd"
# The bd metadata key holding a loader's per-facet BeadLog envelopes (format ADR section 9).
METADATA_KEY = "bdlg"
# Detail key a loader writes beside the ``phase`` facet of a retired bead (format ADR section 9).
RETIRED_KEY = "retired"
METADATA_FIELD_PREFIX = "metadata."

# BeadLog field name -> the ``bd export`` column holding it. ``type`` is the create's ``type``.
FIELD_COLUMNS: Mapping[str, str] = {
    "title": "title",
    "description": "description",
    "design": "design",
    "acceptance_criteria": "acceptance_criteria",
    "notes": "notes",
    "priority": "priority",
    "assignee": "assignee",
    "owner": "owner",
    "external_ref": "external_ref",
    "due_at": "due_at",
    "defer_until": "defer_until",
    "estimated_minutes": "estimated_minutes",
    "spec_id": "spec_id",
}
TIMESTAMP_FIELDS = frozenset({"due_at", "defer_until"})
DEFAULT_TYPE = "task"
# Phases bd stores as ``closed``: a BeadLog ``retire`` lands as a close (``phase.approximated``).
CLOSED = "closed"
RETIRED = "retired"

_METADATA_KEY = re.compile(r"^[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)*$")
# Kinds whose stored envelope carries ``data``; a ``set`` takes the field's current value.
_DATA_KINDS = frozenset(
    {
        EventKind.CREATE,
        EventKind.CHECKPOINT,
        EventKind.LABEL_ADD,
        EventKind.LABEL_REMOVE,
        EventKind.EDGE_ADD,
        EventKind.EDGE_REMOVE,
        EventKind.PHASE,
        EventKind.RETIRE,
        EventKind.COMMENT,
    }
)
_FACET_KINDS: Mapping[str, frozenset[EventKind]] = {
    "created": frozenset({EventKind.CREATE, EventKind.CHECKPOINT}),
    "phase": frozenset({EventKind.PHASE, EventKind.RETIRE}),
    "field": frozenset({EventKind.SET}),
    "label": frozenset({EventKind.LABEL_ADD, EventKind.LABEL_REMOVE}),
    "edge": frozenset({EventKind.EDGE_ADD, EventKind.EDGE_REMOVE}),
    "comment": frozenset({EventKind.COMMENT}),
}
_REBUILD_ERRORS = (
    AdmissibleValueError,
    EventFormatError,
    TimestampError,
    KeyError,
    TypeError,
    ValueError,
)

Runner = Callable[[Sequence[str]], str]


class BdReadError(RuntimeError):
    """``bd export`` failed or printed something that is not issue JSONL."""


# -- values and times ----------------------------------------------------------------------


def bd_timestamp(value: Any) -> str | None:
    """A bd timestamp (RFC 3339, any offset) as a canonical BeadLog timestamp, or ``None``."""
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    return format_timestamp(moment)


def admissible(value: Any) -> Any:
    """``value`` within BeadLog's admissible domain: a non-integer number (or an integer outside
    the I-JSON safe range) becomes its decimal string, recursively."""
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int):
        return value if abs(value) <= MAX_SAFE_INTEGER else str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, Mapping):
        return {str(key): admissible(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [admissible(item) for item in value]
    return str(value)


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


# -- the stored envelope -------------------------------------------------------------------


def facet_key(event: BeadEvent) -> str | None:
    """The ``metadata.bdlg`` key holding ``event`` when it decides its facet (format ADR section
    9): ``created``, ``phase``, ``field:<name>``, ``label:<label>``,
    ``edge:<type>:<group>/<id>``, or ``comment:<event id>``. ``None`` for kinds bd cannot keep
    (``delete``, ``tombstone``)."""
    kind, data = event.kind, event.data
    if kind in (EventKind.CREATE, EventKind.CHECKPOINT):
        return "created"
    if kind is EventKind.SET:
        return f"field:{data['field']}"
    if kind in (EventKind.LABEL_ADD, EventKind.LABEL_REMOVE):
        return f"label:{data['label']}"
    if kind in (EventKind.EDGE_ADD, EventKind.EDGE_REMOVE):
        target = data["target"]
        return f"edge:{data['type']}:{target['group']}/{target['id']}"
    if kind in (EventKind.PHASE, EventKind.RETIRE):
        return "phase"
    if kind is EventKind.COMMENT:
        return f"comment:{event.id}"
    return None


def envelope(event: BeadEvent) -> dict[str, Any]:
    """What a loader stores under :func:`facet_key`: the event id, both times and every other
    identity member, so the event can be re-emitted verbatim. ``data`` is omitted for a ``set``
    (its value is the field's current value, recomputed and checked against the id on read)."""
    raw = event.to_json()
    out: dict[str, Any] = {
        "event_id": event.id,
        "kind": str(event.kind),
        "occurred_at": event.occurred_at,
        "recorded_at": event.recorded_at,
        "provenance": str(event.provenance),
        "source": {"system": event.source.system, "ref": event.source.ref},
    }
    for name in ("occurred_precision", "actor", "lamport", "prev"):
        if raw.get(name) is not None:
            out[name] = raw[name]
    if event.kind in _DATA_KINDS:
        out["data"] = raw["data"]
    return out


# -- one row's current state ---------------------------------------------------------------


@dataclass(frozen=True)
class RowComment:
    """One native bd comment."""

    id: str
    author: str | None
    body: str
    created_at: str | None


@dataclass
class RowState:
    """The current state of one bd issue row, in BeadLog terms."""

    bead: BeadRef
    type: str
    phase: str
    close_reason: str | None
    fields: dict[str, Any]
    labels: frozenset[str]
    edges: dict[Edge, str | None]  # edge -> the dependency's created_at
    comments: list[RowComment]
    created_at: str | None
    updated_at: str | None
    closed_at: str | None
    started_at: str | None
    created_by: str | None
    stored: Mapping[str, Any] = field(default_factory=dict)


def _field_value(name: str, value: Any) -> Any:
    if value is None or value == "":
        return None
    if name in TIMESTAMP_FIELDS:
        return bd_timestamp(value) or admissible(value)
    return admissible(value)


def row_state(row: Mapping[str, Any], group: str) -> RowState:
    """The :class:`RowState` of one ``bd export`` issue row."""
    ident = str(row["id"])
    bead = BeadRef(group, ident)
    fields: dict[str, Any] = {}
    for name, column in FIELD_COLUMNS.items():
        value = _field_value(name, row.get(column))
        if value is not None:
            fields[name] = value
    metadata = row.get("metadata")
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except json.JSONDecodeError:
            metadata = None
    stored: Mapping[str, Any] = {}
    if isinstance(metadata, Mapping):
        for key, value in metadata.items():
            if key == METADATA_KEY:
                stored = value if isinstance(value, Mapping) else {}
            elif _METADATA_KEY.fullmatch(str(key)) and value is not None:
                fields[f"{METADATA_FIELD_PREFIX}{key}"] = admissible(value)
    edges: dict[Edge, str | None] = {}
    for dep in row.get("dependencies") or ():
        if not isinstance(dep, Mapping) or dep.get("issue_id", ident) != ident:
            continue
        target, kind = dep.get("depends_on_id"), dep.get("type")
        if isinstance(target, str) and target and isinstance(kind, str) and kind:
            edges[Edge(kind, BeadRef(group, target))] = bd_timestamp(dep.get("created_at"))
    comments = [
        RowComment(
            str(item.get("id") or index),
            item.get("author") or None,
            str(item.get("text") or ""),
            bd_timestamp(item.get("created_at")),
        )
        for index, item in enumerate(row.get("comments") or ())
        if isinstance(item, Mapping)
    ]
    return RowState(
        bead=bead,
        type=str(row.get("issue_type") or DEFAULT_TYPE),
        phase=str(row.get("status") or "open"),
        close_reason=row.get("close_reason") or None,
        fields=fields,
        labels=frozenset(str(label) for label in row.get("labels") or ()),
        edges=edges,
        comments=comments,
        created_at=bd_timestamp(row.get("created_at")),
        updated_at=bd_timestamp(row.get("updated_at")) or bd_timestamp(row.get("created_at")),
        closed_at=bd_timestamp(row.get("closed_at")),
        started_at=bd_timestamp(row.get("started_at")),
        created_by=row.get("created_by") or None,
        stored=stored,
    )


def same_phase(folded: str | None, status: str) -> bool:
    """Whether a folded phase is what bd stores as ``status`` (``retired`` lands as closed)."""
    return folded == status or (folded == RETIRED and status == CLOSED)


def bd_filled_defaults(bead: BeadRef) -> dict[str, Any]:
    """The values ``bd import`` fills in for a row that omits them: ``priority`` 0, and the bead
    id as the title a loader writes for a create without one. A fold lacking such a field still
    agrees with a row holding its default, so a loaded create that omitted it is re-emitted."""
    return {"priority": 0, "title": bead.id}


def fields_agree(folded: Mapping[str, Any], row: RowState) -> bool:
    """Whether folded fields equal the row's, tolerating :func:`bd_filled_defaults`."""
    plain, actual = _plain(folded), dict(row.fields)
    for name, default in bd_filled_defaults(row.bead).items():
        if name not in plain and actual.get(name) == default:
            del actual[name]
    return bool(plain == actual)


def agrees(state: BeadState | None, row: RowState) -> bool:
    """Whether a fold equals the row's current state (comments compared separately)."""
    return (
        state is not None
        and state.exists
        and state.type == row.type
        and same_phase(state.phase, row.phase)
        and fields_agree(state.fields, row)
        and frozenset(state.labels) == row.labels
        and frozenset(state.edges) == frozenset(row.edges)
    )


# -- the reader ----------------------------------------------------------------------------


def _rebuild(row: RowState, facet: str, stored: Any, extracted_at: str) -> BeadEvent | None:
    """The stored event of one facet, or ``None`` when it is malformed or its recomputed id
    differs from the stored ``event_id`` (the row changed under it)."""
    if not isinstance(stored, Mapping):
        return None
    family = facet.split(":", 1)[0]
    try:
        kind = EventKind(stored["kind"])
        if kind not in _FACET_KINDS.get(family, frozenset()):
            return None
        if kind is EventKind.SET:
            name = facet[len("field:") :]
            value = row.type if name == "type" else row.fields.get(name)
            data: Any = {"field": name, "value": value}
        else:
            data = stored["data"]
        source = stored["source"]
        event = mint_event(
            str(kind),
            row.bead,
            data,
            occurred_at=stored.get("occurred_at"),
            recorded_at=stored.get("recorded_at"),
            extracted_at=extracted_at,
            source=(source["system"], source["ref"]),
            provenance=stored["provenance"],
            occurred_precision=stored.get("occurred_precision"),
            actor=stored.get("actor"),
            lamport=stored.get("lamport"),
            prev=stored.get("prev"),
        )
    except _REBUILD_ERRORS:
        return None
    if event.id != stored.get("event_id") or facet_key(event) != facet:
        return None
    return event


class _Native:
    """Mints the native (``source.system = bd``) events of one row."""

    def __init__(self, row: RowState, extracted_at: str) -> None:
        self.row = row
        self.extracted_at = extracted_at

    def __call__(
        self,
        kind: EventKind,
        suffix: str,
        data: Mapping[str, Any],
        *,
        occurred_at: str | None,
        recorded_at: str | None = None,
        actor: str | None = None,
    ) -> BeadEvent:
        return mint_event(
            str(kind),
            self.row.bead,
            data,
            occurred_at=occurred_at,
            recorded_at=recorded_at if recorded_at is not None else self.row.updated_at,
            extracted_at=self.extracted_at,
            source=(SOURCE_SYSTEM, f"issues/{self.row.bead.id}#{suffix}"),
            provenance="observed",
            actor=actor,
        )

    def create(self) -> BeadEvent:
        return self(
            EventKind.CREATE,
            "create",
            {"type": self.row.type, "fields": dict(sorted(self.row.fields.items()))},
            occurred_at=self.row.created_at,
            actor=self.row.created_by,
        )

    def phase(self) -> BeadEvent:
        row = self.row
        data: dict[str, Any] = {"phase": row.phase}
        if row.phase == CLOSED and row.close_reason:
            data["reason"] = str(row.close_reason)
        when = row.closed_at if row.phase == CLOSED else None
        if row.phase == "in_progress":
            when = row.started_at
        return self(EventKind.PHASE, "phase", data, occurred_at=when)

    def label(self, label: str) -> BeadEvent:
        return self(EventKind.LABEL_ADD, f"label:{label}", {"label": label}, occurred_at=None)

    def edge(self, edge: Edge) -> BeadEvent:
        target = edge.target
        return self(
            EventKind.EDGE_ADD,
            f"edge:{edge.type}:{target.group}/{target.id}",
            edge.to_json(),
            occurred_at=self.row.edges.get(edge),
        )

    def comment(self, comment: RowComment) -> BeadEvent:
        return self(
            EventKind.COMMENT,
            f"comment:{comment.id}",
            {"body": comment.body},
            occurred_at=comment.created_at,
            recorded_at=comment.created_at or self.row.updated_at,
            actor=comment.author,
        )


def _consistent_with_row(event: BeadEvent, row: RowState) -> bool:
    """Per-facet agreement of one stored event with the row (sets agree by construction)."""
    kind, data = event.kind, event.data
    if kind in (EventKind.LABEL_ADD, EventKind.LABEL_REMOVE):
        return (kind is EventKind.LABEL_ADD) == (data["label"] in row.labels)
    if kind in (EventKind.EDGE_ADD, EventKind.EDGE_REMOVE):
        return (kind is EventKind.EDGE_ADD) == (Edge.from_json(data) in row.edges)
    if kind is EventKind.PHASE:
        return same_phase(data["phase"], row.phase)
    if kind is EventKind.RETIRE:
        return row.phase == CLOSED
    return True


def row_events(row: RowState, extracted_at: str) -> list[BeadEvent]:
    """The events of one row: its re-emitted stored events plus native ones (module doc)."""
    native = _Native(row, extracted_at)
    stored = [
        event
        for facet, item in sorted(row.stored.items())
        if (event := _rebuild(row, str(facet), item, extracted_at)) is not None
    ]
    pool = Counter((comment.author, comment.body) for comment in row.comments)

    def claim(actor: str | None, body: Any) -> bool:
        """Take one native comment matching a stored one: same author and body, or — for a
        stored comment with no actor, which bd credits to whoever wrote it — the same body."""
        if pool[(actor, body)] > 0:
            pool[(actor, body)] -= 1
            return True
        if actor is None:
            for key in sorted(pool, key=lambda item: (item[0] or "", item[1])):
                if key[1] == body and pool[key] > 0:
                    pool[key] -= 1
                    return True
        return False

    kept: list[BeadEvent] = []
    for event in stored:
        if event.kind is EventKind.COMMENT and not claim(event.actor, event.data.get("body")):
            continue
        if _consistent_with_row(event, row):
            kept.append(event)
    comments: list[BeadEvent] = []
    for comment in row.comments:
        key = (comment.author, comment.body)
        if pool[key] > 0:
            pool[key] -= 1
            comments.append(native.comment(comment))

    def reconcile(candidates: list[BeadEvent]) -> list[BeadEvent] | None:
        state = fold(candidates).get(row.bead)
        if not agrees(state, row):
            has_create = any(
                event.kind in (EventKind.CREATE, EventKind.CHECKPOINT) for event in candidates
            )
            if has_create:
                return None
            additions = [native.create()]
            if row.phase != "open" and not any(
                event.kind in (EventKind.PHASE, EventKind.RETIRE) for event in candidates
            ):
                additions.append(native.phase())
            candidates = [*candidates, *additions]
            state = fold(candidates).get(row.bead)
        assert state is not None
        labels = row.labels - frozenset(state.labels)
        edges = frozenset(row.edges) - frozenset(state.edges)
        out = [
            *candidates,
            *(native.label(label) for label in sorted(labels)),
            *(native.edge(edge) for edge in sorted(edges)),
        ]
        return out if agrees(fold(out).get(row.bead), row) else None

    result = reconcile(kept)
    if result is None:  # the stored create disagrees: rebuild the scalars natively
        result = reconcile(
            [e for e in kept if e.kind not in (EventKind.CREATE, EventKind.CHECKPOINT)]
        )
    if result is None:  # last resort: a purely native snapshot of the row
        result = reconcile([e for e in kept if e.kind is EventKind.COMMENT]) or []
    return [*result, *comments]


def parse_export(text: str) -> list[dict[str, Any]]:
    """The issue rows of ``bd export`` JSONL; non-issue lines (memories) are skipped."""
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise BdReadError(f"bd export line {number} is not JSON: {exc}") from exc
        if not isinstance(row, dict):
            raise BdReadError(f"bd export line {number} is not an object")
        if row.get("_type", "issue") != "issue" or not row.get("id"):
            continue
        if row.get("status") == "tombstone":
            continue
        rows.append(row)
    return rows


def baseline_events(
    rows: Iterable[Mapping[str, Any]], *, group: str, extracted_at: str
) -> tuple[BeadEvent, ...]:
    """Every row's events (:func:`row_events`), rows in id order."""
    states = sorted((row_state(row, group) for row in rows), key=lambda state: state.bead)
    return tuple(event for state in states for event in row_events(state, extracted_at))


def _subprocess_runner(argv: Sequence[str]) -> str:
    try:
        result = subprocess.run(list(argv), capture_output=True, text=True, check=False)
    except OSError as exc:
        raise BdReadError(f"cannot run {argv[0]}: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        raise BdReadError(f"bd export failed ({result.returncode}): {detail[-1] if detail else ''}")
    return result.stdout


def export_argv(root: Path | str, *, binary: str = "bd", all_: bool = True) -> list[str]:
    """The one bulk read: ``bd -C <root> export [--all]``."""
    argv = [binary, "-C", str(root), "export"]
    if all_:
        argv.append("--all")
    return argv


def read_rows(
    root: Path | str, *, binary: str = "bd", all_: bool = True, runner: Runner | None = None
) -> list[dict[str, Any]]:
    """Run :func:`export_argv` once and parse its issue rows."""
    return parse_export((runner or _subprocess_runner)(export_argv(root, binary=binary, all_=all_)))


class BdBaselineStrategy:
    """Source strategy ``bd``: the current state of the bd store at ``request.root`` as events.

    Options: ``binary`` (default ``bd``) and ``all`` (``true``: include infra, templates and
    gates; ``false``: plain ``bd export``). ``runner`` replaces the subprocess call in tests.
    """

    name = STRATEGY_NAME
    version = STRATEGY_VERSION

    def __init__(self, runner: Runner | None = None) -> None:
        self._runner = runner

    def extract(self, request: ExtractRequest) -> tuple[BeadEvent, ...]:
        options = request.options
        rows = read_rows(
            request.root,
            binary=str(options.get("binary", "bd")),
            all_=str(options.get("all", "true")).lower() not in ("false", "0", "no"),
            runner=self._runner,
        )
        return baseline_events(rows, group=request.group, extracted_at=request.extracted_at)


__all__ = [
    "CLOSED",
    "DEFAULT_TYPE",
    "FIELD_COLUMNS",
    "METADATA_FIELD_PREFIX",
    "METADATA_KEY",
    "RETIRED",
    "RETIRED_KEY",
    "SOURCE_SYSTEM",
    "STRATEGY_NAME",
    "STRATEGY_VERSION",
    "TIMESTAMP_FIELDS",
    "BdBaselineStrategy",
    "BdReadError",
    "RowComment",
    "RowState",
    "admissible",
    "agrees",
    "baseline_events",
    "bd_filled_defaults",
    "bd_timestamp",
    "envelope",
    "export_argv",
    "facet_key",
    "fields_agree",
    "parse_export",
    "read_rows",
    "row_events",
    "row_state",
    "same_phase",
]
