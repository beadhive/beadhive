"""The opt-in dolt_history gap fill: chunked read-only SELECTs, state collapse, events, cursor.

No real ``bd`` or Dolt runs and no clock or sleep is involved: the store is a fixture
(:class:`FakeDolt`) behind the injected command seam, a counting fake that also records every
statement so the tests can assert that nothing but bounded ``SELECT``s is ever issued.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import subprocess
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from beadlog import BeadEvent, BeadRef, EventKind, fold
from beadlog_sources import (
    BdGapfillError,
    GapfillReader,
    HistoryEvent,
    JsonCursorFile,
    baseline_events,
    gap_events,
    gapfill_annotation,
    history_annotation,
    history_events,
    lacking_created,
)
from beadlog_sources import bd_gapfill as gf
from beadlog_sources.runner import subprocess_runner

GROUP = "urn:uuid:5f0c9a52-8a43-4f0e-9a5e-1c7d2b3e4f60"
STAMP = "2026-11-01T00:00:00Z"
OTHER_STAMP = "2026-12-24T00:00:00Z"


def commit_id(tag: str) -> str:
    """A deterministic 32-character dolt-style commit hash."""
    return hashlib.sha256(tag.encode()).hexdigest()[:32]


def issue(title: str = "T", **extra: Any) -> dict[str, Any]:
    """A ``dolt_history_issues`` / ``issues`` row with every column the gap fill projects."""
    row: dict[str, Any] = {column: None for column in gf.STATE_COLUMNS}
    row.update(
        title=title,
        description="",
        status="open",
        priority=2,
        issue_type="task",
        updated_at="2026-08-01T00:00:00Z",
    )
    row.update(extra)
    return row


class FakeDolt:
    """A bd/Dolt fake answering the two statement shapes the gap fill issues.

    ``history`` maps a bead to its versions as ``(commit, date, row)`` in arbitrary (DAG) order.
    ``digest_columns`` is what the fake *server* digest covers (the real one covers exactly the
    projected columns). ``fail_after`` raises from the Nth scan, to interrupt a run.
    """

    def __init__(
        self,
        history: Mapping[str, Sequence[tuple[str, str, dict[str, Any]]]],
        *,
        digest_columns: Sequence[str] = gf.STATE_COLUMNS,
        fail_after: int | None = None,
    ) -> None:
        self.history = history
        self.digest_columns = tuple(digest_columns)
        self.fail_after = fail_after
        self.statements: list[str] = []
        self.scans: list[list[str]] = []
        self.fetches: list[list[tuple[str, str]]] = []
        self.argv: list[list[str]] = []

    def __call__(self, argv: Sequence[str]) -> str:
        argv = list(argv)
        self.argv.append(argv)
        assert argv[:2] == ["bd", "-C"] and argv[3] == "sql", argv
        mode, sql = argv[4], argv[5]
        self.statements.append(sql)
        if mode == "--csv":
            return self.scan(sql)
        assert mode == "--json", mode
        return self.fetch(sql)

    def digest(self, row: Mapping[str, Any]) -> str:
        values = [("" if row.get(c) is None else str(row[c])) for c in self.digest_columns]
        return hashlib.md5("\x1f".join(values).encode()).hexdigest()[:12]

    def scan(self, sql: str) -> str:
        ids = re.findall(r"'([^']+)'", sql[sql.index(" IN (") :])
        self.scans.append(ids)
        if self.fail_after is not None and len(self.scans) > self.fail_after:
            raise BdGapfillError("bd sql timed out after 180s")
        out = io.StringIO()
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(gf.SCAN_COLUMNS)
        for bead in ids:
            for commit, date, row in self.history.get(bead, ()):
                writer.writerow([bead, commit, date, self.digest(row)])
        return out.getvalue()

    def fetch(self, sql: str) -> str:
        points = re.findall(r"AS OF '([A-Za-z0-9]+)' WHERE id = '([^']+)'", sql)
        self.fetches.append([(bead, commit) for commit, bead in points])
        rows = []
        for commit, bead in points:
            for have, _date, row in self.history[bead]:
                if have == commit:
                    rows.append({"commit_hash": commit, "id": bead, **row})
        return json.dumps(rows)


def versions(
    bead: str, states: Sequence[tuple[str, dict[str, Any]]]
) -> list[tuple[str, str, dict[str, Any]]]:
    """``(date, row)`` pairs as history versions with deterministic commit hashes."""
    return [(commit_id(f"{bead}@{date}"), date, row) for date, row in states]


def at(minute: int, second: int = 0) -> str:
    """A fixed-width raw commit date ``2026-08-01T10:MM:SS.250000Z`` as the scan formats it."""
    return f"2026-08-01T10:{minute:02d}:{second:02d}.250000Z"


def row_for(bead: str, state: Mapping[str, Any], created: str = "2026-06-01T00:00:00Z") -> dict:
    """The bd export row of a bead whose current state is ``state``."""
    base = {
        "id": bead,
        "issue_type": state.get("issue_type", "task"),
        "status": state.get("status", "open"),
        "title": state["title"],
        "priority": state.get("priority", 2),
        "labels": [],
        "created_at": created,
        "updated_at": "2026-10-01T00:00:00Z",
    }
    if state.get("assignee"):
        base["assignee"] = state["assignee"]
    if state.get("status") == "closed":
        base.update(closed_at="2026-09-01T00:00:00Z", close_reason=state.get("close_reason"))
    return base


# -- the store: five beads without a created row, one with ------------------------------------

THIRTY_K = 30_000
BIG = [  # 30,000 versions, four distinct states
    (
        f"2026-08-01T{10 + i // 3600:02d}:{(i // 60) % 60:02d}:{i % 60:02d}.250000Z",
        issue(
            "Draft" if i < 12_000 else "Final",
            status="open" if i < 20_000 else ("in_progress" if i < 25_000 else "closed"),
            assignee=None if i < 20_000 else "dev/a",
            close_reason="done" if i >= 25_000 else None,
            updated_at=f"2026-08-{(i % 28) + 1:02d}T00:00:00Z",  # volatile: never a state
        ),
    )
    for i in range(THIRTY_K)
]


def fixture_history() -> dict[str, list[tuple[str, str, dict[str, Any]]]]:
    revert = [  # listed in DAG order, not date order; A -> B -> A
        (at(30), issue("A")),
        (at(10), issue("B")),
        (at(20), issue("A")),
    ]
    revert_sorted = sorted(revert, key=lambda item: item[0])
    tie = [(at(5), issue("Left")), (at(5), issue("Right"))]  # one date, two commits
    return {
        "gx-big": versions("gx-big", BIG),
        "gx-revert": versions("gx-revert", [revert_sorted[1], revert_sorted[0], revert_sorted[2]]),
        "gx-same": versions("gx-same", [(at(i), issue("Same")) for i in range(1, 40)]),
        "gx-tie": [(commit_id(f"tie-{row['title']}"), date, row) for date, row in tie],
        "gx-noise": versions(
            "gx-noise",
            [(at(i), issue("Noise", updated_at=f"2026-08-0{i}T00:00:00Z")) for i in range(1, 6)],
        ),
        "gx-has-created": versions(
            "gx-has-created", [(at(1), issue("Old")), (at(2), issue("New"))]
        ),
    }


CURRENT = {
    "gx-big": issue("Final", status="closed", assignee="dev/a", close_reason="done"),
    "gx-revert": issue("A"),
    "gx-same": issue("Same"),
    "gx-tie": None,  # decided below from the tiebreak
    "gx-noise": issue("Noise"),
    "gx-has-created": issue("New"),
}


def tie_final(history: Mapping[str, Any]) -> dict[str, Any]:
    """The state of the later of the two same-date commits (commit hash breaks the tie)."""
    commit, _date, row = max(history["gx-tie"], key=lambda item: (item[1], item[0]))
    return row


def world(history: Mapping[str, Any] | None = None) -> tuple[Any, list[BeadEvent], list[BeadEvent]]:
    """``(history, baseline, history events)`` for the fixture store."""
    history = history or fixture_history()
    current = dict(CURRENT, **{"gx-tie": tie_final(history)})
    rows = [row_for(bead, state) for bead, state in sorted(current.items())]
    baseline = list(baseline_events(rows, group=GROUP, extracted_at=STAMP))
    table = [HistoryEvent("u-1", "gx-has-created", "created", "2026-08-07T08:00:00Z")]
    result = history_events(table, baseline, group=GROUP, extracted_at=STAMP)
    return history, baseline, list(result.events)


@pytest.fixture(scope="module")
def fixture() -> tuple[Any, list[BeadEvent], list[BeadEvent]]:
    return world()


def read_all(history: Mapping[str, Any], ids: Sequence[str], batch: int = 3) -> Any:
    dolt = FakeDolt(history)
    return dolt, GapfillReader("/hive", batch_size=batch, runner=dolt).read(ids)


# -- the statements -----------------------------------------------------------------------------


def test_the_statements_are_bounded_selects_with_a_formatted_date_and_a_projected_digest() -> None:
    scan = gf.scan_sql(["bh-1", "bh-2"])
    assert scan.startswith("SELECT ") and scan.endswith("WHERE id IN ('bh-1', 'bh-2')")
    assert "FROM dolt_history_issues" in scan and "content_hash" not in scan
    for column in gf.STATE_COLUMNS:
        assert f"COALESCE({column}, '')" in scan
    fetch = gf.fetch_sql([("bh-1", "c" * 32), ("bh-2", "d" * 32)])
    assert fetch.count("UNION ALL") == 1 and fetch.count("SELECT ") == 2
    assert f"issues AS OF '{'c' * 32}' WHERE id = 'bh-1'" in fetch
    for sql in (scan, fetch):
        assert not re.search(r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|CALL|SET)\b", sql)


def test_every_statement_a_run_issues_is_a_select_in_bounded_id_batches() -> None:
    history, _, _ = world()
    dolt, read = read_all(history, sorted(history), batch=2)
    assert dolt.statements and all(sql.startswith("SELECT ") for sql in dolt.statements)
    assert all(len(ids) <= 2 for ids in dolt.scans)
    assert [bead for ids in dolt.scans for bead in ids] == sorted(history)  # each bead once
    assert read.counts["batches"] == 3 and read.counts["beads_queried"] == 6
    assert all(len(points) <= gf.FETCH_CHUNK for points in dolt.fetches)


def test_unsafe_ids_are_never_interpolated() -> None:
    dolt = FakeDolt({})
    read = GapfillReader("/hive", runner=dolt).read(["ok-1", "x'; DROP TABLE issues; --"])
    assert dolt.scans == [["ok-1"]] and read.counts["beads_unsafe_id"] == 1


# -- the production runner: timeout and argv ------------------------------------------------


def test_the_default_runner_gives_every_query_the_configured_timeout(monkeypatch) -> None:
    seen: list[dict[str, Any]] = []

    def fake_run(argv, **kwargs):
        seen.append({"argv": list(argv), **kwargs})
        return subprocess.CompletedProcess(argv, 0, "id,commit_hash,d,h\n", "")

    monkeypatch.setattr("beadlog_sources.runner.subprocess.run", fake_run)
    GapfillReader("/hive", batch_size=2, timeout=42.0).read(["a-1", "a-2", "a-3"])
    assert [call["timeout"] for call in seen] == [42.0, 42.0]  # two scans
    assert all(call["argv"][:5] == ["bd", "-C", "/hive", "sql", "--csv"] for call in seen)


def test_a_timeout_is_a_clean_error_not_a_traceback(monkeypatch) -> None:
    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr("beadlog_sources.runner.subprocess.run", fake_run)
    with pytest.raises(BdGapfillError, match="timed out after 7s"):
        GapfillReader("/hive", timeout=7.0).read(["a-1"])
    assert subprocess_runner  # the shared seam, not a private one


# -- distinct states ---------------------------------------------------------------------------


def test_a_thirty_thousand_version_bead_yields_only_its_distinct_states() -> None:
    history, _, _ = world()
    dolt, read = read_all(history, ["gx-big"])
    states = read.states["gx-big"]
    assert read.counts["rows_scanned"] == THIRTY_K
    assert [s.fields["title"] for s in states] == ["Draft", "Final", "Final", "Final"]
    assert [s.phase for s in states] == ["open", "open", "in_progress", "closed"]
    # only the 4 change commits were ever fetched, never the 30,000 versions
    assert dolt.fetches == [[("gx-big", s.commit) for s in states]]
    assert read.counts["hash_states"] == read.counts["projected_states"] == 4


def test_a_bead_that_never_changed_costs_one_scan_and_nothing_else() -> None:
    history, _, _ = world()
    dolt, read = read_all(history, ["gx-same", "gx-noise"])
    assert read.states == {} and not dolt.fetches
    assert read.counts["beads_single_state"] == 2  # one digest throughout, volatile or not


def test_volatile_columns_alone_collapse_after_the_fetch() -> None:
    history, _, _ = world()
    noisy = FakeDolt(history, digest_columns=(*gf.STATE_COLUMNS, "updated_at"))
    read = GapfillReader("/hive", runner=noisy).read(["gx-noise"])
    assert read.counts["hash_states"] == 5  # the finer digest sees 5 states ...
    assert read.counts["projected_states"] == 1 and read.states == {}  # ... the values, one


def test_versions_order_by_commit_date_not_listing_order_and_commit_hash_breaks_ties() -> None:
    history, _, _ = world()
    _, read = read_all(history, ["gx-revert", "gx-tie"])
    revert = read.states["gx-revert"]
    assert [s.fields["title"] for s in revert] == ["B", "A"]  # listed A,B,A; dated B(:10),A(:20)
    assert [s.at for s in revert] == ["2026-08-01T10:10:00.25Z", "2026-08-01T10:20:00.25Z"]
    tie = read.states["gx-tie"]
    by_hash = sorted(history["gx-tie"], key=lambda v: v[0])
    assert [s.commit for s in tie] == [v[0] for v in by_hash]  # same date: commit hash order


def test_a_state_that_returns_is_a_new_state_but_consecutive_repeats_collapse() -> None:
    a, b = (
        gf.GapState("c1", "2026-08-01T00:00:01Z", {"title": "A"}, "open"),
        gf.GapState("c2", "2026-08-01T00:00:02Z", {"title": "B"}, "open"),
    )
    again = gf.GapState("c3", "2026-08-01T00:00:03Z", {"title": "A"}, "open")
    same_a = gf.GapState("c4", "2026-08-01T00:00:04Z", {"title": "A"}, "open", reason="ignored")
    assert [s.commit for s in gf.collapse([a, a, b, b, again, same_a])] == ["c1", "c2", "c3"]
    points = gf.hash_states(
        [("t3", "c3", "A"), ("t1", "c1", "A"), ("t2", "c2", "B"), ("t4", "c4", "A")]
    )
    assert [(p[1], p[2]) for p in points] == [("c1", "A"), ("c2", "B"), ("c3", "A")]


# -- events ------------------------------------------------------------------------------------


def run_events(history: Mapping[str, Any], baseline, table_events, *, stamp: str = STAMP):
    ids = lacking_created(table_events)
    dolt, read = read_all(history, ids)
    full = [*baseline, *table_events]
    return dolt, ids, gap_events(read, full, group=GROUP, extracted_at=stamp, queried=ids)


def test_only_beads_lacking_a_created_event_are_queried_or_filled(fixture) -> None:
    history, baseline, table = fixture
    dolt, ids, result = run_events(history, baseline, table)
    assert "gx-has-created" not in ids and len(ids) == 5
    assert all("gx-has-created" not in sql for sql in dolt.statements)  # not even queried
    assert {e.bead.id for e in result.events} <= set(ids) and result.events
    assert not [e for e in result.events if e.bead.id == "gx-has-created"]


def test_events_are_derived_commit_dated_and_name_the_dolt_commit(fixture) -> None:
    history, baseline, table = fixture
    _, _, result = run_events(history, baseline, table)
    for event in result.events:
        assert event.provenance == "derived"
        assert event.occurred_at == event.recorded_at  # the commit date: nothing finer is known
        assert event.source.system == "dolt"
        commit = event.source.ref.removeprefix("dolt:history/").split("#")[0]
        assert event.source.ref == f"dolt:history/{commit}#{event.bead.id}"
        assert [(e.type, e.ref) for e in event.source.evidence] == [("dolt-commit", commit)]
        assert event.confidence in (gf.DIFF_CONFIDENCE, gf.INITIAL_CONFIDENCE)
    initial = [e for e in result.events if e.confidence == gf.INITIAL_CONFIDENCE]
    diffs = [e for e in result.events if e.confidence == gf.DIFF_CONFIDENCE]
    assert initial and diffs and gf.INITIAL_CONFIDENCE < gf.DIFF_CONFIDENCE < 1


def test_a_bead_gets_sets_for_changed_fields_and_phases_in_commit_order(fixture) -> None:
    history, baseline, table = fixture
    _, _, result = run_events(history, baseline, table)
    big = [e for e in result.events if e.bead.id == "gx-big"]
    summary = [
        (
            e.kind.value,
            e.data.get("field") or e.data.get("phase"),
            e.data.get("value"),
            e.confidence,
        )
        for e in big
    ]
    first, diff = gf.INITIAL_CONFIDENCE, gf.DIFF_CONFIDENCE
    assert sorted(summary[:3]) == sorted(  # S1: the starting value of every field that changes
        [
            ("set", "assignee", None, first),
            ("set", "title", "Draft", first),
            ("phase", "open", None, first),
        ]
    )
    assert sorted(summary[3:]) == sorted(  # then only what changed between consecutive states
        [
            ("set", "title", "Final", diff),
            ("set", "assignee", "dev/a", diff),
            ("phase", "in_progress", None, diff),
            ("phase", "closed", None, diff),
        ]
    )
    closed = next(e for e in big if e.kind is EventKind.PHASE and e.data["phase"] == "closed")
    assert closed.data == {"phase": "closed", "reason": "done"}
    assert [e.occurred_at for e in big] == sorted(e.occurred_at for e in big)  # commit order
    revert = [e for e in result.events if e.bead.id == "gx-revert"]
    assert [(e.data["field"], e.data["value"]) for e in revert if e.kind is EventKind.SET] == [
        ("title", "B"),
        ("title", "A"),
    ]
    assert not [e for e in result.events if e.bead.id in ("gx-same", "gx-noise")]


def test_re_extraction_gives_identical_event_ids(fixture) -> None:
    history, baseline, table = fixture
    _, _, first = run_events(history, baseline, table, stamp=STAMP)
    _, _, second = run_events(history, baseline, table, stamp=OTHER_STAMP)
    ids = [e.id for e in first.events]
    assert ids == [e.id for e in second.events] and len(ids) == len(set(ids))
    assert first.events != second.events  # extracted_at differs; ids ignore it


def test_the_events_never_change_the_fold_of_the_current_state(fixture) -> None:
    history, baseline, table = fixture
    _, _, result = run_events(history, baseline, table)
    before = fold([*baseline, *table])
    after = fold([*baseline, *table, *result.events])
    assert result.events and after == before


def test_a_history_that_disagrees_with_the_current_state_is_dropped_not_applied() -> None:
    history, _, _ = world()
    # the live row says "Right" but the last committed snapshot says "Wrong": a dirty working set
    dirty = dict(history)
    dirty["gx-revert"] = versions("gx-revert", [(at(1), issue("A")), (at(2), issue("Wrong"))])
    rows = [row_for("gx-revert", issue("A"))]
    baseline = list(baseline_events(rows, group=GROUP, extracted_at=STAMP))
    table = list(history_events([], baseline, group=GROUP, extracted_at=STAMP).events)
    _, _, result = run_events(dirty, baseline, table)
    assert not result.events
    assert any(key.startswith("inconsistent:") for key in result.suppressed)
    assert fold([*baseline, *table, *result.events]) == fold([*baseline, *table])


def test_beads_the_baseline_does_not_hold_get_no_events(fixture) -> None:
    history, baseline, table = fixture
    dolt = FakeDolt(history)
    read = GapfillReader("/hive", runner=dolt).read(["gx-big"])
    without = [e for e in [*baseline, *table] if e.bead.id != "gx-big"]
    result = gap_events(read, without, group=GROUP, extracted_at=STAMP, queried=["gx-big"])
    assert not result.events and result.suppressed["orphan_bead"] > 0


def test_the_annotation_and_note_account_for_the_gap_fill(fixture) -> None:
    history, baseline, table = fixture
    _, ids, result = run_events(history, baseline, table)
    account = gapfill_annotation(result, batch_size=3)
    assert account["enabled"] is True and account["source"] == "dolt_history_issues"
    assert account["events"] == len(result.events) and account["beads_queried"] == len(ids)
    assert (
        "commit date" in account["time_semantics"] and "not recovered" in account["time_semantics"]
    )
    plain = history_annotation(
        [*baseline, *table], history_events([], baseline, group=GROUP, extracted_at=STAMP), baseline
    )
    assert "gapfill" not in plain["bdlg.history"]  # absent unless the gap fill ran
    full = history_events([], baseline, group=GROUP, extracted_at=STAMP)
    filled = history_annotation(
        [*baseline, *full.events, *result.events], full, baseline, gapfill=account
    )
    assert filled["bdlg.history"]["gapfill"] == account
    assert filled["bdlg.history"]["sources"]["dolt:history"] == len(result.events)
    assert "gap fill queried" in filled["bdlg.history"]["gap"]["note"]


# -- the resumable cursor ----------------------------------------------------------------------


def final_state(read: Any) -> dict[str, list[dict[str, Any]]]:
    return {bead: [s.to_json() for s in items] for bead, items in read.states.items()}


def test_interrupting_mid_run_and_rerunning_resumes_without_duplicates(tmp_path, fixture) -> None:
    history, baseline, table = fixture
    ids = lacking_created(table)
    cursor = JsonCursorFile(tmp_path / "out.bdlg.gapfill.json")

    uninterrupted = FakeDolt(history)
    clean = GapfillReader("/hive", batch_size=2, runner=uninterrupted).read(ids)
    assert len(uninterrupted.scans) == 3  # 5 beads, 2 per batch

    broken = FakeDolt(history, fail_after=2)  # the third scan raises
    with pytest.raises(BdGapfillError):
        GapfillReader("/hive", batch_size=2, runner=broken).read(ids, cursor)
    saved = json.loads(cursor.path.read_text())
    assert saved["done"] == 4 and saved["total"] == 5  # two completed batches survive
    assert len(broken.scans) == 3

    resumed = FakeDolt(history)
    again = GapfillReader("/hive", batch_size=2, runner=resumed).read(ids, cursor)
    assert resumed.scans == [[ids[4]]]  # only the unfinished tail is read again
    assert again.resumed_from == 4
    assert final_state(again) == final_state(clean)

    one = gap_events(clean, [*baseline, *table], group=GROUP, extracted_at=STAMP, queried=ids)
    two = gap_events(again, [*baseline, *table], group=GROUP, extracted_at=STAMP, queried=ids)
    assert [e.id for e in one.events] == [e.id for e in two.events]
    assert len({e.id for e in two.events}) == len(two.events)  # no duplicate event ids


def test_interrupting_at_every_batch_boundary_gives_the_same_events(tmp_path, fixture) -> None:
    history, baseline, table = fixture
    ids = lacking_created(table)
    expected = gap_events(
        GapfillReader("/hive", batch_size=1, runner=FakeDolt(history)).read(ids),
        [*baseline, *table],
        group=GROUP,
        extracted_at=STAMP,
        queried=ids,
    )
    for stop in range(len(ids)):
        cursor = JsonCursorFile(tmp_path / f"cursor-{stop}.json")
        with pytest.raises(BdGapfillError):
            GapfillReader("/hive", batch_size=1, runner=FakeDolt(history, fail_after=stop)).read(
                ids, cursor
            )
        read = GapfillReader("/hive", batch_size=1, runner=FakeDolt(history)).read(ids, cursor)
        got = gap_events(read, [*baseline, *table], group=GROUP, extracted_at=STAMP, queried=ids)
        assert [e.id for e in got.events] == [e.id for e in expected.events], stop


def test_a_cursor_from_another_run_is_ignored(tmp_path, fixture) -> None:
    history, _, table = fixture
    ids = lacking_created(table)
    cursor = JsonCursorFile(tmp_path / "c.json")
    cursor.save(
        {"schema": gf.CURSOR_SCHEMA, "fingerprint": "sha256:other", "done": 3, "states": {}}
    )
    dolt = FakeDolt(history)
    read = GapfillReader("/hive", batch_size=2, runner=dolt).read(ids, cursor)
    assert read.resumed_from == 0 and sum(len(s) for s in dolt.scans) == len(ids)
    cursor.path.write_text("{not json")  # a torn or foreign file starts over, never crashes
    assert GapfillReader("/hive", batch_size=2, runner=FakeDolt(history)).read(ids, cursor)


def test_the_cursor_file_is_written_atomically_and_cleared(tmp_path) -> None:
    cursor = JsonCursorFile(tmp_path / "nested" / "x.gapfill.json")
    assert cursor.load() is None
    cursor.save({"a": 1})
    cursor.save({"a": 2})
    assert cursor.load() == {"a": 2}
    assert [p.name for p in cursor.path.parent.iterdir()] == ["x.gapfill.json"]  # no temp left
    cursor.clear()
    cursor.clear()  # idempotent
    assert cursor.load() is None


def test_batch_size_must_be_positive() -> None:
    with pytest.raises(ValueError, match="batch_size"):
        GapfillReader("/hive", batch_size=0)


def test_the_scan_parser_reads_a_banner_and_refuses_a_short_row() -> None:
    text = "Warning: something\nid,commit_hash,d,h\nbh-1,c1,2026-08-01T00:00:00.5Z,abc\n"
    assert gf.parse_scan(text) == {"bh-1": [("2026-08-01T00:00:00.5Z", "c1", "abc")]}
    assert gf.parse_scan("") == {}
    with pytest.raises(BdGapfillError):
        gf.parse_scan("id,commit_hash,d,h\nbh-1,c1\n")
    with pytest.raises(BdGapfillError):
        gf.parse_scan("nonsense\n")


def test_a_bead_with_no_history_rows_is_counted(tmp_path) -> None:
    dolt = FakeDolt({})
    read = GapfillReader("/hive", runner=dolt).read(["bh-1"])
    assert read.counts["beads_without_history"] == 1 and read.states == {}


def test_single_bead_events_helper_returns_nothing_for_one_state() -> None:
    state = gf.GapState("c" * 32, "2026-08-01T00:00:00Z", {"title": "T"}, "open")
    assert gf.bead_gap_events(BeadRef(GROUP, "bh-1"), [state], extracted_at=STAMP) == []
