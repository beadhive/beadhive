"""The neutral ``bd`` baseline strategy: one ``bd export`` read -> current-state events.

No real ``bd`` runs here: rows are recorded ``bd export --all`` output (bd 1.3.0, embedded Dolt)
and the strategy's runner is a fake that returns them.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

import beadlog
from beadlog_sources import BdBaselineStrategy, BdReadError, ExtractRequest, baseline_events
from beadlog_sources.bd import (
    METADATA_KEY,
    admissible,
    agrees,
    bd_timestamp,
    envelope,
    export_argv,
    facet_key,
    parse_export,
    row_state,
)
from beadlog_sources.cli import discover_strategies, main

GROUP = "urn:uuid:00000000-0000-4000-8000-0000000000bd"
EXTRACTED = "2026-10-31T00:00:00Z"

# Recorded from a scratch `bd init` store (bd 1.3.0): two issues, a memory line, a tombstone.
RECORDED = "\n".join(
    [
        '{"_type":"issue","id":"pr-a1","title":"Alpha2","notes":"n","status":"in_progress",'
        '"priority":2,"issue_type":"task","assignee":"dev/bob","estimated_minutes":30,'
        '"created_at":"2026-01-01T10:00:00Z","updated_at":"2026-10-31T11:02:13Z",'
        '"started_at":"2026-10-31T11:01:52Z","due_at":"2026-02-01T00:00:00Z",'
        '"external_ref":"gh-1","metadata":{"j":{"z":1.5},"k":null,"x.y":3,'
        '"bad key":1,"bdlg":{"phase":{"event_id":"e"}}},"labels":["provider:github","x:1"],'
        '"dependencies":[{"issue_id":"pr-a1","depends_on_id":"conf-zzz","type":"blocks",'
        '"created_at":"2026-10-31T11:02:12Z","created_by":"Brian Cripe","metadata":"{}"}],'
        '"comments":[{"id":"484438e4","issue_id":"pr-a1","author":"dev/carol",'
        '"text":"hi there","created_at":"2026-10-31T11:02:12Z"}]}',
        '{"_type":"issue","id":"pr-b1","title":"Beta","status":"closed","priority":2,'
        '"issue_type":"task","created_at":"2026-01-01T00:00:00Z",'
        '"updated_at":"2026-01-09T12:00:00Z","closed_at":"2026-01-09T12:00:00Z",'
        '"close_reason":"done","dependencies":[{"issue_id":"pr-b1","depends_on_id":"pr-a1",'
        '"type":"related","created_at":"2026-10-31T10:58:04Z"}]}',
        '{"_type":"memory","key":"k","value":"agent context"}',
        '{"_type":"issue","id":"pr-gone","title":"Gone","status":"tombstone"}',
        "",
    ]
)


def _by_kind(events: Sequence[beadlog.BeadEvent], bead: str) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        if event.bead.id == bead:
            out.setdefault(str(event.kind), []).append(event.to_json())
    return out


def test_parse_export_keeps_issue_rows_only() -> None:
    rows = parse_export(RECORDED)
    assert [row["id"] for row in rows] == ["pr-a1", "pr-b1"]
    with pytest.raises(BdReadError, match="line 1 is not JSON"):
        parse_export("{nope\n")


def test_rows_become_a_current_state_baseline() -> None:
    rows = parse_export(RECORDED)
    events = baseline_events(rows, group=GROUP, extracted_at=EXTRACTED)
    a = _by_kind(events, "pr-a1")
    (create,) = a["create"]
    assert create["data"] == {
        "type": "task",
        "fields": {
            "assignee": "dev/bob",
            "due_at": "2026-02-01T00:00:00Z",
            "estimated_minutes": 30,
            "external_ref": "gh-1",
            "metadata.j": {"z": "1.5"},  # floats are not admissible identity material
            "metadata.x.y": 3,
            "notes": "n",
            "priority": 2,
            "title": "Alpha2",
        },
    }
    assert (create["occurred_at"], create["recorded_at"]) == (
        "2026-01-01T10:00:00Z",
        "2026-10-31T11:02:13Z",
    )
    assert create["source"] == {"system": "bd", "ref": "issues/pr-a1#create"}
    assert [p["data"] for p in a["phase"]] == [{"phase": "in_progress"}]
    assert a["phase"][0]["occurred_at"] == "2026-10-31T11:01:52Z"  # started_at
    assert sorted(item["data"]["label"] for item in a["label_add"]) == ["provider:github", "x:1"]
    (edge,) = a["edge_add"]
    assert edge["data"] == {"type": "blocks", "target": {"group": GROUP, "id": "conf-zzz"}}
    assert edge["occurred_at"] == "2026-10-31T11:02:12Z"
    (comment,) = a["comment"]
    assert (comment["actor"], comment["data"]) == ("dev/carol", {"body": "hi there"})

    b = _by_kind(events, "pr-b1")
    assert b["phase"][0]["data"] == {"phase": "closed", "reason": "done"}
    assert b["phase"][0]["occurred_at"] == "2026-01-09T12:00:00Z"  # closed_at
    states = beadlog.fold(events)
    for row in rows:
        assert agrees(states[beadlog.BeadRef(GROUP, row["id"])], row_state(row, GROUP))


def test_extraction_is_deterministic() -> None:
    rows = parse_export(RECORDED)
    first = baseline_events(rows, group=GROUP, extracted_at=EXTRACTED)
    again = baseline_events(list(reversed(rows)), group=GROUP, extracted_at="2026-11-01T00:00:00Z")
    assert [event.id for event in first] == [event.id for event in again]


def _loaded_row(events: Sequence[beadlog.BeadEvent], **row: Any) -> dict[str, Any]:
    """A row as a bd loader leaves it: current state plus metadata.bdlg envelopes."""
    stored = {}
    for event in events:
        key = facet_key(event)
        assert key is not None
        stored[key] = envelope(event)
    return {**row, "metadata": {**row.get("metadata", {}), METADATA_KEY: stored}}


def _mint(kind: str, data: dict[str, Any], at: str, **kw: Any) -> beadlog.BeadEvent:
    return beadlog.mint_event(
        kind,
        beadlog.BeadRef(GROUP, "pr-x"),
        data,
        occurred_at=at,
        recorded_at=kw.pop("recorded_at", None),
        extracted_at=EXTRACTED,
        source=("docs", f"docs:{kind}:{at}"),
        provenance="inferred",
        **kw,
    )


LOADED = (
    _mint(
        "create",
        {"type": "task", "fields": {"title": "Draft", "priority": 1}},
        "2026-01-01T00:00:00Z",
        occurred_precision="day",
    ),
    _mint("set", {"field": "title", "value": "Final"}, "2026-01-02T00:00:00Z"),
    _mint("label_add", {"label": "area:x"}, "2026-01-03T00:00:00Z"),
    _mint("label_remove", {"label": "tmp"}, "2026-01-03T01:00:00Z"),
    _mint(
        "edge_add",
        {"type": "blocks", "target": {"group": GROUP, "id": "pr-y"}},
        "2026-01-04T00:00:00Z",
    ),
    _mint("phase", {"phase": "closed", "reason": "done"}, "2026-01-05T00:00:00Z"),
    _mint("comment", {"body": "noted"}, "2026-01-06T00:00:00Z", actor="dev/a"),
)
ROW = {
    "id": "pr-x",
    "title": "Final",
    "priority": 1,
    "issue_type": "task",
    "status": "closed",
    "close_reason": "done",
    "created_at": "2026-01-01T00:00:00Z",
    "updated_at": "2026-10-31T09:00:00Z",
    "closed_at": "2026-10-31T09:00:00Z",
    "labels": ["area:x"],
    "dependencies": [{"issue_id": "pr-x", "depends_on_id": "pr-y", "type": "blocks"}],
    "comments": [{"id": "c1", "author": "dev/a", "text": "noted"}],
}


def test_stored_envelopes_are_re_emitted_with_their_original_ids() -> None:
    events = baseline_events([_loaded_row(LOADED, **ROW)], group=GROUP, extracted_at=EXTRACTED)
    assert sorted(event.id for event in events) == sorted(event.id for event in LOADED)
    assert all(event.source.system == "docs" for event in events)


def test_a_row_changed_under_a_stored_event_falls_back_to_native_events() -> None:
    changed = {**ROW, "title": "Edited by hand", "labels": ["area:x", "hand"]}
    row = _loaded_row(LOADED, **changed)
    events = baseline_events([row], group=GROUP, extracted_at=EXTRACTED)
    ids = {event.id for event in events}
    title_set = LOADED[1]
    assert title_set.id not in ids  # its value is no longer the row's: not re-emitted
    assert LOADED[2].id in ids  # unrelated facets still come back
    natives = {(str(e.kind), e.source.ref) for e in events if e.source.system == "bd"}
    assert ("label_add", "issues/pr-x#label:hand") in natives
    assert agrees(beadlog.fold(events)[beadlog.BeadRef(GROUP, "pr-x")], row_state(row, GROUP))


def test_a_stored_phase_the_row_contradicts_is_replaced() -> None:
    reopened = {**ROW, "status": "open", "close_reason": None}
    row = _loaded_row(LOADED, **reopened)
    events = baseline_events([row], group=GROUP, extracted_at=EXTRACTED)
    assert LOADED[5].id not in {event.id for event in events}
    assert agrees(beadlog.fold(events)[beadlog.BeadRef(GROUP, "pr-x")], row_state(row, GROUP))


def test_malformed_stored_metadata_never_breaks_the_read() -> None:
    row = {**ROW, "metadata": {METADATA_KEY: {"created": "x", "label:q": {"kind": "nope"}}}}
    events = baseline_events([row], group=GROUP, extracted_at=EXTRACTED)
    assert agrees(beadlog.fold(events)[beadlog.BeadRef(GROUP, "pr-x")], row_state(row, GROUP))


def test_values_and_times_are_normalized() -> None:
    assert bd_timestamp("2026-01-01T10:00:00+02:00") == "2026-01-01T08:00:00Z"
    assert bd_timestamp("2026-01-01T10:00:00.250Z") == "2026-01-01T10:00:00.25Z"
    assert bd_timestamp("yesterday") is None
    assert admissible({"a": [1.5, 2**60, True]}) == {"a": ["1.5", str(2**60), True]}


class FakeRunner:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str]) -> str:
        self.calls.append(list(argv))
        return self.text


def test_strategy_runs_one_bulk_export() -> None:
    runner = FakeRunner(RECORDED)
    strategy = BdBaselineStrategy(runner)
    request = ExtractRequest(root=Path("/repo"), group=GROUP, extracted_at=EXTRACTED)
    events = strategy.extract(request)
    assert runner.calls == [["bd", "-C", "/repo", "export", "--all"]]
    assert {event.bead.id for event in events} == {"pr-a1", "pr-b1"}
    strategy.extract(
        ExtractRequest(
            root=Path("/repo"),
            group=GROUP,
            extracted_at=EXTRACTED,
            options={"all": "false", "binary": "/opt/bd"},
        )
    )
    assert runner.calls[-1] == ["/opt/bd", "-C", "/repo", "export"]
    assert export_argv("/r", all_=False) == ["bd", "-C", "/r", "export"]


class _Point:
    name = "bd"

    def load(self) -> Any:
        return BdBaselineStrategy(FakeRunner(RECORDED))


def test_beadlog_extract_bd_writes_a_baseline_archive(tmp_path: Path) -> None:
    out = tmp_path / "baseline.bdlg"
    code = main(
        [
            "extract",
            "bd",
            "--root",
            str(tmp_path),
            "--group",
            GROUP,
            "--group-dir",
            "bh",
            "--extracted-at",
            EXTRACTED,
            "-o",
            str(out),
        ],
        entry_points=lambda: [_Point()],
        builtin_strategies={},
    )
    assert code == 0
    _, events = beadlog.read_archive(out)
    assert {event.bead.id for event in events} == {"pr-a1", "pr-b1"}
    assert "bd" in discover_strategies(lambda: [_Point()], {})


def test_entry_point_is_declared() -> None:
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    if not pyproject.is_file():  # the Pants sandbox ships sources only
        pytest.skip("package metadata not in this sandbox")
    text = pyproject.read_text(encoding="utf-8")
    assert '[project.entry-points."beadlog.sources"]' in text
    assert 'bd = "beadlog_sources.bd:BdBaselineStrategy"' in text


def test_extracted_events_never_carry_the_loader_bookkeeping() -> None:
    row = _loaded_row(LOADED, **ROW)
    events = baseline_events([row], group=GROUP, extracted_at=EXTRACTED)
    fields = beadlog.fold(events)[beadlog.BeadRef(GROUP, "pr-x")].fields
    assert not any(name.startswith("metadata.bdlg") for name in fields)
    assert json.dumps(row["metadata"][METADATA_KEY], sort_keys=True)  # JSON-serializable
