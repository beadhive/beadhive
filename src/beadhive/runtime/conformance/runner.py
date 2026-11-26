"""Run the kit without pytest and print a verdict: ``python -m beadhive.runtime.conformance``."""

from __future__ import annotations

import argparse
import importlib
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .bdhive import default_hive
from .checks import (
    BH_SIDE,
    CHECKS,
    GC_FIXABLE,
    STRUCTURAL,
    ConformanceFailure,
    RuntimeEnv,
    RuntimeFactory,
)
from .hive import DisposableHive


@dataclass(frozen=True)
class CheckResult:
    check: str
    passed: bool
    message: str = ""
    fix_class: str = ""
    hive_kind: str = ""  # what hive the verdict was obtained on: "bd", "file" or "custom"


def run_conformance(
    factory: RuntimeFactory,
    *,
    hive: DisposableHive | None = None,
    workdir: Path | None = None,
    timeout: float = 60.0,
    loop: bool = False,
) -> list[CheckResult]:
    """Run every check against *factory* and return one result per check.

    ``loop=True`` adds the opt-in loop probe (:mod:`.loop`): BeadHive's real dispatcher loop
    drives one turn through the runtime. It needs a real scratch bd store (the default hive when
    ``bd`` is on ``PATH``); on another hive it fails as ``bh-loop``."""
    with tempfile.TemporaryDirectory(prefix="bh-conformance-") as tmp:
        root = Path(workdir) if workdir else Path(tmp)
        the_hive = hive if hive is not None else default_hive(root)
        workspace = root / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        instructions = root / "instructions.md"
        instructions.write_text("conformance turn\n", encoding="utf-8")
        env = RuntimeEnv(the_hive, the_hive.seat_command(), workspace, instructions, timeout)
        kind = env.hive_kind
        results = []
        checks = dict(CHECKS)
        if loop:
            from .loop import LOOP_CHECKS

            checks.update(LOOP_CHECKS)
        try:
            for name, check in checks.items():
                try:
                    check(env, factory)
                except ConformanceFailure as exc:
                    results.append(
                        CheckResult(name, False, str(exc), exc.invariant.fix_class, kind)
                    )
                except Exception as exc:  # a crash is GC-fixable: a broken seam, not a structure
                    msg = f"{type(exc).__name__}: {exc}"
                    results.append(CheckResult(name, False, msg, GC_FIXABLE, kind))
                else:
                    results.append(CheckResult(name, True, hive_kind=kind))
        finally:
            if hive is None:  # a hive the caller passed is the caller's to close
                close = getattr(the_hive, "close", None)
                if callable(close):
                    close()
        return results


def verdict(results: list[CheckResult]) -> str:
    """The ADR Decision 6 reading of a run: GO, fix and re-run, or Mode 2."""
    failed = [r for r in results if not r.passed]
    if not failed:
        if any(r.hive_kind == "file" for r in results):
            return (
                "GO (FileHive: bd-level lifecycle writes NOT exercised): the runtime passes "
                "the kit on a JSON stand-in hive, which cannot see a runtime that writes "
                "through bd; re-run where bd is available before treating this as Mode 1"
            )
        return "GO: the runtime passes the kit (Mode 1)"
    if any(r.fix_class == STRUCTURAL for r in failed):
        return "NO-GO: a structural failure, so Mode 2 (delegated molecule), ADR Decision 6"
    if any(r.fix_class == BH_SIDE for r in failed):
        return (
            "INCONCLUSIVE: the loop probe found a BH-side failure in BeadHive's own loop; it is "
            "not the runtime's to fix, so report it to beadhive and re-run"
        )
    return "NOT YET: every failure is GC-fixable; fix the runtime and re-run the kit"


def _load_factory(spec: str) -> RuntimeFactory:
    module_name, _, attr = spec.partition(":")
    if not attr:
        raise SystemExit("factory must be 'package.module:callable'")
    return getattr(importlib.import_module(module_name), attr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m beadhive.runtime.conformance",
        description="Run the runtime conformance kit against a Runtime factory "
        "(callable taking a RuntimeEnv and returning a Runtime).",
    )
    parser.add_argument("factory", help="package.module:callable")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument(
        "--loop",
        action="store_true",
        help="also run the loop probe: BeadHive's real dispatcher loop drives one turn through "
        "the runtime (needs bd on PATH)",
    )
    args = parser.parse_args(argv)
    results = run_conformance(_load_factory(args.factory), timeout=args.timeout, loop=args.loop)
    for r in results:
        print(f"{'PASS' if r.passed else 'FAIL'} {r.check}")
        if not r.passed:
            print(f"     {r.message}")
    print(verdict(results))
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
