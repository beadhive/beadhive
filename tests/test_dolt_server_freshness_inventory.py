from __future__ import annotations

import ast
import threading
from pathlib import Path

from stateful_fixtures import (
    DOLT_SERVER_FRESHNESS,
    HUB_BULK_FRESHNESS,
    ReusableDoltServer,
    _cleanup_reusable_dolt_server,
    _ensure_reusable_dolt_server,
    _recover_and_start_reusable_dolt_server,
)


def _marked_modules(root: Path) -> set[str]:
    marked = set()
    for path in (root / "tests").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if any(
            isinstance(node, ast.Attribute)
            and node.attr == "dolt_server"
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "mark"
            for node in ast.walk(tree)
        ):
            # The fixture implementation and its own focused contract tests mention the marker
            # without declaring a real-server test module.
            if path.name not in {"stateful_fixtures.py", "test_dolt_slot_scheduling.py"}:
                marked.add(path.relative_to(root).as_posix())
    return marked


def test_every_dolt_server_module_has_a_reviewed_freshness_contract():
    root = Path(__file__).parents[1]
    assert set(DOLT_SERVER_FRESHNESS) == _marked_modules(root)
    assert {classification for classification, _ in DOLT_SERVER_FRESHNESS.values()} == {
        "fresh",
        "mixed",
    }
    assert all(reason for _, reason in DOLT_SERVER_FRESHNESS.values())


def test_mixed_hub_bulk_module_classifies_each_real_server_test_and_fixture():
    root = Path(__file__).parents[1]
    tree = ast.parse((root / "tests/test_hub_bulk_int.py").read_text(encoding="utf-8"))
    functions = {
        node.name: {arg.arg for arg in node.args.args}
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
    }
    assert set(HUB_BULK_FRESHNESS) == set(functions)
    assert {classification for classification, _ in HUB_BULK_FRESHNESS.values()} == {
        "fresh",
        "reusable",
    }
    for name, (classification, reason) in HUB_BULK_FRESHNESS.items():
        fixture = (
            "isolated_shared_server" if classification == "reusable" else "fresh_shared_server"
        )
        assert fixture in functions[name]
        assert reason


def test_reusable_server_namespaces_mutable_databases_per_test():
    first = ReusableDoltServer(3307, "aaaa")
    second = ReusableDoltServer(3307, "bbbb")
    assert first.database("hva") == "hvaaaaa"
    assert second.database("hva") == "hvabbbb"
    assert first.databases.isdisjoint(second.databases)


def test_concurrent_first_use_starts_the_run_server_once(tmp_path):
    state = {"started": False, "starts": 0}
    state_lock = threading.Lock()

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    def connect(_address, timeout):
        del timeout
        with state_lock:
            if not state["started"]:
                raise OSError("not listening")
        return Connection()

    def start():
        with state_lock:
            state["starts"] += 1
            state["started"] = True

    threads = [
        threading.Thread(
            target=_ensure_reusable_dolt_server,
            args=(tmp_path / "server", 3307, start, connect),
        )
        for _ in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert state["starts"] == 1


def test_killed_worker_partial_start_is_reaped_before_replacement(tmp_path):
    server_dir = tmp_path / "server"
    bootstrap = tmp_path / "server-bootstrap"
    bootstrap.mkdir()
    (bootstrap / "partial-state").write_text("orphaned", encoding="utf-8")
    events = []

    def reap(path):
        events.append(("reap", path))

    def run_cmd(argv, **kwargs):
        events.append(("run", argv, kwargs))
        assert not (bootstrap / "partial-state").exists()

    _recover_and_start_reusable_dolt_server(server_dir, run_cmd=run_cmd, reap=reap)

    assert events[0] == ("reap", server_dir)
    assert events[1][0] == "run"
    assert events[1][2]["cwd"] == str(bootstrap)


def test_controller_cleanup_reaps_process_and_removes_all_startup_state(tmp_path):
    server_dir = tmp_path / "server"
    bootstrap = tmp_path / "server-bootstrap"
    bootstrap.mkdir()
    lock = tmp_path / "server.startup.lock"
    lock.touch()
    reaped = []

    _cleanup_reusable_dolt_server(server_dir, reap=reaped.append)

    assert reaped == [server_dir]
    assert not bootstrap.exists()
    assert not lock.exists()
