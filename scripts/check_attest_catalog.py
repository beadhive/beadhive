#!/usr/bin/env python3
"""Verify both explicit gate graphs and that the attest keys partition the native gate."""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path

KEY_RECIPES = {
    "docs": ("attest-docs", ("lint-md",)),
    "unit": ("attest-unit", ("lint", "license-check")),
    "stateful": ("attest-stateful", ("stateful-native",)),
    "root-composition": (
        "attest-root-composition",
        ("root-composition-native", "root-workspace-check"),
    ),
    "integration": ("attest-integration", ("require-bd", "test-integration-land")),
    "architecture-contracts": (
        "attest-architecture-contracts",
        ("architecture-structural-check",),
    ),
    "demos": ("attest-demos", ("demo-local-loop", "demo-live-ingress")),
    "packages": ("attest-packages", ("packages-check", "beads-client-check")),
    "bd-cli": ("attest-bd-cli", ("bd-cli-check",)),
    "hq-storage": ("attest-hq-storage", ("hq-storage-check",)),
}
ROOT = Path(__file__).resolve().parents[1]
NATIVE_FAST = (
    "lint",
    "lint-md",
    "license-check",
    "architecture-structural-check",
    "stateful-native",
    "root-composition-native",
    "beads-client-check",
)
PANTS_FULL = (
    "require-bd",
    "lint",
    "lint-md",
    "license-check",
    "architecture-structural-check",
    "architecture-pants-check",
    "pants-attest",
    "pants-artifact-check",
    "stateful-pants",
    "stateful-native",
    "root-composition-native",
    "root-workspace-check",
    "test-integration-land",
    "demo-local-loop",
    "demo-live-ingress",
    "packages-check",
    "beads-client-check",
    "bd-cli-check",
    "hq-storage-check",
)
PANTS_FAST = ("lint", "lint-md", "license-check", "architecture-structural-check", "test-changed")
NATIVE_FULL = (
    "require-bd",
    "lint",
    "lint-md",
    "license-check",
    "architecture-structural-check",
    "stateful-native",
    "root-composition-native",
    "root-workspace-check",
    "beads-client-check",
    "test-integration-land",
    "demo-local-loop",
    "demo-live-ingress",
    "packages-check",
    "bd-cli-check",
    "hq-storage-check",
)


def _dependencies(justfile: str, recipe: str) -> list[str]:
    declaration = next(line for line in justfile.splitlines() if line.startswith(f"{recipe}:"))
    return [
        parenthesized or plain
        for parenthesized, plain in re.findall(r"\(([-\w]+)[^)]*\)|([-\w]+)", declaration)
        if (parenthesized or plain) != recipe
    ]


def _recipe_body(justfile: str, recipe: str) -> list[str]:
    match = re.search(rf"(?m)^{re.escape(recipe)}:[^\n]*\n((?:    .*\n)+)", justfile)
    return match.group(1).splitlines() if match else []


def catalog_key_errors(configured: Iterable[str], source: str = "configured catalog") -> list[str]:
    """Fail closed when a key set differs from KEY_RECIPES (a missing key never runs)."""
    names = set(configured)
    expected = set(KEY_RECIPES)
    errors: list[str] = []
    if expected - names:
        errors.append(f"{source} is missing attest keys {sorted(expected - names)!r}")
    if names - expected:
        errors.append(f"{source} has keys with no KEY_RECIPES entry {sorted(names - expected)!r}")
    return errors


def docs_keys(docs: str) -> list[str]:
    """Key names from the first column of the docs/ATTEST-KEYS.md catalog table."""
    return re.findall(r"(?m)^\| `([-\w]+)` \| `just attest-", docs)


def live_catalog_errors(live: object, justfile: str, docs: str | None = None) -> list[str]:
    """Compare the live HQ catalog (JSON ``[{name, cmd, enabled}]``) with this tree.

    The key set must equal the justfile ``attest-*`` recipes, ``KEY_RECIPES`` and the docs
    table, and each key's ``cmd`` must be exactly ``just attest-<name>``. Reuses
    ``catalog_key_errors``; there is no second catalog copy.
    """
    if not isinstance(live, list) or not all(
        isinstance(item, dict) and isinstance(item.get("name"), str) for item in live
    ):
        return ["live catalog must be a JSON list of {name, cmd, enabled} objects"]
    names = [item["name"] for item in live]
    errors: list[str] = []
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        errors.append(f"live HQ catalog has duplicate keys {duplicates!r}")
    errors += catalog_key_errors(names, "live HQ catalog")
    recipes = set(re.findall(r"(?m)^(attest-[-\w]+):", justfile))
    recipe_keys = {recipe.removeprefix("attest-") for recipe in recipes}
    only_live = sorted(set(names) - recipe_keys)
    only_recipes = sorted(recipe_keys - set(names))
    if only_live:
        errors.append(f"live HQ catalog has keys with no justfile recipe {only_live!r}")
    if only_recipes:
        errors.append(
            f"justfile has attest recipes absent from the live HQ catalog {only_recipes!r}"
        )
    errors += catalog_key_errors(recipe_keys, "justfile attest-* recipes")
    if docs is not None:
        errors += catalog_key_errors(docs_keys(docs), "docs/ATTEST-KEYS.md table")
    for item in live:
        expected = f"just attest-{item['name']}"
        if item["name"] in KEY_RECIPES and item.get("cmd") != expected:
            errors.append(
                f"live HQ catalog key {item['name']!r} cmd {item.get('cmd')!r} != {expected!r}"
            )
    return errors


def check(justfile: str, push_hook: str | None = None, docs: str | None = None) -> list[str]:
    expected = [leaf for _, leaves in KEY_RECIPES.values() for leaf in leaves]
    errors: list[str] = []
    recipes = set(re.findall(r"(?m)^(attest-[-\w]+):", justfile))
    errors += catalog_key_errors(
        (name.removeprefix("attest-") for name in recipes), "justfile attest-* recipes"
    )
    if docs is not None:
        errors += catalog_key_errors(docs_keys(docs), "docs/ATTEST-KEYS.md table")
    if sorted(expected) != sorted(NATIVE_FULL):
        errors.append(
            f"attest keys must partition check-all-native: expected {sorted(NATIVE_FULL)!r}, "
            f"got {sorted(expected)!r}"
        )
    for recipe, required in (
        ("check-native", NATIVE_FAST),
        ("check-pants", PANTS_FAST),
        ("check-all-native", NATIVE_FULL),
        ("check-all-pants", PANTS_FULL),
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

    artifact = "\n".join(_recipe_body(justfile, "pants-artifact-check"))
    if "./scripts/hermetic.sh uv run python scripts/pytest_with_report.py" not in artifact or (
        "tests/test_beadhive_pants_artifacts.py::test_bh_pex_contains_and_resolves_the_backend"
        not in artifact
    ):
        errors.append("pants-artifact-check must run the recursive PEX proof inside the fence")
    native = "\n".join(_recipe_body(justfile, "stateful-native"))
    if "not integration and not pants_profile" not in native:
        errors.append("stateful-native must exclude the Pants-only artifact proof")

    return errors


def live_catalog_main(source: str) -> int:
    """``--live-catalog <path|->``: exit nonzero when the live HQ catalog drifts from this tree."""
    try:
        raw = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
        live = json.loads(raw)
    except (OSError, ValueError) as exc:
        print(f"attest-catalog: cannot read live catalog: {exc}", file=sys.stderr)
        return 1
    justfile = (ROOT / "justfile").read_text(encoding="utf-8")
    docs = (ROOT / "docs" / "ATTEST-KEYS.md").read_text(encoding="utf-8")
    errors = live_catalog_errors(live, justfile, docs)
    if errors:
        print("attest-catalog: live catalog drift", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    print(f"attest-catalog: live catalog OK ({len(live)} keys)")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "--live-catalog":
        if len(args) != 2:
            print("usage: check_attest_catalog.py [--live-catalog <path|->]", file=sys.stderr)
            return 2
        return live_catalog_main(args[1])
    justfile = (ROOT / "justfile").read_text(encoding="utf-8")
    push_hook = (ROOT / "scripts" / "main-push-gate.sh").read_text(encoding="utf-8")
    docs = (ROOT / "docs" / "ATTEST-KEYS.md").read_text(encoding="utf-8")
    errors = check(justfile, push_hook, docs)
    if errors:
        print("attest-catalog: invalid", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    print(f"attest-catalog: OK ({len(KEY_RECIPES)} native keys, 4 explicit gates)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
