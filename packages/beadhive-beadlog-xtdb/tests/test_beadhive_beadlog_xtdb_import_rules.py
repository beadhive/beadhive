"""The plugin's import rules (package-class ADR section 2, beadlog-format-adr.md A6).

* The ``beadlog_xtdb`` library, and with it psycopg (LGPL, an opt-in extra), is imported only
  inside function bodies: importing any module of this package loads neither.
* From root, the package imports only ``beadhive.modules.*.contracts`` (never the kernel, the
  bootstrap or a root implementation module).

Checked structurally on the source (AST) and behaviourally in a fresh interpreter whose importer
refuses ``psycopg`` and ``beadlog_xtdb``. Each check has a negative control.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

PACKAGE_SRC = Path(__file__).resolve().parents[1] / "src" / "beadhive_beadlog_xtdb"
_HEAVY_ROOTS = frozenset({"beadlog_xtdb", "psycopg", "psycopg_binary"})


def _sites(src: Path):
    """``(path, line, module, at_import_time)`` for every absolute import under ``src``."""
    for path in sorted(src.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        functions = {
            id(node)
            for fn in ast.walk(tree)
            if isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda)
            for node in ast.walk(fn)
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    yield path, node.lineno, alias.name, id(node) not in functions
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                yield path, node.lineno, node.module, id(node) not in functions


def heavy_violations(src: Path) -> list[str]:
    return [
        f"{path.name}:{line}: {module} imported at module level"
        for path, line, module, eager in _sites(src)
        if eager and module.split(".")[0] in _HEAVY_ROOTS
    ]


def root_violations(src: Path) -> list[str]:
    problems = []
    for path, line, module, _eager in _sites(src):
        parts = module.split(".")
        if parts[0] != "beadhive":
            continue
        allowed = len(parts) >= 4 and parts[1] == "modules" and parts[3] == "contracts"
        if not allowed:
            problems.append(f"{path.name}:{line}: imports {module}")
    return problems


def _write(root: Path, files: dict[str, str]) -> Path:
    for name, body in files.items():
        (root / name).write_text(textwrap.dedent(body), encoding="utf-8")
    return root


def test_the_library_and_psycopg_are_imported_only_inside_functions() -> None:
    sites = list(_sites(PACKAGE_SRC))
    assert any(module == "beadlog_xtdb" and not eager for _p, _l, module, eager in sites)
    assert heavy_violations(PACKAGE_SRC) == []


@pytest.mark.parametrize(
    "source",
    [
        "import psycopg\n",
        "from beadlog_xtdb import connect\n",
        "import beadlog_xtdb._connect\n",
        "class C:\n    import psycopg\n",
        "try:\n    import psycopg\nexcept ImportError:\n    pass\n",
    ],
)
def test_negative_control_an_eager_heavy_import_is_reported(tmp_path, source) -> None:
    assert heavy_violations(_write(tmp_path, {"m.py": source}))


def test_the_package_imports_only_module_contracts_from_root() -> None:
    sites = list(_sites(PACKAGE_SRC))
    assert any(module.startswith("beadhive.modules.") for _p, _l, module, _e in sites)
    assert root_violations(PACKAGE_SRC) == []


@pytest.mark.parametrize(
    "source",
    [
        "from beadhive.kernel.plugins import ProviderKey\n",
        "from beadhive.bootstrap.beadlog_store import BeadlogStores\n",
        "import beadhive.hq_sql_transport\n",
        "from beadhive import config\n",
    ],
)
def test_negative_control_a_root_internal_import_is_reported(tmp_path, source) -> None:
    assert root_violations(_write(tmp_path, {"m.py": source}))


_BLOCKER = """
import importlib.abc, sys

class _Block(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if name.split(".")[0] in {"psycopg", "psycopg_binary", "beadlog_xtdb"}:
            raise ImportError("the xtdb extra is not installed: " + name)

sys.meta_path.insert(0, _Block())
"""

_IMPORT_EVERYTHING = """
import importlib, pkgutil
import beadhive
import beadhive_beadlog_xtdb
for info in pkgutil.walk_packages(beadhive_beadlog_xtdb.__path__, "beadhive_beadlog_xtdb."):
    importlib.import_module(info.name)
from beadhive_beadlog_xtdb import factory
store = factory.build({"enabled": True}, lambda: None)
assert store.store == "xtdb"
"""

_NOTHING_HEAVY_LOADED = """
import sys
loaded = sorted(m for m in sys.modules if m.split(".")[0] in {"psycopg", "beadlog_xtdb"})
assert not loaded, loaded
"""


def _python(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)], capture_output=True, text=True, check=False
    )


def test_every_module_imports_and_a_store_builds_without_the_library() -> None:
    result = _python(_BLOCKER + _IMPORT_EVERYTHING)
    assert result.returncode == 0, result.stderr


def test_importing_everything_loads_neither_the_library_nor_psycopg() -> None:
    result = _python(_IMPORT_EVERYTHING + _NOTHING_HEAVY_LOADED)
    assert result.returncode == 0, result.stderr


def test_negative_control_the_blocker_and_the_loaded_check_bite() -> None:
    refused = _python(_BLOCKER + "import psycopg\n")
    assert refused.returncode != 0 and "the xtdb extra is not installed" in refused.stderr
    loaded = _python(
        "import sys, types\nsys.modules['psycopg'] = types.ModuleType('psycopg')\n"
        + _NOTHING_HEAVY_LOADED
    )
    assert loaded.returncode != 0 and "psycopg" in loaded.stderr
