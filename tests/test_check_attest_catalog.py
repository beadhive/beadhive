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


def test_pants_profiles_fail_closed_on_missing_steps() -> None:
    source = (ROOT / "justfile").read_text()
    assert MODULE.check(source) == []
    for recipe, step in (
        ("check-pants", "test-changed"),
        ("check-all-pants", "pants-attest"),
        ("check-all-pants", "pants-artifact-check"),
    ):
        declaration = next(line for line in source.splitlines() if line.startswith(f"{recipe}:"))
        changed = source.replace(declaration, declaration.replace(f" {step}", ""), 1)
        assert any(recipe in error for error in MODULE.check(changed))


def test_check_all_native_cannot_be_hand_listed_or_lose_discovery() -> None:
    source = (ROOT / "justfile").read_text()
    handlisted = source.replace("check-all-native:\n", "check-all-native: lint\n", 1)
    assert any("must not hand-list" in error for error in MODULE.check(handlisted))
    for shape in MODULE.DISCOVERY_SHAPES:
        changed = source.replace(shape, "true", 1)
        assert any("must discover attest-*" in error for error in MODULE.check(changed)), shape


def test_check_native_is_a_validated_key_subset() -> None:
    source = (ROOT / "justfile").read_text()
    keys = MODULE.fast_keys(source)
    assert keys
    assert set(keys) <= set(MODULE.KEY_RECIPES)
    unknown = source.replace('fast_keys := "', 'fast_keys := "nonesuch ', 1)
    assert any("unknown key 'nonesuch'" in error for error in MODULE.check(unknown))
    unnamed = source.replace("fast_keys := ", "slow_keys := ", 1)
    assert any("fast_keys variable" in error for error in MODULE.check(unnamed))
    handlisted = source.replace("check-native:\n", "check-native: lint\n", 1)
    assert any("named key subset" in error for error in MODULE.check(handlisted))
    assert set(MODULE.fast_gate_leaves(source)) <= set(MODULE.native_gate_leaves(source))


def test_leaf_partition_requires_each_leaf_owned_once_and_the_union_complete() -> None:
    source = (ROOT / "justfile").read_text()
    leaves = MODULE.native_gate_leaves(source)
    assert len(leaves) == len(set(leaves))
    assert sorted(leaves) == sorted(
        leaf for _, key_leaves in MODULE.KEY_RECIPES.values() for leaf in key_leaves
    )
    # A leaf run by an attest recipe but unknown to KEY_RECIPES is an unowned part of the gate.
    extra = source.replace(
        "attest-docs:\n    just lint-md\n", "attest-docs:\n    just lint-md\n    just fmt\n", 1
    )
    errors = MODULE.check(extra)
    assert any("partition the native gate" in error for error in errors)
    assert any("docs: attest-docs invokes ['fmt']" in error for error in errors)
    # A leaf dropped from a key body leaves the gate incomplete.
    dropped = source.replace("    just lint-md\n", "", 1)
    assert any("does not invoke" in error for error in MODULE.check(dropped))
    # Two keys owning one leaf is rejected.
    shared = source.replace(
        "attest-hq-storage:\n    just hq-storage-check\n",
        "attest-hq-storage:\n    just hq-storage-check\n    just lint-md\n",
        1,
    )
    assert MODULE.check(shared)


def test_pants_only_steps_cannot_join_a_key() -> None:
    source = (ROOT / "justfile").read_text()
    changed = source.replace(
        "attest-docs:\n    just lint-md\n",
        "attest-docs:\n    just lint-md\n    just pants-attest\n",
        1,
    )
    assert any("Pants-only step" in error for error in MODULE.check(changed))


def test_a_new_attest_recipe_without_its_catalog_pieces_is_named() -> None:
    source = (ROOT / "justfile").read_text() + "\nattest-newkey:\n    just lint-md\n"
    errors = MODULE.check(source)
    assert any("no KEY_RECIPES entry ['newkey']" in error for error in errors)


def test_recipe_body_is_checked_when_recipe_has_dependencies() -> None:
    source = (ROOT / "justfile").read_text()
    assert "root-composition-validate" in MODULE._dependencies(source, "stateful-native")
    assert any(
        "not integration and not pants_profile" in line
        for line in MODULE._recipe_body(source, "stateful-native")
    )


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


def test_justfile_recipes_and_docs_table_must_match_key_recipes() -> None:
    source = (ROOT / "justfile").read_text()
    docs = (ROOT / "docs" / "ATTEST-KEYS.md").read_text()
    assert MODULE.check(source, None, docs) == []
    no_recipe = source.replace("attest-hq-storage:", "attest-hq-storage-gone:", 1)
    assert any("justfile attest-*" in error for error in MODULE.check(no_recipe))
    no_row = docs.replace("| `hq-storage` | `just attest-hq-storage`", "| `x` | `just attest-x`")
    assert any("docs/ATTEST-KEYS.md" in error for error in MODULE.check(source, None, no_row))


def _live(*, drop: str | None = None, extra: str | None = None, cmds: dict | None = None):
    names = [key for key in MODULE.KEY_RECIPES if key != drop] + ([extra] if extra else [])
    return [
        {"name": name, "cmd": (cmds or {}).get(name, f"just attest-{name}"), "enabled": True}
        for name in names
    ]


def test_live_catalog_exact_match_is_clean() -> None:
    source = (ROOT / "justfile").read_text()
    docs = (ROOT / "docs" / "ATTEST-KEYS.md").read_text()
    assert MODULE.live_catalog_errors(_live(), source, docs) == []


def test_live_catalog_missing_hq_storage_key_is_named() -> None:
    source = (ROOT / "justfile").read_text()
    errors = MODULE.live_catalog_errors(_live(drop="hq-storage"), source)
    assert any("hq-storage" in error and "missing" in error for error in errors)
    assert any("absent from the live HQ catalog" in error for error in errors)


def test_live_catalog_key_without_recipe_is_named() -> None:
    source = (ROOT / "justfile").read_text()
    errors = MODULE.live_catalog_errors(_live(extra="ghost"), source)
    assert any("ghost" in error and "no justfile recipe" in error for error in errors)


def test_live_catalog_changed_cmd_is_named() -> None:
    source = (ROOT / "justfile").read_text()
    errors = MODULE.live_catalog_errors(_live(cmds={"unit": "just unit"}), source)
    assert any("'unit'" in error and "just attest-unit" in error for error in errors)


def test_live_catalog_checks_recipes_and_docs_table_too() -> None:
    source = (ROOT / "justfile").read_text()
    docs = (ROOT / "docs" / "ATTEST-KEYS.md").read_text()
    no_row = docs.replace("| `hq-storage` | `just attest-hq-storage`", "| `x` | `just attest-x`")
    assert any(
        "docs/ATTEST-KEYS.md" in e for e in MODULE.live_catalog_errors(_live(), source, no_row)
    )
    no_recipe = source.replace("attest-hq-storage:", "attest-hq-storage-gone:", 1)
    assert any("justfile" in e for e in MODULE.live_catalog_errors(_live(), no_recipe))


def test_live_catalog_rejects_malformed_and_duplicate_input() -> None:
    source = (ROOT / "justfile").read_text()
    assert MODULE.live_catalog_errors({"unit": 1}, source)
    duplicated = _live() + [{"name": "unit", "cmd": "just attest-unit", "enabled": True}]
    assert any("duplicate" in e for e in MODULE.live_catalog_errors(duplicated, source))


def test_live_catalog_mode_reads_stdin_and_exits_nonzero_on_drift(monkeypatch, capsys) -> None:
    import io
    import json

    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_live())))
    assert MODULE.main(["--live-catalog", "-"]) == 0
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_live(drop="hq-storage"))))
    assert MODULE.main(["--live-catalog", "-"]) == 1
    assert "hq-storage" in capsys.readouterr().err
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert MODULE.main(["--live-catalog", "-"]) == 1
    assert MODULE.main(["--live-catalog"]) == 2
