"""``beadhive-plugins`` stays stdlib-only: it never imports ``beadhive`` or any dependency."""

from __future__ import annotations

import ast
import sys
from importlib import resources
from pathlib import Path

import beadhive_plugins

SOURCE = Path(__file__).resolve().parents[1] / "src" / "beadhive_plugins"

# Root application, other in-repo packages, ambient/process/network primitives, and test tooling.
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


def test_package_imports_only_the_standard_library() -> None:
    sources = sorted(SOURCE.rglob("*.py"))
    assert sources
    for path in sources:
        leaked = _imported_roots(path) & FORBIDDEN
        assert not leaked, f"{path.name} imports {sorted(leaked)}"


def test_public_surface_exposes_the_slot_binding_and_lifecycle_contracts() -> None:
    for name in (
        "CapabilityKey",
        "CapabilityRef",
        "PluginManifest",
        "ProviderBinding",
        "bind_application_port",
        "ALL_LIFECYCLE_EVENTS",
        "LifecycleEvent",
        "SubscriberBinding",
    ):
        assert hasattr(beadhive_plugins, name)


def test_typed_marker_ships_and_no_stateful_fixture_plugin_is_loaded() -> None:
    assert (resources.files(beadhive_plugins) / "py.typed").is_file()
    assert "stateful_fixtures" not in sys.modules
