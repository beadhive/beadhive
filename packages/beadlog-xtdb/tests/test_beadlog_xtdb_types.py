"""Single-type column discipline: known columns, first-sight pinning, coercion or refusal."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from beadlog_xtdb import ColumnKind, ColumnTypeError, ColumnTypes, Instant, kind_of


def test_known_columns_are_fixed_before_any_value_is_seen() -> None:
    types = ColumnTypes()
    assert types.kind("beads", "priority") is ColumnKind.INT
    assert types.kind("beads", "due_at") is ColumnKind.TIMESTAMP
    assert types.kind("beads", "title") is None
    assert types.pinned() == {}


def test_an_unknown_column_is_pinned_by_its_first_value() -> None:
    types = ColumnTypes()
    assert types.coerce("beads", "title", "a") == "a"
    assert types.kind("beads", "title") is ColumnKind.TEXT
    assert types.pinned() == {"beads.title": ColumnKind.TEXT}
    # The same name in another table is its own column.
    assert types.coerce("bead_edges", "title", 5) == 5
    assert types.kind("bead_edges", "title") is ColumnKind.INT


def test_a_timestamp_column_always_receives_an_instant() -> None:
    types = ColumnTypes()
    value = types.coerce("beads", "due_at", "2026-08-01T10:00:00Z")
    assert value == Instant.parse("2026-08-01T10:00:00Z")
    assert types.coerce("beads", "due_at", datetime(2026, 8, 1, tzinfo=UTC)) == Instant.parse(
        "2026-08-01T00:00:00Z"
    )
    assert types.coerce("beads", "due_at", "2026-08-01T12:00:00+02:00") == Instant.parse(
        "2026-08-01T10:00:00Z"
    )
    for bad in ("tomorrow", "2026-08-01T10:00:00", 5, True):
        with pytest.raises(ColumnTypeError, match="beads.due_at"):
            types.coerce("beads", "due_at", bad)


def test_an_integer_column_refuses_text_that_is_not_an_integer() -> None:
    types = ColumnTypes()
    assert types.coerce("beads", "priority", 2) == 2
    assert types.coerce("beads", "priority", "3") == 3
    assert types.coerce("beads", "priority", 4.0) == 4
    for bad in ("P2", 2.5, True, [1], 2**63):
        with pytest.raises(ColumnTypeError, match="widen"):
            types.coerce("beads", "priority", bad)


def test_text_columns_take_numbers_as_text_but_never_structures() -> None:
    types = ColumnTypes()
    types.coerce("beads", "notes", "first")
    assert types.coerce("beads", "notes", 7) == "7"
    with pytest.raises(ColumnTypeError):
        types.coerce("beads", "notes", {"a": 1})
    with pytest.raises(ColumnTypeError):
        types.coerce("beads", "notes", True)


def test_a_string_in_a_column_first_seen_as_a_timestamp_is_not_silently_text() -> None:
    types = ColumnTypes()
    types.coerce("beads", "seen_at", Instant.parse("2026-01-01T00:00:00Z"))
    assert types.kind("beads", "seen_at") is ColumnKind.TIMESTAMP
    assert types.coerce("beads", "seen_at", "2026-01-02T00:00:00Z") == Instant.parse(
        "2026-01-02T00:00:00Z"
    )
    with pytest.raises(ColumnTypeError):
        types.coerce("beads", "seen_at", "soon")


def test_bool_float_struct_and_list_columns() -> None:
    types = ColumnTypes()
    assert types.coerce("beads", "flag", True) is True
    assert types.coerce("beads", "flag", "false") is False
    assert types.coerce("beads", "ratio", 1) == 1  # pinned INT first
    types.coerce("beads", "weight", 0.5)
    assert types.coerce("beads", "weight", 2) == 2.0
    assert types.coerce("beads", "tags", ["a"]) == ["a"]
    assert types.coerce("beads", "tags", ("a", "b")) == ["a", "b"]
    assert types.coerce("beads", "extra", {"k": 1}) == {"k": 1}
    with pytest.raises(ColumnTypeError):
        types.coerce("beads", "tags", "a")
    with pytest.raises(ColumnTypeError):
        types.coerce("beads", "flag", "maybe")
    with pytest.raises(ColumnTypeError):
        types.coerce("beads", "extra", [1])


def test_null_is_never_stored_and_never_pins() -> None:
    types = ColumnTypes()
    assert types.coerce("beads", "assignee", None) is None
    assert types.kind("beads", "assignee") is None


def test_kind_of_orders_bool_before_int() -> None:
    assert kind_of(True) is ColumnKind.BOOL
    assert kind_of(1) is ColumnKind.INT
    assert kind_of(1.0) is ColumnKind.FLOAT
    assert kind_of("x") is ColumnKind.TEXT
    assert kind_of(Instant.parse("2026-01-01T00:00:00Z")) is ColumnKind.TIMESTAMP
    assert kind_of({}) is ColumnKind.STRUCT
    assert kind_of([]) is ColumnKind.LIST
