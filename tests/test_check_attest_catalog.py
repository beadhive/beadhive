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
        assert f"attest:{key}" in build_text


def test_no_selectorless_key_and_bd_probe_is_scoped_to_integration() -> None:
    justfile = (ROOT / "justfile").read_text()
    assert "always-run" not in MODULE.KEY_RECIPES
    assert "attest-always-run:" not in justfile
    assert MODULE.KEY_RECIPES["integration"][1] == (
        "require-bd",
        "test-integration-land",
    )
    assert all(
        "require-bd" not in leaves
        for key, (_, leaves) in MODULE.KEY_RECIPES.items()
        if key != "integration"
    )


def test_root_prose_and_exact_doc_reader_input_have_narrow_owners() -> None:
    root_build = (ROOT / "BUILD").read_text()
    tests_build = (ROOT / "tests" / "BUILD").read_text()

    assert 'name="root-build-system"' in root_build
    assert 'name="root-config"' in root_build
    assert 'name="root-prose"' in root_build
    assert 'name="root-guide-doc"' in root_build
    assert 'tags=["category:docs", "attest:docs"]' in root_build
    assert '"dependencies": ["//docs:docs-root", "//:root-guide-doc"]' in tests_build
    assert "//:root-config" not in tests_build
