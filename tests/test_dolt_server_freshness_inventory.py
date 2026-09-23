from __future__ import annotations

import ast
from pathlib import Path

from stateful_fixtures import DOLT_SERVER_FRESHNESS


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
