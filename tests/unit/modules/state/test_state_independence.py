"""Import and dependency-direction sentinel for the state capability."""

from __future__ import annotations

import ast
import importlib
import importlib.abc
import importlib.metadata
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path


def test_state_module_import_has_no_outer_runtime_or_effects(monkeypatch) -> None:
    forbidden_imports = (
        "fastmcp",
        "typer",
        "starlette",
        "beadhive.cli",
        "beadhive.mcp",
        "beadhive.operator_api",
        "beadhive.public_readers",
        "beadhive.state_stream",
        "beadhive.validation_records",
        "beadhive.run_journal",
    )
    for name in tuple(sys.modules):
        if name.startswith(("beadhive.modules.state", *forbidden_imports)):
            monkeypatch.delitem(sys.modules, name, raising=False)

    class RefuseOuterRuntime(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname.startswith(forbidden_imports):
                raise AssertionError(f"state module imported outer runtime: {fullname}")
            return None

    def forbidden(action):
        def fail(*_args, **_kwargs):
            raise AssertionError(f"state module attempted {action}")

        return fail

    finder = RefuseOuterRuntime()
    sys.meta_path.insert(0, finder)
    monkeypatch.setattr(Path, "read_text", forbidden("filesystem read"))
    monkeypatch.setattr(importlib.metadata, "entry_points", forbidden("plugin discovery"))
    monkeypatch.setattr(socket, "create_connection", forbidden("network connection"))
    monkeypatch.setattr(subprocess, "Popen", forbidden("process spawn"))
    monkeypatch.setattr(subprocess, "run", forbidden("process spawn"))
    monkeypatch.setattr(os, "fork", forbidden("process fork"))
    monkeypatch.setattr(threading.Thread, "start", forbidden("runtime thread start"))
    try:
        module = importlib.import_module("beadhive.modules.state")
        assert module.ReadProjectionService.__module__.startswith("beadhive.modules.state")
    finally:
        sys.meta_path.remove(finder)


def test_state_module_has_no_import_back_to_consumers_or_adapters() -> None:
    module_root = Path(__file__).parents[4] / "src" / "beadhive" / "modules" / "state"
    forbidden_dependencies = {
        "fastmcp",
        "typer",
        "starlette",
        "beadhive.cli",
        "beadhive.mcp",
        "beadhive.operator_api",
        "beadhive.public_readers",
        "beadhive.state_stream",
        "beadhive.validation_records",
        "beadhive.run_journal",
    }
    for path in sorted(module_root.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        imports = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        leaked = {
            dependency
            for dependency in forbidden_dependencies
            if any(name == dependency or name.startswith(f"{dependency}.") for name in imports)
        }
        assert not leaked, f"{path.name} imports outer dependency {sorted(leaked)}"
