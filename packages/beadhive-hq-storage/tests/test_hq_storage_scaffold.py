"""Scaffold proof: the package imports and honours its import-boundary rules."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import beadhive_hq_storage
import beadhive_hq_storage.contract as contract

SOURCE = Path(beadhive_hq_storage.__file__).parent


def _imported_roots() -> set[str]:
    roots: set[str] = set()
    for path in SOURCE.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                roots.add(node.module.split(".", 1)[0])
    return roots


def test_public_surfaces_are_declared() -> None:
    assert isinstance(beadhive_hq_storage.__all__, list)
    assert isinstance(contract.__all__, list)


def test_imports_only_the_standard_library() -> None:
    stdlib = set(sys.stdlib_module_names) | {"__future__"}
    assert _imported_roots() <= stdlib


def test_never_imports_beadhive_root() -> None:
    assert "beadhive" not in _imported_roots()
