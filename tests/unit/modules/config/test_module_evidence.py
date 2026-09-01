from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]


def _read(name: str):
    return json.loads((ROOT / "docs/design" / name).read_text(encoding="utf-8"))


def test_config_module_evidence_matches_checked_ledgers():
    evidence = _read("config-module-evidence.json")
    dependencies = _read("config-consumer-migration-ledger.json")
    metrics = _read("config-module-structural-metrics.json")

    assert (
        evidence["fan_in"]["production_config_importers_after"]
        == dependencies["current"]["production_config_importer_count"]
    )
    assert (
        evidence["fan_in"]["production_config_schema_importers_after"]
        == dependencies["current"]["production_config_schema_importer_count"]
    )
    assert (
        evidence["static_test_blast_radius"]["exact_ast_files_after"]
        == dependencies["current"]["test_config_importer_count"]
    )
    assert metrics["after"]["cyclomatic_mean"] < metrics["before"]["cyclomatic_mean"]


def test_config_evidence_keeps_full_gate_and_selective_ci_claim_honest():
    evidence = _read("config-module-evidence.json")

    assert evidence["full_gate"]["command"] == "just check"
    assert evidence["full_gate"]["status"] == "green before authoritative group submit"
    assert evidence["full_gate"]["passed"] == 7344
    assert evidence["selective_ci_graduation_claimed"] is False
    assert "not a selective-CI graduation" in evidence["unit_coverage"]["warning"]
