from __future__ import annotations

from pathlib import Path

import pytest
from scripts.root_composition_tests import (
    PACKAGE_TESTS,
    UnsafeRootCompositionPath,
    contract_tests,
    direct_package_tests,
    selected_tests,
    validate,
)


def test_each_package_selection_includes_contracts_and_its_registered_tests() -> None:
    contracts = set(contract_tests())
    assert contracts
    for package, registered in PACKAGE_TESTS.items():
        selected = set(selected_tests(package))
        assert contracts <= selected, package
        assert set(registered) <= selected, package


def test_union_is_deduplicated_and_every_registered_path_exists() -> None:
    selected = validate()
    assert selected == tuple(sorted(set(selected)))
    assert all((Path(__file__).parents[1] / path).is_file() for path in selected)


def test_every_direct_root_package_dependent_is_registered_or_a_contract() -> None:
    contracts = set(contract_tests())
    dependents = direct_package_tests()

    for package, paths in dependents.items():
        assert set(paths) <= set(PACKAGE_TESTS[package]) | contracts, package
    assert {
        "tests/test_selective_validation_impact_golden.py",
        "tests/unit/modules/work/test_impact_plugin_backends.py",
    } <= set(PACKAGE_TESTS["beadhive-pants"])


def test_unregistered_direct_root_package_dependent_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "packages" / "beadhive-example"
    package.mkdir(parents=True)
    (package / "pyproject.toml").write_text(
        '[project]\nname = "beadhive-example"\n'
        '[tool.hatch.build.targets.wheel]\npackages = ["src/beadhive_example"]\n'
    )
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_consumer.py").write_text("import beadhive_example\n")
    monkeypatch.setattr("scripts.root_composition_tests.PACKAGE_TESTS", {"beadhive-example": ()})

    with pytest.raises(SystemExit, match="direct root dependents lack PACKAGE_TESTS coverage"):
        validate(tmp_path)


@pytest.mark.parametrize(
    "packages_value",
    ('"src/beadhive_example"', "[]", "[1]"),
)
def test_malformed_wheel_packages_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, packages_value: str
) -> None:
    package = tmp_path / "packages" / "beadhive-example"
    package.mkdir(parents=True)
    (package / "pyproject.toml").write_text(
        '[project]\nname = "beadhive-example"\n'
        f"[tool.hatch.build.targets.wheel]\npackages = {packages_value}\n"
    )
    (tmp_path / "tests").mkdir()
    monkeypatch.setattr("scripts.root_composition_tests.PACKAGE_TESTS", {"beadhive-example": ()})

    with pytest.raises(SystemExit, match="wheel.packages must be a non-empty list of paths"):
        validate(tmp_path)


def test_registered_test_path_rejects_shell_unsafe_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(PACKAGE_TESTS, "unsafe-package", ("tests/unsafe name.py",))

    with pytest.raises(UnsafeRootCompositionPath, match="unsafe for shell expansion"):
        selected_tests("unsafe-package")


def test_discovered_contract_path_rejects_shell_unsafe_names(tmp_path: Path) -> None:
    contracts = tmp_path / "tests" / "contracts"
    contracts.mkdir(parents=True)
    (contracts / "test_unsafe name.py").write_text("def test_placeholder(): pass\n")

    with pytest.raises(UnsafeRootCompositionPath, match="unsafe for shell expansion"):
        selected_tests(root=tmp_path)
