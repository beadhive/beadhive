"""Cross-platform policy for real-process tests."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from harness import processes

ROOT = Path(__file__).resolve().parents[1]
FORK_POLICY = ROOT / "tests" / "harness" / "processes.py"


def _unsafe_process_calls(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    aliases = {"multiprocessing"}
    os_aliases = {"os"}
    direct_start_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for name in node.names:
                if name.name == "multiprocessing":
                    aliases.add(name.asname or name.name)
                elif name.name == "os":
                    os_aliases.add(name.asname or name.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "multiprocessing":
            for name in node.names:
                if name.name in {"Pool", "Process", "get_context", "set_start_method"}:
                    direct_start_names.add(name.asname or name.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "os":
            for name in node.names:
                if name.name == "fork":
                    direct_start_names.add(name.asname or name.name)

    findings = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        rendered = ast.unparse(node.func)
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr in {"Pool", "Process", "get_context", "set_start_method"}
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in aliases
        ):
            findings.append((node.lineno, rendered))
        elif (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "fork"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in os_aliases
        ):
            findings.append((node.lineno, rendered))
        elif isinstance(node.func, ast.Name) and node.func.id in direct_start_names:
            findings.append((node.lineno, rendered))
    return findings


def test_tests_select_processes_only_through_the_reviewed_policy() -> None:
    violations = {
        str(path.relative_to(ROOT)): _unsafe_process_calls(path)
        for path in (ROOT / "tests").rglob("*.py")
        if path != FORK_POLICY and _unsafe_process_calls(path)
    }
    assert violations == {}


def test_ordinary_process_policy_is_spawn_on_every_platform() -> None:
    assert processes.process_context().get_start_method() == "spawn"


def test_fork_policy_refuses_an_xdist_worker(monkeypatch) -> None:
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
    with pytest.raises(processes.UnsafeProcessStart, match="must not run inside"):
        processes.isolated_fork_context()


def test_fork_policy_refuses_any_multithreaded_parent(monkeypatch) -> None:
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    monkeypatch.setattr(processes.threading, "active_count", lambda: 2)
    with pytest.raises(processes.UnsafeProcessStart, match="single-threaded"):
        processes.isolated_fork_context()
