#!/usr/bin/env python3
"""Verify that named attest recipes are an exact partition of ``just check-all``."""

from __future__ import annotations

import re
import sys
from pathlib import Path

KEY_RECIPES = {
    "docs": ("attest-docs", ("lint-md",)),
    "unit": ("attest-unit", ("lint", "license-check")),
    "stateful": ("attest-stateful", ("stateful-pants", "stateful-native")),
    "integration": ("attest-integration", ("require-bd", "test-integration-land")),
    "architecture-contracts": (
        "attest-architecture-contracts",
        (
            "architecture-structural-check",
            "transport-artifact-check",
            "wire-schema-compat",
            "proof-digest-check",
        ),
    ),
    "package": ("attest-package", ("pants-attest",)),
    "demos": ("attest-demos", ("demo-local-loop", "demo-live-ingress")),
}
ROOT = Path(__file__).resolve().parents[1]


def _check_all_dependencies(justfile: str) -> list[str]:
    declaration = next(line for line in justfile.splitlines() if line.startswith("check-all:"))
    return [
        parenthesized or plain
        for parenthesized, plain in re.findall(r"\(([-\w]+)[^)]*\)|([-\w]+)", declaration)
        if (parenthesized or plain) != "check-all"
    ]


def _recipe_body(justfile: str, recipe: str) -> list[str]:
    match = re.search(rf"(?m)^{re.escape(recipe)}:\n((?:    .*\n)+)", justfile)
    return match.group(1).splitlines() if match else []


def main() -> int:
    justfile = (ROOT / "justfile").read_text(encoding="utf-8")
    declared = _check_all_dependencies(justfile)
    expected = [leaf for _, leaves in KEY_RECIPES.values() for leaf in leaves]
    errors: list[str] = []
    if sorted(declared) != sorted(expected) or len(declared) != len(set(declared)):
        errors.append(f"check-all keys differ: expected {expected!r}, got {declared!r}")

    owners: dict[str, str] = {}
    for key, (recipe_name, leaves) in KEY_RECIPES.items():
        body = _recipe_body(justfile, recipe_name)
        missing = [leaf for leaf in leaves if not any(f"just {leaf}" in line for line in body)]
        if missing:
            errors.append(f"{key}: {recipe_name} does not invoke {missing!r}")
        for leaf in leaves:
            previous = owners.setdefault(leaf, key)
            if previous != key:
                errors.append(f"{leaf}: owned by both {previous} and {key}")

    if errors:
        print("attest-catalog: invalid", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    print(f"attest-catalog: OK ({len(KEY_RECIPES)} keys, {len(owners)} gate steps)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
