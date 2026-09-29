#!/usr/bin/env python3
"""List native package test roots without consulting Pants proof manifests."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED_PACKAGES = frozenset({"beadhive-bd-cli", "beadhive-beads-client", "beadhive-pants"})
SAFE_SHELL_PATH = re.compile(r"[A-Za-z0-9_./-]+")


class UnsafeNativePackagePath(ValueError):
    """A discovered path would be split or interpreted by command substitution."""


def package_test_roots(root: Path = ROOT) -> tuple[Path, ...]:
    packages = root / "packages"
    paths = tuple(
        path
        for path in sorted(packages.glob("*/tests"))
        if path.parent.name not in EXCLUDED_PACKAGES and path.is_dir()
    )
    for path in paths:
        relative = path.relative_to(root).as_posix()
        if SAFE_SHELL_PATH.fullmatch(relative) is None:
            raise UnsafeNativePackagePath(
                f"native package test path is unsafe for shell expansion: {relative!r}"
            )
    return paths


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--relative", action="store_true")
    args = parser.parse_args()
    paths = package_test_roots()
    if args.relative:
        paths = tuple(path.relative_to(ROOT) for path in paths)
    print(" ".join(path.as_posix() for path in paths))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
