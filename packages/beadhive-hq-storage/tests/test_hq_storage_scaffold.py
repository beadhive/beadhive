"""The package imports and honours its import-boundary rules (hq-storage-port-adr section 7)."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import beadhive_hq_storage
import beadhive_hq_storage.contract as contract

SOURCE = Path(beadhive_hq_storage.__file__).parent
STDLIB = set(sys.stdlib_module_names) | {"__future__"}
LAZY_TEST_TOOLS = {"pytest", "hypothesis"}


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


def test_public_surfaces_are_declared() -> None:
    assert isinstance(beadhive_hq_storage.__all__, list) and beadhive_hq_storage.__all__
    assert isinstance(contract.__all__, list) and contract.__all__
    for name in beadhive_hq_storage.__all__:
        assert hasattr(beadhive_hq_storage, name), name
    for name in contract.__all__:
        assert hasattr(contract, name), name


def test_module_scope_imports_only_the_standard_library() -> None:
    for path in SOURCE.rglob("*.py"):
        module_scope, _ = _imports(path)
        assert module_scope <= STDLIB, path.name


def test_test_tools_are_imported_lazily_and_only_by_the_contract_module() -> None:
    for path in SOURCE.rglob("*.py"):
        _, lazy = _imports(path)
        allowed = LAZY_TEST_TOOLS if path.name == "contract.py" else set()
        assert lazy <= STDLIB | allowed, path.name


def test_never_imports_beadhive_root() -> None:
    for path in SOURCE.rglob("*.py"):
        module_scope, lazy = _imports(path)
        assert "beadhive" not in module_scope | lazy, path.name
