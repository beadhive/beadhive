#!/usr/bin/env python3
"""Fail when the native path impact map cannot account for tracked repository inputs."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from beadhive import config, config_work_settings, registry
from beadhive.adapters.impact_paths import (
    PATHS_BACKEND,
    ROOT_WORKSPACE_PACKAGES,
    expanded_selector_patterns,
    matches_path,
    root_workspace_package_patterns,
)
from beadhive.bootstrap.impact import attest_keys
from beadhive.modules.config.contracts import AttestConfig

ROOT = Path(__file__).resolve().parents[1]
ROOT_CODE_KEYS = ("stateful", "integration", "demos")


def tracked_files(repo: Path) -> tuple[str, ...]:
    result = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "-z"], capture_output=True, check=True
    )
    return tuple(path.decode() for path in result.stdout.split(b"\0") if path)


def check(repo: Path, attest: AttestConfig, paths: tuple[str, ...] | None = None) -> list[str]:
    errors: list[str] = []
    keys = attest_keys(attest)
    rules = {
        key.name: expanded_selector_patterns(repo, key.selector(PATHS_BACKEND)) for key in keys
    }
    if attest.impact.backend != PATHS_BACKEND:
        errors.append(f"impact backend must be {PATHS_BACKEND!r}, got {attest.impact.backend!r}")
    for name, patterns in rules.items():
        if not patterns:
            errors.append(f"{name}: no {PATHS_BACKEND} selector patterns")

    paths = tracked_files(repo) if paths is None else paths
    uncovered = [
        path
        for path in paths
        if not any(
            matches_path(path, pattern) for patterns in rules.values() for pattern in patterns
        )
    ]
    if uncovered:
        errors.append("tracked paths have no native impact owner: " + ", ".join(uncovered))

    dependent_patterns = root_workspace_package_patterns(repo)
    if dependent_patterns:
        by_name = {key.name: key for key in keys}
        for name in ROOT_CODE_KEYS:
            key = by_name.get(name)
            if key is None:
                errors.append(f"missing root-code attest key {name!r}")
                continue
            configured = key.selector(PATHS_BACKEND) or ""
            if ROOT_WORKSPACE_PACKAGES not in configured.splitlines():
                errors.append(
                    f"{name}: root depends on workspace packages {dependent_patterns!r}, but its "
                    f"selector lacks {ROOT_WORKSPACE_PACKAGES}"
                )
    return errors


def main() -> int:
    cfg = config.load()
    entry = registry.current_hive(cfg)
    if not entry:
        print("native-impact-map: no managed hive for cwd", file=sys.stderr)
        return 1
    attest = config_work_settings.attest_config(cfg, entry)
    errors = check(ROOT, attest)
    if errors:
        print("native-impact-map: invalid", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    print(f"native-impact-map: OK ({len(tracked_files(ROOT))} tracked paths)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
