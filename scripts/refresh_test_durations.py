#!/usr/bin/env python3
"""Regenerate tests/harness/test_durations.json from validation JUnit reports.

Takes the median wall time per test across every report matching the glob (default: the hive's
``.bh/validation/runs/*/reports/*.xml``).  Tests faster than ``--min-seconds`` are omitted; the
ordering hook gives them (and unknown tests) the recorded overall median.  Skipped tests are
ignored.  Run from the repository root:

    uv run python scripts/refresh_test_durations.py
"""

from __future__ import annotations

import argparse
import glob
import json
import statistics
import sys
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from xml.etree import ElementTree  # noqa: S405 - our own validation reports

DEFAULT_GLOB = ".bh/validation/runs/*/reports/*.xml"
DEFAULT_OUTPUT = Path("tests/harness/test_durations.json")


def read_report(path: Path) -> dict[str, float]:
    """Return ``classname::name -> seconds`` for the executed cases in one JUnit file."""
    found: dict[str, float] = {}
    for case in ElementTree.parse(path).getroot().iter("testcase"):  # noqa: S314
        if case.find("skipped") is not None or not case.get("classname"):
            continue
        key = f"{case.get('classname', '')}::{case.get('name', '')}"
        found[key] = found.get(key, 0.0) + float(case.get("time", "0") or 0)
    return found


def summarize(reports: Iterable[dict[str, float]], min_seconds: float) -> dict:
    samples: dict[str, list[float]] = defaultdict(list)
    for report in reports:
        for key, seconds in report.items():
            samples[key].append(seconds)
    medians = {key: statistics.median(values) for key, values in samples.items()}
    overall = statistics.median(medians.values()) if medians else 0.0
    kept = {k: round(v, 2) for k, v in sorted(medians.items()) if v >= min_seconds}
    return {"version": 1, "unit": "seconds", "median": round(overall, 3), "tests": kept}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--glob", default=DEFAULT_GLOB, help="JUnit report glob")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--min-seconds", type=float, default=0.5)
    args = parser.parse_args(argv)
    reports = []
    unreadable = 0
    for name in sorted(glob.glob(args.glob)):
        try:
            reports.append(read_report(Path(name)))
        except (ElementTree.ParseError, OSError, ValueError):
            unreadable += 1  # truncated by a killed run; the rest still carry the signal
    if unreadable:
        print(f"skipped {unreadable} unreadable report(s)", file=sys.stderr)
    if not reports:
        print(f"no JUnit reports match {args.glob}", file=sys.stderr)
        return 1
    result = summarize(reports, args.min_seconds)
    args.output.write_text(json.dumps(result, indent=0, sort_keys=False) + "\n", encoding="utf-8")
    print(f"{len(reports)} reports -> {len(result['tests'])} tests -> {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
