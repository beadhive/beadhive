"""Import and side-effect independence sentinel for the hives module."""

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
from dataclasses import fields
from pathlib import Path


def test_hives_module_import_has_no_outer_runtime_or_effects(monkeypatch) -> None:
    forbidden_imports = (
        "fastmcp",
        "typer",
        "beadhive.cli",
        "beadhive.mcp",
        "beadhive.config",
        "beadhive.gitworkspace",
        "beadhive.plugins",
        "beadhive.registry",
        "beadhive.retire",
        "beadhive.hive_ready",
    )
    for name in tuple(sys.modules):
        if name.startswith(("beadhive.modules.hives", *forbidden_imports)):
            monkeypatch.delitem(sys.modules, name, raising=False)

    class RefuseOuterRuntime(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname.startswith(forbidden_imports):
                raise AssertionError(f"hives module imported outer runtime: {fullname}")
            return None

    def forbidden(action):
        def fail(*_args, **_kwargs):
            raise AssertionError(f"hives module attempted {action}")

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
        hives = importlib.import_module("beadhive.modules.hives")
        assert hives.HiveIdentity.__module__ == "beadhive.modules.hives.domain.models"
    finally:
        sys.meta_path.remove(finder)


def test_hive_dtos_exclude_transport_presentation_vocabulary() -> None:
    from beadhive.modules import hives

    dto_types = (
        hives.HiveListRequest,
        hives.HiveListResult,
        hives.HiveStatusResult,
        hives.OnboardHiveRequest,
        hives.OnboardHiveResult,
        hives.ReadinessRequest,
        hives.ReadinessResult,
        hives.RetireHiveRequest,
        hives.RetireEvent,
        hives.RetireHiveResult,
    )
    forbidden = {"machine", "as_json", "payload", "text", "stream", "exit_code", "render"}

    for dto_type in dto_types:
        names = {field.name for field in fields(dto_type)}
        assert names.isdisjoint(forbidden), f"{dto_type.__name__} leaks {names & forbidden}"

    retirement_names = {field.name for field in fields(hives.RetireEvent)}
    retirement_presentation = {"detail", "message", "line", "glyph", "indent"}
    assert retirement_names.isdisjoint(retirement_presentation)


def test_public_hives_api_excludes_presentation_functions_methods_and_dependencies() -> None:
    module_root = Path(__file__).parents[4] / "src" / "beadhive" / "modules" / "hives"
    forbidden_names = {
        "machine",
        "as_json",
        "payload",
        "text",
        "stream",
        "exit_code",
        "render",
        "jsonout",
        "typer",
    }
    forbidden_dependencies = {"fastmcp", "json", "time", "typer", "beadhive.jsonout"}

    def name_parts(name: str) -> set[str]:
        return {part for part in name.strip("_").split("_") if part}

    for path in sorted(module_root.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported = {alias.name for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                imported = {node.module or ""}
            else:
                continue
            leaked = {
                dependency
                for dependency in forbidden_dependencies
                if any(name == dependency or name.startswith(f"{dependency}.") for name in imported)
            }
            assert not leaked, f"{path.name} imports presentation dependency {sorted(leaked)}"

        public_functions = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and not node.name.startswith("_")
        ]
        for function in public_functions:
            signature_names = {
                function.name,
                *(argument.arg for argument in function.args.posonlyargs),
                *(argument.arg for argument in function.args.args),
                *(argument.arg for argument in function.args.kwonlyargs),
            }
            leaked = {
                forbidden
                for name in signature_names
                for forbidden in forbidden_names
                if forbidden == name or forbidden in name_parts(name)
            }
            body_names = {node.id for node in ast.walk(function) if isinstance(node, ast.Name)} | {
                node.attr for node in ast.walk(function) if isinstance(node, ast.Attribute)
            }
            leaked.update(forbidden_names & body_names)
            assert not leaked, f"{path.name}:{function.name} leaks {sorted(leaked)}"

        exported = {
            element.value
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets
            )
            and isinstance(node.value, (ast.List, ast.Tuple))
            for element in node.value.elts
            if isinstance(element, ast.Constant) and isinstance(element.value, str)
        }
        for name in exported:
            leaked = forbidden_names & name_parts(name)
            assert not leaked, f"{path.name} exports presentation name {name!r}"
