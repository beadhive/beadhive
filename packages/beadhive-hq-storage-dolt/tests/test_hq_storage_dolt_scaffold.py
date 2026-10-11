"""Scaffold proof: the package imports and honours its import-boundary rules."""

from __future__ import annotations

import ast
from pathlib import Path

import beadhive_hq_storage_dolt

SOURCE = Path(beadhive_hq_storage_dolt.__file__).parent


def _imported_roots() -> set[str]:
    roots: set[str] = set()
    for path in SOURCE.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                roots.add(node.module.split(".", 1)[0])
    return roots


def test_public_surface_is_declared() -> None:
    assert isinstance(beadhive_hq_storage_dolt.__all__, list)


def test_never_imports_beadhive_root_or_the_sql_driver() -> None:
    roots = _imported_roots()
    assert "beadhive" not in roots
    assert "pymysql" not in {root.lower() for root in roots}
