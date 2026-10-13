"""Contracts for the direct pytest and backend-neutral validation routes."""

from __future__ import annotations

import re
from pathlib import Path

from scripts.check_attest_catalog import (
    KEY_RECIPES,
    fast_gate_leaves,
    fast_keys,
    native_gate_leaves,
)

ROOT = Path(__file__).parents[1]


def _recipe_body(justfile: str, recipe: str) -> list[str]:
    lines = justfile.splitlines()
    start = next(
        i
        for i, line in enumerate(lines)
        if line.startswith(f"{recipe}:") or line.startswith(f"{recipe} ")
    )
    body: list[str] = []
    for line in lines[start + 1 :]:
        if not line.startswith("    "):
            break
        body.append(line.strip())
    return body


def _declaration_index(key: str) -> int:
    justfile = (ROOT / "justfile").read_text(encoding="utf-8")
    return justfile.index(f"\nattest-{key}:")


def _dependencies(justfile: str, recipe: str) -> set[str]:
    line = next(line for line in justfile.splitlines() if line.startswith(f"{recipe}:"))
    return set(line.split(":", 1)[1].split())


def test_native_full_gate_has_no_pants_engine_dependency() -> None:
    justfile = (ROOT / "justfile").read_text(encoding="utf-8")
    # check-all-native is generated from the attest-* recipes, so the native gate is the leaf set
    # those recipes reach, and it carries no hand-listed dependencies of its own.
    assert not _dependencies(justfile, "check-all-native")
    leaves = set(native_gate_leaves(justfile))
    assert "architecture-structural-check" in leaves
    assert "stateful-native" in leaves
    assert "root-composition-native" in leaves
    assert "test-integration-land" in leaves
    assert "packages-check" in leaves
    assert (
        not {
            "architecture-pants-check",
            "pants-attest",
            "stateful-pants",
            "test-changed",
        }
        & leaves
    )

    full_body = "\n".join(_recipe_body(justfile, "check-all-native"))
    assert "pants" not in full_body.lower()


def test_check_all_native_runs_every_attest_recipe_in_declaration_order() -> None:
    justfile = (ROOT / "justfile").read_text(encoding="utf-8")
    body = "\n".join(_recipe_body(justfile, "check-all-native"))
    # Discovery is `just --summary --unsorted` (declaration order; the default is alphabetical),
    # filtered to attest-* and run one recipe at a time under `set -e`, with no hand-kept list.
    assert "set -euo pipefail" in body
    assert "just --summary --unsorted" in body
    assert "grep '^attest-'" in body
    assert 'just "$recipe"' in body
    code = [line for line in body.splitlines() if not line.lstrip().startswith("#")]
    assert not re.search(r"attest-[a-z]", "\n".join(code)), "a key is hand-named in the gate"
    # An empty discovery must fail instead of passing vacuously.
    assert "exit 1" in body
    declared = [name for name in re.findall(r"(?m)^(attest-[-\w]+):", justfile)]
    assert declared == [f"attest-{key}" for key in sorted(KEY_RECIPES, key=_declaration_index)]


def test_fast_profile_is_a_named_key_subset_of_the_catalog() -> None:
    justfile = (ROOT / "justfile").read_text(encoding="utf-8")
    assert not _dependencies(justfile, "check-native")
    body = "\n".join(_recipe_body(justfile, "check-native"))
    assert "{{fast_keys}}" in body
    assert 'just "attest-$key"' in body
    subset = fast_keys(justfile)
    assert subset
    assert set(subset) <= set(KEY_RECIPES)
    # The fast profile never runs the integration lane, the demos or the bd-requiring guard.
    assert not {"integration", "demos"} & set(subset)
    assert set(fast_gate_leaves(justfile)) <= set(native_gate_leaves(justfile))


def test_backend_neutral_structural_gate_owns_shared_contract_checks() -> None:
    justfile = (ROOT / "justfile").read_text(encoding="utf-8")
    neutral = "\n".join(_recipe_body(justfile, "architecture-structural-check"))
    for required in (
        "check_import_boundaries.py",
        "check_package_imports.py",
        "check_test_wall_clock.py",
        "test_closure_certification.py --check-structural",
        "test_closure_shadow_policy.py --check",
        "test_closure_promotion_policy.py --check",
        "test_closure_operational_report.py --check",
        "validation-evidence-refresh",
    ):
        assert required in neutral
    assert "pants" not in neutral.lower()

    # The transport/wire sub-recipes stay standalone entry points; the structural gate flattens
    # their commands into its concurrent pool, so each must stay in lockstep with its source.
    for sub_recipe in ("transport-artifact-check", "wire-schema-compat"):
        for command in _recipe_body(justfile, sub_recipe):
            assert f'"{command}"' in neutral, f"{sub_recipe}: {command} missing from the pool"

    pants = "\n".join(_recipe_body(justfile, "architecture-pants-check"))
    for required in (
        "check_pants_ownership.py",
        "check_pants_proven.py",
        "pants_ci.py verify",
    ):
        assert required in pants

    native_audit = "\n".join(_recipe_body(justfile, "architecture-check"))
    assert "check_pants_proven.py" not in native_audit


def test_stateful_attestation_waits_for_its_closure_evidence_producer() -> None:
    justfile = (ROOT / "justfile").read_text(encoding="utf-8")
    stateful = _dependencies(justfile, "stateful-native")

    # `attest-stateful` is a standalone selective-validation key. Its consumer must establish
    # current-tree evidence itself instead of relying on the architecture key to finish first,
    # but only through the minimal evidence producer: re-running the whole structural check
    # here duplicated the architecture-contracts lane (bh-wtrjy).
    assert "validation-evidence-refresh" in stateful
    assert "architecture-structural-check" not in stateful


def test_recursive_pants_artifact_is_excluded_only_from_native_profile() -> None:
    justfile = (ROOT / "justfile").read_text(encoding="utf-8")
    native = "\n".join(_recipe_body(justfile, "stateful-native"))
    artifact = "\n".join(_recipe_body(justfile, "pants-artifact-check"))
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    test_source = (ROOT / "tests" / "test_beadhive_pants_artifacts.py").read_text()

    assert "not integration and not pants_profile" in native
    assert "pants-artifact-check" in _dependencies(justfile, "check-all-pants")
    assert "pants-artifact-check" not in native_gate_leaves(justfile)
    assert "@pytest.mark.pants_profile\n@pytest.mark.skipif(" in test_source
    assert "pants_profile: executes the Pants engine" in pyproject
    assert "test_bh_pex_contains_and_resolves_the_backend" in artifact


def test_structural_gate_refreshes_evidence_serially_before_the_concurrent_pool() -> None:
    body = _recipe_body(
        (ROOT / "justfile").read_text(encoding="utf-8"), "architecture-structural-check"
    )
    assert body[0] == "just validation-evidence-refresh"
    assert body[1].startswith("uv run python scripts/run_checks_concurrently.py")
    assert all(line.startswith('"uv run ') for line in body[2:])
