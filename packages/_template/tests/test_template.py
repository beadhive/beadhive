"""Sandbox-proven from birth: these tests need only this package and the standard library."""

from __future__ import annotations

import sys
from importlib import resources

import beadhive_package_template


def test_distribution_name_is_the_public_surface() -> None:
    assert beadhive_package_template.distribution_name() == "beadhive-package-template"


def test_typed_marker_ships_as_a_resource() -> None:
    marker = resources.files(beadhive_package_template) / "py.typed"
    assert marker.is_file()


def test_runs_without_the_stateful_fixture_plugin() -> None:
    assert "stateful_fixtures" not in sys.modules
