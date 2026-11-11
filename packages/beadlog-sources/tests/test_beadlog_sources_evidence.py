"""beadlog-sources: the evidence IR (Artifact / Ref / Category) and ``bdlg.evidence/1`` JSONL."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import beadlog_sources
from beadlog import canonical_json
from beadlog_sources import (
    CATEGORY_SHAPES,
    EVIDENCE_SCHEMA,
    Artifact,
    ArtifactShapeError,
    Category,
    EvidenceCorpus,
    EvidenceFormatError,
    ReaderHeader,
    Ref,
    artifact_id,
    dumps_evidence,
    evidence_schema,
    loads_evidence,
    read_evidence,
    write_evidence,
)
from beadlog_sources import evidence as evidence_module
from beadlog_sources.testing import evidence as ev

REPO = Path(__file__).resolve().parents[3]
DOCS_SCHEMA = REPO / "docs" / "schemas" / "beadlog" / "bdlg-evidence-v1.schema.json"


def _corpus() -> EvidenceCorpus:
    return ev.corpus(
        ev.change("c0ffee1", refs=[ev.ref("bh-12x"), ev.ref("#143", "pr", "body")]),
        ev.change("a1b2c3d", reader="git", reader_version="2", parents=["c0ffee1"], cc_scope="cli"),
        ev.review(143, commits=["a1b2c3d"], closes=["gh:issue/7"], kind="pr"),
        ev.tracker_item("7", state="closed", closed_at="2026-01-02T00:00:00Z"),
        ev.design_record(
            "docs/design/x.html",
            declared_status="accepted",
            raw_attrs={"status": "Accepted (2026-01)"},
            provenance="derived",
        ),
        ev.plan_item("phase-1/plan-02", parent="phase-1", depends_on=["plan:phase-1/plan-01"]),
        ev.release_marker("v1.2.0", commit_range="v1.1.0..v1.2.0", occurred_at=None),
        headers=[ReaderHeader("git", "2", {"depth": 0}, {"head": "a1b2c3d"})],
    )


# -- the public surface ----------------------------------------------------------------------


def test_evidence_names_are_exported_from_the_package() -> None:
    for name in ("Artifact", "Ref", "Category", "EvidenceCorpus", "ReaderHeader"):
        assert name in beadlog_sources.__all__
    assert set(evidence_module.__all__) - {"ARTIFACT", "HEADER", "OBSERVED", "DERIVED"} <= set(
        beadlog_sources.__all__
    )
    for name in evidence_module.__all__:
        assert hasattr(evidence_module, name)


def test_category_set_is_closed_and_every_category_has_a_shape() -> None:
    assert [category.value for category in Category] == [
        "change",
        "review",
        "tracker-item",
        "design-record",
        "plan-item",
        "release-marker",
    ]
    assert set(CATEGORY_SHAPES) == set(Category)
    for category in Category:
        assert category.shape.category is category
        assert category.shape.required


# -- validators ------------------------------------------------------------------------------


@pytest.mark.parametrize("category", list(Category))
def test_every_required_attr_is_enforced_with_a_precise_error(category: Category) -> None:
    valid = ev.BUILDERS[category]()
    for attr in category.shape.required:
        attrs = {name: value for name, value in valid.attrs.items() if name != attr}
        with pytest.raises(ArtifactShapeError) as caught:
            ev.artifact(category, valid.key, attrs, kind=valid.kind)
        error = caught.value
        assert (error.key, error.category, error.attr) == (valid.key, category, attr)
        assert str(error).startswith(f"{category.value} artifact {valid.key!r}: attr {attr!r}")
        assert "is required" in str(error)


@pytest.mark.parametrize(
    ("builder", "override", "attr", "problem"),
    [
        (ev.change, {"parents": "c0ffee1"}, "parents", "array of strings"),
        (ev.change, {"committed_at": "2026-01-01"}, "committed_at", "canonical BeadLog timestamp"),
        (ev.change, {"trailers": []}, "trailers", "object"),
        (ev.review, {"number": "143"}, "number", "integer"),
        (ev.review, {"number": True}, "number", "integer"),
        (ev.tracker_item, {"state": None}, "state", "string"),
        (ev.design_record, {"declared_status": "Accepted"}, "declared_status", "one of"),
        (ev.plan_item, {"done_marker": "yes"}, "done_marker", "boolean"),
        (ev.release_marker, {"version": 1}, "version", "string"),
    ],
)
def test_mistyped_attrs_are_rejected(builder, override, attr, problem) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ArtifactShapeError, match=problem) as caught:
        builder(**override)
    assert caught.value.attr == attr


@pytest.mark.parametrize("value", ["2026-02-28", "2024-02-29", "2026-01-02T03:04:05Z", None])
def test_declared_date_accepts_a_calendar_date_a_timestamp_or_null(value) -> None:  # type: ignore[no-untyped-def]
    record = ev.design_record(declared_date=value)
    assert record.attrs["declared_date"] == value  # a day-precision date is kept as written
    _validator().validate(record.to_json())


@pytest.mark.parametrize(
    "value",
    ["2026-13-01", "2026-02-30", "2025-02-29", "2026-1-01", "20260101", "Nov 2026", "", 20260101],
)
def test_declared_date_refuses_anything_else(value) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ArtifactShapeError, match="calendar date YYYY-MM-DD") as caught:
        ev.design_record(declared_date=value)
    assert caught.value.attr == "declared_date"


def test_schema_refuses_malformed_dates() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    line = ev.design_record().to_json()
    for value in ("2026-13-01", "Nov 2026", "2026-01-01T00:00:00"):
        with pytest.raises(jsonschema.ValidationError):
            _validator().validate({**line, "attrs": {**line["attrs"], "declared_date": value}})


def test_extra_attrs_and_nullable_attrs_are_allowed() -> None:
    record = ev.design_record(declared_status=None, title_case="Kept", supersedes=["doc:old.md"])
    assert record.attrs["title_case"] == "Kept"


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"category": "commit"}, "unknown category"),
        ({"provenance": "inferred"}, "readers never infer"),
        ({"occurred_at": "2026-01-01 00:00:00"}, "occurred_at"),
        ({"key": ""}, "artifact key"),
        ({"reader": ""}, "reader"),
        ({"attrs": {**ev.change().attrs, "ratio": 0.5}}, "non-integer number"),
    ],
)
def test_invalid_record_fields_are_rejected(override, message) -> None:  # type: ignore[no-untyped-def]
    base = ev.change()
    fields = {
        "category": base.category,
        "kind": base.kind,
        "key": base.key,
        "title": base.title,
        "occurred_at": base.occurred_at,
        "attrs": dict(base.attrs),
        "reader": base.reader,
        "reader_version": base.reader_version,
        **override,
    }
    with pytest.raises(EvidenceFormatError, match=message):
        Artifact(**fields)


def test_refs_are_validated_and_artifacts_are_frozen() -> None:
    with pytest.raises(EvidenceFormatError):
        Ref("bead-token", "", "subject")
    record = ev.change(paths=["a.py"], trailers={"Refs": ["bh-1"]})
    with pytest.raises(TypeError):
        record.attrs["sha"] = "x"  # type: ignore[index]
    assert record.attrs["paths"] == ("a.py",)
    assert record.attrs["trailers"]["Refs"] == ("bh-1",)


# -- the fixtures kit ------------------------------------------------------------------------


def test_fixtures_build_one_artifact_of_every_category_in_one_line_each() -> None:
    records = (
        ev.change(),
        ev.review(),
        ev.tracker_item(),
        ev.design_record(),
        ev.plan_item(),
        ev.release_marker(),
    )
    assert [record.category for record in records] == list(Category)
    assert ev.one_of_each() == records
    assert {record.reader for record in records} == {ev.READER}
    corpus = ev.corpus(*records)
    assert [header.name for header in corpus.headers] == [ev.READER]
    assert corpus.by_category("review") == (ev.review(),)


# -- the corpus ------------------------------------------------------------------------------


def test_round_trip_is_byte_identical_and_ids_are_stable(tmp_path: Path) -> None:
    data = dumps_evidence(_corpus())
    assert dumps_evidence(loads_evidence(data)) == data
    assert dumps_evidence(_corpus()) == data  # a second build, the same bytes
    path = write_evidence(_corpus(), tmp_path / "evidence.jsonl")
    assert path.read_bytes() == data
    assert read_evidence(path) == _corpus()
    ids = [record.id for record in _corpus().artifacts]
    assert ids == [record.id for record in read_evidence(path).artifacts]
    assert all(record_id.startswith("sha256:") and len(record_id) == 71 for record_id in ids)


def test_record_id_pins_the_canonical_bytes() -> None:
    record = ev.release_marker("v0.1.0")
    # A golden id: changing canonical serialization or the identity rule must be deliberate.
    assert artifact_id(record) == record.id == _golden_release_id()
    renamed = ev.release_marker("v0.1.0", reader="git")
    assert renamed.id != record.id
    # A reader version bump with identical output keeps its ids (the header carries the version).
    bumped = ev.release_marker("v0.1.0", reader_version="1.0.1")
    assert bumped.id == record.id
    assert bumped.to_json()["reader_version"] == "1.0.1"
    assert loads_evidence(dumps_evidence(ev.corpus(bumped))).artifacts == (bumped,)
    assert ev.change(refs=[ev.ref("bh-1")]).id != ev.change().id


def _golden_release_id() -> str:
    line = (
        '{"attrs":{"commit_range":null,"released_at":"2026-01-01T00:00:00Z","version":"v0.1.0"},'
        '"category":"release-marker","key":"git:tag/v0.1.0","kind":"tag",'
        '"occurred_at":"2026-01-01T00:00:00Z","provenance":"observed","raw_attrs":{},'
        '"reader":"fixture","refs":[],"title":"git:tag/v0.1.0",'
        '"type":"artifact"}'
    )
    return "sha256:" + hashlib.sha256(line.encode()).hexdigest()


def test_layout_is_headers_by_name_then_records_by_key() -> None:
    lines = [json.loads(line) for line in dumps_evidence(_corpus()).splitlines()]
    headers = [line for line in lines if line["type"] == "header"]
    records = [line for line in lines if line["type"] == "artifact"]
    assert lines == headers + records
    assert [header["name"] for header in headers] == ["fixture", "git"]
    assert all(header["schema"] == EVIDENCE_SCHEMA for header in headers)
    assert headers[1]["options"] == {"depth": 0} and headers[1]["cursor"] == {"head": "a1b2c3d"}
    assert [record["key"] for record in records] == sorted(record["key"] for record in records)


def test_construction_order_does_not_change_the_bytes() -> None:
    corpus = _corpus()
    shuffled = EvidenceCorpus(corpus.headers[::-1], corpus.artifacts[::-1])
    assert dumps_evidence(shuffled) == dumps_evidence(corpus)


def test_empty_corpus_is_the_empty_file() -> None:
    assert dumps_evidence(ev.corpus()) == b""
    assert loads_evidence(b"") == ev.corpus()


FIXTURE = ReaderHeader(ev.READER, ev.READER_VERSION)


@pytest.mark.parametrize(
    ("artifacts", "headers", "message"),
    [
        ((ev.change(),), (), "has no header"),
        ((ev.change(),), (ReaderHeader(ev.READER, "9"),), "differs from header version"),
        ((ev.change(), ev.change()), (FIXTURE,), "appears twice"),
        ((), (FIXTURE, FIXTURE), "two headers"),
    ],
)
def test_corpus_invariants(artifacts, headers, message) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(EvidenceFormatError, match=message):
        EvidenceCorpus(headers, artifacts)


def _lines() -> list[bytes]:
    return dumps_evidence(_corpus()).splitlines(keepends=True)


def _tampered_id() -> bytes:
    lines = _lines()
    record = json.loads(lines[-1])
    record["title"] = "edited"
    lines[-1] = canonical_json(record).encode() + b"\n"
    return b"".join(lines)


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (lambda: b"".join(_lines())[:-1], "end with a newline"),
        (lambda: b"not json\n", "not JSON"),
        (lambda: b"[1]\n", "not a JSON object"),
        (lambda: b'{"type": "header"}\n', "not canonical"),
        (lambda: b'{"type":"note"}\n', "unknown line type"),
        (lambda: b'{"ratio":0.5,"type":"note"}\n', "non-integer number"),
        (lambda: b"".join(_lines()[:2][::-1] + _lines()[2:]), "headers out of name order"),
        (lambda: b"".join([_lines()[0], _lines()[2], _lines()[1]]), "header after an artifact"),
        (lambda: b"".join(_lines()[:2] + _lines()[2:][::-1]), "out of"),
        (_tampered_id, "does not match its content"),
        (
            lambda: _lines()[0].replace(b"bdlg.evidence/1", b"bdlg.evidence/2"),
            "unsupported evidence schema",
        ),
    ],
)
def test_reader_refuses_what_the_writer_would_not_write(data, message) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(EvidenceFormatError, match=message):
        loads_evidence(data())


# -- the JSON Schema -------------------------------------------------------------------------


def test_docs_copy_of_the_schema_is_byte_identical() -> None:
    packaged = Path(evidence_module.__file__).parent / "schemas" / "bdlg-evidence-v1.schema.json"
    assert DOCS_SCHEMA.read_bytes() == packaged.read_bytes()


def _validator():  # type: ignore[no-untyped-def]
    jsonschema = pytest.importorskip("jsonschema")
    schema = evidence_schema()
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema)


def test_every_written_line_validates_against_the_schema() -> None:
    validator = _validator()
    for line in dumps_evidence(_corpus()).splitlines():
        validator.validate(json.loads(line))


def test_schema_requires_each_categorys_attrs() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    validator = _validator()
    for category in Category:
        line = ev.BUILDERS[category]().to_json()
        for attr in category.shape.required:
            broken = {**line, "attrs": {k: v for k, v in line["attrs"].items() if k != attr}}
            with pytest.raises(jsonschema.ValidationError):
                validator.validate(broken)
    with pytest.raises(jsonschema.ValidationError):
        validator.validate({**ev.change().to_json(), "provenance": "inferred"})
    with pytest.raises(jsonschema.ValidationError):
        validator.validate({**ev.change().to_json(), "extra": 1})
