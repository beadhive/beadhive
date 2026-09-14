"""Structural regressions for the non-authoritative Pants foundation."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

ROOT = Path(__file__).parents[1]


def _toml(path: str) -> dict:
    with (ROOT / path).open("rb") as stream:
        return tomllib.load(stream)


def test_pants_is_pinned_to_the_uv_capable_patch_release() -> None:
    config = _toml("pants.toml")
    version = tuple(int(part) for part in config["GLOBAL"]["pants_version"].split("."))

    assert version >= (2, 32, 1)
    assert "pants.backend.experimental.python" in config["GLOBAL"]["backend_packages"]
    assert config["python"]["resolver"] == "uv"
    assert config["python"]["run_against_entire_lockfile"] is True


def test_pytest_uses_one_repository_resolve_with_cov_and_xdist() -> None:
    config = _toml("pants.toml")
    pytest_config = config["pytest"]

    assert pytest_config["install_from_resolve"] == "beadhive"
    assert set(pytest_config["requirements"]) == {
        "//3rdparty/python:pytest",
        "//3rdparty/python:pytest-cov",
        "//3rdparty/python:pytest-xdist",
    }

    locked = {package["name"] for package in _toml("3rdparty/python/beadhive.lock")["package"]}
    assert {"pytest", "pytest-cov", "pytest-xdist"} <= locked

    metadata = json.loads(
        (ROOT / "3rdparty" / "python" / "beadhive.lock.metadata").read_text(encoding="utf-8")
    )
    assert metadata["lockfile_format"] == "uv"
    assert metadata["resolve"] == "beadhive"
    assert {"pytest>=8", "pytest-cov>=5", "pytest-xdist>=3"} <= set(
        metadata["generated_with_requirements"]
    )
    assert "pants generate-lockfiles --resolve=beadhive" in metadata["description"]


def test_unowned_imports_fail_closed_and_pure_tests_exclude_stateful_fixtures() -> None:
    config = _toml("pants.toml")
    build = (ROOT / "tests" / "BUILD").read_text(encoding="utf-8")

    assert config["python-infer"]["unowned_dependency_behavior"] == "error"
    pure = build.split('name="pure-tests"', maxsplit=1)[1].split(")\n\n", maxsplit=1)[0]
    stateful = build.split('name="legacy-stateful-tests"', maxsplit=1)[1]
    assert '":root-conftest"' in pure
    assert "stateful-fixtures" not in pure
    assert '":stateful-fixtures"' in stateful


def test_package_target_owns_runtime_resources() -> None:
    build = (ROOT / "src" / "beadhive" / "BUILD").read_text(encoding="utf-8")

    assert 'name="package-data"' in build
    assert '":package-data"' in build
    assert 'entry_point="beadhive.bootstrap.cli:main"' in build


def test_local_pants_runtime_state_is_ignored() -> None:
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()

    assert ".pants.d/" in ignored
