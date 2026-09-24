#!/usr/bin/env python3
"""Fail closed when a tracked file has no owning Pants target.

An unowned file is what makes `pants --changed-dependents=transitive` report "No targets were
matched" for a change — which is indistinguishable from "safe" (bh-1j3ei.2, bh-1j3ei epic). This
compares `git ls-files` against every file any Pants target depends on (`pants filedeps ::`,
which also counts each target's own BUILD file) and fails on anything left over. No allowlist:
a new tracked file with no BUILD entry is a red build, not a warning.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from beadhive_pants.verify import pants_owned_files as _pants_owned_files
from beadhive_pants.verify import tracked_files as _tracked_files

try:
    from scripts import check_attest_catalog
    from scripts.pants_launcher import launcher
except ModuleNotFoundError:
    # Runtime fallback for `python scripts/check_pants_ownership.py` (scripts/ on sys.path,
    # not repo root). The `scripts.` import above already gives Pants a real dependency edge.
    import check_attest_catalog  # pants: no-infer-dep
    from pants_launcher import launcher  # pants: no-infer-dep

ROOT = Path(__file__).parents[1]


class OwnershipCheckError(RuntimeError):
    """Pants or git could not be queried, so ownership cannot be established."""


def tracked_files(root: Path = ROOT) -> set[str]:
    try:
        return _tracked_files(root)
    except RuntimeError as exc:
        raise OwnershipCheckError(str(exc)) from exc


def pants_owned_files(root: Path = ROOT, *, pants: str | None = None) -> set[str]:
    pants_bin = pants or launcher()

    def query(path: Path, args):
        result = subprocess.run(
            [
                sys.executable,
                "scripts/pants_cache.py",
                "run",
                "--",
                pants_bin,
                "--no-pantsd",
                *args,
            ],
            cwd=path,
            check=False,
            text=True,
            capture_output=True,
        )
        return result.returncode, result.stdout, result.stderr

    try:
        return _pants_owned_files(root, query=query)
    except RuntimeError as exc:
        raise OwnershipCheckError(str(exc)) from exc


def unowned(tracked: set[str], owned: set[str]) -> set[str]:
    """Tracked paths no Pants target claims as a source.

    `owned` may contain paths that were never tracked (synthetic Pants addresses such as
    generated lockfile validation targets) — those are irrelevant here since we only report
    the intersection the other direction: tracked-but-not-owned.
    """
    return tracked - owned


def report(unowned_paths: set[str]) -> str:
    lines = [f"pants-ownership: FAILED ({len(unowned_paths)} unowned tracked file(s))"]
    for path in sorted(unowned_paths):
        lines.append(f"- {path}")
    lines.append(
        "Every tracked file needs a Pants target (files()/resources()/python_sources()) that "
        "declares it as a source, or `pants --changed-dependents=transitive` cannot be trusted "
        "for it. No allowlist — add ownership, don't skip the check."
    )
    return "\n".join(lines)


def main() -> int:
    if check_attest_catalog.main():
        return 1
    try:
        tracked = tracked_files()
        owned = pants_owned_files()
    except OwnershipCheckError as exc:
        print(f"pants-ownership: FAILED (could not evaluate)\n{exc}", file=sys.stderr)
        return 1
    missing = unowned(tracked, owned)
    if missing:
        print(report(missing))
        return 1
    print(f"pants-ownership: OK ({len(tracked)} tracked files, all Pants-owned)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
