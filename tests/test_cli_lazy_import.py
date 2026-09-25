"""Regression gates for the installed CLI's import-lazy process boundary."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from click import Context
from typer.main import get_command

ROOT = Path(__file__).resolve().parents[1]
CATALOG = (
    ROOT
    / "src"
    / "beadhive"
    / "schemas"
    / "contracts"
    / "v1.0.0"
    / "artifacts"
    / "operation-catalog-v1.json"
)


def _leaf_paths(command, prefix: tuple[str, ...] = ()) -> set[str]:
    paths: set[str] = set()
    for name, child in command.commands.items():
        path = (*prefix, name)
        if getattr(child, "commands", None) is not None:
            paths.update(_leaf_paths(child, path))
        else:
            paths.add(" ".join(path))
    return paths


def test_installed_entrypoint_imports_no_command_modules() -> None:
    script = """
import json
import sys
import beadhive.bootstrap.cli
print(json.dumps(sorted(name for name in sys.modules if name.startswith('beadhive.'))))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(result.stdout) == ["beadhive.bootstrap", "beadhive.bootstrap.cli"]


def test_lazy_entrypoint_preserves_the_complete_checked_command_tree() -> None:
    from beadhive.cli import app

    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    expected = {
        projection["path"]
        for operation in catalog["operations"]
        for projection in [operation["surfaces"].get("cli")]
        if projection is not None
    }
    expected.update(
        alias["path"]
        for operation in catalog["operations"]
        for projection in [operation["surfaces"].get("cli")]
        if projection is not None
        for alias in projection["aliases"]
    )
    expected.update(app._bh_manifest_commands)

    assert _leaf_paths(get_command(app)) == expected


def test_nested_work_completion_still_resolves() -> None:
    from beadhive.cli import app

    root = get_command(app)
    root_ctx = Context(root, info_name="bh")
    work = root.get_command(root_ctx, "work")
    assert work is not None
    work_ctx = Context(work, info_name="work", parent=root_ctx)

    assert "brief" in {item.value for item in work.shell_complete(work_ctx, "br")}


def test_version_exits_without_importing_the_command_tree() -> None:
    script = """
import json
import sys
from beadhive.cli_entrypoint import main
sys.argv = ['bh', '--version']
main()
print(json.dumps(sorted(name for name in sys.modules if name in {
    'beadhive.cli', 'beadhive.work', 'beadhive.plan', 'beadhive.herdr_views'
})))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        check=True,
        capture_output=True,
        text=True,
    )

    version, imported = result.stdout.splitlines()
    assert version
    assert json.loads(imported) == []
