from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "docs/proof/bh-j5uyb.1-modularization-closeout.json"
EXPECTED_EPICS = {
    "bh-inqwc",
    "bh-qw9oi",
    "bh-5wuc0",
    "bh-18hud",
    "bh-bptze",
    "bh-3qkmk",
    "bh-id9pp",
    "bh-ck1t6",
}


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _load_report() -> dict[str, object]:
    return json.loads(REPORT.read_text(encoding="utf-8"))


def test_closeout_snapshot_is_complete_and_pinned() -> None:
    report = _load_report()
    workstream = report["workstream"]

    assert report["schema_version"] == 1
    assert report["bead"]["id"] == "bh-j5uyb.1"
    assert workstream["id"] == "bh-j5uyb"
    assert len(workstream["implementation_assembly_tip"]) == 40
    assert _git("cat-file", "-t", workstream["implementation_assembly_tip"]) == "commit"
    assert (
        _git("rev-parse", f"{workstream['implementation_assembly_tip']}^{{tree}}")
        == workstream["implementation_assembly_tree"]
    )
    assert (
        _git(
            "merge-base",
            workstream["main_anchor"],
            workstream["implementation_assembly_tip"],
        )
        == workstream["main_anchor"]
    )
    assert workstream["rollback_point"] == workstream["main_anchor"]


def test_all_eight_first_tier_epics_have_closed_no_ff_bubbles() -> None:
    report = _load_report()
    epics = report["first_tier_epics"]

    assert {epic["id"] for epic in epics} == EXPECTED_EPICS
    for epic in epics:
        assert epic["status"] == "closed"
        assert epic["close_reason"] == "molecule landed"
        merge_commit = epic["merge_commit"]
        parents = _git("show", "-s", "--format=%P", merge_commit).split()
        assert len(parents) == 2
        assert parents == [epic["first_parent"], epic["second_parent"]]
        subject = _git("show", "-s", "--format=%s", merge_commit)
        assert subject == f"chore(merge): molecule {epic['id']}"
        assert epic["children"]
        assert all(child["status"] == "closed" for child in epic["children"])
        assert all(child["review"]["submissions"] >= 1 for child in epic["children"])


def test_closeout_records_review_validation_debt_and_operator_commands() -> None:
    report = _load_report()

    review = report["review_history"]
    assert review["total_submissions"] >= len(EXPECTED_EPICS)
    assert review["reviewer_bounces"] > 0
    assert review["bounce_event_ids"]

    validation = report["validation"]
    assert validation["assembled_tree_full_gate"]["verdict"] == "green"
    assert validation["assembled_tree_full_gate"]["command"] == "just check-all"
    assert validation["schema_compatibility"]["status"] == "compatible"
    assert validation["transport_inventory"]["status"] == "current"
    assert validation["selective_ci"]["production_routes"] == 0

    structural = report["structural"]
    assert structural["current"]["unowned_architecture_errors"] == 0
    assert (
        structural["current"]["cyclic_modules"]
        <= structural["foundation_baseline"]["cyclic_modules"]
    )
    assert (
        structural["current"]["cyclic_edges"] <= structural["foundation_baseline"]["cyclic_edges"]
    )

    debt = report["remaining_debt"]
    assert len(debt["active_cycle_exception_ids"]) == debt["active_cycle_exceptions"]
    assert len(debt["active_boundary_exception_ids"]) == debt["active_boundary_exceptions"]
    assert len(debt["active_facade_ids"]) == debt["active_facades"]
    assert debt["ledger_paths"]

    repowise = report["repowise"]
    assert repowise["last_sync_commit"] == report["workstream"]["implementation_assembly_tip"]
    assert repowise["model_tokens"] == 0

    commands = report["operator_review"]["commands"]
    assert "bh work review bh-j5uyb --run --demo --view stat" in commands
    assert "bh work review bh-j5uyb --view diff" in commands
    assert report["known_risks"]
    assert report["rollback"]["commit"] == report["workstream"]["rollback_point"]
