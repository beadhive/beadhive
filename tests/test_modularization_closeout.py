from __future__ import annotations

import hashlib
import json
import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "docs/proof/bh-j5uyb.1-modularization-closeout.json"
ARCHITECTURE_DEBT_LEDGER = ROOT / "docs/design/import-boundary-exceptions.toml"
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

    assert report["schema_version"] == 2
    assert report["bead"]["id"] == "bh-j5uyb.1"
    assert workstream["id"] == "bh-j5uyb"
    assert len(workstream["current_candidate_tip"]) == 40
    assert _git("cat-file", "-t", workstream["current_candidate_tip"]) == "commit"
    assert (
        _git("rev-parse", f"{workstream['current_candidate_tip']}^{{tree}}")
        == workstream["current_candidate_tree"]
    )
    assert (
        _git(
            "merge-base",
            workstream["main_anchor"],
            workstream["current_candidate_tip"],
        )
        == workstream["main_anchor"]
    )
    assert (
        _git("merge-base", workstream["current_candidate_tip"], "HEAD")
        == workstream["current_candidate_tip"]
    )
    lineage = workstream["candidate_lineage"]
    assert lineage[-1]["merge"] == workstream["current_candidate_tip"]
    assert lineage[-1]["tree"] == workstream["current_candidate_tree"]
    assert [row["bead"] for row in lineage] == [
        "bh-j5uyb.1",
        "bh-uvotu.1",
        "bh-uvotu.2",
        "bh-uvotu.3",
    ]
    for row in lineage:
        assert _git("rev-parse", f"{row['merge']}^{{tree}}") == row["tree"]
    assert workstream["rollback_point"] == workstream["main_anchor"]
    assert "No SHA or receipt recorded before" in workstream["final_tree_authority"]


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
    historical_gate = validation["historical_assembly_full_gate"]
    assert historical_gate["verdict"] == "green"
    assert historical_gate["command"] == "just check-all"
    assert historical_gate["tree"] == report["workstream"]["historical_assembly_tree"]
    assert "not final-candidate proof" in historical_gate["classification"]
    assert validation["schema_compatibility"]["status"] == "compatible"
    assert validation["transport_inventory"]["status"] == "current"
    assert validation["selective_ci"]["production_routes"] == 0
    receipt = validation["receipt_bootstrap_remediation"]
    assert "historical bh-ck1t6.5 bead identity is not authority" in receipt["completed_policy"]
    assert all(
        token in receipt["in_flight_policy"]
        for token in ("exact current worktree", "host", "PID", "process-start token")
    )

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
    architecture_debt = tomllib.loads(ARCHITECTURE_DEBT_LEDGER.read_text(encoding="utf-8"))
    debt_sections = (
        ("cycle_exception", "active_cycle_exception_ids", "active_cycle_exceptions"),
        ("boundary_exception", "active_boundary_exception_ids", "active_boundary_exceptions"),
        ("facade", "active_facade_ids", "active_facades"),
    )
    for ledger_key, report_ids_key, report_count_key in debt_sections:
        authoritative_ids = {
            row["id"] for row in architecture_debt[ledger_key] if row["status"] == "active"
        }
        reported_ids = debt[report_ids_key]
        assert set(reported_ids) == authoritative_ids
        assert len(reported_ids) == len(authoritative_ids)
        assert debt[report_count_key] == len(authoritative_ids)
    assert ARCHITECTURE_DEBT_LEDGER.relative_to(ROOT).as_posix() in debt["ledger_paths"]

    repowise = report["repowise"]
    assert repowise["last_sync_commit"] == report["workstream"]["historical_assembly_tip"]
    assert repowise["classification"].startswith("historical")
    assert repowise["model_tokens"] == 0

    commands = report["operator_review"]["commands"]
    assert commands == [
        "bh work review bh-j5uyb --run --view stat",
        "just demo-local-loop",
        "bh work review bh-j5uyb --view diff",
    ]
    assert not any("--demo" in command for command in commands)
    demo = report["operator_review"]["demo_authority"]
    assert demo["authoritative_command"] == "just demo-local-loop"
    assert "scripts/hermetic.sh" in demo["isolation"]
    diagnostic = demo["ambient_diagnostic"]
    assert diagnostic["run_id"] == "run-4146531c0d2a8c7c5ec79dcbf02ffdc7"
    assert diagnostic["verdict"] == "red"
    assert "not authoritative" in diagnostic["classification"]
    assert report["known_risks"]
    assert report["rollback"]["commit"] == report["workstream"]["rollback_point"]


def test_current_candidate_artifact_digests_are_reproducible() -> None:
    report = _load_report()
    current = report["evidence_inventory"]["current_candidate"]
    rows = [
        *current["transport_composition"]["artifacts"],
        current["import_boundary"]["ledger"],
        *current["schema_release"]["artifacts"],
        *current["selective_ci"]["artifacts"],
        current["package_and_entry_point"]["manifest"],
    ]

    assert len({row["path"] for row in rows}) == len(rows)
    for row in rows:
        path = ROOT / row["path"]
        assert path.is_file(), row["path"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == row["sha256"], row["path"]


def test_frame_bridge_ownership_and_follow_up_debt_are_unambiguous() -> None:
    report = _load_report()
    current = report["evidence_inventory"]["current_candidate"]
    package = current["package_and_entry_point"]
    project = tomllib.loads((ROOT / package["manifest"]["path"]).read_text(encoding="utf-8"))

    assert project["project"]["scripts"]["beadhive-frame-bridge"] == (
        "beadhive.bootstrap.frame_bridge:main"
    )
    assert "beadhive-gateway" not in project["project"]["scripts"]
    boundary = report["validation"]["transport_inventory"]["gateway_boundary"]
    assert "sibling beadhive-gateway repository" in boundary
    assert "not a core process" in boundary

    follow_ups = {row["id"] for row in report["remaining_debt"]["follow_up_beads"]}
    assert {"bh-9ghuh.1", "bh-gw-ywh.1", "bh-gw-ywh.2", "bh-gw-ywh.3"} <= follow_ups
    historical = report["evidence_inventory"]["immutable_historical"]
    assert historical
    assert all("classification" in row for row in historical)
