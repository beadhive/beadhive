#!/usr/bin/env python3
"""Validate the fail-closed inventory of tests that read docs at runtime."""

from __future__ import annotations

import ast
import json
from pathlib import Path

ROOT = Path(__file__).parents[1]
MANIFEST = ROOT / "scripts" / "pants_proven_tests.json"


def doc_readers() -> set[str]:
    """Find test modules containing literal docs path components.

    This deliberately uses syntax rather than grep: comments and prose about documentation are
    not runtime inputs. Dynamic paths still remain unproven until explicitly reconciled here.
    """
    readers = set()
    for path in (ROOT / "tests").rglob("test_*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        strings = (
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        )
        if any(
            value == "docs" or value.startswith("docs/") or "/docs/" in value for value in strings
        ):
            readers.add(path.relative_to(ROOT).as_posix())
    return readers


def main() -> int:
    data = json.loads(MANIFEST.read_text())
    entries = data["tests"]
    listed = set(entries)
    actual = doc_readers()
    errors = []
    if listed != actual:
        errors.append(f"missing={sorted(actual - listed)!r} extra={sorted(listed - actual)!r}")
    for path, record in entries.items():
        status = record.get("status")
        if status not in {"proven", "unproven"}:
            errors.append(f"{path}: invalid status {status!r}")
        if status == "proven" and not record.get("dependencies"):
            errors.append(f"{path}: proven entry has no declared dependencies")
        if status == "unproven" and not record.get("reason"):
            errors.append(f"{path}: unproven entry has no reason")
    if errors:
        print("pants-proven: FAILED\n" + "\n".join(errors))
        return 1
    proven = sum(row["status"] == "proven" for row in entries.values())
    print(
        f"pants-proven: OK "
        f"({len(entries)} readers: {proven} proven, {len(entries) - proven} unproven)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
