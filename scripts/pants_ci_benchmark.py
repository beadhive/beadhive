#!/usr/bin/env python3
"""Collect and verify selective-CI wall-clock evidence without statistical overclaiming."""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).parents[1]
DEFAULT_EVIDENCE = ROOT / "docs/proof/bh-t8t7r-ci-benchmark.json"
CHANGE_CATEGORIES = ("build-system", "code", "config", "docs", "test-only")
LEGACY_CHANGE_CLASS_MAP = {
    "global-input": "build-system",
    "proven-leaf": "test-only",
    "unproven-native-impact": "test-only",
    "selectorless-floor": "retired-unconditional-host-floor",
}


class BenchmarkError(RuntimeError):
    """Benchmark evidence is malformed or reports unsupported statistics."""


def nearest_rank(samples: Sequence[float], percentile: float) -> float:
    ordered = sorted(samples)
    return ordered[max(0, math.ceil(percentile * len(ordered)) - 1)]


def status_for(sample_count: int) -> str:
    if sample_count >= 10:
        return "measured"
    if sample_count >= 5:
        return "provisional"
    return "pending"


def check_evidence(path: Path) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise BenchmarkError("benchmark evidence must use schema version 1")
    if payload.get("minimum_samples_per_class") != 10:
        raise BenchmarkError("benchmark evidence must target at least 10 samples per class")
    vocabulary = payload.get("change_class_vocabulary")
    if not isinstance(vocabulary, dict):
        raise BenchmarkError("benchmark evidence needs change_class_vocabulary")
    if vocabulary.get("categories") != list(CHANGE_CATEGORIES):
        raise BenchmarkError(f"change categories must be {list(CHANGE_CATEGORIES)!r}")
    if vocabulary.get("legacy_mapping") != LEGACY_CHANGE_CLASS_MAP:
        raise BenchmarkError("legacy change classes are not reconciled with category vocabulary")
    observations = payload.get("observations")
    if not isinstance(observations, list) or not observations:
        raise BenchmarkError("benchmark evidence has no observations")
    seen: set[tuple[str, str]] = set()
    for row in observations:
        if not isinstance(row, dict):
            raise BenchmarkError("benchmark observation must be an object")
        identity = (row.get("change_class"), row.get("phase"))
        if not all(isinstance(value, str) and value for value in identity):
            raise BenchmarkError("benchmark observation needs change_class and phase")
        if identity in seen:
            raise BenchmarkError(f"duplicate benchmark observation: {identity}")
        seen.add(identity)
        samples = row.get("raw_elapsed_seconds")
        if not isinstance(samples, list) or not all(
            isinstance(value, (int, float)) and value > 0 for value in samples
        ):
            raise BenchmarkError(f"{identity} raw samples must be positive")
        if not samples and not row.get("component_observations"):
            raise BenchmarkError(f"{identity} needs raw samples or component observations")
        if row.get("sample_count") != len(samples):
            raise BenchmarkError(f"{identity} sample_count does not match raw samples")
        expected_status = status_for(len(samples))
        if row.get("statistics_status") != expected_status:
            raise BenchmarkError(f"{identity} must report statistics_status={expected_status}")
        if expected_status == "pending":
            if "p50_seconds" in row or "p90_seconds" in row:
                raise BenchmarkError(f"{identity} cannot report percentiles from <5 samples")
        else:
            expected_p50 = nearest_rank(samples, 0.5)
            expected_p90 = nearest_rank(samples, 0.9)
            if row.get("p50_seconds") != expected_p50 or row.get("p90_seconds") != expected_p90:
                raise BenchmarkError(f"{identity} percentile values do not match raw samples")
    identities = {(row["change_class"], row["phase"]) for row in observations}
    if ("selectorless-floor", "before") not in identities:
        raise BenchmarkError("retired selectorless floor needs a historical before observation")
    for category in CHANGE_CATEGORIES:
        for phase in ("before", "after"):
            if (category, phase) not in identities:
                raise BenchmarkError(f"{category} needs a {phase} observation")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    sub = result.add_subparsers(dest="action", required=True)
    check = sub.add_parser("check")
    check.add_argument("path", nargs="?", type=Path, default=DEFAULT_EVIDENCE)
    sample = sub.add_parser("sample")
    sample.add_argument(
        "--change-class",
        choices=(*CHANGE_CATEGORIES, *LEGACY_CHANGE_CLASS_MAP),
        required=True,
    )
    sample.add_argument("--phase", choices=("before", "after", "floor"), required=True)
    sample.add_argument("command", nargs=argparse.REMAINDER)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    options = parser().parse_args(argv)
    try:
        if options.action == "check":
            path = options.path if options.path.is_absolute() else ROOT / options.path
            check_evidence(path)
            print(f"pants-ci-benchmark: OK ({path.relative_to(ROOT)})")
            return 0
        command = list(options.command)
        if command[:1] == ["--"]:
            command = command[1:]
        if not command:
            raise BenchmarkError("sample requires a command after --")
        started = time.monotonic()
        result = subprocess.run(command, cwd=ROOT, check=False)
        print(
            json.dumps(
                {
                    "change_class": options.change_class,
                    "phase": options.phase,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "exit_code": result.returncode,
                    "command": command,
                },
                sort_keys=True,
            )
        )
        return result.returncode
    except (BenchmarkError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"pants-ci-benchmark: FAILED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
