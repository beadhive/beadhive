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


def test_architecture_key_owns_the_bootstrap_safe_gate_recipe() -> None:
    assert "architecture-structural-check" in MODULE.KEY_RECIPES["architecture-contracts"][1]
    assert "architecture-check" not in MODULE.KEY_RECIPES["architecture-contracts"][1]


def test_catalog_names_match_pants_tag_slugs() -> None:
    build_text = "\n".join(path.read_text() for path in ROOT.rglob("BUILD"))
    for key in MODULE.KEY_RECIPES:
        if key != "always-run":
            assert f"attest:{key}" in build_text
