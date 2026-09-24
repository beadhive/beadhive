#!/usr/bin/env python3
"""Verify both explicit gate graphs and the Pants attest-key partition."""

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
        ("architecture-structural-check",),
    ),
    "package": ("attest-package", ("pants-attest", "architecture-pants-check")),
    "demos": ("attest-demos", ("demo-local-loop", "demo-live-ingress")),
    "packages": ("attest-packages", ("packages-check",)),
}
ROOT = Path(__file__).resolve().parents[1]
NATIVE_FAST = (
    "lint",
    "lint-md",
    "license-check",
    "architecture-structural-check",
    "stateful-native",
)
PANTS_FAST = ("lint", "lint-md", "license-check", "architecture-structural-check", "test-changed")
NATIVE_FULL = (
    "require-bd",
    "lint",
    "lint-md",
    "license-check",
    "architecture-structural-check",
    "stateful-native",
    "test-integration-land",
    "demo-local-loop",
    "demo-live-ingress",
    "packages-check",
)


def _dependencies(justfile: str, recipe: str) -> list[str]:
    declaration = next(line for line in justfile.splitlines() if line.startswith(f"{recipe}:"))
    return [
        parenthesized or plain
        for parenthesized, plain in re.findall(r"\(([-\w]+)[^)]*\)|([-\w]+)", declaration)
        if (parenthesized or plain) != recipe
    ]


def _recipe_body(justfile: str, recipe: str) -> list[str]:
    match = re.search(rf"(?m)^{re.escape(recipe)}:\n((?:    .*\n)+)", justfile)
    return match.group(1).splitlines() if match else []


def check(justfile: str, push_hook: str | None = None) -> list[str]:
    expected = [leaf for _, leaves in KEY_RECIPES.values() for leaf in leaves]
    errors: list[str] = []
    for recipe, required in (
        ("check-native", NATIVE_FAST),
        ("check-pants", PANTS_FAST),
        ("check-all-native", NATIVE_FULL),
        ("check-all-pants", expected),
    ):
        declared = _dependencies(justfile, recipe)
        if sorted(declared) != sorted(required) or len(declared) != len(set(declared)):
            errors.append(f"{recipe} steps differ: expected {required!r}, got {declared!r}")

    selected_profiles: dict[str, str] = {}
    for alias in ("check", "check-all"):
        selected = _dependencies(justfile, alias)
        allowed = (f"{alias}-native", f"{alias}-pants")
        if len(selected) != 1 or selected[0] not in allowed:
            errors.append(f"{alias} must alias exactly one of {allowed!r}, got {selected!r}")
        else:
            selected_profiles[alias] = selected[0]
    if len(selected_profiles) == 2 and (
        selected_profiles["check"].split("-")[-1] != selected_profiles["check-all"].split("-")[-1]
    ):
        errors.append("check and check-all must select the same profile")
    if push_hook is not None and "check-all" in selected_profiles:
        expected_gate = f'gate_cmd="just {selected_profiles["check-all"]}"'
        if expected_gate not in push_hook:
            errors.append(f"push hook must name {expected_gate}")

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

    return errors


def main() -> int:
    justfile = (ROOT / "justfile").read_text(encoding="utf-8")
    push_hook = (ROOT / "scripts" / "main-push-gate.sh").read_text(encoding="utf-8")
    errors = check(justfile, push_hook)
    if errors:
        print("attest-catalog: invalid", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    print(f"attest-catalog: OK ({len(KEY_RECIPES)} Pants keys, 4 explicit gates)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
