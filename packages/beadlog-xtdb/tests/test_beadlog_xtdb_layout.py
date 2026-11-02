"""Layout (b) row builders over fixture beads of all ten issue types (no XTDB needed)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import pytest

from beadlog import BeadEvent, BeadRef, Edge, fold, mint_event
from beadlog_xtdb import (
    BEAD_COMMENTS,
    BEAD_EDGES,
    BEAD_LABELS,
    BEADS,
    RECORD_TYPE_COLUMN,
    TABLES,
    ColumnTypeError,
    ColumnTypes,
    Instant,
    Layout,
    LayoutError,
    ReservedColumnError,
    UnsupportedFieldError,
    bead_row,
    comment_row,
    decode,
    decode_bead_row,
    edge_row,
    encode,
    label_row,
    record_rows,
    rename_reserved,
    split_key,
    state_rows,
)

GROUP = "urn:uuid:5f0c9a52-8a43-4f0e-9a5e-1c7d2b3e4f60"
EXTRACTED = "2026-10-31T00:00:00Z"
# The ten issue types a `bh bd export` holds (bh-w56ts E1) and a type-specific sparse field each.
ISSUE_TYPES: Mapping[str, Mapping[str, Any]] = {
    "bug": {"severity": "high"},
    "chore": {},
    "decision": {"decision_status": "accepted"},
    "epic": {"design": "two packages"},
    "event": {"event_kind": "heartbeat", "payload": "~tilde — 中文"},
    "feature": {"spec_id": "SPEC-1"},
    "gate": {"await_type": "human", "await_id": "review-1"},
    "molecule": {"mol_type": "swarm"},
    "spike": {"estimated_minutes": 90},
    "task": {"external_ref": "gh-12"},
}


def bead(name: str) -> BeadRef:
    return BeadRef(GROUP, f"bh-{name}")


def event(kind: str, name: str, data: Mapping[str, Any], at: str, **kw: Any) -> BeadEvent:
    return mint_event(
        kind,
        bead(name),
        data,
        occurred_at=at,
        recorded_at=kw.pop("recorded_at", at),
        extracted_at=EXTRACTED,
        source=("fixture", f"{kind}:{name}:{at}"),
        provenance="observed",
        actor=kw.pop("actor", "dev/fixture"),
        **kw,
    )


def history(issue_type: str, extra: Mapping[str, Any]) -> list[BeadEvent]:
    fields = {
        "title": f"A {issue_type}",
        "description": "multi\nline ~ ^ ` text",
        "priority": 2,
        "due_at": "2026-12-01T09:30:00Z",
        "assignee": "dev/alice",
        "metadata.git.commits": '["abc123"]',
        "metadata.estimate": 90,
        **extra,
    }
    name = issue_type
    return [
        event("create", name, {"type": issue_type, "fields": fields}, "2026-10-01T10:00:00Z"),
        event("phase", name, {"phase": "in_progress"}, "2026-10-02T10:00:00Z"),
        event("label_add", name, {"label": "area:format"}, "2026-10-02T11:00:00Z"),
        event("label_add", name, {"label": "~odd|label"}, "2026-10-02T11:30:00Z"),
        event(
            "edge_add",
            name,
            {"type": "blocks", "target": bead("target").to_json()},
            "2026-10-03T10:00:00Z",
        ),
        event("comment", name, {"body": "^first comment"}, "2026-10-04T10:00:00.5Z"),
        event("set", name, {"field": "assignee", "value": None}, "2026-10-05T10:00:00Z"),
    ]


def folded(issue_type: str) -> Any:
    extra = ISSUE_TYPES[issue_type]
    return fold(history(issue_type, extra))[bead(issue_type)]


def test_the_fixture_really_covers_all_ten_issue_types() -> None:
    assert len(ISSUE_TYPES) == 10
    assert {folded(name).type for name in ISSUE_TYPES} == set(ISSUE_TYPES)


@pytest.mark.parametrize("issue_type", sorted(ISSUE_TYPES))
def test_a_bead_of_every_issue_type_builds_the_layout_b_rows(issue_type: str) -> None:
    state = folded(issue_type)
    layout = Layout()
    rows = state_rows(layout, state, ColumnTypes(), valid_from="2026-10-01T10:00:00Z")
    key = f"bh-{issue_type}"

    doc = rows.bead
    assert doc["_id"] == key and doc["bead_id"] == key and doc["group_id"] == GROUP
    assert doc["issue_type"] == issue_type and doc["phase"] == "in_progress"
    assert doc["_valid_from"] == Instant.parse("2026-10-01T10:00:00Z")
    assert doc["title"] == f"A {issue_type}"
    assert doc["priority"] == 2 and isinstance(doc["priority"], int)
    assert doc["due_at"] == Instant.parse("2026-12-01T09:30:00Z")  # a typed instant, not text
    assert doc["metadata"] == {"git.commits": '["abc123"]', "estimate": 90}  # keys verbatim
    assert "assignee" not in doc  # the null set cleared it; XTDB cannot store an explicit null
    assert "labels" not in doc and "dependencies" not in doc and "comments" not in doc
    for name, value in ISSUE_TYPES[issue_type].items():
        assert doc[name] == value, name
    # Sparse columns are absent on the other types, not null.
    if issue_type != "gate":
        assert "await_type" not in doc
    if issue_type != "molecule":
        assert "mol_type" not in doc

    assert [row["_id"] for row in rows.labels] == [
        f"{key}|area:format",
        f"{key}|~odd|label",
    ]
    assert {row["bead_id"] for row in rows.labels} == {key}
    assert [row["label"] for row in rows.labels] == ["area:format", "~odd|label"]
    (edge,) = rows.edges
    assert edge["_id"] == f"{key}|blocks|bh-target"
    assert (edge["from_id"], edge["type"], edge["to_id"]) == (key, "blocks", "bh-target")
    (comment,) = rows.comments
    assert comment["_id"] == state.comments[0].id
    assert comment["bead_id"] == key and comment["text"] == "^first comment"
    assert comment["author"] == "dev/fixture"
    assert comment["created_at"] == Instant.parse("2026-10-04T10:00:00.5Z")
    assert comment["_valid_from"] == comment["created_at"] and "_valid_to" not in comment
    assert set(rows.by_table()) == set(TABLES) == {BEADS, BEAD_LABELS, BEAD_EDGES, BEAD_COMMENTS}


@pytest.mark.parametrize("issue_type", sorted(ISSUE_TYPES))
def test_rows_survive_the_transit_wire_and_read_back_field_equal(issue_type: str) -> None:
    state = folded(issue_type)
    rows = state_rows(Layout(), state, ColumnTypes(), valid_from="2026-10-01T10:00:00Z")
    for documents in rows.by_table().values():
        for document in documents:
            wire = decode(encode(document))  # what XTDB stores and a transit read returns
            assert set(wire) == set(document)
    back = decode_bead_row(decode(encode(rows.bead)))
    assert back.bead == state.bead
    assert back.type == state.type and back.phase == state.phase
    assert back.fields == {name: value for name, value in sorted(state.fields.items())}
    assert back.retired is None and back.deleted is False


def test_a_document_is_one_transit_parameter_with_typed_instants_and_escapes() -> None:
    import json

    state = folded("gate")
    wire = json.loads(encode(state_rows(Layout(), state, ColumnTypes()).bead))
    assert wire["due_at"] == {"~#time/instant": "2026-12-01T09:30:00Z"}
    assert wire["description"] == "multi\nline ~ ^ ` text"  # only a leading ~ ^ ` is special
    event_wire = json.loads(encode(state_rows(Layout(), folded("event"), ColumnTypes()).bead))
    assert event_wire["payload"] == "~~tilde — 中文"


def test_retired_and_deleted_state_is_kept_as_columns() -> None:
    events = [
        event("create", "r", {"type": "task", "fields": {"title": "r"}}, "2026-10-01T10:00:00Z"),
        event("retire", "r", {"successor": "bh-n", "reason": "dup"}, "2026-10-02T10:00:00Z"),
    ]
    state = fold(events)[bead("r")]
    doc = bead_row(Layout(), state, ColumnTypes())
    assert doc["phase"] == "retired" and doc["retired"] == {"successor": "bh-n", "reason": "dup"}
    back = decode_bead_row(decode(encode(doc)))
    assert back.retired == {"successor": "bh-n", "reason": "dup"}
    events.append(event("delete", "r", {}, "2026-10-03T10:00:00Z"))
    deleted = fold(events)[bead("r")]
    assert bead_row(Layout(), deleted, ColumnTypes())["deleted"] is True


def test_valid_periods_are_typed_instants_and_only_present_when_given() -> None:
    layout = Layout()
    plain = label_row(layout, bead("a"), "x")
    assert "_valid_from" not in plain and "_valid_to" not in plain
    bounded = label_row(
        layout,
        bead("a"),
        "x",
        valid_from="2026-01-01T00:00:00Z",
        valid_to="2026-01-02T00:00:00.000001Z",
    )
    assert bounded["_valid_from"] == Instant.parse("2026-01-01T00:00:00Z")
    assert bounded["_valid_to"].iso() == "2026-01-02T00:00:00.000001Z"
    moment = datetime(2026, 1, 1, tzinfo=UTC)
    assert label_row(layout, bead("a"), "x", valid_from=moment)["_valid_from"] == Instant(moment)


def test_edge_rows_carry_provenance_columns_and_an_open_period_by_default() -> None:
    types = ColumnTypes()
    row = edge_row(
        Layout(),
        bead("a"),
        Edge("parent-child", bead("b")),
        types,
        created_by="dev/alice",
        created_at="2026-10-03T10:00:00Z",
        metadata="{}",
        valid_from="2026-10-03T10:00:00Z",
        valid_to="2026-10-09T10:00:00Z",
    )
    assert row["_id"] == "bh-a|parent-child|bh-b"
    assert row["created_at"] == Instant.parse("2026-10-03T10:00:00Z")
    assert row["created_by"] == "dev/alice" and row["metadata"] == "{}"
    assert row["_valid_to"] == Instant.parse("2026-10-09T10:00:00Z")
    with pytest.raises(LayoutError, match="edge type"):
        edge_row(Layout(), bead("a"), Edge("a|b", bead("b")), types)


def test_a_redacted_comment_has_no_text_column() -> None:
    from beadlog import Comment

    redacted = Comment("sha256:" + "0" * 64, None, None, "2026-10-04T10:00:00Z", None)
    row = comment_row(Layout(), bead("a"), redacted, ColumnTypes())
    assert "text" not in row and "author" not in row
    assert row["_valid_from"] == Instant.parse("2026-10-04T10:00:00Z")


def test_multi_group_ids_are_qualified_and_edges_use_the_same_form() -> None:
    a, b = BeadRef("urn:uuid:g1", "bh-1"), BeadRef("urn:uuid:g2", "bh-2")
    layout = Layout.for_refs([a, b])
    assert (
        layout.multi_group and not Layout.for_refs([a, BeadRef("urn:uuid:g1", "bh-3")]).multi_group
    )
    assert layout.bead_key(a) == "urn:uuid:g1:bh-1"
    assert layout.label_id(a, "x") == "urn:uuid:g1:bh-1|x"
    assert layout.edge_id(a, "blocks", b) == "urn:uuid:g1:bh-1|blocks|urn:uuid:g2:bh-2"
    assert split_key("urn:uuid:g1:bh-1") == ("urn:uuid:g1", "bh-1")
    assert split_key("bh-1") == (None, "bh-1")
    state = fold(
        [event("create", "1", {"type": "task", "fields": {"title": "t"}}, "2026-10-01T10:00:00Z")]
    )
    (only,) = state.values()
    doc = bead_row(layout, only, ColumnTypes())
    assert doc["_id"] == f"{GROUP}:bh-1" and doc["bead_id"] == "bh-1" and doc["group_id"] == GROUP
    row = edge_row(layout, a, Edge("blocks", b), ColumnTypes())
    assert (row["from_id"], row["to_id"]) == ("urn:uuid:g1:bh-1", "urn:uuid:g2:bh-2")
    assert label_row(layout, a, "x")["bead_id"] == "urn:uuid:g1:bh-1"


def test_ids_that_would_make_a_composite_key_ambiguous_are_refused() -> None:
    with pytest.raises(LayoutError, match=r"\|"):
        Layout().bead_key(BeadRef(GROUP, "bh|1"))
    with pytest.raises(LayoutError, match=":"):
        Layout(multi_group=True).bead_key(BeadRef(GROUP, "bh:1"))
    assert Layout().bead_key(BeadRef(GROUP, "bh:1")) == "bh:1"  # fine when ids are not qualified


def test_the_type_discipline_reaches_the_builders() -> None:
    types = ColumnTypes()
    ok = fold(
        [
            event(
                "create",
                "p1",
                {"type": "task", "fields": {"title": "t", "priority": 2}},
                "2026-10-01T10:00:00Z",
            )
        ]
    )
    bad = fold(
        [
            event(
                "create",
                "p2",
                {"type": "task", "fields": {"title": "t", "priority": "P2"}},
                "2026-10-01T10:00:00Z",
            )
        ]
    )
    bead_row(Layout(), ok[bead("p1")], types)
    with pytest.raises(ColumnTypeError, match="beads.priority"):
        bead_row(Layout(), bad[bead("p2")], types)
    mixed = fold(
        [
            event(
                "create",
                "m",
                {"type": "task", "fields": {"title": "t", "x": 1}},
                "2026-10-01T10:00:00Z",
            )
        ]
    )
    more = fold(
        [
            event(
                "create",
                "n",
                {"type": "task", "fields": {"title": "t", "x": "one"}},
                "2026-10-01T10:00:00Z",
            )
        ]
    )
    types = ColumnTypes()
    assert bead_row(Layout(), mixed[bead("m")], types)["x"] == 1  # `x` is now an int column
    with pytest.raises(ColumnTypeError):
        bead_row(Layout(), more[bead("n")], types)


def test_unsupported_and_colliding_fields_are_named() -> None:
    state = fold(
        [
            event(
                "create",
                "u",
                {"type": "task", "fields": {"title": "t", "foo.bar": 1}},
                "2026-10-01T10:00:00Z",
            )
        ]
    )[bead("u")]
    with pytest.raises(UnsupportedFieldError, match="foo.bar"):
        bead_row(Layout(), state, ColumnTypes())
    skipped: list[str] = []
    doc = bead_row(Layout(), state, ColumnTypes(), unsupported=skipped)
    assert skipped == ["foo.bar"] and "foo.bar" not in doc and doc["title"] == "t"
    rows = state_rows(Layout(), state, ColumnTypes(), skip_unsupported=True)
    assert rows.unsupported == ("foo.bar",)
    clash = fold(
        [
            event(
                "create",
                "c",
                {"type": "task", "fields": {"title": "t", "bead_id": "z"}},
                "2026-10-01T10:00:00Z",
            )
        ]
    )[bead("c")]
    with pytest.raises(LayoutError, match="collides"):
        bead_row(Layout(), clash, ColumnTypes())


def test_reserved_underscore_keys_are_renamed_or_refused() -> None:
    doc, renamed = rename_reserved({"_id": "x", "_type": "issue", "_valid_from": 1, "title": "t"})
    assert doc == {"_id": "x", RECORD_TYPE_COLUMN: "issue", "_valid_from": 1, "title": "t"}
    assert renamed == {"_type": "bd_record_type"} and RECORD_TYPE_COLUMN == "bd_record_type"
    with pytest.raises(ReservedColumnError, match="_secret"):
        rename_reserved({"_id": "x", "_secret": 1})
    with pytest.raises(ReservedColumnError, match="already present"):
        rename_reserved({"_type": "issue", "bd_record_type": "x"})


def export_record(issue_type: str) -> dict[str, Any]:
    return {
        "_type": "issue",
        "id": f"bh-{issue_type}",
        "title": f"A {issue_type}",
        "description": "~starts with tilde",
        "status": "open",
        "priority": 2,
        "issue_type": issue_type,
        "created_at": "2026-10-01T10:00:00Z",
        "updated_at": "2026-10-02T10:00:00Z",
        "metadata": {"git.commits": '["abc"]'},
        "labels": ["area:format", "size:l"],
        "dependencies": [
            {
                "issue_id": f"bh-{issue_type}",
                "depends_on_id": "bh-other",
                "type": "blocks",
                "created_at": "2026-10-03T10:00:00Z",
                "created_by": "dev/alice",
            }
        ],
        "comments": [
            {
                "id": "c-1",
                "issue_id": f"bh-{issue_type}",
                "author": "dev/alice",
                "text": "hi",
                "created_at": "2026-10-04T10:00:00Z",
            }
        ],
    }


@pytest.mark.parametrize("issue_type", sorted(ISSUE_TYPES))
def test_bd_export_records_of_every_type_build_the_same_layout(issue_type: str) -> None:
    layout, types = Layout(), ColumnTypes()
    rows = record_rows(layout, "bh", export_record(issue_type), types)
    doc = rows.bead
    assert doc["_id"] == f"bh-{issue_type}" and doc["group_id"] == "bh"
    assert doc["bd_record_type"] == "issue" and "_type" not in doc
    assert rows.renamed == {"_type": "bd_record_type"}
    assert doc["created_at"] == Instant.parse("2026-10-01T10:00:00Z")
    assert doc["metadata"] == {"git.commits": '["abc"]'}
    assert not {"labels", "dependencies", "comments"} & set(doc)
    assert [row["label"] for row in rows.labels] == ["area:format", "size:l"]
    (edge,) = rows.edges
    assert edge["_id"] == f"bh-{issue_type}|blocks|bh-other"
    assert edge["_valid_from"] == Instant.parse("2026-10-03T10:00:00Z")
    (comment,) = rows.comments
    assert comment["_id"] == "c-1" and comment["bead_id"] == f"bh-{issue_type}"
    assert decode(encode(doc))["description"] == "~starts with tilde"
