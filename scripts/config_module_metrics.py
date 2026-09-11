#!/usr/bin/env python3
"""Generate deterministic structural metrics for the configuration module."""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
from collections.abc import Iterable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BASE_REVISION = "5e47c61107a67a26676fc5890a89f0fd0b715c78"
MODULE_ROOT = "src/beadhive/modules/config"
DEFAULT_OUTPUT = ROOT / "docs/design/config-module-structural-metrics.json"


def _git(*args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout


def _baseline_sources() -> dict[str, str]:
    names = _git("ls-tree", "-r", "--name-only", BASE_REVISION, "--", MODULE_ROOT)
    paths = [name for name in names.splitlines() if name.endswith(".py")]
    return {path: _git("show", f"{BASE_REVISION}:{path}") for path in paths}


def _current_sources() -> dict[str, str]:
    directory = ROOT / MODULE_ROOT
    return {
        path.relative_to(ROOT).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(directory.rglob("*.py"))
    }


def _function_complexity(node: ast.AST) -> int:
    score = 1
    for child in ast.walk(node):
        if isinstance(child, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.IfExp, ast.Assert)):
            score += 1
        elif isinstance(child, ast.BoolOp):
            score += max(len(child.values) - 1, 0)
        elif isinstance(child, ast.Try):
            score += len(child.handlers) + bool(child.orelse) + bool(child.finalbody)
        elif isinstance(child, ast.Match):
            score += len(child.cases)
        elif isinstance(child, ast.comprehension):
            score += 1 + len(child.ifs)
    return score


def _measure(sources: dict[str, str]) -> dict[str, Any]:
    complexities: list[int] = []
    classes = 0
    physical_lines = 0
    nonblank_lines = 0
    for path, source in sources.items():
        del path
        lines = source.splitlines()
        physical_lines += len(lines)
        nonblank_lines += sum(bool(line.strip()) for line in lines)
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                complexities.append(_function_complexity(node))
            elif isinstance(node, ast.ClassDef):
                classes += 1
    total = sum(complexities)
    return {
        "python_files": len(sources),
        "physical_lines": physical_lines,
        "nonblank_lines": nonblank_lines,
        "functions": len(complexities),
        "classes": classes,
        "cyclomatic_sum": total,
        "cyclomatic_max": max(complexities, default=0),
        "cyclomatic_mean": round(total / len(complexities), 3) if complexities else 0.0,
        "functions_over_10": sum(value > 10 for value in complexities),
    }


def _delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    return {
        key: round(after[key] - before[key], 3)
        for key in before
        if isinstance(before[key], (int, float)) and not isinstance(before[key], bool)
    }


def build_metrics() -> dict[str, Any]:
    before = _measure(_baseline_sources())
    after = _measure(_current_sources())
    return {
        "schema_version": 1,
        "metric_definition": {
            "lines": "physical and nonblank Python source lines",
            "cyclomatic": (
                "AST decision count: function base 1 plus branches, boolean decisions, "
                "handlers, match cases, comprehensions, and assertions"
            ),
        },
        "before": {"revision": BASE_REVISION, **before},
        "after": after,
        "delta": _delta(before, after),
    }


def _render(value: dict[str, Any]) -> str:
    return json.dumps(value, indent=2, sort_keys=True) + "\n"


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    rendered = _render(build_metrics())
    if args.check:
        if not args.output.exists() or args.output.read_text(encoding="utf-8") != rendered:
            print(f"config structural metrics drifted: {args.output}")
            return 1
        print(f"config structural metrics: OK ({args.output})")
        return 0
    args.output.write_text(rendered, encoding="utf-8")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
