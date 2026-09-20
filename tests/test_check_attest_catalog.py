from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_attest_catalog.py"
SPEC = importlib.util.spec_from_file_location("check_attest_catalog", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_catalog_partitions_check_all() -> None:
    assert MODULE.main() == 0


def test_architecture_key_explicitly_maps_full_gate_to_structural_recipe() -> None:
    assert MODULE.SELECTIVE_RECIPE_OVERRIDES == {
        "architecture-check": "architecture-structural-check"
    }


def test_catalog_names_match_pants_tag_slugs() -> None:
    build_text = "\n".join(path.read_text() for path in ROOT.rglob("BUILD"))
    for key in MODULE.KEY_RECIPES:
        if key != "always-run":
            assert f"attest:{key}" in build_text
