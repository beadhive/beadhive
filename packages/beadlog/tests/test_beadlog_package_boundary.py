"""beadlog is a stdlib-only library package that never imports ``beadhive``."""

from __future__ import annotations

import ast
import sys
import tomllib
from importlib import resources
from pathlib import Path

import beadlog

SOURCE = Path(beadlog.__file__).parent
STDLIB = set(sys.stdlib_module_names) | {"__future__"}


def _import_roots(path: Path) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            roots |= {alias.name.split(".", 1)[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".", 1)[0])
    return roots


def test_public_surface_is_declared_and_importable() -> None:
    assert len(set(beadlog.__all__)) == len(beadlog.__all__)
    for name in beadlog.__all__:
        assert hasattr(beadlog, name), name
    assert beadlog.distribution_name() == "beadlog"


def test_library_imports_only_the_standard_library() -> None:
    sources = sorted(SOURCE.rglob("*.py"))
    assert sources
    for path in sources:
        outside = _import_roots(path) - STDLIB - {"beadlog"}
        assert not outside, f"{path.name} imports {sorted(outside)}"


def test_no_runtime_dependency_is_declared() -> None:
    pyproject = tomllib.loads((SOURCE.parents[1] / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["dependencies"] == []
    assert "Private :: Do Not Upload" in pyproject["project"]["classifiers"]


def test_package_is_a_library_not_a_plugin() -> None:
    assert not (SOURCE / "plugin.json").exists()
    assert (resources.files(beadlog) / "py.typed").is_file()


def test_runs_without_the_stateful_fixture_plugin() -> None:
    assert "stateful_fixtures" not in sys.modules
