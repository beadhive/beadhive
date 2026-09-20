#!/usr/bin/env python3
"""Execute the proven Pants/native pytest partition without coverage gaps.

Pants owns the graduated files and their transitive dependency closure.  Native pytest keeps
every test that has not passed the sandbox proof.  The checked manifest is the single boundary
between those runners; malformed or stale entries fail before either runner starts.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path

try:
    from scripts.pants_launcher import launcher
except ModuleNotFoundError:
    from pants_launcher import launcher  # pants: no-infer-dep

ROOT = Path(__file__).parents[1]
MANIFEST = ROOT / "scripts/pants_proven_tests.json"
BUILD = ROOT / "tests/BUILD"


class PartitionError(RuntimeError):
    """The checked partition cannot prove complete test coverage."""


def load_manifest(root: Path = ROOT) -> dict[str, dict[str, object]]:
    payload = json.loads((root / MANIFEST.relative_to(ROOT)).read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1 or not isinstance(payload.get("tests"), dict):
        raise PartitionError("pants proven-test manifest must use schema version 1")
    return payload["tests"]


def graduated(entries: dict[str, dict[str, object]]) -> tuple[str, ...]:
    return tuple(
        sorted(
            path
            for path, row in entries.items()
            if row.get("status") == "proven" and row.get("partition") == "pants"
        )
    )


def verify_partition(root: Path = ROOT) -> tuple[str, ...]:
    entries = load_manifest(root)
    selected = graduated(entries)
    errors: list[str] = []
    build = (root / "tests/BUILD").read_text(encoding="utf-8")
    for path in selected:
        source = root / path
        relative = path.removeprefix("tests/")
        row = entries[path]
        if not source.is_file():
            errors.append(f"missing graduated test: {path}")
        if not path.startswith("tests/unit/"):
            errors.append(f"graduated test is outside the first hermetic unit cohort: {path}")
        if not row.get("dependencies"):
            errors.append(f"graduated test has no dependency evidence: {path}")
        if f'"{relative}"' not in build:
            errors.append(f"graduated test lacks an explicit tests/BUILD override: {path}")
    if selected and '"pants:proven"' not in build:
        errors.append("tests/BUILD does not tag the graduated Pants targets")

    # Native receives every discovered test except this exact set.  This equality is the parity
    # invariant: a new test defaults to native, and a stale/missing graduated path is red.
    discovered = {path.relative_to(root).as_posix() for path in (root / "tests").rglob("test_*.py")}
    native = discovered - set(selected)
    if (native | set(selected)) != discovered or native & set(selected):
        errors.append("Pants/native test partitions are not disjoint and exhaustive")
    if errors:
        raise PartitionError("\n".join(errors))
    return selected


def _pants_command(pants: str, args: Sequence[str]) -> list[str]:
    return [sys.executable, "scripts/pants_cache.py", "run", "--", pants, "--no-pantsd", *args]


def run_pants(paths: Sequence[str], *, action: str) -> int:
    if not paths:
        print(json.dumps({"event": "pants-ci", "action": action, "selected": 0, "exit_code": 0}))
        return 0
    started = time.monotonic()
    command = _pants_command(launcher(), ["--test-output=all", "test", *paths])
    result = subprocess.run(command, cwd=ROOT, check=False, text=True, capture_output=True)
    if result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n", file=sys.stderr)
    if result.stderr:
        print(result.stderr, end="" if result.stderr.endswith("\n") else "\n", file=sys.stderr)
    output = (result.stdout or "") + (result.stderr or "")
    print(
        json.dumps(
            {
                "event": "pants-ci",
                "action": action,
                "selected": len(paths),
                "cache_served": output.count("cached locally") + output.count("memoized"),
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "exit_code": result.returncode,
            },
            sort_keys=True,
        )
    )
    return result.returncode


def affected(base: str, selected: Sequence[str]) -> tuple[str, ...]:
    command = [
        launcher(),
        "--no-pantsd",
        f"--changed-since={base}",
        "--changed-dependents=transitive",
        "peek",
    ]
    result = subprocess.run(command, cwd=ROOT, check=False, text=True, capture_output=True)
    if result.returncode:
        detail = result.stderr.strip() or f"exit {result.returncode}"
        raise PartitionError(f"Pants affected query failed closed: {detail}")
    try:
        rows = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PartitionError(f"Pants affected query returned invalid JSON: {exc}") from exc
    selected_set = set(selected)
    affected_sources = {
        source for row in rows for source in row.get("sources", ()) if source in selected_set
    }
    return tuple(sorted(affected_sources))


def run_native(pytest_args: Sequence[str], selected: Sequence[str]) -> int:
    command = [sys.executable, "-m", "pytest", *pytest_args]
    command.extend(f"--ignore={path}" for path in selected)
    return subprocess.run(command, cwd=ROOT, check=False).returncode


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    sub = result.add_subparsers(dest="action", required=True)
    sub.add_parser("verify")
    sub.add_parser("all")
    changed = sub.add_parser("affected")
    changed.add_argument("base")
    native = sub.add_parser("native")
    native.add_argument("pytest_args", nargs=argparse.REMAINDER)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    options = parser().parse_args(argv)
    try:
        selected = verify_partition()
        if options.action == "verify":
            print(f"pants-ci-partition: OK ({len(selected)} Pants, residual native default)")
            return 0
        if options.action == "all":
            return run_pants(selected, action="complete-closure")
        if options.action == "affected":
            return run_pants(affected(options.base, selected), action="affected-closure")
        args = list(options.pytest_args)
        if args[:1] == ["--"]:
            args = args[1:]
        return run_native(args, selected)
    except (OSError, ValueError, PartitionError, RuntimeError) as exc:
        print(f"pants-ci: FAILED\n{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
