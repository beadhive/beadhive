"""``bh backfill timeline`` and ``bh backfill export --history`` over the in-memory fake bd.

The four history sources (the bd events table, first-parent lands, commit dates, run journals) are
fixtures behind the two module seams, ``history_inputs`` and ``timeline_pass``; every bd read and
write goes through :class:`FakeBd` behind the real bd StoreLoader. Nothing touches a live hive.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from tests.test_backfill_bd import FakeBd, Ticks
from tests.test_backfill_bd import cfg_path as cfg_path  # noqa: F401 — fixture resolved by name
from typer.testing import CliRunner

import beadlog
from beadhive import backfill_cli, cli
from beadhive.backfill_bd import BdStoreLoader
from beadhive.modules.config.contracts import BackfillGapfillConfig
from beadhive_backfill import PathReaders, Plan, TimelinePass
from beadhive_backfill.merges import parse_first_parent
from beadhive_backfill.run_journals import RunJournalRead
from beadlog_sources import (
    BdGapfillError,
    CommitDate,
    CommitDates,
    GapfillReader,
    HistoryEvent,
    HistoryRead,
)

ENV = {"COLUMNS": "120", "BH_SKIP_SETUP_CHECK": "1", "NO_COLOR": "1"}
ENTRY = {"provider": "github", "org": "acme", "repo": "widgets", "prefix": "wd"}
SHA1, SHA2, LAND1, LAND2 = ("a" * 40, "b" * 40, "1" * 40, "2" * 40)
STORE_START = "2026-07-25T17:02:33Z"


def _row(iid: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": iid,
        "issue_type": "task",
        "status": "open",
        "title": f"Bead {iid}",
        "priority": 2,
        "labels": [],
        "created_at": "2026-08-10T09:00:00Z",
        "updated_at": "2026-08-20T00:00:00Z",
        **extra,
    }


def _ev(row_id: str, bead: str, kind: str, at: str, **extra: Any) -> HistoryEvent:
    return HistoryEvent(row_id, bead, kind, at, actor="dev/a", **extra)


ROWS = [
    _row(
        "wd-1",
        status="closed",
        closed_at="2026-08-12T10:00:00Z",
        assignee="dev/a",
        labels=["review:pending"],
        metadata={"git.commits": json.dumps([SHA1, SHA2])},
    ),
    _row("wd-2", status="closed", closed_at="2026-08-13T10:00:00Z"),
    _row("wd-old", created_at="2026-06-29T03:43:46Z"),
]
HISTORY = HistoryRead(
    (
        _ev("e-01", "wd-1", "created", "2026-08-10T09:00:00Z"),
        _ev(
            "e-02",
            "wd-1",
            "claimed",
            "2026-08-10T10:00:00Z",
            assignee="dev/a",
            new_status="in_progress",
            changes={"assignee": "dev/a", "status": "in_progress"},
        ),
        _ev("e-03", "wd-1", "label_added", "2026-08-11T09:00:00Z", label="review:pending"),
        _ev("e-04", "wd-1", "closed", "2026-08-12T10:00:00Z", new_status="closed"),
        _ev("e-05", "wd-2", "closed", "2026-08-13T10:00:00Z", new_status="closed"),
        _ev("e-06", "wd-gone", "created", "2026-08-07T07:25:10Z"),  # a deleted bead's row
    ),
    rows=6,
)
LOG = [
    f"{LAND2}\t2026-08-13T09:00:00+00:00\tchore(merge): bead wd-2",
    f"{LAND1}\t2026-08-12T09:30:00+00:00\tchore(merge): bead wd-1",
    f"{'9' * 40}\t2026-08-11T09:00:00+00:00\tchore(merge): refresh wt/bead/epic/wd-9 from main",
]
DATES = {
    SHA1: CommitDate(SHA1, "2026-08-10T11:00:00Z", "2026-08-10T11:05:00Z"),
    SHA2: CommitDate(SHA2, "2026-08-11T08:00:00Z", "2026-08-11T08:10:00Z"),
}


class Readers:
    """Fixture sources for the timeline pass; counts every read."""

    def __init__(self) -> None:
        self.calls = {"history": 0, "lands": 0, "commit_dates": 0, "journals": 0}

    def history(self) -> HistoryRead:
        self.calls["history"] += 1
        return HISTORY

    def lands(self):
        self.calls["lands"] += 1
        return parse_first_parent(LOG)

    def commit_dates(self, shas: Sequence[str]) -> CommitDates:
        self.calls["commit_dates"] += 1
        return CommitDates({sha: DATES[sha] for sha in shas if sha in DATES})

    def run_journals(self) -> RunJournalRead:
        self.calls["journals"] += 1
        return RunJournalRead()


@pytest.fixture
def hive(cfg_path, monkeypatch, tmp_path):  # noqa: F811 — the imported fixture
    store = FakeBd([copy.deepcopy(row) for row in ROWS])
    group = backfill_cli.hive_group(ENTRY, tmp_path)
    readers = Readers()
    monkeypatch.setattr(backfill_cli, "_target", lambda hive: (tmp_path, group))

    def loader(cwd: Path, *, group: str, pre_state_path: Path | None = None) -> BdStoreLoader:
        return BdStoreLoader(store, group=group, clock=Ticks(), pre_state_path=pre_state_path)

    monkeypatch.setattr(backfill_cli, "store_loader", loader)
    monkeypatch.setattr(backfill_cli, "history_inputs", lambda cwd: (HISTORY, STORE_START))
    monkeypatch.setattr(backfill_cli, "timeline_pass", lambda cwd, hive: TimelinePass(readers))
    return store, group, readers


def _invoke(*args: str, code: int = 0) -> Any:
    result = CliRunner().invoke(cli.app, list(args), env=ENV)
    assert result.exit_code == code, result.output
    return result


# -- bh backfill timeline --------------------------------------------------------------------


def test_timeline_previews_by_default_and_writes_nothing(hive, tmp_path) -> None:
    store, _, readers = hive
    plan_path = tmp_path / "timeline.bdlg"

    text = _invoke("backfill", "timeline", "--plan", str(plan_path)).stdout
    assert store.writes == []
    assert "timeline: 2 beads would change" in text
    assert "merged_at=2" in text and "claimed_at=1" in text
    assert f"saved plan: {plan_path}" in text
    plan = Plan.read(plan_path)
    assert plan.pass_name == "timeline" and plan.changes == 2

    summary = json.loads(_invoke("backfill", "timeline", "--dry-run", "--json").stdout)
    assert summary["pass"] == "timeline" and summary["changes"] == 2
    assert summary["report"]["facts"]["merged_at"] == 2
    assert store.writes == []
    # one bulk read of each source per dry run, however many beads there are
    assert readers.calls["history"] == readers.calls["lands"] == 2

    both = _invoke("backfill", "timeline", "--apply", "--dry-run", code=2)
    assert "either --dry-run or --apply" in both.output


def test_apply_writes_only_lifecycle_keys_and_a_second_run_proposes_nothing(hive, tmp_path) -> None:
    store, _, _ = hive
    plan_path = tmp_path / "timeline.bdlg"
    _invoke("backfill", "timeline", "--plan", str(plan_path))
    before = copy.deepcopy(store.rows)

    audit = json.loads(
        _invoke("backfill", "timeline", "--apply", "--plan", str(plan_path), "--json").stdout
    )
    assert audit["state"] == "applied" and audit["pass"] == "timeline"
    assert audit["verification"]["second_dry_run_changes"] == 0
    assert audit["verification"]["pass_errors"] == []
    assert {op for op, _ in store.writes} == {"update"}  # one bd update per bead, nothing else
    assert (tmp_path / "timeline.audit.json").is_file()
    assert (tmp_path / "timeline.pre-state.jsonl").is_file()

    changed_keys: set[str] = set()
    for iid, row in store.rows.items():
        old = before[iid]
        assert {k: v for k, v in row.items() if k not in ("metadata", "updated_at")} == {
            k: v for k, v in old.items() if k not in ("metadata", "updated_at")
        }
        metadata, previous = row.get("metadata", {}), old.get("metadata", {})
        changed_keys |= {
            key for key in {*metadata, *previous} if metadata.get(key) != previous.get(key)
        }
    assert changed_keys and all(
        key.startswith("lifecycle.") or key == "bdlg" for key in changed_keys
    )  # `bdlg` is the loader's own occurrence-time bookkeeping
    assert "git.commits" not in changed_keys
    meta = store.rows["wd-1"]["metadata"]
    assert meta["lifecycle.merged_at"] == "2026-08-12T09:30:00Z"
    assert meta["lifecycle.merged_at_confidence"] == "high"
    assert meta["lifecycle.claimed_at"] == "2026-08-10T10:00:00Z"
    assert meta["git.commits"] == json.dumps([SHA1, SHA2])  # untouched

    # the second run, from scratch, against what apply wrote: nothing to do
    writes = len(store.writes)
    text = _invoke("backfill", "timeline").stdout
    assert "timeline: 0 beads would change" in text
    assert len(store.writes) == writes


def test_apply_demands_a_timeline_plan(hive, tmp_path) -> None:
    store, group, _ = hive
    missing = CliRunner().invoke(cli.app, ["backfill", "timeline", "--apply"], env=ENV)
    assert missing.exit_code == 1 and "--apply requires --plan" in missing.output

    neutral = tmp_path / "neutral.bdlg"
    ref = beadlog.BeadRef(group.id, "wd-9")
    proposed = beadlog.mint_event(
        "create",
        ref,
        {"type": "task", "fields": {"title": "New"}},
        occurred_at="2026-08-01T00:00:00Z",
        recorded_at=None,
        extracted_at="2026-10-31T00:00:00Z",
        source=("docs", "x"),
        provenance="inferred",
    )
    from beadlog_sources import reconcile, write_plan

    baseline = BdStoreLoader(store, group=group.id, clock=Ticks()).export()
    write_plan(
        neutral,
        reconcile([proposed], baseline),
        groups=[group],
        generator=beadlog.Generator("beadlog", "0.1.0"),
    )
    refused = CliRunner().invoke(
        cli.app, ["backfill", "timeline", "--apply", "--plan", str(neutral)], env=ENV
    )
    assert refused.exit_code == 1 and "not a timeline plan" in refused.output
    assert store.writes == []


def test_a_saved_timeline_plan_also_applies_through_backfill_apply(hive, tmp_path) -> None:
    store, _, _ = hive
    plan_path = tmp_path / "timeline.bdlg"
    _invoke("backfill", "timeline", "--plan", str(plan_path))
    audit = json.loads(_invoke("backfill", "apply", str(plan_path), "--json").stdout)
    assert audit["state"] == "applied" and audit["pass"] == "timeline"
    assert store.rows["wd-2"]["metadata"]["lifecycle.merged_at"] == "2026-08-13T09:00:00Z"


def test_the_real_pass_reads_the_checkout_and_the_journal_directory(
    cfg_path,  # noqa: F811 — the imported fixture
    monkeypatch,
    tmp_path,
) -> None:
    """``timeline_pass`` wires path readers on the hive's integration branch and journals."""
    cfg = {"hives": [{**ENTRY, "path": str(tmp_path)}], "work": {"integration_branch": "trunk"}}
    monkeypatch.setattr(backfill_cli.config, "load", lambda: cfg)
    monkeypatch.setattr(backfill_cli.registry, "resolve_hive", lambda c, h: ENTRY)
    monkeypatch.setattr(backfill_cli.registry, "current_hive", lambda c: ENTRY)
    monkeypatch.setattr(backfill_cli.config, "integration_branch", lambda c, e: "trunk")
    pass_ = backfill_cli.timeline_pass(tmp_path, "")
    readers = pass_.readers
    assert isinstance(readers, PathReaders)
    assert readers.root == tmp_path and readers.ref == "trunk"
    assert readers.journals is not None and readers.journals.name == "github-acme-widgets"


# -- bh backfill export --history -------------------------------------------------------------


def test_the_default_export_is_unchanged_by_the_history_option(hive, tmp_path) -> None:
    store, group, _ = hive
    plain = tmp_path / "plain.bdlg"
    summary = json.loads(_invoke("backfill", "export", "--out", str(plain), "--json").stdout)
    assert set(summary) == {"path", "group", "events", "beads"}  # no new key without --history
    with beadlog.ArchiveReader(plain) as reader:
        manifest = reader.manifest.to_json()
    assert "annotations" not in manifest
    text = _invoke("backfill", "export", "--out", str(plain)).stdout
    assert text.count("\n") == 1 and text.startswith("exported ")  # one line, as always
    inspected = json.loads(_invoke("backfill", "inspect", str(plain), "--json").stdout)
    assert "history" not in inspected


def test_export_history_adds_the_events_table_and_reports_its_coverage(hive, tmp_path) -> None:
    store, group, _ = hive
    plain, rich = tmp_path / "plain.bdlg", tmp_path / "rich.bdlg"
    plain_summary = json.loads(_invoke("backfill", "export", "--out", str(plain), "--json").stdout)
    summary = json.loads(
        _invoke("backfill", "export", "--history", "--out", str(rich), "--json").stdout
    )
    assert store.writes == []  # export never writes to a hive
    assert summary["group"] == plain_summary["group"] == group.to_json()
    assert summary["events"] > plain_summary["events"]
    history = summary["history"]
    assert history["earliest_recorded"] == "2026-08-07T07:25:10Z"  # the table's first row
    assert history["sources"]["bd:events"] > 0 and history["sources"]["bd:issues"] > 0
    assert history["sources"]["bd:inferred"] == 2  # wd-2 and wd-old have no created row
    assert history["gap"]["first_bead_created"] == "2026-06-29T03:43:46Z"
    assert history["gap"]["first_store_commit"] == STORE_START
    assert "2026-06-29" in history["gap"]["note"] and "2026-08-07" in history["gap"]["note"]

    # it passes `bh backfill verify` and inspect surfaces the same numbers
    verified = json.loads(_invoke("backfill", "verify", str(rich), "--json").stdout)
    assert verified["ok"] is True and verified["events"] == summary["events"]
    inspected = json.loads(_invoke("backfill", "inspect", str(rich), "--json").stdout)
    assert inspected["history"] == history
    text = _invoke("backfill", "inspect", str(rich)).stdout
    assert "earliest recorded 2026-08-07T07:25:10Z" in text and "bd:events=" in text
    assert "Pre-retention gap" in text


def test_the_two_exports_union_merge_without_changing_current_state(hive, tmp_path) -> None:
    store, group, _ = hive
    plain, rich = tmp_path / "plain.bdlg", tmp_path / "rich.bdlg"
    _invoke("backfill", "export", "--out", str(plain))
    _invoke("backfill", "export", "--history", "--out", str(rich))
    plain_manifest, plain_events = beadlog.read_archive(plain)
    rich_manifest, rich_events = beadlog.read_archive(rich)

    assert plain_manifest.groups == rich_manifest.groups == (group,)  # the same group id
    current = {event.id for event in plain_events}
    assert current <= {event.id for event in rich_events}  # history adds, never replaces
    history = [event for event in rich_events if event.id not in current]
    assert history and all(
        event.source.ref.startswith(("bd:events/", "bd:inferred/")) for event in history
    )

    merged = beadlog.union_merge([plain_events, rich_events])
    assert {event.id for event in merged} == {event.id for event in rich_events}
    assert beadlog.fold(merged) == beadlog.fold(plain_events)  # current state unchanged
    # the dates of history are bi-temporal: occurrence is not the recording
    inferred = next(e for e in history if e.source.ref.startswith("bd:inferred/"))
    assert inferred.provenance == "inferred" and inferred.recorded_at is None
    assert not [e for e in history if e.bead.id == "wd-gone"]  # a deleted bead stays gone


def test_export_history_defaults_to_its_own_file_name(hive, tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _invoke("backfill", "export", "--history")
    _invoke("backfill", "export")
    assert (tmp_path / "wd.history.bdlg").is_file() and (tmp_path / "wd.bdlg").is_file()


# -- bh backfill export --history with the opt-in gap fill --------------------------------------


def _version(commit: str, when: str, **extra: Any) -> tuple[str, str, dict[str, Any]]:
    row = {"title": "Bead", "status": "open", "priority": 2, "issue_type": "task", **extra}
    return commit, when, row


# wd-2 changes (open -> closed); wd-old never does. Both lack a created row in HISTORY.
DOLT = {
    "wd-2": [
        _version("c" * 32, "2026-07-26T09:00:00.000000Z", title="Bead wd-2"),
        _version("d" * 32, "2026-08-13T10:00:00.000000Z", title="Bead wd-2", status="closed"),
    ],
    "wd-old": [
        _version("e" * 32, "2026-07-26T09:00:00.000000Z", title="Bead wd-old"),
        _version("f" * 32, "2026-07-27T09:00:00.000000Z", title="Bead wd-old"),
    ],
}


class Dolt:
    """The gap fill's two statement shapes over :data:`DOLT`; counts scans, can break one."""

    def __init__(self, fail_on_scan: int | None = None) -> None:
        self.scans: list[list[str]] = []
        self.fail_on_scan = fail_on_scan
        self.statements: list[str] = []

    def __call__(self, argv: Sequence[str]) -> str:
        mode, sql = argv[4], argv[5]
        self.statements.append(sql)
        if mode == "--csv":
            ids = re.findall(r"'([^']+)'", sql[sql.index(" IN (") :])
            self.scans.append(ids)
            if self.fail_on_scan == len(self.scans):
                raise BdGapfillError("bd sql timed out after 180s")
            lines = ["id,commit_hash,d,h"]
            for bead in ids:
                for commit, when, row in DOLT.get(bead, ()):
                    lines.append(f"{bead},{commit},{when},{row['status']}")
            return "\n".join(lines) + "\n"
        points = re.findall(r"AS OF '([A-Za-z0-9]+)' WHERE id = '([^']+)'", sql)
        found = [
            {"commit_hash": c, "id": b, **row}
            for c, b in points
            for commit, _, row in DOLT[b]
            if commit == c
        ]
        return json.dumps(found)


@pytest.fixture
def gapfill(hive, monkeypatch):
    """Enable the gap fill with one-bead batches over the fake Dolt; returns the fake."""
    dolt = Dolt()
    settings = BackfillGapfillConfig(enabled=True, batch_size=1, timeout_seconds=5)
    monkeypatch.setattr(backfill_cli, "gapfill_settings", lambda cfg=None: settings)

    def reader(cwd: Path, settings: BackfillGapfillConfig) -> GapfillReader:
        return GapfillReader(cwd, batch_size=settings.batch_size, runner=dolt)

    monkeypatch.setattr(backfill_cli, "gapfill_reader", reader)
    return dolt


def test_the_gap_fill_is_off_by_default_and_asks_the_store_nothing(
    hive, tmp_path, monkeypatch
) -> None:
    def boom(cwd, settings):
        raise AssertionError("the gap fill ran although it is disabled")

    monkeypatch.setattr(backfill_cli, "gapfill_reader", boom)
    assert backfill_cli.gapfill_settings({}).enabled is False  # nothing configured
    out = tmp_path / "off.bdlg"
    summary = json.loads(
        _invoke("backfill", "export", "--history", "--out", str(out), "--json").stdout
    )
    assert "gapfill" not in summary["history"]
    assert "dolt:history" not in summary["history"]["sources"]
    assert not (tmp_path / "off.bdlg.gapfill.json").exists()


def test_the_gap_fill_settings_come_from_backfill_timeline_gapfill() -> None:
    assert backfill_cli.gapfill_settings({"backfill": {}}).enabled is False
    cfg = {"backfill": {"timeline": {"gapfill": {"enabled": True, "batch_size": 7}}}}
    tuned = backfill_cli.gapfill_settings(cfg)
    assert (tuned.enabled, tuned.batch_size, tuned.timeout_seconds) == (True, 7, 180.0)
    reader = backfill_cli.gapfill_reader(Path("/hive"), tuned)
    assert reader.batch_size == 7


def test_the_enabled_gap_fill_adds_snapshot_events_only_for_beads_without_a_created_row(
    hive, gapfill, tmp_path
) -> None:
    store, group, _ = hive
    plain, filled = tmp_path / "plain.bdlg", tmp_path / "filled.bdlg"
    _invoke("backfill", "export", "--out", str(plain))
    summary = json.loads(
        _invoke("backfill", "export", "--history", "--out", str(filled), "--json").stdout
    )
    assert store.writes == []
    assert gapfill.scans == [
        ["wd-2"],
        ["wd-old"],
    ]  # wd-1 and wd-gone have created rows: never asked
    assert all(sql.startswith("SELECT ") for sql in gapfill.statements)
    account = summary["history"]["gapfill"]
    assert account["beads_queried"] == 2 and account["events"] > 0
    assert account["counts"]["beads_changed"] == 1  # wd-2; wd-old never changed
    assert summary["history"]["sources"]["dolt:history"] == account["events"]
    assert not (tmp_path / "filled.bdlg.gapfill.json").exists()  # complete: the cursor is gone

    _, events = beadlog.read_archive(filled)
    gap = [e for e in events if e.source.system == "dolt"]
    assert {e.bead.id for e in gap} == {"wd-2"} and len(gap) == account["events"]
    assert {e.provenance for e in gap} == {"derived"}
    assert all(e.occurred_at == e.recorded_at for e in gap)
    assert any(e.kind is beadlog.EventKind.PHASE and e.data["phase"] == "closed" for e in gap)
    _, plain_events = beadlog.read_archive(plain)
    assert beadlog.fold(events) == beadlog.fold(plain_events)  # the current state is untouched

    verified = json.loads(_invoke("backfill", "verify", str(filled), "--json").stdout)
    assert verified["ok"] is True
    text = _invoke("backfill", "inspect", str(filled)).stdout
    assert "gap fill: 2 beads queried" in text


def test_an_interrupted_export_resumes_from_the_cursor_and_matches_an_uninterrupted_one(
    hive, gapfill, tmp_path, monkeypatch
) -> None:
    clean = tmp_path / "clean.bdlg"
    _invoke("backfill", "export", "--history", "--out", str(clean))
    expected = [e.id for e in beadlog.read_archive(clean)[1]]

    broken = Dolt(fail_on_scan=2)  # the second one-bead batch times out
    monkeypatch.setattr(
        backfill_cli,
        "gapfill_reader",
        lambda cwd, settings: GapfillReader(cwd, batch_size=1, runner=broken),
    )
    out = tmp_path / "resumed.bdlg"
    result = CliRunner().invoke(
        cli.app, ["backfill", "export", "--history", "--out", str(out)], env=ENV
    )
    assert result.exit_code == 1 and "gap fill stopped" in result.output
    assert "re-run to resume" in result.output
    assert not out.exists()  # no half archive
    cursor = tmp_path / "resumed.bdlg.gapfill.json"
    assert json.loads(cursor.read_text())["done"] == 1

    healthy = Dolt()
    monkeypatch.setattr(
        backfill_cli,
        "gapfill_reader",
        lambda cwd, settings: GapfillReader(cwd, batch_size=1, runner=healthy),
    )
    _invoke("backfill", "export", "--history", "--out", str(out))
    assert healthy.scans == [["wd-old"]]  # only the unfinished bead is read again
    got = [e.id for e in beadlog.read_archive(out)[1]]
    assert got == expected and len(got) == len(set(got))  # same events, no duplicates
    assert not cursor.exists()
