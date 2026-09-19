#!/usr/bin/env python3
"""Verify that named attest recipes are an exact partition of ``just check-all``."""

from __future__ import annotations

import json
import subprocess
import sys

KEY_RECIPES = {
    "docs": ("attest-docs", ("lint-md",)),
    "unit": ("attest-unit", ("lint", "license-check")),
    "stateful": ("attest-stateful", ("test",)),
    "integration": ("attest-integration", ("test-integration-land",)),
    "architecture-contracts": (
        "attest-architecture-contracts",
        (
            "architecture-check",
            "transport-artifact-check",
            "wire-schema-compat",
            "proof-digest-check",
        ),
    ),
    "package": ("attest-package", ("pants-attest",)),
    "always-run": (
        "attest-always-run",
        ("require-bd", "demo-local-loop", "demo-live-ingress"),
    ),
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
    expected = [leaf for _, leaves in KEY_RECIPES.values() for leaf in leaves]
    errors: list[str] = []
    if sorted(declared) != sorted(expected) or len(declared) != len(set(declared)):
        errors.append(f"check-all keys differ: expected {expected!r}, got {declared!r}")

    owners: dict[str, str] = {}
    for key, (recipe_name, leaves) in KEY_RECIPES.items():
        body = [row[0] for row in recipes[recipe_name]["body"]]
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
