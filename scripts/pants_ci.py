#!/usr/bin/env python3
"""Execute the proven Pants/native pytest partition without coverage gaps.

Pants owns the graduated files and their transitive dependency closure.  Native pytest keeps
every test that has not passed the sandbox proof.  The checked manifest is the single boundary
between those runners; malformed or stale entries fail before either runner starts.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

try:
    from scripts.pants_launcher import launcher
except ModuleNotFoundError:
    from pants_launcher import launcher  # pants: no-infer-dep

ROOT = Path(__file__).parents[1]
MANIFEST = ROOT / "scripts/pants_proven_tests.json"
BUILD = ROOT / "tests/BUILD"
GLOBAL_INPUTS = (
    "pants.toml",
    "BUILD",
    "**/BUILD",
    "*.lock",
    "**/*.lock",
    "pyproject.toml",
    "uv.lock",
    "justfile",
    ".mise.toml",
    "scripts/hermetic.sh",
    "scripts/pants_ci.py",
    "scripts/pants_proven_tests.json",
)
SAFE_NON_CODE_PREFIXES = ("docs/", ".beads/")
SAFE_NON_CODE_FILES = ("README.md", "CHANGELOG.md", "LICENSE")


class PartitionError(RuntimeError):
    """The checked partition cannot prove complete test coverage."""


@dataclass(frozen=True)
class ChangeRoute:
    """A fail-closed developer route over the complete Pants/native partition."""

    changed: tuple[str, ...]
    pants_tests: tuple[str, ...]
    run_native: bool
    run_all_pants: bool
    reason: str


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


def _query_affected(base: str) -> list[dict[str, object]]:
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
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise PartitionError("Pants affected query must return a JSON array of targets")
    return rows


def affected(base: str, selected: Sequence[str]) -> tuple[str, ...]:
    rows = _query_affected(base)
    selected_set = set(selected)
    affected_sources = {
        source for row in rows for source in row.get("sources", ()) if source in selected_set
    }
    return tuple(sorted(affected_sources))


def changed_paths(base: str) -> tuple[str, ...]:
    result = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=ACDMRTUXB", base, "--"],
        cwd=ROOT,
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode:
        detail = result.stderr.strip() or f"exit {result.returncode}"
        raise PartitionError(f"git changed-path query failed closed: {detail}")
    return tuple(sorted({line for line in result.stdout.splitlines() if line}))


def _is_global(path: str) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in GLOBAL_INPUTS)


def _is_safe_non_code(path: str) -> bool:
    return path in SAFE_NON_CODE_FILES or path.startswith(SAFE_NON_CODE_PREFIXES)


def plan_changed(
    base: str,
    selected: Sequence[str],
    *,
    changes: Sequence[str] | None = None,
    rows: Sequence[dict[str, object]] | None = None,
) -> ChangeRoute:
    """Classify developer feedback without treating uncertainty as no impact."""
    changed = tuple(sorted(changes if changes is not None else changed_paths(base)))
    if not changed:
        return ChangeRoute(changed, (), False, False, "no-changes")
    if any(_is_global(path) for path in changed):
        return ChangeRoute(changed, tuple(selected), True, True, "global-input")

    affected_rows = tuple(rows if rows is not None else _query_affected(base))
    selected_set = set(selected)
    pants_tests: set[str] = set()
    native_tests: set[str] = set()
    for row in affected_rows:
        sources = row.get("sources") or ()
        if not isinstance(sources, (list, tuple)) or not all(
            isinstance(source, str) for source in sources
        ):
            raise PartitionError("Pants affected target has invalid sources")
        if row.get("target_type") not in {"python_test", "python_tests"}:
            continue
        for source in sources:
            if source in selected_set:
                pants_tests.add(source)
            elif source.startswith("tests/") and Path(source).name.startswith("test_"):
                native_tests.add(source)

    if native_tests:
        return ChangeRoute(
            changed,
            tuple(sorted(pants_tests)),
            True,
            False,
            "affected-unproven-tests",
        )
    if pants_tests:
        return ChangeRoute(
            changed,
            tuple(sorted(pants_tests)),
            False,
            False,
            "affected-proven-tests",
        )
    if all(_is_safe_non_code(path) for path in changed):
        return ChangeRoute(changed, (), False, False, "non-code-only")
    # Empty ownership and non-test graph results are not proof that executable changes are safe.
    return ChangeRoute(changed, tuple(selected), True, True, "unproven-or-unowned-impact")


def run_changed(base: str, selected: Sequence[str]) -> int:
    try:
        route = plan_changed(base, selected)
    except (OSError, ValueError, PartitionError, RuntimeError) as exc:
        route = ChangeRoute(
            changed=(),
            pants_tests=tuple(selected),
            run_native=True,
            run_all_pants=True,
            reason=f"analysis-failed-closed: {exc}",
        )
    print(json.dumps({"event": "pants-ci-route", **asdict(route)}, sort_keys=True))
    pants_paths = selected if route.run_all_pants else route.pants_tests
    pants_status = run_pants(pants_paths, action="developer-affected")
    if pants_status or not route.run_native:
        return pants_status
    return subprocess.run(["just", "stateful-native"], cwd=ROOT, check=False).returncode


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
            return run_changed(options.base, selected)
        args = list(options.pytest_args)
        if args[:1] == ["--"]:
            args = args[1:]
        return run_native(args, selected)
    except (OSError, ValueError, PartitionError, RuntimeError) as exc:
        print(f"pants-ci: FAILED\n{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
