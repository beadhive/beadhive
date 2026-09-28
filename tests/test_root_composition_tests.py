from __future__ import annotations

from pathlib import Path

from scripts.root_composition_tests import PACKAGE_TESTS, contract_tests, selected_tests, validate


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
