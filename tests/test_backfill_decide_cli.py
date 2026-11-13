"""``bh backfill decide`` and its consumers (bh-2sicb): the decision bead in the store, identify
step 0 on a re-run, the deleted-bead tombstone, ``backfill.decisions.on_delete`` and stale targets.

Every bd read and write goes through :class:`FakeBd` behind the real loader (the ``hive`` fixture
of the history CLI tests); ``decided_at`` comes from a fixed clock, never the wall clock.
"""
# ruff: noqa: F811 -- the imported fixtures are used by name

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from tests.test_backfill_bd import FakeBd
from tests.test_backfill_history_cli import (  # noqa: F401 — fixtures resolved by name
    TRIPLET,
    _config,
    _invoke,
    _run,
    cfg_path,
    hive,
)

from beadhive import backfill_cli, backfill_create, backfill_decisions
from beadlog_sources import ProjectOptions, read_decisions

NOW = "2026-11-13T12:00:00Z"


class Clock:
    """A mocked ``decided_at``: every call is one second after the previous."""

    def __init__(self) -> None:
        self.n = 0

    def __call__(self) -> str:
        self.n += 1
        return f"2026-11-13T12:{self.n // 60:02d}:{self.n % 60:02d}Z"


LABELS = ("origin:backfill", *TRIPLET)


@pytest.fixture(autouse=True)
def _fixed(monkeypatch, hive):  # noqa: F811
    monkeypatch.setattr(backfill_cli, "decision_clock", Clock())
    monkeypatch.setattr(backfill_create, "creation_labels", lambda h, c: LABELS)
    monkeypatch.setattr(
        backfill_create,
        "create_mode_options",
        lambda h, c, m: ProjectOptions(creation_labels=LABELS),
    )
    return hive


def decide(*args: str, code: int = 0) -> Any:
    return _invoke("backfill", "decide", *args, code=code)


def decision_rows(store: FakeBd) -> list[dict[str, Any]]:
    return [r for r in store.rows.values() if r.get("issue_type") == "decision"]


def collation(tmp_path: Path) -> dict[str, Any]:
    return json.loads((tmp_path / "run" / "collation.json").read_text())


def verdicts(tmp_path: Path, unit: str) -> list[dict[str, Any]]:
    return [v for v in collation(tmp_path)["verdicts"] if v["unit"] == unit]


def a_unit(tmp_path: Path) -> str:
    """The key of a unit the git history produced (and, in create mode, proposes as NEW)."""
    _run(tmp_path, "--sources", "git", "--until", "collate")
    keys = [u["key"] for u in collation(tmp_path)["units"]]
    assert keys
    return sorted(keys)[0]


# -- the verb ----------------------------------------------------------------------------------


def test_skip_is_one_closed_decision_bead_with_the_adr_shape(hive) -> None:
    store, _ = hive
    out = json.loads(decide("commit:abc", "skip", "--reason", "noise by hand", "--json").stdout)
    assert out["action"] == "create" and out["dry_run"] is False
    (row,) = decision_rows(store)
    assert row["status"] == "closed" and row["id"] == out["bead"]
    assert row["external_ref"] == "bfdec:commit:abc"
    assert row["title"] == "Backfill decision: skip commit:abc"
    assert {*LABELS, "source:decision"} <= set(row["labels"])
    meta = row["metadata"]
    assert meta["backfill.decision"] == "skip" and meta["backfill.unit"] == "commit:abc"
    assert meta["backfill.reason"] == "noise by hand" and meta["backfill.decided_at"].startswith(
        "2026-11-13T12:00:"
    )
    assert "backfill.target" not in meta and meta["backfill.decided_by"]


def test_link_and_noise_need_a_live_target_and_noise_an_epic(hive) -> None:
    store, _ = hive
    decide("u:1", "link", "ag-infra-1")
    store.rows["ag-infra-epic"] = {
        **store.rows["ag-infra-1"],
        "id": "ag-infra-epic",
        "issue_type": "epic",
    }
    decide("u:2", "noise", "ag-infra-epic")
    metas = {r["metadata"]["backfill.unit"]: r["metadata"] for r in decision_rows(store)}
    assert metas["u:1"]["backfill.target"] == "ag-infra-1"
    assert metas["u:2"]["backfill.target"] == "ag-infra-epic"
    assert "not a live bead" in decide("u:3", "link", "ag-infra-nope", code=1).output
    assert "needs a covering epic" in decide("u:3", "noise", "ag-infra-1", code=1).output
    assert "takes no target" in decide("u:3", "skip", "ag-infra-1", code=1).output
    assert "needs a target" in decide("u:3", "link", code=1).output
    assert "must be one of" in decide("u:3", "maybe", code=1).output
    assert len(decision_rows(store)) == 2


def test_dry_run_prints_the_bead_and_writes_nothing(hive) -> None:
    store, _ = hive
    before = dict(store.rows)
    text = decide("commit:abc", "skip", "--dry-run").stdout
    assert "would record create" in text and "source:decision" in text
    assert "metadata.backfill.decision: skip" in text and "dry run" in text
    assert store.writes == [] and store.rows == before
    out = json.loads(decide("commit:abc", "skip", "--dry-run", "--json").stdout)
    assert out["dry_run"] is True and out["labels"] == sorted({*LABELS, "source:decision"})


def test_deciding_again_updates_the_same_bead(hive) -> None:
    store, _ = hive
    decide("u:1", "skip")
    decide("u:1", "link", "ag-infra-1", "--reason", "same work")
    (row,) = decision_rows(store)
    assert row["metadata"]["backfill.decision"] == "link"
    assert row["title"] == "Backfill decision: link u:1" and row["status"] == "closed"
    (found,) = read_decisions(_events(store)).values()
    assert (found.kind, found.target, found.reason) == ("link", "ag-infra-1", "same work")


def _events(store: FakeBd):
    from beadlog_sources.bd import baseline_events

    group = backfill_cli.hive_group(
        {"provider": "github", "org": "agentguides", "repo": "infra", "prefix": "ag-infra"},
        Path("."),
    ).id
    return baseline_events(store.export_rows(), group=group, extracted_at=NOW)


def test_decisions_survive_export_and_import(hive) -> None:
    store, _ = hive
    decide("u:1", "skip")
    decide("u:2", "link", "ag-infra-1")
    fresh = FakeBd(identity_labels=TRIPLET)
    fresh.import_records(store.export_rows())  # the bd export/import round trip
    assert read_decisions(_events(fresh)) == read_decisions(_events(store))
    assert set(read_decisions(_events(fresh))) == {"u:1", "u:2"}


def test_view_decisions_lists_them(hive) -> None:
    decide("u:1", "skip", "--reason", "templates")
    decide("u:2", "link", "ag-infra-2")
    view = json.loads(_invoke("backfill", "view", "decisions", "--json").stdout)
    assert view["counts"] == {"skip": 1, "noise": 0, "link": 1}
    assert [d["unit"] for d in view["decisions"]] == ["u:1", "u:2"]
    assert view["decisions"][0]["reason"] == "templates"
    text = _invoke("backfill", "view", "decisions").stdout
    assert "skip  u:1" in text and "link  u:2 -> ag-infra-2" in text


# -- identify step 0 ---------------------------------------------------------------------------


def test_a_recorded_skip_is_not_proposed_and_not_residual(hive, tmp_path) -> None:
    unit = a_unit(tmp_path)
    decide(unit, "skip")
    _run(tmp_path, "--sources", "git", "--until", "collate")
    decided = [v for v in verdicts(tmp_path, unit) if v["class"] is not None]
    assert [(v["step"], v["class"]) for v in decided] == [("decision", "SKIP")]
    assert unit not in [r["unit"] for r in collation(tmp_path)["residual"]]
    unit_view = json.loads(
        _invoke("backfill", "view", "unit", str(tmp_path / "run"), "--json").stdout
    )
    assert unit_view["counts"]["SKIP"] == 1
    assert "(operator)" in _invoke("backfill", "view", "unit", str(tmp_path / "run")).stdout


def test_a_recorded_link_is_present_against_the_named_bead(hive, tmp_path) -> None:
    unit = a_unit(tmp_path)
    decide(unit, "link", "ag-infra-2")
    _run(tmp_path, "--sources", "git", "--until", "collate")
    decided = [v for v in verdicts(tmp_path, unit) if v["class"] is not None]
    assert [(v["step"], v["class"], v["bead"]) for v in decided] == [
        ("decision", "PRESENT", "ag-infra-2")
    ]


def test_a_recorded_noise_is_noise_against_the_named_epic(hive, tmp_path) -> None:
    store, _ = hive
    unit = a_unit(tmp_path)
    store.rows["ag-infra-epic"] = {
        **store.rows["ag-infra-1"],
        "id": "ag-infra-epic",
        "issue_type": "epic",
    }
    decide(unit, "noise", "ag-infra-epic")
    _run(tmp_path, "--sources", "git", "--until", "collate")
    decided = [v for v in verdicts(tmp_path, unit) if v["class"] is not None]
    assert [(v["step"], v["class"], v["bead"]) for v in decided] == [
        ("decision", "NOISE", "ag-infra-epic")
    ]


def test_a_stale_target_is_reported_and_not_applied(hive, tmp_path) -> None:
    store, _ = hive
    unit = a_unit(tmp_path)
    decide(unit, "link", "ag-infra-2")
    del store.rows["ag-infra-2"]
    result = _run(tmp_path, "--sources", "git", "--until", "collate")
    assert "not applied" in result.output and "ag-infra-2" in result.output
    assert all(v["step"] != "decision" for v in verdicts(tmp_path, unit))


# -- deleted beads -----------------------------------------------------------------------------


def propose(cfg_path, **extra: Any) -> None:
    _config(cfg_path, collate={"create": "propose"}, **extra)


def _make_and_delete(hive, tmp_path, cfg_path, monkeypatch, **extra):
    store, _ = hive
    propose(cfg_path, **extra)
    _run(tmp_path, "--sources", "git", "--until", "apply", "--yes")
    made = {
        r["external_ref"]: r["id"]
        for r in store.rows.values()
        if "origin:backfill" in r.get("labels", []) and r.get("external_ref")
    }
    assert made, "create mode made a bead"
    key, ident = sorted(made.items())[0]
    snapshot = dict(store.rows[ident])
    del store.rows[ident]
    monkeypatch.setattr(
        backfill_decisions,
        "bead_history",
        lambda cwd, i: [{"Issue": {"id": i, "external_ref": key, "labels": snapshot["labels"]}}],
    )
    return store, key, ident


def _new_units(tmp_path: Path) -> list[str]:
    return sorted({v["unit"] for v in collation(tmp_path)["verdicts"] if v["class"] == "NEW"})


def test_delete_then_decide_deleted_then_rerun_does_not_propose_it_again(
    hive, tmp_path, cfg_path, monkeypatch
) -> None:
    store, key, ident = _make_and_delete(hive, tmp_path, cfg_path, monkeypatch)
    _run(tmp_path, "--sources", "git", "--until", "collate")
    assert key in _new_units(tmp_path)  # today's behaviour: proposed again
    text = decide("--deleted", ident, "skip", "--reason", "hand-deleted").stdout
    assert "recorded create" in text
    (row,) = decision_rows(store)
    assert row["external_ref"] == f"bfdec:{key}"
    assert row["metadata"]["backfill.reason"] == "hand-deleted"
    _run(tmp_path, "--sources", "git", "--until", "collate")
    assert key not in _new_units(tmp_path)
    assert [v["class"] for v in verdicts(tmp_path, key) if v["class"]] == ["SKIP"]


def test_decide_deleted_refuses_a_live_bead_and_a_non_backfilled_one(
    hive, tmp_path, cfg_path, monkeypatch
) -> None:
    store, key, ident = _make_and_delete(hive, tmp_path, cfg_path, monkeypatch)
    assert "still exists" in decide("--deleted", "ag-infra-1", "skip", code=1).output
    monkeypatch.setattr(
        backfill_decisions,
        "bead_history",
        lambda cwd, i: [{"Issue": {"external_ref": key, "labels": ["other"]}}],
    )
    assert "not a backfilled bead" in decide("--deleted", ident, "skip", code=1).output
    monkeypatch.setattr(backfill_decisions, "bead_history", lambda cwd, i: [{"Issue": {}}])
    assert "no external_ref" in decide("--deleted", ident, "skip", code=1).output
    monkeypatch.setattr(backfill_decisions, "bead_history", lambda cwd, i: None)
    assert "failed" in decide("--deleted", ident, "skip", code=1).output
    assert "skip decision only" in decide("--deleted", ident, "link", code=1).output
    assert decision_rows(store) == []


def test_on_delete_ignore_is_today(hive, tmp_path, cfg_path, monkeypatch) -> None:
    store, key, _ = _make_and_delete(hive, tmp_path, cfg_path, monkeypatch)
    result = _run(tmp_path, "--sources", "git", "--until", "collate")
    assert "deleted since the last apply" not in result.output
    assert key in _new_units(tmp_path) and decision_rows(store) == []


def test_on_delete_report_lists_but_proposes_again(hive, tmp_path, cfg_path, monkeypatch) -> None:
    store, key, ident = _make_and_delete(
        hive, tmp_path, cfg_path, monkeypatch, decisions={"on_delete": "report"}
    )
    result = _run(tmp_path, "--sources", "git", "--until", "collate", "--json")
    assert f"{ident} (unit {key}) was deleted since the last apply" in result.output
    assert key in _new_units(tmp_path) and decision_rows(store) == []


def test_on_delete_skip_records_the_decision_on_an_apply_run(
    hive, tmp_path, cfg_path, monkeypatch
) -> None:
    store, key, ident = _make_and_delete(
        hive, tmp_path, cfg_path, monkeypatch, decisions={"on_delete": "skip"}
    )
    # a run that stops before apply only reports: it must not write
    _run(tmp_path, "--sources", "git", "--until", "collate")
    assert decision_rows(store) == [] and key in _new_units(tmp_path)
    _run(tmp_path, "--sources", "git", "--until", "apply", "--yes")
    (row,) = decision_rows(store)
    assert row["external_ref"] == f"bfdec:{key}" and row["status"] == "closed"
    assert "on_delete" in row["metadata"]["backfill.reason"]
    assert key not in [
        r.get("external_ref") for r in store.rows.values() if r.get("issue_type") != "decision"
    ]
    _run(tmp_path, "--sources", "git", "--until", "collate")
    assert key not in _new_units(tmp_path)
