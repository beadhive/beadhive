"""Contracts for the direct pytest and backend-neutral validation routes."""

from __future__ import annotations

from pathlib import Path

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


def _dependencies(justfile: str, recipe: str) -> set[str]:
    line = next(line for line in justfile.splitlines() if line.startswith(f"{recipe}:"))
    return set(line.split(":", 1)[1].split())


def test_native_full_gate_has_no_pants_engine_dependency() -> None:
    justfile = (ROOT / "justfile").read_text(encoding="utf-8")
    dependencies = _dependencies(justfile, "check-all-native")
    assert "architecture-structural-check" in dependencies
    assert "stateful-native" in dependencies
    assert "test-integration-land" in dependencies
    assert "packages-check" in dependencies
    assert (
        not {
            "architecture-pants-check",
            "pants-attest",
            "stateful-pants",
            "test-changed",
        }
        & dependencies
    )

    full_body = "\n".join(_recipe_body(justfile, "check-all-native"))
    assert "pants" not in full_body.lower()


def test_backend_neutral_structural_gate_owns_shared_contract_checks() -> None:
    justfile = (ROOT / "justfile").read_text(encoding="utf-8")
    neutral = "\n".join(_recipe_body(justfile, "architecture-structural-check"))
    for required in (
        "check_import_boundaries.py",
        "check_package_imports.py",
        "test_closure_certification.py --check-structural",
        "test_closure_shadow_policy.py --check",
        "test_closure_promotion_policy.py --check",
        "test_closure_operational_report.py --check",
        "transport-artifact-check",
        "wire-schema-compat",
        "proof-digest-check",
    ):
        assert required in neutral
    assert "pants" not in neutral.lower()

    pants = "\n".join(_recipe_body(justfile, "architecture-pants-check"))
    for required in (
        "pants_shadow_evidence.py",
        "check_pants_ownership.py",
        "check_pants_proven.py",
        "pants_ci.py verify",
        "pants_ci_benchmark.py check",
    ):
        assert required in pants


def test_recursive_pants_artifact_is_excluded_only_from_native_profile() -> None:
    justfile = (ROOT / "justfile").read_text(encoding="utf-8")
    native = "\n".join(_recipe_body(justfile, "stateful-native"))
    artifact = "\n".join(_recipe_body(justfile, "pants-artifact-check"))
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    test_source = (ROOT / "tests" / "test_beadhive_pants_artifacts.py").read_text()

    assert "not integration and not pants_profile" in native
    assert "pants-artifact-check" in _dependencies(justfile, "check-all-pants")
    assert "pants-artifact-check" not in _dependencies(justfile, "check-all-native")
    assert "@pytest.mark.pants_profile\n@pytest.mark.skipif(" in test_source
    assert "pants_profile: executes the Pants engine" in pyproject
    assert "test_bh_pex_contains_and_resolves_the_backend" in artifact
