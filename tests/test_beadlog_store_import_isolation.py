"""With the XTDB store plugin disabled or not installed, ``import beadhive`` and every ``bh``
command work and never import psycopg (beadlog-format-adr.md A6; psycopg is LGPL and opt-in).

Two layers:

* **Structural.** No module of root ``beadhive`` imports psycopg, the ``beadlog_xtdb`` library or
  the plugin package, statically or through a literal ``import_module``, except the two lazy
  seams that run only for ``--store xtdb`` and ``bh plugin beadlog-xtdb doctor``. So no other
  command can reach psycopg at all.
* **Behavioural**, in a fresh interpreter with an empty ``BH_HOME`` and the test fixture
  config (no plugins section): ``import beadhive`` and the whole CLI tree (every plugin mount),
  ``--help`` of the root and of every top-level group, the plugin's own verbs and the
  ``--store xtdb`` composition's refusal, then an assertion over ``sys.modules``. One run blocks
  the plugin, the library and psycopg at the importer (not installed); the other has them
  installed (the dev environment) with the plugin disabled.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import textwrap
from pathlib import Path

_BLOCKER = """
import importlib.abc, sys

class _Block(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if name.split(".")[0] in {"psycopg", "psycopg_binary", "beadlog_xtdb",
                                  "beadhive_beadlog_xtdb"}:
            raise ImportError("the xtdb extra is not installed: " + name)

sys.meta_path.insert(0, _Block())
import importlib.util
_find = importlib.util.find_spec
importlib.util.find_spec = lambda name, *a: (
    None if name.split(".")[0] in {"beadhive_beadlog_xtdb", "beadlog_xtdb"} else _find(name, *a)
)
"""

_EXERCISE = """
import sys
import beadhive
from typer.main import get_command
from typer.testing import CliRunner
from beadhive.cli import app

def leaves(command, path=()):
    for name, child in sorted(getattr(command, "commands", {}).items()):
        if getattr(child, "commands", None) is not None:
            yield from leaves(child, (*path, name))
        else:
            yield (*path, name)

runner = CliRunner()
paths = list(leaves(get_command(app)))
assert ("plugin", "beadlog-xtdb", "doctor") in paths, "the plugin CLI is mounted"
assert ("backfill", "apply") in paths
groups = sorted({path[:1] for path in paths if len(path) > 1})
broken = []
for path in [(), *groups, ("backfill", "apply"), ("backfill", "export")]:
    result = runner.invoke(app, [*path, "--help"])
    if result.exit_code != 0:
        broken.append((path, result.exit_code, result.output[-300:]))
assert not broken, broken

status = runner.invoke(app, ["plugin", "beadlog-xtdb", "status"])
assert status.exit_code == 0, status.output
doctor = runner.invoke(app, ["plugin", "beadlog-xtdb", "doctor"])
assert doctor.exit_code == 0, doctor.output
from beadhive import backfill_cli
from beadhive.modules.beadlog_store.contracts import BeadlogStoreUnavailable
try:
    backfill_cli.beadlog_store("xtdb")
except BeadlogStoreUnavailable as exc:
    assert "--store xtdb needs the beadlog-xtdb plugin" in str(exc), exc
else:
    raise AssertionError("a disabled store must be refused")
print(status.output)

loaded = sorted(m for m in sys.modules if m.split(".")[0] in {"psycopg", "beadlog_xtdb"})
assert not loaded, loaded
"""


ROOT_SRC = Path(__file__).resolve().parents[1] / "src" / "beadhive"
FIXTURE_CONFIG = Path(__file__).resolve().parent / "fixture_config.yaml"
_OPT_IN = frozenset({"psycopg", "psycopg_binary", "beadlog_xtdb", "beadhive_beadlog_xtdb"})
#: The only root modules allowed to name the plugin package, both lazily inside a function.
_SEAMS = frozenset({"bootstrap/beadlog_store.py", "beadlog_xtdb_plugin.py"})


def opt_in_imports(src: Path) -> list[str]:
    """Every root import of an opt-in module (static or a literal ``import_module``), as
    ``"<path>:<line>: <module>"``, outside the lazy seams' function bodies."""
    found: list[str] = []
    for path in sorted(src.rglob("*.py")):
        relative = path.relative_to(src).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        lazy = {
            id(node)
            for fn in ast.walk(tree)
            if isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef)
            for node in ast.walk(fn)
        }
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            elif isinstance(node, ast.Call) and node.args:
                func = node.func
                called = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
                first = node.args[0]
                if called in {"import_module", "__import__"} and isinstance(first, ast.Constant):
                    names = [str(first.value)]
                elif called == "import_module" and isinstance(first, ast.JoinedStr):
                    names = [
                        str(part.value) for part in first.values if isinstance(part, ast.Constant)
                    ]
            for name in names:
                if name.split(".")[0] not in _OPT_IN:
                    continue
                if relative in _SEAMS and id(node) in lazy:
                    continue
                found.append(f"{relative}:{node.lineno}: {name}")
    return found


def test_no_root_module_imports_psycopg_or_the_plugin_outside_the_lazy_seams() -> None:
    assert opt_in_imports(ROOT_SRC) == []


def test_negative_control_an_opt_in_import_is_reported(tmp_path) -> None:
    (tmp_path / "bootstrap").mkdir()
    (tmp_path / "eager.py").write_text("import psycopg\n", encoding="utf-8")
    (tmp_path / "dynamic.py").write_text(
        "from importlib import import_module\nimport_module('beadlog_xtdb')\n", encoding="utf-8"
    )
    (tmp_path / "bootstrap" / "beadlog_store.py").write_text(
        "import beadhive_beadlog_xtdb.factory\n", encoding="utf-8"
    )
    found = opt_in_imports(tmp_path)
    assert [line.split(":")[0] for line in found] == [
        "bootstrap/beadlog_store.py",
        "dynamic.py",
        "eager.py",
    ]


def _run(code: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    config = tmp_path / "config.yaml"
    config.write_text(FIXTURE_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
    env = {
        **os.environ,
        "BH_HOME": str(tmp_path),
        "BH_CONFIG": str(config),
        "BH_SKIP_SETUP_CHECK": "1",
        "NO_COLOR": "1",
        "COLUMNS": "120",
    }
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        cwd=tmp_path,
    )


def test_every_command_works_without_the_plugin_installed(tmp_path) -> None:
    result = _run(_BLOCKER + _EXERCISE, tmp_path)
    assert result.returncode == 0, result.stderr[-3000:]
    assert "not installed" in result.stdout


def test_every_command_works_and_never_loads_psycopg_while_the_plugin_is_disabled(
    tmp_path,
) -> None:
    result = _run(_EXERCISE, tmp_path)
    assert result.returncode == 0, result.stderr[-3000:]
    assert "  package      installed" in result.stdout


def test_negative_control_the_loaded_check_bites(tmp_path) -> None:
    result = _run(
        "import sys, types\nsys.modules['psycopg'] = types.ModuleType('psycopg')\n"
        "loaded = sorted(m for m in sys.modules if m.split('.')[0] in {'psycopg'})\n"
        "assert not loaded, loaded\n",
        tmp_path,
    )
    assert result.returncode != 0 and "psycopg" in result.stderr
