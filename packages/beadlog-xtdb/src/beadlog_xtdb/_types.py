"""Single-type column discipline (bh-w56ts contract 1, evidence E4).

XTDB widens a column to a union type the first time a row brings a different type, and the
widening is permanent for the table: a column that mixes timestamps and text breaks ``WHERE``
and ``ORDER BY`` on it (a server ``ClassCastException`` in 2.2.0), and ``2`` against ``"P2"``
breaks numeric predicates. So the loader keeps **every column one type**: :class:`ColumnTypes`
knows the bead fields whose type is fixed by the bd schema, pins every other column to the type
of its first value, and coerces each later value to that type or refuses it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Any

from ._transit import INT64_MAX, INT64_MIN, Instant, TransitError


class ColumnKind(StrEnum):
    """The one type a column holds."""

    TEXT = "text"
    INT = "int"
    FLOAT = "float"
    BOOL = "bool"
    TIMESTAMP = "timestamp"
    STRUCT = "struct"
    LIST = "list"


# Columns whose type the bd schema fixes, by bare name (every table) or ``table.column``.
# Everything else is pinned by its first value.
KNOWN_COLUMNS: Mapping[str, ColumnKind] = {
    "priority": ColumnKind.INT,
    "estimated_minutes": ColumnKind.INT,
    "due_at": ColumnKind.TIMESTAMP,
    "defer_until": ColumnKind.TIMESTAMP,
    "created_at": ColumnKind.TIMESTAMP,
    "updated_at": ColumnKind.TIMESTAMP,
    "closed_at": ColumnKind.TIMESTAMP,
    "started_at": ColumnKind.TIMESTAMP,
    "lease_expires_at": ColumnKind.TIMESTAMP,
    "heartbeat_at": ColumnKind.TIMESTAMP,
    "beads.deleted": ColumnKind.BOOL,
    "beads.metadata": ColumnKind.STRUCT,
    "beads.retired": ColumnKind.STRUCT,
}

_INTEGER = re.compile(r"^[+-]?[0-9]+$")


class ColumnTypeError(ValueError):
    """A value that cannot be stored in its column without changing the column's type."""

    def __init__(self, table: str, column: str, kind: ColumnKind, value: Any) -> None:
        self.table, self.column, self.kind, self.value = table, column, kind, value
        super().__init__(
            f"{table}.{column} holds {kind.value} values but got {type(value).__name__} "
            f"{_brief(value)}; XTDB would widen the column to a union type and break range "
            f"predicates on it. Fix the value or the producer instead of mixing types"
        )


def _brief(value: Any) -> str:
    text = repr(value)
    return text if len(text) <= 60 else text[:57] + "..."


def kind_of(value: Any) -> ColumnKind:
    """The column kind a non-null value would create."""
    if isinstance(value, bool):
        return ColumnKind.BOOL
    if isinstance(value, int):
        return ColumnKind.INT
    if isinstance(value, float):
        return ColumnKind.FLOAT
    if isinstance(value, (Instant, datetime)):
        return ColumnKind.TIMESTAMP
    if isinstance(value, str):
        return ColumnKind.TEXT
    if isinstance(value, Mapping):
        return ColumnKind.STRUCT
    if isinstance(value, (list, tuple)):
        return ColumnKind.LIST
    raise TransitError(f"no column type for {type(value).__name__}: {value!r}")


def _timestamp(value: Any) -> Instant | None:
    if isinstance(value, Instant):
        return value
    if isinstance(value, datetime):
        return Instant(value) if value.tzinfo is not None else None
    if isinstance(value, str):
        try:
            return Instant.parse(value)
        except TransitError:
            try:
                moment = datetime.fromisoformat(value)
            except ValueError:
                return None
            return Instant(moment) if moment.tzinfo is not None else None
    return None


def _coerce(kind: ColumnKind, value: Any) -> Any:
    """``value`` as ``kind``, or ``None`` when it cannot be (never a guess)."""
    if kind is ColumnKind.TEXT:
        if isinstance(value, str):
            return value
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
        return None
    if kind is ColumnKind.INT:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            number = value
        elif isinstance(value, float) and value.is_integer():
            number = int(value)
        elif isinstance(value, str) and _INTEGER.fullmatch(value.strip()):
            number = int(value)
        else:
            return None
        return number if INT64_MIN <= number <= INT64_MAX else None
    if kind is ColumnKind.FLOAT:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                return None
        return None
    if kind is ColumnKind.BOOL:
        if isinstance(value, bool):
            return value
        return {"true": True, "false": False}.get(value) if isinstance(value, str) else None
    if kind is ColumnKind.TIMESTAMP:
        return _timestamp(value)
    if kind is ColumnKind.STRUCT:
        return value if isinstance(value, Mapping) else None
    if kind is ColumnKind.LIST:
        return list(value) if isinstance(value, (list, tuple)) else None
    return None  # pragma: no cover - the enum is closed


class ColumnTypes:
    """Per-load registry of column types: known columns are fixed, others pin on first sight.

    One registry per load, shared by every builder, so a column keeps one type across beads. It is
    not thread-safe and holds no connection.
    """

    def __init__(self, known: Mapping[str, ColumnKind] | None = None) -> None:
        self._known = dict(KNOWN_COLUMNS if known is None else known)
        self._pinned: dict[tuple[str, str], ColumnKind] = {}

    def kind(self, table: str, column: str) -> ColumnKind | None:
        """The kind ``table.column`` is fixed or pinned to, or ``None`` if not seen yet."""
        return (
            self._known.get(f"{table}.{column}")
            or self._known.get(column)
            or self._pinned.get((table, column))
        )

    def pinned(self) -> dict[str, ColumnKind]:
        """The columns pinned so far by first sight, as ``"table.column" -> kind``."""
        return {f"{table}.{column}": kind for (table, column), kind in sorted(self._pinned.items())}

    def coerce(self, table: str, column: str, value: Any) -> Any:
        """``value`` in the column's one type, ready for :func:`~beadlog_xtdb.encode`.

        ``None`` stays ``None`` (XTDB cannot store an explicit null, so builders omit it). A
        timestamp becomes an :class:`~beadlog_xtdb.Instant`. A value that cannot become the
        column's type raises :class:`ColumnTypeError`; it is never silently stringified.
        """
        if value is None:
            return None
        kind = self.kind(table, column)
        if kind is None:
            kind = kind_of(value)
            self._pinned[(table, column)] = kind
        coerced = _coerce(kind, value)
        if coerced is None:
            raise ColumnTypeError(table, column, kind, value)
        return coerced


__all__ = [
    "KNOWN_COLUMNS",
    "ColumnKind",
    "ColumnTypeError",
    "ColumnTypes",
    "kind_of",
]
