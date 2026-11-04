"""``beadhive[github]`` and ``beadhive[xtdb]`` resolve from PyPI alone (bh-lrs9v).

The plugin import packages ride in the root wheel (as beadhive-core and the backfill libraries
do), so each extra carries only its third-party dependency and never names a ``Private :: Do Not
Upload`` workspace member. These are the source-level guards; the release workflow's pre-publish
smoke installs the BUILT wheel in clean venvs and is the end-to-end proof.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONFIG = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

VENDORED = {
    "packages/beadhive-tracker-github/src/beadhive_tracker_github": True,
    "packages/beadhive-beadlog-xtdb/src/beadhive_beadlog_xtdb": True,
    # a LIBRARY package: no plugin.json, but typed
    "packages/beadlog-xtdb/src/beadlog_xtdb": False,
}


def test_extras_carry_only_third_party_dependencies() -> None:
    extras = CONFIG["project"]["optional-dependencies"]
    assert extras["github"] == ["githubkit>=0.16.1,<1"]
    assert extras["xtdb"] == ["psycopg[binary]>=3.2,<4"]


def test_psycopg_is_in_no_default_install_path() -> None:
    project = CONFIG["project"]
    assert not any("psycopg" in dep for dep in project["dependencies"])
    for name, deps in project["optional-dependencies"].items():
        assert name == "xtdb" or not any("psycopg" in dep for dep in deps), name


@pytest.mark.parametrize("package", sorted(VENDORED))
def test_the_plugin_packages_are_vendored_with_their_resources(package: str) -> None:
    assert package in CONFIG["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    directory = ROOT / package
    assert (directory / "py.typed").is_file()
    assert (directory / "plugin.json").is_file() is VENDORED[package]


def test_no_extra_names_an_unpublished_workspace_member() -> None:
    private = set()
    for path in (ROOT / "packages").glob("*/pyproject.toml"):
        project = tomllib.loads(path.read_text(encoding="utf-8"))["project"]
        if "Private :: Do Not Upload" in project.get("classifiers", []):
            private.add(project["name"])
    assert "beadhive-tracker-github" in private  # the guard is meaningful
    for extra, deps in CONFIG["project"]["optional-dependencies"].items():
        if extra == "pants":  # still awaiting its own publication (bh-9mfg8)
            continue
        for dep in deps:
            name = re.split(r"[\[<>=!~ ;]", dep, maxsplit=1)[0]
            assert name not in private, f"beadhive[{extra}] requires unpublished {name}"
