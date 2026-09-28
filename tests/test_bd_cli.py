"""Root's composition over ``beadhive-bd-cli`` (bh-o3xuf).

The ``bd`` argv routes themselves are proven in ``packages/beadhive-bd-cli/tests``. What root owns
is the wiring: it resolves the package lazily by name (never a static import), and every route
runs through root's own ``bd`` invocation seam — so a route still gets ``-C <hive>`` scoping and
the audit ``--actor``, and a test patching ``bd._run`` still intercepts it.
"""

from __future__ import annotations

import ast
import json
import subprocess
from pathlib import Path

import pytest

from beadhive import bd, bd_cli, coordination

SRC = Path(__file__).resolve().parents[1] / "src" / "beadhive"
MAIN = Path("/hive/main")


def test_root_never_imports_beadhive_bd_cli_statically():
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            if any(name.split(".")[0] == bd_cli.PACKAGE for name in names):
                offenders.append(f"{path.relative_to(SRC)}:{node.lineno}")
    assert offenders == []


class RecordedBd:
    def __init__(self, stdout=""):
        self.stdout = stdout
        self.calls = []

    def __call__(self, cmd, **_kwargs):
        self.calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, self.stdout, "")


@pytest.fixture
def recorded(monkeypatch):
    def install(stdout=""):
        runner = RecordedBd(stdout)
        monkeypatch.setattr(bd, "_run", runner)
        return runner

    return install


def test_ports_run_through_roots_engine_with_hive_scoping_and_actor(recorded):
    runner = recorded()
    bd_cli.leases(MAIN).acquire("mr-1", actor="dev/a")
    bd_cli.state_operations(MAIN).set_state("mr-1", "review", "pending", reason="r", actor="dev/a")
    assert runner.calls == [
        ["bd", "-C", str(MAIN), "--actor", "dev/a", "update", "mr-1", "--claim"],
        [
            "bd", "-C", str(MAIN), "--actor", "dev/a",
            "set-state", "mr-1", "review=pending", "--reason", "r",
        ],
    ]  # fmt: skip


def test_coordination_forwards_through_roots_engine_and_returns_the_packages_types(recorded):
    runner = recorded(json.dumps({"id": "mr-1", "status": "heartbeat"}))
    result = coordination.heartbeat(MAIN, "mr-1", actor="dev/a")
    assert result.ok is True
    assert isinstance(result, coordination.HeartbeatResult)
    assert runner.calls == [
        ["bd", "-C", str(MAIN), "--actor", "dev/a", "heartbeat", "mr-1", "--json"]
    ]


def test_roots_bd_read_helpers_forward_to_the_package_routes(recorded):
    rows = [{"id": "e.1", "parent": "e"}, {"id": "e.2"}]
    runner = recorded(json.dumps(rows))
    assert [row["id"] for row in bd.children("e", MAIN)] == ["e.1"]
    assert bd.child_rows("e", MAIN) == rows
    assert bd_cli.ready_rows(MAIN, ["--limit", "0"]) == rows
    assert runner.calls[-1] == ["bd", "-C", str(MAIN), "ready", "--limit", "0", "--json"]
    assert bd.names_bead("blocks e.1", "e.1") and not bd.names_bead("blocks e.10", "e.1")
