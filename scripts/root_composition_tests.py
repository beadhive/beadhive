#!/usr/bin/env python3
"""Render the registered root tests for workspace package composition.

The native path backend selects one ``root-composition`` key for any workspace package that the
root distribution consumes.  Attest commands do not receive the changed path set, so the key runs
the small union below plus every public contract test.  Keeping the registry package-shaped makes
missing root coverage reviewable without coupling the native gate to Pants' proven-test manifest.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import tomllib
from pathlib import Path

from beadhive.adapters.impact_paths import PACKAGE_TESTS

ROOT = Path(__file__).resolve().parents[1]
SAFE_SHELL_PATH = re.compile(r"[A-Za-z0-9_./-]+")


class UnsafeRootCompositionPath(ValueError):
    """A selected test path would be split or interpreted by command substitution."""


def _validate_safe_paths(paths: tuple[str, ...]) -> tuple[str, ...]:
    for path in paths:
        if SAFE_SHELL_PATH.fullmatch(path) is None:
            raise UnsafeRootCompositionPath(
                f"root-composition test path is unsafe for shell expansion: {path!r}"
            )
    return paths


def contract_tests(root: Path = ROOT) -> tuple[str, ...]:
    return tuple(
        path.relative_to(root).as_posix()
        for path in sorted((root / "tests" / "contracts").glob("test_*.py"))
    )


def _package_import_roots(package: str, root: Path) -> tuple[str, ...]:
    manifest = root / "packages" / package / "pyproject.toml"
    if not manifest.is_file():
        raise SystemExit(f"missing workspace package manifest for root composition: {manifest}")
    payload = tomllib.loads(manifest.read_text(encoding="utf-8"))
    try:
        wheel = payload["tool"]["hatch"]["build"]["targets"]["wheel"]
    except (KeyError, TypeError) as exc:
        raise SystemExit(f"workspace package has no wheel target: {package}") from exc
    package_paths = wheel.get("packages") if isinstance(wheel, dict) else None
    if (
        not isinstance(package_paths, list)
        or not package_paths
        or any(not isinstance(path, str) or not path for path in package_paths)
    ):
        raise SystemExit(
            f"workspace package wheel.packages must be a non-empty list of paths: {package}"
        )
    roots = tuple(sorted({Path(path).name for path in package_paths if Path(path).name}))
    if not roots:
        raise SystemExit(f"workspace package has no import roots for root composition: {package}")
    return roots


def direct_package_tests(root: Path = ROOT) -> dict[str, tuple[str, ...]]:
    """Find root tests that directly import each registered workspace distribution."""
    imports = {package: set(_package_import_roots(package, root)) for package in PACKAGE_TESTS}
    dependents: dict[str, list[str]] = {package: [] for package in PACKAGE_TESTS}
    for path in sorted((root / "tests").rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError) as exc:
            detail = f"cannot inspect root-composition test imports in {path}: {exc}"
            raise SystemExit(detail) from exc
        modules = {
            alias.name.split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        modules.update(
            node.module.split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module
        )
        relative = path.relative_to(root).as_posix()
        for package, package_roots in imports.items():
            if modules & package_roots:
                dependents[package].append(relative)
    return {package: tuple(paths) for package, paths in dependents.items()}


def selected_tests(package: str | None = None, root: Path = ROOT) -> tuple[str, ...]:
    package_sets = PACKAGE_TESTS.values() if package is None else (PACKAGE_TESTS[package],)
    return _validate_safe_paths(
        tuple(sorted({*contract_tests(root), *(path for paths in package_sets for path in paths)}))
    )


def validate(root: Path = ROOT) -> tuple[str, ...]:
    selected = selected_tests(root=root)
    missing = tuple(path for path in selected if not (root / path).is_file())
    if missing:
        raise SystemExit("missing registered root-composition tests: " + ", ".join(missing))
    contracts = set(contract_tests(root))
    omitted = {
        package: tuple(sorted(set(paths) - set(PACKAGE_TESTS[package]) - contracts))
        for package, paths in direct_package_tests(root).items()
        if set(paths) - set(PACKAGE_TESTS[package]) - contracts
    }
    if omitted:
        detail = "; ".join(
            f"{package}: {', '.join(paths)}" for package, paths in sorted(omitted.items())
        )
        raise SystemExit("direct root dependents lack PACKAGE_TESTS coverage: " + detail)
    return selected


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", choices=sorted(PACKAGE_TESTS))
    parser.add_argument("--ignore-args", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    validate()
    if args.validate_only:
        return 0
    paths = selected_tests(args.package)
    if args.json:
        print(json.dumps({"package": args.package, "count": len(paths), "paths": paths}, indent=2))
    elif args.ignore_args:
        print(" ".join(f"--ignore={path}" for path in paths))
    else:
        print(" ".join(paths))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
