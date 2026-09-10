"""Closeout contract for honest selective-CI operations and recertification."""

from __future__ import annotations

import importlib.util
import json
import sys
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "test_closure_operational_report.py"
EVIDENCE = ROOT / "docs" / "proof" / "bh-ck1t6.5-selective-ci-operations.json"
REPORT = ROOT / "docs" / "SELECTIVE-CI-OPERATIONS.md"
SPEC = importlib.util.spec_from_file_location("test_closure_operational_report", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
operations = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = operations
SPEC.loader.exec_module(operations)


def _evidence() -> dict[str, object]:
    return json.loads(EVIDENCE.read_text(encoding="utf-8"))


def test_checked_report_accounts_for_every_current_closure_without_inventing_savings() -> None:
    evidence = _evidence()

    assert operations.validate_checked_evidence(evidence, ROOT) == ()
    assert evidence["summary"] == {
        "certified": 0,
        "production_selective_routes": 0,
        "total": 24,
        "uncertified": 24,
    }
    assert len(evidence["closures"]) == 24
    assert all(row["evidence_date"] == "2026-09-10" for row in evidence["closures"])
    assert all(row["owner"] for row in evidence["closures"])
    assert all(row["fallback"] == "just check" for row in evidence["closures"])
    assert all(row["recertification_triggers"] for row in evidence["closures"])
    report = REPORT.read_text(encoding="utf-8")
    assert report == operations.render_report(evidence)
    assert all(f"`{row['id']}`" in report for row in evidence["closures"])

    for boundary in ("commit", "main-integration"):
        metrics = evidence["measurements"][boundary]
        assert metrics["status"] == "unavailable"
        assert metrics["sample_count"] == 0
        assert metrics["before"] == {
            "compute_seconds": None,
            "queue_seconds": None,
            "wall_seconds": None,
        }
        assert metrics["after"] == {
            "compute_seconds": None,
            "queue_seconds": None,
            "wall_seconds": None,
        }
        assert metrics["full_suite_frequency"] is None
        assert metrics["miss_rate"] is None
        assert metrics["flake_rate"] is None
        assert metrics["measured_savings_seconds"] is None


def test_registration_and_invalidation_contracts_fail_closed() -> None:
    evidence = _evidence()
    registration = evidence["registration"]

    assert registration["applies_to"] == ["module", "plugin"]
    assert registration["requires"] == ["closure", "conformance-declaration"]
    assert registration["gate"] == "just test-closure-check"

    invalidation = evidence["invalidation"]
    assert set(invalidation) == {
        "boundary-change",
        "coverage-staleness",
        "escaped-regression",
        "schema-change",
        "tool-version-change",
    }
    assert all(item["automatic_fallback"] == "just check" for item in invalidation.values())
    assert all(item["requires_recertification"] is True for item in invalidation.values())


def test_release_and_workstream_boundaries_remain_complete_full_only() -> None:
    evidence = _evidence()

    assert evidence["boundaries"]["full_only"] == [
        "child-epic-finish",
        "final-workstream-review",
        "final-workstream-submit",
        "leaf-merge",
        "release",
        "scheduled",
    ]
    assert evidence["boundaries"]["safety_gate"] == "just check-all"


def test_checked_report_rejects_even_an_optimistic_metric_mutation() -> None:
    forged = deepcopy(_evidence())
    forged["measurements"]["commit"]["measured_savings_seconds"] = 180.0

    assert operations.validate_checked_evidence(forged, ROOT)


def test_docs_and_full_gate_check_the_operational_contract() -> None:
    justfile = (ROOT / "justfile").read_text(encoding="utf-8")
    modules = (ROOT / "docs" / "MODULES.md").read_text(encoding="utf-8")
    process = (ROOT / "docs" / "TEST_PROCESS_POLICY.md").read_text(encoding="utf-8")

    assert "scripts/test_closure_operational_report.py --check" in justfile
    assert "conformance declaration" in modules
    assert "final-workstream-submit" in process
    assert "production selective routes: **0**" in process
