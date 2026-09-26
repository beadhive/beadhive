#!/usr/bin/env python3
"""Rank slow test files and cases from one validation run's JUnit reports directory."""

from __future__ import annotations

import argparse
import json
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path


def _seconds(raw: str | None) -> float:
    try:
        return float(raw or 0)
    except ValueError:
        return 0.0


def _case_file(case: ET.Element) -> str:
    explicit = case.get("file")
    if explicit:
        return explicit
    classname = case.get("classname", "")
    return f"{classname.replace('.', '/')}.py" if classname else "<unknown>"


def rankings(reports: Path, limit: int = 20) -> dict[str, list[dict[str, object]]]:
    """Return descending cumulative file timings and individual test timings."""
    files: defaultdict[str, float] = defaultdict(float)
    tests: list[dict[str, object]] = []
    for report in sorted(reports.glob("*.xml")):
        try:
            root = ET.parse(report).getroot()
        except (OSError, ET.ParseError):
            continue
        for case in root.iter("testcase"):
            duration = _seconds(case.get("time"))
            filename = _case_file(case)
            name = "::".join(part for part in (case.get("classname"), case.get("name")) if part)
            files[filename] += duration
            tests.append({"name": name or "<unknown>", "seconds": duration, "file": filename})
    ranked_files = sorted(files.items(), key=lambda item: (-item[1], item[0]))[:limit]
    ranked_tests = sorted(tests, key=lambda item: (-float(item["seconds"]), str(item["name"])))[
        :limit
    ]
    return {
        "files": [{"file": name, "seconds": seconds} for name, seconds in ranked_files],
        "tests": ranked_tests,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", type=Path, help="a validation run's reports/ directory")
    parser.add_argument("--limit", type=int, default=20, help="rows per ranking (default: 20)")
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit must be positive")
    print(json.dumps(rankings(args.reports, args.limit), indent=2))


if __name__ == "__main__":
    main()
