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
from typing import TYPE_CHECKING

from beadhive.modules.work.contracts.impact import AttestKey, ChangedPath, ImpactReceipt
from beadhive.modules.work.contracts.impact_resolution import select_resolver
from beadhive_pants.impact import PantsImpactBackend

from .launcher import launcher
from .repository import find_repository

if TYPE_CHECKING:
    from beadhive.modules.work.contracts.impact import ImpactResolver

ROOT = find_repository()
QUALIFIED_TEST = "tests/unit/modules/config/test_resolution.py"
QUALIFIED_SOURCE = "src/beadhive/modules/config/application/resolution.py"
QUALIFIED = frozenset({QUALIFIED_TEST, QUALIFIED_SOURCE})
QUALIFIED_CLOSURE_COUNT = 1
UNIT_KEY = AttestKey(
    name="unit",
    cmd="just test",
    selectors={"pants": "attest:unit"},
)


class GitTreeDiff:
    """Resolve Git trees and changed paths without importing a core adapter."""

    def _run(self, repo: str, *args: str) -> str:
        result = subprocess.run(
            ["git", "-C", repo, *args],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            raise RuntimeError(
                f"git {' '.join(args)} failed ({result.returncode}): {result.stderr.strip()}"
            )
        return result.stdout

    def tree_of(self, repo: str, rev: str) -> str:
        return self._run(repo, "rev-parse", "--verify", "--quiet", f"{rev}^{{tree}}").strip()

    def changed_paths(self, repo: str, base_tree: str, head_tree: str) -> tuple[ChangedPath, ...]:
        if base_tree == head_tree:
            return ()
        fields = self._run(
            repo,
            "diff-tree",
            "-r",
            "-z",
            "--no-renames",
            "--name-status",
            base_tree,
            head_tree,
        ).split("\0")
        return tuple(
            sorted(
                ChangedPath(path=path, status=status[0])
                for status, path in zip(fields[0::2], fields[1::2], strict=False)
                if status and path
            )
        )


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


def classify(receipt: ImpactReceipt) -> tuple[str, str | None]:
    """Choose a route from the build graph's receipt, never from path-name heuristics."""
    if not receipt.changed_paths:
        return "avoid", None
    if receipt.is_fallback:
        return "native", receipt.fallback_reason
    if UNIT_KEY.name in receipt.invalidated_keys:
        return "pants", None
    if receipt.is_unaffected(UNIT_KEY.name):
        return "avoid", None
    return "native", "impact-receipt-missing-unit-key"


def _pants_command(pants: str, args: Sequence[str]) -> list[str]:
    return [sys.executable, "scripts/pants_cache.py", "run", "--", pants, "--no-pantsd", *args]


def _emit(receipt: Receipt) -> int:
    if receipt.fallback_reason:
        print(
            "!!! WARNING: PANTS IMPACT FALLBACK — "
            f"{receipt.fallback_reason}; running the native full route !!!",
            file=sys.stderr,
        )
    print(json.dumps({"event": "pants-selective-route", **asdict(receipt)}, sort_keys=True))
    return receipt.exit_code


def route(
    action: str,
    selector: str,
    *,
    pants: str,
    native_command: Sequence[str],
    resolver: ImpactResolver | None = None,
) -> int:
    started = time.monotonic()
    if os.environ.get("BH_PANTS_ROUTING", "1").lower() in {"0", "false", "off", "no"}:
        result = _run(native_command)
        return _emit(
            Receipt(
                action,
                "native-full-fallback",
                [selector],
                [],
                [],
                None,
                0,
                0,
                0,
                "pants-routing-disabled",
                time.monotonic() - started,
                result.returncode,
            )
        )
    paths = [selector]
    decision, fallback = "pants", None
    if action == "changed":
        try:
            selected = resolver or select_resolver(
                "pants",
                tree_diff=GitTreeDiff(),
                backends={"pants": PantsImpactBackend(ROOT)},
            )
            receipt = selected.resolve(str(ROOT), selector, "HEAD", (UNIT_KEY,))
            paths = list(receipt.changed_paths)
            decision, fallback = classify(receipt)
        except Exception as exc:
            paths = []
            decision, fallback = "native", f"selector-error: {exc}"

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
    result.add_argument("--pants")
    result.add_argument("--native-command", nargs="+", default=["just", "check"])
    sub = result.add_subparsers(dest="action", required=True)
    sub.add_parser("leaf").add_argument("target")
    sub.add_parser("changed").add_argument("base")
    sub.add_parser("dependent").add_argument("source")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    options = parser().parse_args(argv)
    selector = getattr(options, "target", None) or getattr(options, "base", None) or options.source
    try:
        pants = options.pants or launcher()
    except RuntimeError as exc:
        native = _run(options.native_command)
        return _emit(
            Receipt(
                options.action,
                "native-full-fallback",
                [selector],
                [],
                [],
                None,
                0,
                0,
                0,
                f"launcher-error: {exc}",
                0.0,
                native.returncode,
            )
        )
    return route(options.action, selector, pants=pants, native_command=options.native_command)


if __name__ == "__main__":
    raise SystemExit(main())
