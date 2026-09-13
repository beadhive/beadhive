#!/usr/bin/env python3
"""Conservative Pants test routing with machine-readable selection receipts."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).parents[1]
QUALIFIED_TEST = "tests/unit/modules/config/test_resolution.py"
QUALIFIED_SOURCE = "src/beadhive/modules/config/application/resolution.py"
QUALIFIED = frozenset({QUALIFIED_TEST, QUALIFIED_SOURCE})
QUALIFIED_CLOSURE_COUNT = 1


@dataclass
class Receipt:
    route: str
    selector_reason: str
    changed: list[str]
    targets: list[str]
    transitive_dependents: list[str]
    invalidation_cause: str | None
    executed_count: int
    avoided_count: int
    cache_served_count: int
    fallback_reason: str | None
    edit_to_result_seconds: float
    exit_code: int


def _run(command: Sequence[str], *, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
    )


def changed_paths(base: str) -> list[str]:
    result = _run(["git", "diff", "--name-only", base, "--"], capture=True)
    if result.returncode:
        raise RuntimeError(result.stdout or f"git diff failed with {result.returncode}")
    return sorted({line for line in (result.stdout or "").splitlines() if line})


def classify(paths: Sequence[str]) -> tuple[str, str | None]:
    if not paths:
        return "avoid", None
    if all(path in QUALIFIED for path in paths):
        return "pants", None
    if all(path.endswith(".md") and path != "docs/PANTS.md" for path in paths):
        return "avoid", None

    reasons: list[tuple[str, bool]] = [
        (
            "dependency-lock",
            any(Path(p).name in {"uv.lock", "beadhive.lock", "pyproject.toml"} for p in paths),
        ),
        (
            "build-config",
            any(
                Path(p).name in {"pants.toml", "BUILD", "BUILDROOT"} or p == "docs/PANTS.md"
                for p in paths
            ),
        ),
        (
            "test-infrastructure",
            any(
                p.startswith("tests/harness/")
                or p in {"tests/conftest.py", "tests/stateful_fixtures.py"}
                for p in paths
            ),
        ),
        ("plugin-dynamic", any("plugin" in Path(p).name for p in paths)),
        (
            "generated",
            any(
                "generated" in p or "schema_artifacts" in p or p.startswith("docs/schemas/")
                for p in paths
            ),
        ),
        ("shared-contract", any("contract" in Path(p).name for p in paths)),
        (
            "compatibility",
            any(p in {"src/beadhive/config.py", "src/beadhive/config_store.py"} for p in paths),
        ),
        (
            "installed-console-script",
            any("console" in p or p.endswith("test_cli.py") for p in paths),
        ),
        (
            "ambiguous-integration",
            any(p.startswith("tests/") and not p.startswith("tests/unit/") for p in paths),
        ),
        (
            "multi-module",
            len(
                {
                    p.split("/")[3]
                    for p in paths
                    if p.startswith("src/beadhive/modules/") and len(p.split("/")) > 3
                }
            )
            > 1,
        ),
    ]
    return "native", next(
        (reason for reason, applies in reasons if applies), "unknown-or-unqualified"
    )


def _pants_command(pants: str, args: Sequence[str]) -> list[str]:
    return [sys.executable, "scripts/pants_cache.py", "run", "--", pants, "--no-pantsd", *args]


def _emit(receipt: Receipt) -> int:
    print(json.dumps({"event": "pants-selective-route", **asdict(receipt)}, sort_keys=True))
    return receipt.exit_code


def route(
    action: str,
    selector: str,
    *,
    pants: str,
    native_command: Sequence[str],
) -> int:
    started = time.monotonic()
    try:
        paths = changed_paths(selector) if action == "changed" else [selector]
    except Exception as exc:
        paths = []
        decision, fallback = "native", f"selector-error: {exc}"
    else:
        decision, fallback = classify(paths)

    if decision == "avoid":
        return _emit(
            Receipt(
                action,
                "known-unrelated" if paths else "no-changes",
                paths,
                [],
                [],
                None,
                0,
                QUALIFIED_CLOSURE_COUNT,
                0,
                None,
                time.monotonic() - started,
                0,
            )
        )

    if decision == "native":
        result = _run(native_command)
        return _emit(
            Receipt(
                action,
                "native-full-fallback",
                paths,
                [],
                [],
                None,
                0,
                0,
                0,
                fallback,
                time.monotonic() - started,
                result.returncode,
            )
        )

    target = QUALIFIED_TEST
    reason = "explicit-qualified-leaf" if action == "leaf" else "qualified-change"
    dependents: list[str] = []
    if action == "dependent":
        # This first route is an explicit, reviewed source-to-test edge. A global `dependents`
        # query traverses still-unqualified harness targets and correctly fails closed on them.
        dependents = [target]
        reason = "transitive-dependent-of-qualified-source"

    result = _run(_pants_command(pants, ["--test-output=all", "test", target]), capture=True)
    output = result.stdout or ""
    if output:
        print(output, end="" if output.endswith("\n") else "\n", file=sys.stderr)
    cached = int("(cached locally)" in output or "(memoized)" in output)
    return _emit(
        Receipt(
            action,
            reason,
            paths,
            [target],
            dependents,
            selector if action != "leaf" else None,
            int(result.returncode == 0 and not cached),
            0,
            int(result.returncode == 0 and cached),
            None if result.returncode == 0 else "pants-execution-failed",
            time.monotonic() - started,
            result.returncode,
        )
    )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--pants", default=os.environ.get("PANTS_BIN", "pants"))
    result.add_argument("--native-command", nargs="+", default=["just", "check"])
    sub = result.add_subparsers(dest="action", required=True)
    sub.add_parser("leaf").add_argument("target")
    sub.add_parser("changed").add_argument("base")
    sub.add_parser("dependent").add_argument("source")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    options = parser().parse_args(argv)
    selector = getattr(options, "target", None) or getattr(options, "base", None) or options.source
    return route(
        options.action, selector, pants=options.pants, native_command=options.native_command
    )


if __name__ == "__main__":
    raise SystemExit(main())
