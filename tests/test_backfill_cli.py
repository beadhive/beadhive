"""``bh backfill``: the flat complexity alias, export/inspect/verify round trip, plan apply.

Every bd read and write goes through the in-memory :class:`FakeBd` behind the real bd
StoreLoader (``tests/test_backfill_bd.py``); nothing touches a live hive.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from tests.test_backfill_bd import FakeBd, Ticks
from tests.test_backfill_bd import cfg_path as cfg_path  # noqa: F401 — fixture resolved by name
from typer.testing import CliRunner

import beadlog
from beadhive import backfill_cli, cli, complexity_backfill
from beadhive.backfill_bd import BdStoreLoader
from beadhive_backfill import Plan
from beadlog_sources import reconcile, write_plan

ENV = {"COLUMNS": "120", "BH_SKIP_SETUP_CHECK": "1", "NO_COLOR": "1"}
ENTRY = {"provider": "github", "org": "acme", "repo": "widgets", "prefix": "wd"}
STAMP = "2026-07-01T00:00:00Z"


def _corpus_row(iid: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": iid,
        "issue_type": "task",
        "status": "open",
        "title": "Implement a Python API and unit test",
        "description": "",
        "design": "",
        "acceptance_criteria": "",
        "labels": [],
        **extra,
    }


def _store_row(iid: str, *, labels: list[str] | None = None, **extra: Any) -> dict[str, Any]:
    return {
        "id": iid,
        "issue_type": "task",
        "status": "open",
        "title": f"Bead {iid}",
        "priority": 2,
        "labels": list(labels or []),
        "created_at": STAMP,
        "updated_at": STAMP,
        **extra,
    }


@pytest.fixture
def hive(cfg_path, monkeypatch, tmp_path):  # noqa: F811 — the imported fixture
    """A fake hive: ``_target`` resolves to ``tmp_path`` and every loader reads ``store``."""
    store = FakeBd(
        [
            _store_row("wd-1", labels=["area:cli"]),
            _store_row("wd-2", status="closed", closed_at=STAMP),
        ]
    )
    group = backfill_cli.hive_group(ENTRY, tmp_path)
    monkeypatch.setattr(backfill_cli, "_target", lambda hive: (tmp_path, group))

    def loader(cwd: Path, *, group: str, pre_state_path: Path | None = None) -> BdStoreLoader:
        return BdStoreLoader(store, group=group, clock=Ticks(), pre_state_path=pre_state_path)

    monkeypatch.setattr(backfill_cli, "store_loader", loader)
    return store, group


def _invoke(*args: str) -> Any:
    result = CliRunner().invoke(cli.app, list(args), env=ENV)
    assert result.exit_code == 0, result.output
    return result


def test_the_flat_alias_and_the_nested_verb_produce_identical_plans(
    cfg_path,  # noqa: F811 — the imported fixture
    monkeypatch,
    tmp_path,
) -> None:
    corpus = [
        _corpus_row("h-1", labels=["model:sonnet"]),
        _corpus_row("h-2", status="closed", labels=["complexity:bogus"]),
        _corpus_row("h-3", labels=["complexity:REASONING"]),
        _corpus_row("h-gate", issue_type="gate"),
        _corpus_row("bh-merge-slot", labels=["gt:slot"]),
    ]
    monkeypatch.setattr(complexity_backfill, "enumerate_corpus", lambda cwd: corpus)
    monkeypatch.setattr(complexity_backfill.registry, "hive_dir_for", lambda cfg, hive: tmp_path)

    flat = _invoke("backfill-complexity", "--json", "--plan", str(tmp_path / "flat.json"))
    nested = _invoke("backfill", "complexity", "--json", "--plan", str(tmp_path / "nested.json"))
    assert flat.stdout == nested.stdout
    assert (tmp_path / "flat.json").read_text() == (tmp_path / "nested.json").read_text()
    plan = json.loads(flat.stdout)
    assert plan == complexity_backfill.build_plan(corpus)
    assert plan["totals"] == {"records": 5, "in_scope": 3, "excluded": 2, "changes": 2}

    flat_report = _invoke("backfill-complexity")
    nested_report = _invoke("backfill", "complexity", "--dry-run")
    assert flat_report.stdout == nested_report.stdout
    assert f"plan hash: {plan['plan_hash']}" in flat_report.stdout


def test_export_then_inspect_round_trips_the_hive(hive, tmp_path) -> None:
    store, group = hive
    archive = tmp_path / "hive.bdlg"

    exported = json.loads(_invoke("backfill", "export", "--out", str(archive), "--json").stdout)
    summary = json.loads(_invoke("backfill", "inspect", str(archive), "--json").stdout)

    assert exported["group"] == group.to_json()
    assert summary["groups"] == [group.to_json()]
    assert summary["counts"]["events"] == exported["events"] > 0
    assert summary["counts"]["beads"] == exported["beads"] == 2
    assert "plan" not in summary
    manifest, events = beadlog.read_archive(archive)
    assert manifest.groups == (group,)
    live = BdStoreLoader(store, group=group.id, clock=Ticks()).export()
    assert {event.id for event in events} == {event.id for event in live}
    assert beadlog.fold(events) == beadlog.fold(live)

    verified = json.loads(_invoke("backfill", "verify", str(archive), "--json").stdout)
    assert verified == {
        "path": str(archive),
        "ok": True,
        "events": exported["events"],
        "plan_hash": None,
    }
    assert store.writes == []


def _neutral_plan(store: FakeBd, group: beadlog.Group, path: Path) -> Plan:
    """A plan as the standalone ``beadlog reconcile`` writes it: one NEW bead, ``wd-9``."""
    baseline = BdStoreLoader(store, group=group.id, clock=Ticks()).export()
    ref = beadlog.BeadRef(group.id, "wd-9")
    proposed = [
        beadlog.mint_event(
            kind,
            ref,
            data,
            occurred_at=at,
            recorded_at=None,
            extracted_at="2026-10-31T00:00:00Z",
            source=("docs", f"docs/decisions/0009.md#{kind}"),
            provenance="inferred",
        )
        for kind, data, at in (
            ("create", {"type": "decision", "fields": {"title": "Adopt BeadLog"}}, STAMP),
            ("label_add", {"label": "origin:backfill"}, "2026-07-01T00:00:01Z"),
        )
    ]
    write_plan(
        path,
        reconcile(proposed, baseline),
        groups=[group],
        generator=beadlog.Generator("beadlog", "0.1.0"),
    )
    return Plan.read(path)


def test_apply_lands_a_standalone_beadlog_plan_inside_the_envelope(hive, tmp_path) -> None:
    store, group = hive
    plan_path = tmp_path / "plan.bdlg"
    plan = _neutral_plan(store, group, plan_path)
    assert plan.neutral and plan.changes == 1

    summary = json.loads(_invoke("backfill", "inspect", str(plan_path), "--json").stdout)
    assert summary["plan"]["pass"] == "reconcile"
    assert summary["plan"]["plan_hash"] == plan.plan_hash
    assert summary["plan"]["classes"]["NEW"] == 1
    assert set(summary["plan"]["verdicts"]) == {"recorded", "by_engine", "by_outcome"}
    verified = json.loads(_invoke("backfill", "verify", str(plan_path), "--json").stdout)
    assert verified["plan_hash"] == plan.plan_hash

    preview = json.loads(_invoke("backfill", "apply", str(plan_path), "--dry-run", "--json").stdout)
    assert preview["changes"] == 1
    assert preview["loss_report"]["mode"] == "dry-run"
    assert store.writes == []

    audit = json.loads(_invoke("backfill", "apply", str(plan_path), "--json").stdout)
    assert audit["state"] == "applied"
    assert audit["verification"]["second_dry_run_changes"] == 0
    assert store.rows["wd-9"]["title"] == "Adopt BeadLog"
    assert "origin:backfill" in store.rows["wd-9"]["labels"]
    assert json.loads((tmp_path / "plan.audit.json").read_text()) == audit
    assert (tmp_path / "plan.pre-state.jsonl").is_file()

    # Applying the same plan again is refused: the corpus it was planned against has changed.
    again = CliRunner().invoke(cli.app, ["backfill", "apply", str(plan_path)], env=ENV)
    assert again.exit_code == 1
    assert "corpus changed after planning" in again.output


def test_the_hive_group_is_a_stable_uuid_over_the_triplet(tmp_path) -> None:
    group = backfill_cli.hive_group(ENTRY, tmp_path)
    assert group == backfill_cli.hive_group(dict(ENTRY), tmp_path / "elsewhere")
    assert group.id.startswith("urn:uuid:") and group.dir == "wd"
    other = backfill_cli.hive_group({**ENTRY, "repo": "gadgets"}, tmp_path)
    assert other.id != group.id
    odd = backfill_cli.hive_group({**ENTRY, "prefix": "Weird_Prefix!"}, tmp_path)
    assert odd.dir == "weird_prefix"
