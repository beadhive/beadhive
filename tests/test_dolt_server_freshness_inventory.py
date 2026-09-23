from __future__ import annotations

import ast
import threading
from pathlib import Path

from stateful_fixtures import (
    DOLT_SERVER_FRESHNESS,
    ReusableDoltServer,
    _ensure_reusable_dolt_server,
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
        "reusable",
    }
    assert all(reason for _, reason in DOLT_SERVER_FRESHNESS.values())


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
