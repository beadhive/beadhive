"""Event ids and RFC 8785 canonicalization (format ADR section 4)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

import beadlog

EXAMPLES = (
    Path(__file__).resolve().parents[3] / "docs" / "schemas" / "beadlog" / "v0.1" / "examples"
)
GROUP = "urn:uuid:5f0c9a52-8a43-4f0e-9a5e-1c7d2b3e4f60"


def _archive_events() -> list[dict]:
    lines = []
    for part in ("events/bh/part-0000.jsonl", "checkpoints/bh/part-0000.jsonl"):
        lines += (EXAMPLES / "archive" / part).read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


VECTORS = json.loads((EXAMPLES / "id-vectors.json").read_text(encoding="utf-8"))["vectors"]


@pytest.mark.parametrize("vector", VECTORS, ids=[vector["name"] for vector in VECTORS])
def test_worked_id_vectors_hold_byte_for_byte(vector: dict) -> None:
    projection = beadlog.identity_projection(vector["event"])
    assert beadlog.canonical_json(projection) == vector["projection_jcs"]
    assert beadlog.event_id(vector["event"]) == vector["id"]
    parsed = beadlog.BeadEvent.from_json(vector["event"])
    assert beadlog.event_id(parsed) == vector["id"] == parsed.id


def test_every_example_archive_event_id_recomputes() -> None:
    events = _archive_events()
    assert len(events) == 22
    for raw in events:
        event = beadlog.BeadEvent.from_json(raw)
        assert beadlog.event_id(event) == raw["id"]
        assert beadlog.BeadEvent.from_json(event.to_json()) == event


def test_reextraction_with_different_non_identity_members_keeps_the_id() -> None:
    raw = next(event for event in _archive_events() if event["kind"] == "create")
    again = json.loads(json.dumps(raw))
    again["extracted_at"] = "2027-01-01T00:00:00Z"
    again["confidence"] = 0.1
    again["source"]["evidence"] = [{"type": "file", "ref": "elsewhere.md", "note": "re-read"}]
    again["annotations"] = {"x-tool": {"run": 2}}
    again["redacted"] = ["/data/fields"]
    again["id"] = "sha256:" + "f" * 64
    assert beadlog.event_id(again) == raw["id"]


def test_top_level_nulls_are_dropped_but_nulls_inside_data_are_kept() -> None:
    base = {
        "kind": "set",
        "bead": {"group": GROUP, "id": "bh-ex1"},
        "occurred_at": None,
        "recorded_at": None,
        "source": {"system": "bd", "ref": "bd:events/1"},
        "provenance": "observed",
        "data": {"field": "assignee", "value": None},
    }
    with_null_actor = dict(base, actor=None)
    assert beadlog.event_id(base) == beadlog.event_id(with_null_actor)
    assert "occurred_at" not in beadlog.identity_projection(base)
    assert beadlog.identity_projection(base)["data"] == {"field": "assignee", "value": None}
    without_value = dict(base, data={"field": "assignee"})
    assert beadlog.event_id(base) != beadlog.event_id(without_value)


def test_different_sources_of_one_fact_have_different_ids() -> None:
    raw = next(event for event in _archive_events() if event["kind"] == "create")
    other = json.loads(json.dumps(raw))
    other["source"]["ref"] = "docs:docs/plans/other.md#L1"
    assert beadlog.event_id(other) != raw["id"]


@pytest.mark.parametrize(
    "value",
    [2.0, 0.5, 1e3, 2**53, -(2**53), float("nan"), b"bytes", {1: "x"}, "\ud800"],
    ids=[
        "integral-float",
        "fraction",
        "exponent",
        "unsafe",
        "unsafe-neg",
        "nan",
        "bytes",
        "int-key",
        "surrogate",
    ],
)
def test_inadmissible_identity_values_are_refused(value: object) -> None:
    with pytest.raises(beadlog.AdmissibleValueError):
        beadlog.canonical_json({"value": value})


def test_safe_integer_bounds_are_admissible() -> None:
    assert beadlog.canonical_json([2**53 - 1, -(2**53 - 1), 0, -7]) == (
        "[9007199254740991,-9007199254740991,0,-7]"
    )


def test_string_escaping_follows_ecmascript() -> None:
    text = 'q"b\\\b\t\n\f\r\x00\x1f\x7f é ✓ \U0001f600'
    assert beadlog.canonical_json(text) == (
        '"q\\"b\\\\\\b\\t\\n\\f\\r\\u0000\\u001f\x7f é ✓ \U0001f600"'
    )


def test_object_members_sort_by_utf16_code_units_not_code_points() -> None:
    # U+FF61 < U+1F600 by code point, but the emoji's UTF-16 lead surrogate 0xD83D sorts first.
    value = {"｡": 1, "\U0001f600": 2, "b": 3, "a": 4}
    assert beadlog.canonical_json(value) == '{"a":4,"b":3,"\U0001f600":2,"｡":1}'
    naive = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert naive != beadlog.canonical_json(value)


def test_mint_event_reproduces_a_vector() -> None:
    vector = next(vector for vector in VECTORS if vector["name"] == "create-day-precision")
    event = vector["event"]
    minted = beadlog.mint_event(
        event["kind"],
        beadlog.BeadRef(**event["bead"]),
        event["data"],
        occurred_at=event["occurred_at"],
        occurred_precision=event["occurred_precision"],
        recorded_at=event["recorded_at"],
        extracted_at="2030-01-01T00:00:00Z",
        source=(event["source"]["system"], event["source"]["ref"]),
        evidence=[beadlog.Evidence("file", "docs/plans/beadlog.md")],
        provenance=event["provenance"],
        confidence=0.75,
    )
    assert minted.id == vector["id"]
    assert minted.source.evidence == (beadlog.Evidence("file", "docs/plans/beadlog.md"),)


def test_mint_event_sorts_and_deduplicates_prev() -> None:
    first, second = "sha256:" + "b" * 64, "sha256:" + "a" * 64
    event = beadlog.mint_event(
        "comment",
        beadlog.BeadRef(GROUP, "bh-1"),
        {"body": "hi"},
        occurred_at=None,
        recorded_at=None,
        extracted_at="2026-01-01T00:00:00Z",
        source=("git-bug", "op/1"),
        provenance="observed",
        lamport=3,
        prev=[first, second, first],
    )
    assert event.prev == (second, first)
    assert beadlog.event_id(event) == event.id


def test_events_reject_malformed_envelopes() -> None:
    raw = _archive_events()[0]
    for broken in (
        dict(raw, id="sha256:XYZ"),
        dict(raw, kind="alias"),
        dict(raw, occurred_at="2026-07-03T00:00:00+00:00"),
        dict(raw, extracted_at="2026-10-30T20:00:00.250Z"),
        {key: value for key, value in raw.items() if key != "recorded_at"},
    ):
        with pytest.raises(ValueError):
            beadlog.BeadEvent.from_json(broken)


@pytest.mark.parametrize(
    "text",
    ["2026-08-08T14:30:00.25Z", "2026-08-08T14:30:00Z", "2026-02-28T23:59:59.123456789Z"],
)
def test_canonical_timestamps_are_accepted(text: str) -> None:
    assert beadlog.check_timestamp(text) == text


@pytest.mark.parametrize(
    "text",
    [
        "2026-08-08T14:30:00.250Z",
        "2026-08-08T14:30:00.0Z",
        "2026-08-08T14:30:00+00:00",
        "2026-08-08T14:30Z",
        "2026-08-08 14:30:00Z",
        "2026-08-08T14:30:00.1234567891Z",
    ],
)
def test_non_canonical_timestamps_are_refused(text: str) -> None:
    with pytest.raises(beadlog.TimestampError):
        beadlog.check_timestamp(text)


def test_timestamps_compare_as_instants_not_text() -> None:
    whole, fraction = "2026-08-08T14:30:00Z", "2026-08-08T14:30:00.25Z"
    assert fraction < whole  # text order is wrong
    assert beadlog.instant(whole) < beadlog.instant(fraction)
    assert beadlog.instant("2026-08-08T14:30:00.000000001Z") < beadlog.instant(fraction)
    assert beadlog.instant(None) > beadlog.instant("9999-12-31T23:59:59.999999999Z")


def test_format_timestamp_spells_instants_canonically() -> None:
    moment = datetime(2026, 8, 8, 16, 30, 0, 250000, tzinfo=timezone(timedelta(hours=2)))
    assert beadlog.format_timestamp(moment) == "2026-08-08T14:30:00.25Z"
    assert beadlog.format_timestamp(datetime(2026, 1, 2, tzinfo=UTC)) == "2026-01-02T00:00:00Z"
    with pytest.raises(beadlog.TimestampError):
        beadlog.format_timestamp(datetime(2026, 1, 2))


def test_ordering_key_falls_back_to_recorded_time_then_infinity() -> None:
    def key(occurred: str | None, recorded: str | None) -> beadlog.OrderKey:
        return beadlog.order_key(occurred, recorded, "sha256:" + "0" * 64)

    assert key(None, "2026-01-01T00:00:00Z").k1 == beadlog.instant("2026-01-01T00:00:00Z")
    assert key("2026-01-02T00:00:00Z", "2026-01-03T00:00:00Z") < key("2026-01-02T00:00:00Z", None)
    assert key("2026-12-31T00:00:00Z", None) < key(None, None)
