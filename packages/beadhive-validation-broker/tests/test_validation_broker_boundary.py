"""The package is stdlib only and never imports root ``beadhive`` (validation-broker-adr)."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import beadhive_validation_broker as broker
import beadhive_validation_broker.contract as contract

SOURCE = Path(broker.__file__).parent
STDLIB = set(sys.stdlib_module_names) | {"__future__"}
LAZY_TEST_TOOLS = {"pytest"}


def _roots(node: ast.AST) -> set[str]:
    if isinstance(node, ast.Import):
        return {alias.name.split(".", 1)[0] for alias in node.names}
    if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
        return {node.module.split(".", 1)[0]}
    return set()


def _imports(path: Path) -> tuple[set[str], set[str]]:
    """(module-scope roots, function-scope roots) of one source file."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    module_scope: set[str] = set()
    for node in tree.body:
        module_scope |= _roots(node)
    everywhere: set[str] = set()
    for node in ast.walk(tree):
        everywhere |= _roots(node)
    return module_scope, everywhere - module_scope


def test_public_surfaces_are_declared_and_importable() -> None:
    for module in (broker, contract):
        assert isinstance(module.__all__, list) and module.__all__
        for name in module.__all__:
            assert hasattr(module, name), f"{module.__name__}.__all__ names missing {name}"


def test_library_is_stdlib_only_and_never_imports_root_beadhive() -> None:
    sources = sorted(SOURCE.glob("*.py"))
    assert sources
    for path in sources:
        module_scope, lazy = _imports(path)
        third_party = (module_scope | lazy) - STDLIB - {"beadhive_validation_broker"}
        assert third_party <= LAZY_TEST_TOOLS, f"{path.name} imports {sorted(third_party)}"
        assert not module_scope & LAZY_TEST_TOOLS, (
            f"{path.name} imports a test tool at module scope"
        )
        assert "beadhive" not in module_scope | lazy, f"{path.name} imports root beadhive"


def test_no_third_party_runtime_dependency_is_declared() -> None:
    import tomllib

    pyproject = tomllib.loads((SOURCE.parents[1] / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["dependencies"] == []


def test_package_is_a_library_not_a_plugin() -> None:
    assert not (SOURCE.parents[1] / "plugin.json").exists()
    assert (SOURCE / "py.typed").is_file()
