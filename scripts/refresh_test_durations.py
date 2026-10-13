#!/usr/bin/env python3
"""Regenerate tests/harness/test_durations.json from validation JUnit reports.

Takes the median wall time per test across the selected reports.  Two modes:

* **Lane mode** (``--lanes``, what ``just refresh-test-durations`` runs).  Collects each gate lane
  (stateful, root-composition, integration) with ``pytest --collect-only``, assigns every report to
  the lane it covers (at least ``--min-fill`` of the lane's collected tests, so a selective partial
  run never sets a median), keeps the newest ``--reports`` full-lane reports per lane, records
  EVERY collected test it saw (``--min-seconds 0`` by default, rounded to 0.01 s), prunes keys
  that are no longer collected, adds a per-module median table that the ordering hook uses for
  tests the file does not list, and prints known/collected coverage per lane.
* **Glob mode** (default): every report matching ``--glob``, no collection and no pruning.

Skipped tests are ignored, except that a collected test which only ever skipped is recorded at
0.01 s so the lane is fully covered.  Run from the repository root:

    just refresh-test-durations
"""

from __future__ import annotations

import argparse
import glob
import json
import shlex
import statistics
import subprocess
import sys
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree  # noqa: S405 - our own validation reports

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from harness.duration_order import junit_key, module_of_key  # noqa: E402

DEFAULT_GLOB = ".bh/validation/runs/*/reports/*.xml"
DEFAULT_OUTPUT = Path("tests/harness/test_durations.json")
FLOOR_SECONDS = 0.01


@dataclass(frozen=True)
class Report:
    path: str
    mtime: float
    seconds: dict[str, float]  # executed cases: classname::name -> seconds
    skipped: frozenset[str]


@dataclass(frozen=True)
class Lane:
    name: str
    #: ``scripts/root_composition_tests.py`` mode that supplies the file arguments, if any.
    files: str | None
    marker: str | None


#: The gate lanes, as the justfile selects them: ``stateful-native``, ``root-composition-native``
#: and ``test-integration-land``.  The three selections are disjoint.
LANES = (
    Lane("stateful", "--ignore-args", "not integration and not pants_profile"),
    Lane("root-composition", "", "not integration"),
    Lane("integration", None, "integration"),
)

#: ``collect(lane)`` returns the junit keys of every test the lane selects.
Collector = Callable[[Lane], set[str]]


def read_report(path: Path) -> dict[str, float]:
    """Return ``classname::name -> seconds`` for the executed cases in one JUnit file."""
    return read_cases(path)[0]


def read_cases(path: Path) -> tuple[dict[str, float], frozenset[str]]:
    found: dict[str, float] = {}
    skipped: set[str] = set()
    for case in ElementTree.parse(path).getroot().iter("testcase"):  # noqa: S314
        if not case.get("classname"):
            continue
        key = f"{case.get('classname', '')}::{case.get('name', '')}"
        if case.find("skipped") is not None:
            skipped.add(key)
            continue
        found[key] = found.get(key, 0.0) + float(case.get("time", "0") or 0)
    return found, frozenset(skipped)


def load_reports(pattern: str) -> tuple[list[Report], int]:
    reports, unreadable = [], 0
    for name in sorted(glob.glob(pattern)):
        try:
            seconds, skipped = read_cases(Path(name))
            reports.append(Report(name, Path(name).stat().st_mtime, seconds, skipped))
        except (ElementTree.ParseError, OSError, ValueError):
            unreadable += 1  # truncated by a killed run; the rest still carry the signal
    return reports, unreadable


def summarize(reports: Iterable[dict[str, float]], min_seconds: float) -> dict:
    samples: dict[str, list[float]] = defaultdict(list)
    for report in reports:
        for key, seconds in report.items():
            samples[key].append(seconds)
    medians = {key: statistics.median(values) for key, values in samples.items()}
    overall = statistics.median(medians.values()) if medians else 0.0
    kept = {k: round(v, 2) for k, v in sorted(medians.items()) if v >= min_seconds}
    return {"version": 1, "unit": "seconds", "median": round(overall, 3), "tests": kept}


def select_lane_reports(
    reports: Sequence[Report], lane_keys: dict[str, set[str]], min_fill: float, newest: int
) -> dict[str, list[Report]]:
    """Newest full-lane reports per lane; a report belongs to the lane it overlaps most."""
    chosen: dict[str, list[Report]] = {name: [] for name in lane_keys}
    for report in sorted(reports, key=lambda r: (r.mtime, r.path), reverse=True):
        seen = set(report.seconds) | report.skipped
        name, hits = max(
            ((n, len(seen & keys)) for n, keys in lane_keys.items()), key=lambda pair: pair[1]
        )
        if (
            lane_keys[name]
            and hits >= min_fill * len(lane_keys[name])
            and len(chosen[name]) < newest
        ):
            chosen[name].append(report)
    return chosen


def build_lane_file(
    chosen: dict[str, list[Report]], lane_keys: dict[str, set[str]], min_seconds: float
) -> tuple[dict, dict[str, dict]]:
    """Return the durations document and per-lane stats (known, collected, predicted)."""
    samples: dict[str, list[float]] = defaultdict(list)
    skipped_only: set[str] = set()
    for reports in chosen.values():
        for report in reports:
            for key, seconds in report.seconds.items():
                samples[key].append(seconds)
            skipped_only |= report.skipped
    collected = set().union(*lane_keys.values())
    medians = {
        key: max(statistics.median(values), FLOOR_SECONDS)
        for key, values in samples.items()
        if key in collected
    }
    overall = round(statistics.median(medians.values()), 3) if medians else 0.0
    by_module: dict[str, list[float]] = defaultdict(list)
    for key, value in medians.items():  # executed tests only: a skip says nothing about the cost
        by_module[module_of_key(key)].append(value)
    for key in skipped_only & collected - set(medians):
        medians[key] = FLOOR_SECONDS
    modules = {
        m: round(max(statistics.median(v), FLOOR_SECONDS), 2) for m, v in sorted(by_module.items())
    }
    tests = {k: round(v, 2) for k, v in sorted(medians.items()) if v >= min_seconds}
    stats = {}
    for name, keys in lane_keys.items():
        stats[name] = {
            "reports": len(chosen[name]),
            "collected": len(keys),
            "known": len(keys & tests.keys()),
            "predicted": sum(tests.get(k, modules.get(module_of_key(k), overall)) for k in keys),
        }
    document = {
        "version": 1,
        "unit": "seconds",
        "median": overall,
        "tests": tests,
        "modules": modules,
    }
    return document, stats


def collect_with_pytest(lane: Lane) -> set[str]:
    """Collect one lane in the same fence ``just collection-index-refresh`` uses."""
    files: list[str] = []
    if lane.files is not None:
        script = [sys.executable, str(ROOT / "scripts/root_composition_tests.py")]
        files = shlex.split(
            subprocess.run(
                [*script, *([lane.files] if lane.files else [])],
                check=True,
                capture_output=True,
                text=True,
                cwd=ROOT,
            ).stdout
        )
    # Stateful collects the whole tests/ tree minus the root-composition files.
    targets = ["tests"] if lane.name != "root-composition" else []
    command = [
        "./scripts/hermetic.sh",
        sys.executable,
        "-m",
        "pytest",
        "--collect-only",
        "-q",
        "-p",
        "no:xdist",
        "-p",
        "no:cacheprovider",
        *targets,
        *files,
    ]
    if lane.marker:
        command += ["-m", lane.marker]
    result = subprocess.run(command, capture_output=True, text=True, cwd=ROOT)  # noqa: S603
    keys = {junit_key(line.strip()) for line in result.stdout.splitlines() if "::" in line}
    if result.returncode not in (0, 5) or not keys:
        raise RuntimeError(
            f"collecting {lane.name} failed (rc={result.returncode}):\n{result.stderr[-2000:]}"
        )
    return keys


def run_lanes(args: argparse.Namespace, reports: list[Report], collect: Collector) -> int:
    lane_keys = {lane.name: collect(lane) for lane in LANES}
    chosen = select_lane_reports(reports, lane_keys, args.min_fill, args.reports)
    empty = [name for name, picked in chosen.items() if not picked]
    if empty:
        print(f"no full-lane report for: {', '.join(empty)}", file=sys.stderr)
        return 1
    document, stats = build_lane_file(chosen, lane_keys, args.min_seconds)
    args.output.write_text(json.dumps(document, indent=0, sort_keys=False) + "\n", encoding="utf-8")
    for name, lane in stats.items():
        pct = 100.0 * lane["known"] / lane["collected"]
        print(
            f"{name}: coverage {lane['known']}/{lane['collected']} ({pct:.1f}%) from "
            f"{lane['reports']} reports; predicted sum {lane['predicted']:.1f} s"
        )
    size = args.output.stat().st_size
    print(
        f"{len(document['tests'])} tests, {len(document['modules'])} modules -> {args.output} "
        f"({size:,} bytes)"
    )
    return 0


def main(argv: list[str] | None = None, collect: Collector = collect_with_pytest) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--glob", default=DEFAULT_GLOB, help="JUnit report glob")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--min-seconds", type=float, default=0.5)
    parser.add_argument("--lanes", action="store_true", help="lane mode: collect, filter, prune")
    parser.add_argument("--reports", type=int, default=6, help="newest full-lane reports per lane")
    parser.add_argument(
        "--min-fill", type=float, default=0.9, help="share of a lane a report covers"
    )
    args = parser.parse_args(argv)
    reports, unreadable = load_reports(args.glob)
    if unreadable:
        print(f"skipped {unreadable} unreadable report(s)", file=sys.stderr)
    if not reports:
        print(f"no JUnit reports match {args.glob}", file=sys.stderr)
        return 1
    if args.lanes:
        return run_lanes(args, reports, collect)
    result = summarize([r.seconds for r in reports], args.min_seconds)
    args.output.write_text(json.dumps(result, indent=0, sort_keys=False) + "\n", encoding="utf-8")
    print(f"{len(reports)} reports -> {len(result['tests'])} tests -> {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
