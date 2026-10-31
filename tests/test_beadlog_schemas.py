"""Guards the BeadLog v0.1 format contract (bead bh-65jen).

The design deliverable is ``docs/design/beadlog-format-adr.md`` plus the JSON Schemas and the
worked example under ``docs/schemas/beadlog/v0.1``. There is no BeadLog code yet (that is
``packages/beadlog``, bead bh-lycrj); what this pins is the agreement between the prose, the
schemas and the examples:

* the schemas validate the example MANIFEST and every example event, and the invalid examples
  (missing id, unknown required capability, ...) fail;
* every example event id is the SHA-256 of the RFC 8785 serialization of its identity
  projection, and the ADR's worked id vectors hold byte-for-byte;
* the example archive is internally consistent (file digests, ordering, the recorded-time index,
  the checkpoint fold);
* the ADR cites every source URL of the store-landscape research, and BEAD_GROUPS links it.

Pure file reads; no store, no clock, no network.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import jsonschema
import pytest

_REPO = Path(__file__).resolve().parents[1]
_V01 = _REPO / "docs" / "schemas" / "beadlog" / "v0.1"
_EXAMPLES = _V01 / "examples"
_ARCHIVE = _EXAMPLES / "archive"
_ADR = _REPO / "docs" / "design" / "beadlog-format-adr.md"
_RESEARCH = _REPO / "docs" / "design" / "beadlog-store-landscape-research.md"
_BEAD_GROUPS = _REPO / "docs" / "BEAD_GROUPS.md"

_KINDS = (
    "create",
    "set",
    "label_add",
    "label_remove",
    "edge_add",
    "edge_remove",
    "comment",
    "phase",
    "retire",
    "delete",
    "tombstone",
    "checkpoint",
)
# ADR "Event id": the identity members, besides source.system and source.ref.
_IDENTITY = (
    "kind",
    "bead",
    "occurred_at",
    "occurred_precision",
    "recorded_at",
    "provenance",
    "actor",
    "lamport",
    "prev",
    "data",
)
_MAX_SAFE = 2**53 - 1


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _validator(name: str) -> jsonschema.Draft202012Validator:
    return jsonschema.Draft202012Validator(_load(_V01 / f"{name}.schema.json"))


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# -- an independent RFC 8785 serializer for the restricted identity domain ---------------------


def _jcs_string(text: str) -> str:
    out = []
    for ch in text:
        code = ord(ch)
        assert not 0xD800 <= code <= 0xDFFF, "lone surrogate in identity material"
        if ch in {'"', "\\"}:
            out.append("\\" + ch)
        elif ch in "\b\t\n\f\r":
            out.append({"\b": "\\b", "\t": "\\t", "\n": "\\n", "\f": "\\f", "\r": "\\r"}[ch])
        elif code < 0x20:
            out.append(f"\\u{code:04x}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _jcs(value: object) -> str:
    if value is None or isinstance(value, bool):
        return {None: "null", True: "true", False: "false"}[value]
    if isinstance(value, int):
        assert abs(value) <= _MAX_SAFE, "integer outside the I-JSON safe range"
        return str(value)
    if isinstance(value, float):
        raise ValueError("non-integer number in identity material")
    if isinstance(value, str):
        return _jcs_string(value)
    if isinstance(value, list):
        return "[" + ",".join(_jcs(item) for item in value) + "]"
    assert isinstance(value, dict)
    keys = sorted(value, key=lambda key: key.encode("utf-16-be"))
    return "{" + ",".join(f"{_jcs_string(key)}:{_jcs(value[key])}" for key in keys) + "}"


def _projection(event: dict) -> dict:
    projected = {key: event[key] for key in _IDENTITY if event.get(key) is not None}
    projected["source"] = {"system": event["source"]["system"], "ref": event["source"]["ref"]}
    return projected


def _event_id(event: dict) -> str:
    digest = hashlib.sha256(_jcs(_projection(event)).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


# -- ordering ----------------------------------------------------------------------------------


def _instant(timestamp: str | None) -> tuple:
    """Canonical timestamps compare as instants; null sorts after every instant."""
    if timestamp is None:
        return (1, "", 0)
    fraction = timestamp[20:-1] if timestamp[19] == "." else ""
    return (0, timestamp[:19], int(fraction.ljust(9, "0") or "0"))


def _order_key(event: dict) -> tuple:
    first = event["occurred_at"] if event["occurred_at"] is not None else event["recorded_at"]
    return (_instant(first), _instant(event["recorded_at"]), event["id"])


def _part_key(event: dict) -> tuple:
    bead = event["bead"]
    return (bead["group"].encode(), bead["id"].encode(), *_order_key(event))


# -- fixtures ----------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def manifest() -> dict:
    return _load(_ARCHIVE / "MANIFEST.json")


@pytest.fixture(scope="module")
def located(manifest: dict) -> list[tuple[dict, str, int]]:
    """Every archive event with the part it lives in and its 1-based line number."""
    rows = []
    for entry in manifest["files"]:
        if entry["role"] in {"events", "checkpoints"}:
            for number, event in enumerate(_jsonl(_ARCHIVE / entry["path"]), start=1):
                rows.append((event, entry["path"], number))
    return rows


# -- schema <-> example agreement --------------------------------------------------------------


@pytest.mark.parametrize("name", ["manifest", "event", "loss-report"])
def test_schemas_are_valid_draft_2020_12(name: str) -> None:
    jsonschema.Draft202012Validator.check_schema(_load(_V01 / f"{name}.schema.json"))


def test_example_manifest_and_every_example_event_validate(
    manifest: dict, located: list[tuple[dict, str, int]]
) -> None:
    _validator("manifest").validate(manifest)
    validator = _validator("event")
    for event, part, number in located:
        errors = [error.message for error in validator.iter_errors(event)]
        assert not errors, f"{part}:{number}: {errors}"


@pytest.mark.parametrize(
    ("example", "schema"),
    [
        ("manifest-plan.json", "manifest"),
        ("manifest-minimal.json", "manifest"),
        ("loss-report.json", "loss-report"),
    ],
)
def test_valid_examples_validate(example: str, schema: str) -> None:
    _validator(schema).validate(_load(_EXAMPLES / "valid" / example))


def test_every_invalid_example_fails() -> None:
    examples = sorted((_EXAMPLES / "invalid").glob("*.json"))
    assert examples
    for path in examples:
        schema = "event" if path.name.startswith("event-") else "manifest"
        assert not _validator(schema).is_valid(_load(path)), f"{path.name} unexpectedly valid"


def test_event_missing_id_fails_on_id() -> None:
    bad = _load(_EXAMPLES / "invalid/event-missing-id.json")
    errors = list(_validator("event").iter_errors(bad))
    assert [error.message for error in errors] == ["'id' is a required property"]


def test_unknown_required_capability_fails() -> None:
    bad = _load(_EXAMPLES / "invalid/manifest-unknown-required-capability.json")
    errors = list(_validator("manifest").iter_errors(bad))
    assert errors
    assert all(list(error.absolute_path)[:2] == ["capabilities", "required"] for error in errors)
    # The same name is acceptable as an *optional* capability: a reader may ignore it.
    bad["capabilities"]["required"].remove("kind:alias")
    bad["capabilities"]["optional"].append("kind:alias")
    _validator("manifest").validate(bad)


# -- event ids ---------------------------------------------------------------------------------


def test_every_example_event_id_recomputes(located: list[tuple[dict, str, int]]) -> None:
    for event, part, number in located:
        assert _event_id(event) == event["id"], f"{part}:{number}"


def test_worked_id_vectors_hold_byte_for_byte() -> None:
    vectors = _load(_EXAMPLES / "id-vectors.json")["vectors"]
    assert len(vectors) >= 4
    adr = _ADR.read_text(encoding="utf-8")
    for vector in vectors:
        projection = _jcs(_projection(vector["event"]))
        assert projection == vector["projection_jcs"], vector["name"]
        digest = hashlib.sha256(projection.encode("utf-8")).hexdigest()
        assert f"sha256:{digest}" == vector["id"] == vector["event"]["id"]
        assert vector["id"] in adr, f"ADR does not show vector {vector['name']}"


def test_excluded_members_do_not_change_the_id() -> None:
    event = _load(_EXAMPLES / "id-vectors.json")["vectors"][1]["event"]
    varied = json.loads(json.dumps(event))
    varied["extracted_at"] = "2027-01-01T00:00:00Z"
    varied["confidence"] = 0.1
    varied["source"]["evidence"] = []
    varied["annotations"] = {"x.example": 1}
    varied["actor"] = None  # a null identity member is dropped, exactly like an absent one
    assert _event_id(varied) == event["id"]
    varied["provenance"] = "observed"
    assert _event_id(varied) != event["id"]


def test_float_identity_material_is_rejected() -> None:
    with pytest.raises(ValueError):
        _event_id(_load(_EXAMPLES / "invalid/event-float-in-identity.json"))


# -- archive consistency -----------------------------------------------------------------------


def test_manifest_files_match_archive_members(manifest: dict) -> None:
    # The unpacked example omits `mimetype` (fixed content, written first by any packer).
    assert not (_ARCHIVE / "mimetype").exists()
    members = sorted(
        path.relative_to(_ARCHIVE).as_posix() for path in _ARCHIVE.rglob("*") if path.is_file()
    )
    listed = [entry["path"] for entry in manifest["files"]]
    assert listed == sorted(listed)
    assert sorted([*listed, "MANIFEST.json"]) == members
    for entry in manifest["files"]:
        data = (_ARCHIVE / entry["path"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == entry["sha256"], entry["path"]
        assert len(data) == entry["bytes"], entry["path"]
        if "records" in entry:
            assert data.count(b"\n") == entry["records"], entry["path"]
            assert data.endswith(b"\n")


def test_archive_schema_copies_are_byte_identical() -> None:
    for name in ("manifest.schema.json", "event.schema.json"):
        assert (_ARCHIVE / "schema" / name).read_bytes() == (_V01 / name).read_bytes()


def test_parts_are_sorted_and_counts_match(
    manifest: dict, located: list[tuple[dict, str, int]]
) -> None:
    for entry in manifest["files"]:
        if entry["role"] in {"events", "checkpoints"}:
            events = _jsonl(_ARCHIVE / entry["path"])
            assert events == sorted(events, key=_part_key), entry["path"]
    events = [event for event, _, _ in located]
    assert len({event["id"] for event in events}) == len(events) == manifest["counts"]["events"]
    by_kind = {kind: sum(event["kind"] == kind for event in events) for kind in _KINDS}
    assert by_kind == manifest["counts"]["by_kind"]
    assert all(by_kind.values()), "the worked example exercises every kind"
    assert len({event["bead"]["id"] for event in events}) == manifest["counts"]["beads"]
    groups = {group["id"] for group in manifest["groups"]}
    assert {event["bead"]["group"] for event in events} <= groups


def test_recorded_index_replays_every_event_in_recorded_order(
    manifest: dict, located: list[tuple[dict, str, int]]
) -> None:
    capabilities = manifest["capabilities"]["optional"] + manifest["capabilities"]["required"]
    assert "recorded-time-monotone-load" in capabilities
    index = _jsonl(_ARCHIVE / "index/recorded.jsonl")
    where = {event["id"]: (event, part, number) for event, part, number in located}
    assert sorted(row["id"] for row in index) == sorted(where)
    keys = [(_instant(row["recorded_at"]), row["id"]) for row in index]
    assert keys == sorted(keys)
    for row in index:
        event, part, number = where[row["id"]]
        assert (row["recorded_at"], row["part"], row["line"]) == (
            event["recorded_at"],
            part,
            number,
        )


def test_tombstoned_content_is_absent(located: list[tuple[dict, str, int]]) -> None:
    ids = {event["id"] for event, _, _ in located}
    tombstones = [event for event, _, _ in located if event["kind"] == "tombstone"]
    assert tombstones
    for tombstone in tombstones:
        assert not set(tombstone["data"]["events"]) & ids


def test_checkpoint_matches_the_fold_of_its_bead(located: list[tuple[dict, str, int]]) -> None:
    """ADR fold rules: create seeds type/phase/fields; set, phase and label/edge add/remove
    are last-writer-wins per field / per element on the ordering key; null fields are omitted."""
    events = [event for event, _, _ in located]
    for checkpoint in (event for event in events if event["kind"] == "checkpoint"):
        bead = checkpoint["bead"]
        cut = checkpoint["data"]["covers_through"]
        history = sorted(
            (e for e in events if e["bead"] == bead and e["kind"] != "checkpoint"),
            key=_order_key,
        )
        history = [e for e in history if _order_key(e) <= _order_key(cut)]
        assert history[-1]["id"] == cut["id"]
        state: dict = {"fields": {}, "labels": set(), "edges": set(), "deleted": False}
        for event in history:
            kind, data = event["kind"], event["data"]
            if kind == "create":
                state.update(type=data["type"], phase="open")
                state["fields"].update(data["fields"])
            elif kind == "set":
                state["fields"][data["field"]] = data["value"]
            elif kind == "phase":
                state["phase"] = data["phase"]
            elif kind in {"label_add", "label_remove"}:
                getattr(state["labels"], "add" if kind == "label_add" else "discard")(data["label"])
            elif kind in {"edge_add", "edge_remove"}:
                getattr(state["edges"], "add" if kind == "edge_add" else "discard")(_jcs(data))
        expected = checkpoint["data"]["state"]
        assert expected["type"] == state["type"]
        assert expected["phase"] == state["phase"]
        assert expected["fields"] == {k: v for k, v in state["fields"].items() if v is not None}
        assert expected["labels"] == sorted(state["labels"])
        assert sorted(_jcs(edge) for edge in expected["edges"]) == sorted(state["edges"])
        assert expected["deleted"] is state["deleted"]


# -- documentation links -----------------------------------------------------------------------


def test_adr_references_every_research_source_url() -> None:
    urls = set(re.findall(r"<(https?://[^>]+)>", _RESEARCH.read_text(encoding="utf-8")))
    assert len(urls) >= 34
    adr = _ADR.read_text(encoding="utf-8")
    missing = sorted(url for url in urls if url not in adr)
    assert not missing, missing


def test_bead_groups_occurrence_time_row_links_the_adr() -> None:
    rows = [
        line
        for line in _BEAD_GROUPS.read_text(encoding="utf-8").splitlines()
        if line.startswith("| Occurrence time |")
    ]
    assert rows and all("design/beadlog-format-adr.md" in row for row in rows)
