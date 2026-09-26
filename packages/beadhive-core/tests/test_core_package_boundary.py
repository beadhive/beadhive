"""The core stays independent of the root application and on its declared routes."""

from __future__ import annotations

import ast
import sys
from importlib import resources
from pathlib import Path

import beadhive_core
from beadhive_beads_client import cli_compatibility_operations, load_operation_matrix

SOURCE = Path(__file__).resolve().parents[1] / "src" / "beadhive_core"

# Root application, legacy argv helpers, process/network primitives, storage and build tooling.
FORBIDDEN = {
    "beadhive",
    "beadhive_pants",
    "bd",
    "dolt",
    "pants",
    "pytest",
    "stateful_fixtures",
    "subprocess",
    "socket",
    "tests",
    "typer",
}


def _imported_roots(path: Path) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "import_module":
            raise AssertionError(f"{path.name}: dynamic imports are not part of the core")
    return roots


def test_core_imports_only_its_client_and_the_standard_library() -> None:
    sources = sorted(SOURCE.rglob("*.py"))
    assert sources
    for path in sources:
        leaked = _imported_roots(path) & FORBIDDEN
        assert not leaked, f"{path.name} imports {sorted(leaked)}"


def test_ports_stand_in_only_for_operations_without_an_http_route() -> None:
    cli_routes = cli_compatibility_operations()
    assert set(beadhive_core.GATE_ROUTES) <= cli_routes
    assert set(beadhive_core.STATE_ROUTES) <= cli_routes
    api_ready = {
        row["name"]: row["capability"]
        for row in load_operation_matrix()["operations"]
        if row["classification"] == "api-ready"
    }
    for operation in ("work.issue.get", "plan.labels.update", "work.feedback.comment.add"):
        assert api_ready[operation] in beadhive_core.REVIEW_CAPABILITIES


def test_typed_marker_ships_and_no_stateful_fixture_plugin_is_loaded() -> None:
    assert (resources.files(beadhive_core) / "py.typed").is_file()
    assert "stateful_fixtures" not in sys.modules
