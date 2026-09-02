from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "scripts/config_dependency_ledger.py"


def _ledger_module():
    spec = importlib.util.spec_from_file_location("config_dependency_ledger", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_checked_config_dependency_ledger_is_current():
    module = _ledger_module()
    checked = module.DEFAULT_LEDGER.read_text(encoding="utf-8")

    assert checked == module._render(module.build_ledger())


def test_dependency_ledger_records_honest_compatibility_and_no_ci_graduation():
    ledger = _ledger_module().build_ledger()

    assert ledger["baseline"]["historical_test_blast_radius"] == 129
    assert ledger["current"]["production_config_importer_count"] < 95
    assert ledger["current"]["config_patch_points"]
    assert ledger["current"]["config_patch_point_count"] == len(
        ledger["current"]["config_patch_points"]
    )
    assert (
        len(ledger["current"]["remaining_broad_dependencies"])
        == ledger["current"]["production_config_importer_count"]
    )
    assert all(
        item["owner"] and item["rationale"]
        for item in ledger["current"]["remaining_broad_dependencies"]
    )
    assert ledger["compatibility"]["facades_retained"] == [
        "beadhive.config",
        "beadhive.config_schema",
    ]
    assert ledger["selective_ci_graduation_claimed"] is False


def test_dependency_ledger_records_indirect_adapter_patch_points_exactly():
    ledger = _ledger_module().build_ledger()
    points = ledger["current"]["config_patch_points"]

    assert ledger["schema_version"] == 2
    assert ledger["current"]["config_patch_point_count"] == 270
    expected = {
        (
            "tests/test_structural_facade_contracts.py",
            "beadhive.work.config.load",
            "beadhive.config.load",
        ),
        (
            "tests/test_orca.py",
            "beadhive.orca.config.orca_data_path",
            "beadhive.config.orca_data_path",
        ),
        (
            "tests/test_validation_ledger_cfg_threading.py",
            "beadhive.validation_ledger.config.load",
            "beadhive.config.load",
        ),
    }
    actual = {
        (point["path"], point["target"], point["facade_target"])
        for point in points
        if point["kind"] == "indirect-adapter"
    }
    assert expected <= actual


def test_indirect_patch_classifier_follows_only_declared_dynamic_adapter_aliases():
    module = _ledger_module()
    tree = ast.parse(
        "from beadhive import work as lifecycle\n"
        "monkeypatch.setattr(lifecycle.config, 'load', replacement)\n"
        "import beadhive.work\n"
        "monkeypatch.setattr(beadhive.work.config, 'max_commits', replacement)\n"
        "from beadhive.work import config as imported_settings\n"
        "monkeypatch.setattr(imported_settings, 'review_gate', replacement)\n"
        "monkeypatch.setattr(lifecycle.registry, 'load', replacement)\n"
    )
    imports = module._beadhive_module_imports(tree)
    receiver, qualified, imported, unrelated = [
        call.args[0] for call in ast.walk(tree) if isinstance(call, ast.Call) and call.args
    ]
    routes = {"beadhive.work": {"config": "beadhive.config"}}

    assert module._indirect_adapter_patch(receiver, "load", imports, routes) == (
        "beadhive.work.config.load",
        "beadhive.config.load",
    )
    assert module._indirect_adapter_patch(qualified, "max_commits", imports, routes) == (
        "beadhive.work.config.max_commits",
        "beadhive.config.max_commits",
    )
    assert module._indirect_adapter_patch(imported, "review_gate", imports, routes) == (
        "beadhive.work.config.review_gate",
        "beadhive.config.review_gate",
    )
    assert module._indirect_adapter_patch(unrelated, "load", imports, routes) is None
