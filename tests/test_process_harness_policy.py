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
    bindings: dict[str, str] = {}
    findings: list[tuple[int, str]] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for name in node.names:
                bound_name = name.asname or name.name.split(".", maxsplit=1)[0]
                bindings[bound_name] = name.name if name.asname else bound_name
                if name.name == "multiprocessing" or name.name.startswith("multiprocessing."):
                    findings.append((node.lineno, f"import {name.name}"))
        elif isinstance(node, ast.ImportFrom) and node.module:
            for name in node.names:
                qualified = f"{node.module}.{name.name}"
                bindings[name.asname or name.name] = qualified
                if node.module == "multiprocessing" or node.module.startswith("multiprocessing."):
                    findings.append((node.lineno, f"from {node.module} import {name.name}"))
                elif qualified == "concurrent.futures.ProcessPoolExecutor":
                    findings.append((node.lineno, qualified))
                elif node.module == "concurrent.futures" and name.name == "*":
                    findings.append((node.lineno, "from concurrent.futures import *"))

    def qualified_name(node: ast.expr) -> str | None:
        if isinstance(node, ast.Name):
            return bindings.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            parent = qualified_name(node.value)
            return f"{parent}.{node.attr}" if parent else None
        return None

    # Follow simple module/class aliases so `alias = futures` cannot hide a process pool.
    assignments = [node for node in ast.walk(tree) if isinstance(node, (ast.Assign, ast.AnnAssign))]
    for node in sorted(assignments, key=lambda item: item.lineno):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        value = qualified_name(node.value) if node.value is not None else None
        if value is None or not value.startswith(("multiprocessing", "concurrent.futures", "os")):
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                bindings[target.id] = value

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        qualified = qualified_name(node.func)
        if qualified == "os.fork" or (
            qualified is not None
            and qualified.startswith("concurrent.futures.")
            and qualified.endswith(".ProcessPoolExecutor")
        ):
            findings.append((node.lineno, ast.unparse(node.func)))
    return findings


def test_tests_select_processes_only_through_the_reviewed_policy() -> None:
    violations = {
        str(path.relative_to(ROOT)): _unsafe_process_calls(path)
        for path in (ROOT / "tests").rglob("*.py")
        if path != FORK_POLICY and _unsafe_process_calls(path)
    }
    assert violations == {}


@pytest.mark.parametrize(
    "source",
    [
        "import multiprocessing as mp\nalias = mp\nalias.Process()",
        "import multiprocessing as mp\nmp.Manager()",
        ("from concurrent.futures import ProcessPoolExecutor\nProcessPoolExecutor()"),
        ("import concurrent.futures as futures\nalias = futures\nalias.ProcessPoolExecutor()"),
        "from concurrent.futures import *\nProcessPoolExecutor()",
        "import os\nforker = os\nforker.fork()",
    ],
)
def test_policy_rejects_indirect_or_ambient_process_spawners(tmp_path, source) -> None:
    probe = tmp_path / "unsafe_process_test.py"
    probe.write_text(source)
    assert _unsafe_process_calls(probe)


def test_policy_allows_thread_pool_executor(tmp_path) -> None:
    probe = tmp_path / "thread_test.py"
    probe.write_text("from concurrent.futures import ThreadPoolExecutor\nThreadPoolExecutor()")
    assert _unsafe_process_calls(probe) == []


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
