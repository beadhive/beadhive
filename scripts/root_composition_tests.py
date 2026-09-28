#!/usr/bin/env python3
"""Render the registered root tests for workspace package composition.

The native path backend selects one ``root-composition`` key for any workspace package that the
root distribution consumes.  Attest commands do not receive the changed path set, so the key runs
the small union below plus every public contract test.  Keeping the registry package-shaped makes
missing root coverage reviewable without coupling the native gate to Pants' proven-test manifest.
"""

from __future__ import annotations

import argparse
import json
import shlex
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PACKAGE_TESTS: dict[str, tuple[str, ...]] = {
    "beadhive-bd-cli": ("tests/test_beads_routing.py",),
    "beadhive-beads-client": (
        "tests/test_beads_routing.py",
        "tests/test_dispatch_state.py",
        "tests/test_plan.py",
        "tests/test_work_lifecycle_shell.py",
        "tests/test_work_queue.py",
        "tests/test_work_reads.py",
        "tests/test_work_review_shell.py",
    ),
    "beadhive-core": (
        "tests/test_beads_routing.py",
        "tests/test_claim_fence.py",
        "tests/test_dispatch_state.py",
        "tests/test_work_assignment_boundaries.py",
        "tests/test_work_lifecycle_shell.py",
        "tests/test_work_queue.py",
        "tests/test_work_reads.py",
        "tests/test_work_review_shell.py",
    ),
    "beadhive-pants": ("tests/unit/modules/work/test_impact_pants.py",),
    "beadhive-plugins": (
        "tests/test_build_verify_surfaces.py",
        "tests/test_plugins.py",
    ),
    "beadhive-worktrees": (
        "tests/test_retire.py",
        "tests/test_worktree.py",
        "tests/test_worktree_inventory_boundaries.py",
        "tests/unit/integrations/test_herdr_binding_conformance.py",
    ),
}


def contract_tests(root: Path = ROOT) -> tuple[str, ...]:
    return tuple(
        path.relative_to(root).as_posix()
        for path in sorted((root / "tests" / "contracts").glob("test_*.py"))
    )


def selected_tests(package: str | None = None, root: Path = ROOT) -> tuple[str, ...]:
    package_sets = PACKAGE_TESTS.values() if package is None else (PACKAGE_TESTS[package],)
    return tuple(
        sorted({*contract_tests(root), *(path for paths in package_sets for path in paths)})
    )


def validate(root: Path = ROOT) -> tuple[str, ...]:
    missing = tuple(path for path in selected_tests(root=root) if not (root / path).is_file())
    if missing:
        raise SystemExit("missing registered root-composition tests: " + ", ".join(missing))
    return selected_tests(root=root)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", choices=sorted(PACKAGE_TESTS))
    parser.add_argument("--ignore-args", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    validate()
    paths = selected_tests(args.package)
    if args.json:
        print(json.dumps({"package": args.package, "count": len(paths), "paths": paths}, indent=2))
    elif args.ignore_args:
        print(" ".join(f"--ignore={shlex.quote(path)}" for path in paths))
    else:
        print(" ".join(shlex.quote(path) for path in paths))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
