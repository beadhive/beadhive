#!/usr/bin/env python3
"""Verify that named attest recipes are an exact partition of ``just check-all``."""

from __future__ import annotations

import json
import subprocess
import sys

KEY_RECIPES = {
    "docs": "attest-docs",
    "unit": "attest-unit",
    "stateful": "attest-stateful",
    "integration": "attest-integration",
    "architecture-contracts": "attest-architecture-contracts",
    "package": "attest-package",
    "always-run": "attest-always-run",
}


def _dependency_names(recipe: dict[str, object]) -> list[str]:
    dependencies = recipe.get("dependencies", [])
    if not isinstance(dependencies, list):
        raise ValueError("recipe dependencies are not a list")
    return [str(dependency["recipe"]) for dependency in dependencies]


def main() -> int:
    result = subprocess.run(
        ["just", "--dump", "--dump-format", "json"],
        check=True,
        capture_output=True,
        text=True,
    )
    recipes = json.loads(result.stdout)["recipes"]
    declared = _dependency_names(recipes["check-all"])
    expected = list(KEY_RECIPES.values())
    errors: list[str] = []
    if declared != expected:
        errors.append(f"check-all keys differ: expected {expected!r}, got {declared!r}")

    owners: dict[str, str] = {}
    for key, recipe_name in KEY_RECIPES.items():
        leaves = _dependency_names(recipes[recipe_name])
        if not leaves:
            errors.append(f"{key}: {recipe_name} has no gate steps")
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
