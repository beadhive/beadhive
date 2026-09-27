"""``beadhive-worktrees`` depends only on ``beadhive-plugins``: it never imports root ``beadhive``
or any other dependency."""

from __future__ import annotations

import ast
import sys
from importlib import resources
from pathlib import Path

import beadhive_worktrees

SOURCE = Path(__file__).resolve().parents[1] / "src" / "beadhive_worktrees"

# Root application, other in-repo packages, ambient/process/network primitives, and test tooling.
# `beadhive_plugins` is this package's one allowed dependency and is deliberately absent here.
FORBIDDEN = {
    "beadhive",
    "beadhive_core",
    "beadhive_pants",
    "beadhive_beads_client",
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
            raise AssertionError(f"{path.name}: dynamic imports are not part of this package")
    return roots


def test_package_imports_only_the_standard_library_and_beadhive_plugins() -> None:
    sources = sorted(SOURCE.rglob("*.py"))
    assert sources
    for path in sources:
        leaked = _imported_roots(path) & FORBIDDEN
        assert not leaked, f"{path.name} imports {sorted(leaked)}"


def test_public_surface_exposes_naming_policy_contracts_and_the_native_adapter() -> None:
    for name in (
        "WT_PREFIX",
        "WORKTREE_MANAGER",
        "WORKTREE_MANAGER_KEY",
        "WORKSPACE_BINDING",
        "WorktreeManagerPort",
        "WorkspaceBindingPort",
        "WorktreeSpec",
        "WorktreeHandle",
        "WorktreeManagerCapabilities",
        "WorktreeInventory",
        "NativeGitWorktreeManager",
        "native_worktree_manager_provider_binding",
        "WorktreeLifecycleService",
        "bind_worktree",
    ):
        assert hasattr(beadhive_worktrees, name)


def test_typed_marker_ships_and_no_stateful_fixture_plugin_is_loaded() -> None:
    assert (resources.files(beadhive_worktrees) / "py.typed").is_file()
    assert "stateful_fixtures" not in sys.modules


def test_slot_declarations_come_from_beadhive_plugins_not_this_package() -> None:
    import beadhive_plugins

    assert beadhive_worktrees.WORKTREE_MANAGER is beadhive_plugins.WORKTREE_MANAGER
    assert beadhive_worktrees.WORKSPACE_BINDING is beadhive_plugins.WORKSPACE_BINDING
    assert beadhive_worktrees.WORKTREE_MANAGER_KEY.port_type is beadhive_plugins.WorktreeManager
    assert not hasattr(beadhive_worktrees, "PluginWorktreeProvisioner")
