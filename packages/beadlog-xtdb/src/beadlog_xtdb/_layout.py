"""Layout (b) (bh-w56ts): the BeadLog folded state as XTDB documents.

Four tables, each row a transit document with its own valid period:

=================  =============================  ==========================================
table              ``_id``                        columns
=================  =============================  ==========================================
``beads``          bead id (``bh-1``); multi-     ``group_id``, ``bead_id``, ``issue_type``,
                   group: ``<group_id>:<bead_id>``  ``phase``, ``retired``, ``deleted``, one
                                                  column per BeadLog field, ``metadata``
                                                  struct (``metadata.<k>`` fields, key kept
                                                  verbatim)
``bead_labels``    ``<bead>|<label>``             ``bead_id``, ``label``
``bead_edges``     ``<from>|<type>|<to>``         ``from_id``, ``type``, ``to_id``,
                                                  ``created_by``, ``created_at``,
                                                  ``metadata``
``bead_comments``  the comment's event id         ``bead_id``, ``author``, ``text``,
                                                  ``created_at``
=================  =============================  ==========================================

In the child tables ``bead_id``, ``from_id`` and ``to_id`` hold the owning or target bead's
``_id`` (the key above), so they join ``beads._id`` directly. ``beads.bead_id`` is the plain id
and ``beads.group_id`` its group, which lets a reader rebuild the :class:`~beadlog.BeadRef`.

Transforms every builder applies, and a loss report declares: ``_type`` becomes
``bd_record_type`` (top-level ``_`` keys are reserved by XTDB), timestamps are typed instants
(never strings), strings starting ``~``, ``^`` or a backtick are escaped by the transit writer,
``null`` is omitted (XTDB cannot store an explicit null), and every column keeps one type.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from beadlog import BeadRef, BeadState, Comment, Edge, format_timestamp

from ._transit import Instant
from ._types import ColumnTypes

BEADS = "beads"
BEAD_LABELS = "bead_labels"
BEAD_EDGES = "bead_edges"
BEAD_COMMENTS = "bead_comments"
TABLES = (BEADS, BEAD_LABELS, BEAD_EDGES, BEAD_COMMENTS)

RECORD_TYPE_COLUMN = "bd_record_type"
# Top-level keys XTDB reserves that a record may carry; renamed, and reported by the loader.
RENAMED_COLUMNS: Mapping[str, str] = {"_type": RECORD_TYPE_COLUMN}
METADATA_PREFIX = "metadata."
GROUP_SEPARATOR = ":"
ID_SEPARATOR = "|"
# XTDB's own columns; nothing else may start with an underscore.
_XTDB_COLUMNS = frozenset({"_id", "_valid_from", "_valid_to"})
_BEAD_COLUMNS = frozenset(
    {"_id", "group_id", "bead_id", "issue_type", "phase", "retired", "deleted", "metadata"}
)


class LayoutError(ValueError):
    """A bead, label or edge the layout cannot represent without ambiguity."""


class ReservedColumnError(LayoutError):
    """A top-level key XTDB reserves (``_``-prefixed) that has no rename."""


class UnsupportedFieldError(LayoutError):
    """A BeadLog field with no column (``field.unsupported`` in a loss report)."""

    def __init__(self, name: str, reason: str) -> None:
        self.name = name
        super().__init__(f"field {name!r} has no column: {reason}")


@dataclass(frozen=True)
class Layout:
    """Id spelling for one load. ``multi_group`` is true when the load spans several groups."""

    multi_group: bool = False

    @classmethod
    def for_refs(cls, refs: Iterable[BeadRef]) -> Layout:
        """Multi-group when ``refs`` name more than one group."""
        return cls(multi_group=len({ref.group for ref in refs}) > 1)

    def bead_key(self, ref: BeadRef) -> str:
        """The bead's ``_id``: its id, or ``<group_id>:<bead_id>`` in a multi-group load."""
        if ID_SEPARATOR in ref.id or ID_SEPARATOR in ref.group:
            raise LayoutError(f"{ref} contains {ID_SEPARATOR!r}, which composite ids rely on")
        if not self.multi_group:
            return ref.id
        if GROUP_SEPARATOR in ref.id:
            raise LayoutError(
                f"{ref}: a bead id may not contain {GROUP_SEPARATOR!r} in a multi-group load"
            )
        return f"{ref.group}{GROUP_SEPARATOR}{ref.id}"

    def label_id(self, bead: BeadRef, label: str) -> str:
        return f"{self.bead_key(bead)}{ID_SEPARATOR}{label}"

    def edge_id(self, source: BeadRef, edge_type: str, target: BeadRef) -> str:
        if ID_SEPARATOR in edge_type:
            raise LayoutError(f"edge type {edge_type!r} contains {ID_SEPARATOR!r}")
        return (
            f"{self.bead_key(source)}{ID_SEPARATOR}{edge_type}{ID_SEPARATOR}{self.bead_key(target)}"
        )


def split_key(key: str) -> tuple[str | None, str]:
    """``(group_id, bead_id)`` of a multi-group key; ``(None, key)`` when it has no group part.

    The group is everything before the last ``:`` (group ids such as ``urn:uuid:...`` contain
    colons; bead ids do not, which :meth:`Layout.bead_key` enforces).
    """
    group, sep, ident = key.rpartition(GROUP_SEPARATOR)
    return (group, ident) if sep else (None, key)


def _valid_period(
    doc: dict[str, Any], valid_from: str | Instant | None, valid_to: str | Instant | None
) -> dict[str, Any]:
    if valid_from is not None:
        doc["_valid_from"] = _instant(valid_from)
    if valid_to is not None:
        doc["_valid_to"] = _instant(valid_to)
    return doc


def _instant(value: str | Instant | datetime) -> Instant:
    if isinstance(value, Instant):
        return value
    if isinstance(value, datetime):
        return Instant(value)
    return Instant.parse(value)


def rename_reserved(document: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """``document`` with reserved top-level keys renamed, and the ``{old: new}`` renames done.

    ``_type`` becomes :data:`RECORD_TYPE_COLUMN`. ``_id``, ``_valid_from`` and ``_valid_to`` are
    XTDB's own and pass through. Any other ``_``-prefixed key raises :class:`ReservedColumnError`
    rather than being dropped or guessed at.
    """
    out: dict[str, Any] = {}
    renamed: dict[str, str] = {}
    for key, value in document.items():
        if key in RENAMED_COLUMNS:
            target = RENAMED_COLUMNS[key]
            if target in document:
                raise ReservedColumnError(f"cannot rename {key!r}: {target!r} is already present")
            renamed[key] = target
            out[target] = value
        elif key.startswith("_") and key not in _XTDB_COLUMNS:
            raise ReservedColumnError(
                f"top-level key {key!r} is reserved by XTDB and has no rename; "
                f"known renames: {sorted(RENAMED_COLUMNS)}"
            )
        else:
            out[key] = value
    return out, renamed


# -- row builders --------------------------------------------------------------------------


def bead_row(
    layout: Layout,
    state: BeadState,
    types: ColumnTypes,
    *,
    valid_from: str | Instant | None = None,
    valid_to: str | Instant | None = None,
    unsupported: list[str] | None = None,
) -> dict[str, Any]:
    """The ``beads`` document for a folded bead: one full version, ``_valid_from`` as given.

    ``unsupported`` collects the names of fields that have no column (and skips them); without
    it such a field raises :class:`UnsupportedFieldError`. A field whose name collides with a
    structural column raises :class:`LayoutError`.
    """
    doc: dict[str, Any] = {
        "_id": layout.bead_key(state.bead),
        "group_id": state.bead.group,
        "bead_id": state.bead.id,
    }
    structural = {"issue_type": state.type, "phase": state.phase}
    for column, value in structural.items():
        if value is not None:
            doc[column] = types.coerce(BEADS, column, value)
    if state.retired is not None:
        doc["retired"] = types.coerce(BEADS, "retired", dict(state.retired))
    if state.deleted:
        doc["deleted"] = True
    metadata: dict[str, Any] = {}
    for name, value in state.fields.items():
        if value is None:
            continue
        if name.startswith(METADATA_PREFIX):
            key = name[len(METADATA_PREFIX) :]
            if not key:
                _unsupported(name, "an empty metadata key", unsupported)
                continue
            metadata[key] = value
        elif "." in name:
            _unsupported(name, "only metadata.<key> may be dotted", unsupported)
        elif name in _BEAD_COLUMNS or name.startswith("_"):
            raise LayoutError(f"field {name!r} collides with a structural beads column")
        else:
            doc[name] = types.coerce(BEADS, name, value)
    if metadata:
        doc["metadata"] = types.coerce(BEADS, "metadata", metadata)
    return _valid_period(doc, valid_from, valid_to)


def _unsupported(name: str, reason: str, sink: list[str] | None) -> None:
    if sink is None:
        raise UnsupportedFieldError(name, reason)
    sink.append(name)


def label_row(
    layout: Layout,
    bead: BeadRef,
    label: str,
    *,
    valid_from: str | Instant | None = None,
    valid_to: str | Instant | None = None,
) -> dict[str, Any]:
    """A ``bead_labels`` row: one held period of one label."""
    doc = {"_id": layout.label_id(bead, label), "bead_id": layout.bead_key(bead), "label": label}
    return _valid_period(doc, valid_from, valid_to)


def edge_row(
    layout: Layout,
    source: BeadRef,
    edge: Edge,
    types: ColumnTypes,
    *,
    created_by: str | None = None,
    created_at: str | Instant | None = None,
    metadata: Any = None,
    valid_from: str | Instant | None = None,
    valid_to: str | Instant | None = None,
) -> dict[str, Any]:
    """A ``bead_edges`` row: one held period of one ``(from, type, to)`` edge."""
    doc: dict[str, Any] = {
        "_id": layout.edge_id(source, edge.type, edge.target),
        "from_id": layout.bead_key(source),
        "type": edge.type,
        "to_id": layout.bead_key(edge.target),
    }
    extras = {"created_by": created_by, "created_at": created_at, "metadata": metadata}
    for column, value in extras.items():
        if value is not None:
            doc[column] = types.coerce(BEAD_EDGES, column, value)
    return _valid_period(doc, valid_from, valid_to)


def comment_row(
    layout: Layout,
    bead: BeadRef,
    comment: Comment,
    types: ColumnTypes,
    *,
    valid_to: str | Instant | None = None,
) -> dict[str, Any]:
    """A ``bead_comments`` row, valid from the comment's ``occurred_at`` (open-ended).

    A redacted comment (``body`` withheld) has no ``text`` column: absent, never an empty string.
    """
    doc: dict[str, Any] = {"_id": comment.id, "bead_id": layout.bead_key(bead)}
    for column, value in (
        ("author", comment.actor),
        ("text", comment.body),
        ("created_at", comment.occurred_at),
    ):
        if value is not None:
            doc[column] = types.coerce(BEAD_COMMENTS, column, value)
    return _valid_period(doc, comment.occurred_at, valid_to)


@dataclass(frozen=True)
class BeadRows:
    """Every layout (b) document for one bead."""

    bead: dict[str, Any]
    labels: tuple[dict[str, Any], ...] = ()
    edges: tuple[dict[str, Any], ...] = ()
    comments: tuple[dict[str, Any], ...] = ()
    renamed: Mapping[str, str] = field(default_factory=dict)
    unsupported: tuple[str, ...] = ()

    def by_table(self) -> dict[str, list[dict[str, Any]]]:
        """``{table: [documents]}`` in table order."""
        return {
            BEADS: [self.bead],
            BEAD_LABELS: list(self.labels),
            BEAD_EDGES: list(self.edges),
            BEAD_COMMENTS: list(self.comments),
        }


def state_rows(
    layout: Layout,
    state: BeadState,
    types: ColumnTypes,
    *,
    valid_from: str | Instant | None = None,
    skip_unsupported: bool = False,
) -> BeadRows:
    """All rows for one folded bead state, every period starting at ``valid_from``.

    This is the snapshot shape (a checkpoint or a current-state load). A history load builds the
    same rows one event at a time with the per-table builders, each with its own period.
    """
    skipped: list[str] = []
    bead = bead_row(
        layout,
        state,
        types,
        valid_from=valid_from,
        unsupported=skipped if skip_unsupported else None,
    )
    bead, renamed = rename_reserved(bead)
    return BeadRows(
        bead=bead,
        labels=tuple(
            label_row(layout, state.bead, label, valid_from=valid_from) for label in state.labels
        ),
        edges=tuple(
            edge_row(layout, state.bead, edge, types, valid_from=valid_from) for edge in state.edges
        ),
        comments=tuple(
            comment_row(layout, state.bead, comment, types) for comment in state.comments
        ),
        renamed=renamed,
        unsupported=tuple(skipped),
    )


# -- bd-export-shaped records (parity with the bh-w56ts load) -----------------------------

_NESTED = ("labels", "dependencies", "comments")


def record_rows(
    layout: Layout, group: str, record: Mapping[str, Any], types: ColumnTypes
) -> BeadRows:
    """Rows for one ``bd export``-shaped record, as the bh-w56ts load wrote them.

    Every top-level key except ``labels``, ``dependencies`` and ``comments`` becomes a column
    verbatim (so ``id``, ``status`` and ``issue_type`` keep bd's names), ``_type`` is renamed
    ``bd_record_type``, known timestamp columns become instants, and ``metadata`` stays a struct
    with its keys verbatim. ``group_id`` is added. Edges come from ``dependencies``
    (``issue_id``, ``depends_on_id``, ``type``) and comments from ``comments``.
    """
    ident = str(record["id"])
    bead = BeadRef(group, ident)
    doc: dict[str, Any] = {"_id": layout.bead_key(bead), "group_id": group}
    for key, value in record.items():
        if key in _NESTED or value is None:
            continue
        doc[key] = value
    doc, renamed = rename_reserved(doc)
    for column in [c for c in doc if c not in _XTDB_COLUMNS and c != "group_id"]:
        doc[column] = types.coerce(BEADS, column, doc[column])
    labels = tuple(label_row(layout, bead, str(label)) for label in record.get("labels") or ())
    edges = []
    for dep in record.get("dependencies") or ():
        source = BeadRef(group, str(dep.get("issue_id") or ident))
        target = BeadRef(group, str(dep["depends_on_id"]))
        edges.append(
            edge_row(
                layout,
                source,
                Edge(str(dep["type"]), target),
                types,
                created_by=dep.get("created_by"),
                created_at=dep.get("created_at"),
                metadata=dep.get("metadata"),
                valid_from=dep.get("created_at"),
            )
        )
    comments = []
    for note in record.get("comments") or ():
        row: dict[str, Any] = {"_id": str(note["id"]), "bead_id": layout.bead_key(bead)}
        for column in ("author", "text", "created_at"):
            if note.get(column) is not None:
                row[column] = types.coerce(BEAD_COMMENTS, column, note[column])
        comments.append(_valid_period(row, note.get("created_at"), None))
    return BeadRows(
        bead=doc, labels=labels, edges=tuple(edges), comments=tuple(comments), renamed=renamed
    )


# -- reading back --------------------------------------------------------------------------


@dataclass(frozen=True)
class DecodedBead:
    """A ``beads`` row read back (transit mode) as BeadLog state components."""

    bead: BeadRef
    type: str | None
    phase: str | None
    fields: dict[str, Any]
    retired: dict[str, Any] | None
    deleted: bool


def decode_bead_row(row: Mapping[str, Any]) -> DecodedBead:
    """Inverse of :func:`bead_row` for a row read with transit output.

    Timestamp columns come back as aware ``datetime`` and are returned as canonical BeadLog
    timestamp strings; ``metadata`` is flattened back to ``metadata.<key>`` fields.
    """
    try:
        ref = BeadRef(row["group_id"], row["bead_id"])
    except KeyError as exc:
        raise LayoutError(
            f"a beads row needs group_id and bead_id: missing {exc.args[0]!r}"
        ) from exc
    structural = _BEAD_COLUMNS | _XTDB_COLUMNS
    fields: dict[str, Any] = {}
    for name, value in row.items():
        if name in structural or value is None:
            continue
        fields[name] = format_timestamp(value) if isinstance(value, datetime) else value
    for key, value in (row.get("metadata") or {}).items():
        fields[METADATA_PREFIX + str(key)] = value
    return DecodedBead(
        bead=ref,
        type=row.get("issue_type"),
        phase=row.get("phase"),
        fields=dict(sorted(fields.items())),
        retired=dict(row["retired"]) if row.get("retired") is not None else None,
        deleted=bool(row.get("deleted", False)),
    )


__all__ = [
    "BEADS",
    "BEAD_COMMENTS",
    "BEAD_EDGES",
    "BEAD_LABELS",
    "GROUP_SEPARATOR",
    "ID_SEPARATOR",
    "RECORD_TYPE_COLUMN",
    "RENAMED_COLUMNS",
    "TABLES",
    "BeadRows",
    "DecodedBead",
    "Layout",
    "LayoutError",
    "ReservedColumnError",
    "UnsupportedFieldError",
    "bead_row",
    "comment_row",
    "decode_bead_row",
    "edge_row",
    "label_row",
    "record_rows",
    "rename_reserved",
    "split_key",
    "state_rows",
]
