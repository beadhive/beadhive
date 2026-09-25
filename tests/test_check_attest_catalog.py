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


def test_both_explicit_profiles_fail_closed_on_missing_steps() -> None:
    source = (ROOT / "justfile").read_text()
    assert MODULE.check(source) == []
    for recipe, step in (
        ("check-native", "stateful-native"),
        ("check-pants", "test-changed"),
        ("check-all-native", "packages-check"),
        ("check-all-pants", "pants-attest"),
        ("check-all-pants", "pants-artifact-check"),
    ):
        declaration = next(line for line in source.splitlines() if line.startswith(f"{recipe}:"))
        changed = source.replace(declaration, declaration.replace(f" {step}", ""), 1)
        assert any(recipe in error for error in MODULE.check(changed))


def test_aliases_select_one_explicit_profile() -> None:
    source = (ROOT / "justfile").read_text()
    changed = source.replace("check: check-native", "check: check-pants check-native", 1)
    assert any("check must alias" in error for error in MODULE.check(changed))


def test_alias_and_push_hook_profiles_cannot_drift() -> None:
    source = (ROOT / "justfile").read_text()
    changed = source.replace("check-all: check-all-native", "check-all: check-all-pants", 1)
    assert any("same profile" in error for error in MODULE.check(changed))
    hook = (ROOT / "scripts" / "main-push-gate.sh").read_text()
    changed_hook = hook.replace(
        'gate_cmd="just check-all-native"', 'gate_cmd="just check-all-pants"'
    )
    assert any("push hook" in error for error in MODULE.check(source, changed_hook))


def test_pants_artifact_proof_cannot_disappear_from_its_recipe() -> None:
    source = (ROOT / "justfile").read_text()
    changed = source.replace(
        "tests/test_beadhive_pants_artifacts.py::test_bh_pex_contains_and_resolves_the_backend",
        "tests/test_beadhive_pants_artifacts.py::test_bh_pex_declares_the_plugin_in_its_build_closure",
        1,
    )
    assert any("recursive PEX proof" in error for error in MODULE.check(changed))


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
