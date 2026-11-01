"""beadlog-xtdb is a library package: it never imports ``beadhive`` and ships typed."""

from __future__ import annotations

import ast
import subprocess
import sys
import tomllib
from importlib import resources
from pathlib import Path

import beadlog_xtdb

SOURCE = Path(beadlog_xtdb.__file__).parent
STDLIB = set(sys.stdlib_module_names) | {"__future__"}
ALLOWED_THIRD_PARTY = {"beadlog", "psycopg", "beadlog_xtdb"}


def _import_roots(path: Path) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            roots |= {alias.name.split(".", 1)[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".", 1)[0])
    return roots


def test_public_surface_is_declared_and_importable() -> None:
    assert len(set(beadlog_xtdb.__all__)) == len(beadlog_xtdb.__all__)
    for name in beadlog_xtdb.__all__:
        assert hasattr(beadlog_xtdb, name), name


def test_library_imports_only_stdlib_beadlog_and_psycopg() -> None:
    sources = sorted(SOURCE.rglob("*.py"))
    assert sources
    for path in sources:
        outside = _import_roots(path) - STDLIB - ALLOWED_THIRD_PARTY
        assert not outside, f"{path.name} imports {sorted(outside)}"
        assert "beadhive" not in _import_roots(path), path.name


def test_declared_dependencies_and_privacy() -> None:
    pyproject = tomllib.loads((SOURCE.parents[1] / "pyproject.toml").read_text(encoding="utf-8"))
    project = pyproject["project"]
    assert project["dependencies"] == ["beadlog", "psycopg[binary]>=3.2,<4"]
    assert "Private :: Do Not Upload" in project["classifiers"]
    assert "transit-python2" not in " ".join(project["dependencies"])  # the codec is in-package
    assert "plugin.json" not in {path.name for path in SOURCE.rglob("*")}


def test_package_is_a_library_not_a_plugin_and_ships_typed() -> None:
    assert not (SOURCE / "plugin.json").exists()
    assert (resources.files(beadlog_xtdb) / "py.typed").is_file()


def test_importing_the_library_does_not_import_beadhive() -> None:
    code = "import sys, beadlog_xtdb; sys.exit(1 if 'beadhive' in sys.modules else 0)"
    assert subprocess.run([sys.executable, "-c", code], check=False).returncode == 0


def test_runs_without_the_stateful_fixture_plugin() -> None:
    assert "stateful_fixtures" not in sys.modules
