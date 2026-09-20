#!/usr/bin/env python3
"""Generate the checked capability-boundary closeout for bh-bptze.7."""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
import tempfile
import tomllib
from collections.abc import Iterable
from functools import cache
from pathlib import Path
from typing import Any

# Bare sibling import for `python scripts/capability_closeout.py` (scripts/ on sys.path, not
# repo root); Pants can't infer it since the module lives under the `scripts.` namespace.
# The real edge is declared explicitly in scripts/BUILD.
from check_import_boundaries import (
    _cycle_digest,  # pants: no-infer-dep
    _cyclic_edges,  # pants: no-infer-dep
    check,  # pants: no-infer-dep
    collect_imports,  # pants: no-infer-dep
)

ROOT = Path(__file__).resolve().parents[1]
SOURCE_REVISION = "ad4077d7ee5ae40c966f089920ca794006538090"
FOUNDATION_REVISION = "adf182bc4fc9c628a23b9b76c55f5291ec337b7a"
PRE_MIGRATION_REVISION = "287061f089764dac29b5f584ebcdbb5f25f86c26"
PRE_CUT_REVISION = "9dc7c10549712c42b5a38b020dcb75c26e7652c1"
AGENTS_BASE_REVISION = "5e4a76700e85b4bdda97640eeec5c2bbac632b46"
CONFIG_BASE_REVISION = "5e47c61107a67a26676fc5890a89f0fd0b715c78"
CHURN_SINCE = "2026-06-03T00:00:00Z"
MEASURED_AT = "2026-09-02T00:00:00Z"
DEFAULT_OUTPUT = ROOT / "docs/proof/bh-bptze.7-capability-closeout.json"
LEDGER_PATH = "docs/design/import-boundary-exceptions.toml"

MODULES = ("agents", "config", "hives", "planning", "state", "work", "worktrees")
AGENT_BASE_PATHS = (
    "src/beadhive/seat_contracts.py",
    "src/beadhive/agent_launch_profile.py",
    "src/beadhive/herdr_launch_profile.py",
    "src/beadhive/herdr_plugin.py",
)

MODULE_RUNS = {
    "agents": (292, 0, 0, 28.62, 31.350),
    "config": (282, 0, 1, 49.30, 51.495),
    "hives": (159, 0, 0, 30.15, 35.169),
    "planning": (173, 0, 0, 124.93, 127.998),
    "state": (88, 0, 0, 14.84, 17.102),
    "work": (465, 0, 0, 224.07, 227.467),
    "worktrees": (449, 0, 0, 54.76, 57.824),
}

CURRENT_COVERAGE = {
    "agents": (776, 848),
    "config": (1033, 1072),
    "hives": (276, 285),
    "planning": (152, 160),
    "state": (535, 554),
    "work": (211, 212),
    "worktrees": (234, 243),
}

BASELINE_EXECUTION = {
    "agents": {
        "passed": 322,
        "skipped": 0,
        "pytest_seconds": 14.28,
        "wall_seconds": 15.455,
        "covered": None,
        "statements": None,
        "percent": 82.0,
        "provenance": "docs/design/agent-launch-boundary-characterization.md",
        "note": "The source records rounded combined coverage only; no ratio is invented.",
    },
    "config": {
        "passed": 102,
        "skipped": 0,
        "pytest_seconds": 36.79,
        "wall_seconds": 38.36,
        "covered": 964,
        "statements": 1059,
        "percent": round(964 / 1059 * 100, 12),
        "provenance": "docs/design/config-module-evidence.json#unit_coverage",
    },
    "hives": {
        "collected": 690,
        "covered": 2097,
        "statements": 2234,
        "percent": 93.867502238138,
    },
    "worktrees": {
        "collected": 372,
        "covered": 2141,
        "statements": 2495,
        "percent": 85.811623246493,
    },
    "work": {
        "collected": 931,
        "covered": 2759,
        "statements": 3203,
        "percent": 86.137995629098,
    },
    "planning": {
        "collected": 231,
        "covered": 1186,
        "statements": 1336,
        "percent": 88.772455089820,
    },
    "state": {
        "collected": 109,
        "covered": 1948,
        "statements": 2175,
        "percent": 89.563218390805,
    },
}

for _name in ("hives", "worktrees", "work", "planning", "state"):
    BASELINE_EXECUTION[_name].update(
        {
            "pytest_seconds": None,
            "wall_seconds": None,
            "provenance": "docs/design/capability-module-boundaries.md",
            "note": (
                "The historical evidence recorded collection and coverage, not per-slice timing."
            ),
        }
    )


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout


@cache
def _paths(revision: str, prefix: str) -> tuple[str, ...]:
    return tuple(
        line
        for line in _git("ls-tree", "-r", "--name-only", revision, "--", prefix).splitlines()
        if line.endswith(".py")
    )


@cache
def _source(revision: str, path: str) -> str:
    return _git("show", f"{revision}:{path}")


def _module(path: str) -> str:
    parts = list(Path(path).with_suffix("").parts)
    if parts and parts[0] == "src":
        parts.pop(0)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _decision_nodes(tree: ast.AST) -> int:
    kinds = (
        ast.If,
        ast.For,
        ast.AsyncFor,
        ast.While,
        ast.Try,
        ast.ExceptHandler,
        ast.IfExp,
        ast.Match,
        ast.comprehension,
        ast.BoolOp,
    )
    return sum(isinstance(node, kinds) for node in ast.walk(tree))


@cache
def _graph(revision: str) -> tuple[dict[str, Path], tuple[Any, ...], tuple[Any, ...]]:
    with tempfile.TemporaryDirectory(prefix="bh-bptze-closeout-") as tmp:
        root = Path(tmp)
        for path in _paths(revision, "src/beadhive"):
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(_source(revision, path), encoding="utf-8")
        modules, edges, dynamic = collect_imports(root / "src")
    return modules, edges, dynamic


def _churn(revision: str, paths: tuple[str, ...]) -> dict[str, int]:
    output = _git(
        "log",
        revision,
        f"--since={CHURN_SINCE}",
        "--format=commit:%H",
        "--numstat",
        "--",
        *paths,
    )
    commits: set[str] = set()
    additions = deletions = 0
    for line in output.splitlines():
        if line.startswith("commit:"):
            commits.add(line.removeprefix("commit:"))
        else:
            fields = line.split("\t")
            if len(fields) >= 2 and fields[0].isdigit() and fields[1].isdigit():
                additions += int(fields[0])
                deletions += int(fields[1])
    return {"commits": len(commits), "additions": additions, "deletions": deletions}


def _slice_metrics(revision: str, paths: tuple[str, ...]) -> dict[str, Any]:
    selected = {_module(path) for path in paths}
    modules, edges, _ = _graph(revision)
    inbound = [
        edge for edge in edges if edge.importer not in selected and edge.imported_module in selected
    ]
    shape = {
        "python_files": len(paths),
        "physical_lines": 0,
        "nonblank_lines": 0,
        "functions": 0,
        "classes": 0,
        "decision_nodes": 0,
    }
    for path in paths:
        source = _source(revision, path)
        tree = ast.parse(source, filename=path)
        shape["physical_lines"] += len(source.splitlines())
        shape["nonblank_lines"] += sum(bool(line.strip()) for line in source.splitlines())
        shape["functions"] += sum(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) for node in ast.walk(tree)
        )
        shape["classes"] += sum(isinstance(node, ast.ClassDef) for node in ast.walk(tree))
        shape["decision_nodes"] += _decision_nodes(tree)
    return {
        "revision": revision,
        "source_paths": list(paths),
        "shape": shape,
        "fan_in": {
            "distinct_external_importers": len({edge.importer for edge in inbound}),
            "import_edges": len(inbound),
            "imported_symbols": sum(len(edge.symbols) for edge in inbound),
        },
        "churn_since": CHURN_SINCE,
        "churn": _churn(revision, paths),
    }


def _current_graph(ledger: dict[str, Any]) -> dict[str, Any]:
    modules, edges, dynamic = _graph(SOURCE_REVISION)
    components, cyclic = _cyclic_edges(modules, edges)
    active = [row for row in ledger["cycle_exception"] if row["status"] == "active"]
    cycles = []
    for component in sorted(components, key=lambda item: (-len(item), sorted(item))):
        edges_here = [
            edge
            for edge in cyclic
            if edge.importer in component and edge.imported_module in component
        ]
        exception_ids = sorted(
            row["id"]
            for row in active
            if row["importer"] in component and row["imported_module"] in component
        )
        owner, followup, rationale = _cycle_disposition(component)
        cycles.append(
            {
                "size": len(component),
                "members": sorted(component),
                "edges": len(edges_here),
                "symbols": sum(len(edge.symbols) for edge in edges_here),
                "active_exception_ids": exception_ids,
                "owner": owner,
                "followup": followup,
                "rationale": rationale,
            }
        )
    return {
        "python_modules": len(modules),
        "import_edges": len(edges),
        "nonliteral_dynamic_sites": len(dynamic),
        "scc_count": len(components),
        "cyclic_modules": sum(len(component) for component in components),
        "scc_sizes": [len(row) for row in components]
        if not cycles
        else [row["size"] for row in cycles],
        "cyclic_edges": len(cyclic),
        "cyclic_symbols": sum(len(edge.symbols) for edge in cyclic),
        "cycle_digest": _cycle_digest(cyclic),
        "cycles": cycles,
    }


def _cycle_disposition(component: set[str]) -> tuple[str, str, str]:
    if "beadhive.bd" in component:
        return (
            "legacy core and capability-compatibility maintainers",
            "bh-8kn42 raw-bd seam, then a separately planned consumer-zero cycle cut",
            "The remaining flat-package composition and compatibility facades cannot be deleted "
            "by this acceptance-only bead; all 24 retained feedback exceptions are exact.",
        )
    if "beadhive.host_daemon" in component:
        return (
            "bh-q0lol unified-host-daemon owners",
            "consume module contracts while retiring host transport feedback edges",
            "Transport composition remains outward and is owned by the active host-daemon work.",
        )
    if "beadhive.storage_migrate" in component:
        return (
            "hive/storage adapter maintainers",
            "bh-8kn42 raw-bd seam, followed by storage-adapter consumer-zero cleanup",
            "Backup, hub, HQ, hive, onboarding, and migration still share durable-store "
            "composition.",
        )
    if "beadhive.herdr_plugin" in component:
        return (
            "bh-5wuc0 Herdr compatibility maintainers",
            "retire cycle-edge-004 only after the documented facade consumer-zero gate",
            "The public Herdr patch/import surface remains an explicit compatibility contract.",
        )
    if "beadhive.localloop" in component:
        return (
            "local runtime maintainers",
            "separate runtime policy from the local-loop adapter before cycle-edge-026 expires",
            "The runtime tier facade still delegates to the local execution loop.",
        )
    if "beadhive.report" in component:
        return (
            "planning/report compatibility maintainers",
            "move report projection behind the planning read port before cycle-edge-036 expires",
            "Report and triage retain a two-way compatibility seam outside the pure planning "
            "module.",
        )
    if "beadhive.state_stream_polling" in component:
        return (
            "state adapter maintainers",
            "inject the epic-schedule projection into polling before cycle-edge-033 expires",
            "The pure state module is inward; only the legacy polling/schedule adapters remain "
            "cyclic.",
        )
    if "beadhive.work_show" in component:
        return (
            "work compatibility maintainers",
            "move review presentation behind a work read port before cycle-edge-037 expires",
            "The public work facade and review projection retain one checked feedback edge.",
        )
    raise AssertionError(f"unowned cycle: {sorted(component)}")


def _closure_rows() -> dict[str, Any]:
    registry = tomllib.loads(_git("show", f"{SOURCE_REVISION}:tests/closures.toml"))
    rows = {}
    for module in MODULES:
        closure = next(row for row in registry["closures"] if row["id"] == f"module.{module}")
        passed, skipped, warnings, pytest_seconds, wall_seconds = MODULE_RUNS[module]
        covered, statements = CURRENT_COVERAGE[module]
        rows[module] = {
            "id": closure["id"],
            "status": closure["status"],
            "owner_path": closure["owner_path"],
            "source_paths": closure["source_paths"],
            "tests": closure["tests"],
            "shared_contracts": closure["shared_contracts"],
            "shared_contract_tests": closure["shared_contract_tests"],
            "reverse_dependencies": closure["reverse_dependencies"],
            "reverse_dependency_tests": closure["reverse_dependency_tests"],
            "selector_counts": {
                "direct": len(closure["tests"]),
                "shared_contract": len(closure["shared_contract_tests"]),
                "reverse_dependent": len(closure["reverse_dependency_tests"]),
            },
            "execution": {
                "command": f"time just test-module {module}",
                "passed": passed,
                "skipped": skipped,
                "warnings": warnings,
                "pytest_seconds": pytest_seconds,
                "wall_seconds": wall_seconds,
            },
            "coverage": {
                "covered": covered,
                "statements": statements,
                "missing": statements - covered,
                "percent": round(covered / statements * 100, 12),
            },
        }
    return rows


def _baseline_metrics(module: str, baseline_map: dict[str, Any]) -> dict[str, Any]:
    if module == "agents":
        structural = _slice_metrics(AGENTS_BASE_REVISION, AGENT_BASE_PATHS)
    elif module == "config":
        structural = _slice_metrics(
            CONFIG_BASE_REVISION,
            _paths(CONFIG_BASE_REVISION, "src/beadhive/modules/config"),
        )
    else:
        row = baseline_map["slices"][module]
        structural = {
            "revision": PRE_MIGRATION_REVISION,
            "source_paths": row["source_paths"],
            "shape": {"python_files": len(row["source_paths"]), **row["shape"]},
            "fan_in": row["fan_in"],
            "churn_since": CHURN_SINCE,
            "churn": row["churn"],
        }
    return {**structural, "execution_and_coverage": BASELINE_EXECUTION[module]}


def build_proof() -> dict[str, Any]:
    baseline_map = json.loads(
        (ROOT / "docs/design/capability-module-dependency-map.json").read_text(encoding="utf-8")
    )
    # Reproduce the historical proof from one coherent snapshot.  Current active exceptions
    # describe today's source graph and must not be evaluated against the pinned .7 source.
    ledger_source = _source(SOURCE_REVISION, LEDGER_PATH)
    ledger = tomllib.loads(ledger_source)
    # The ledger file at the measured commit necessarily names its predecessor: a commit cannot
    # contain its own hash.  The checked proof records the immutable source revision that this
    # historical ledger snapshot verifies.
    ledger["verified_commit"] = SOURCE_REVISION
    graph = _current_graph(ledger)
    with tempfile.TemporaryDirectory(prefix="bh-bptze-check-") as tmp:
        root = Path(tmp)
        for path in _paths(SOURCE_REVISION, "src/beadhive"):
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(_source(SOURCE_REVISION, path), encoding="utf-8")
        ledger_path = root / LEDGER_PATH
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        ledger_path.write_text(ledger_source, encoding="utf-8")
        checked = check(root / "src", ledger_path)
    if checked.errors:
        raise RuntimeError("; ".join(checked.errors))
    closures = _closure_rows()
    modules = {}
    for module in MODULES:
        current = _slice_metrics(
            SOURCE_REVISION, _paths(SOURCE_REVISION, f"src/beadhive/modules/{module}")
        )
        modules[module] = {
            "before": _baseline_metrics(module, baseline_map),
            "after": {**current, **closures[module]},
        }
    active_cycles = [row for row in ledger["cycle_exception"] if row["status"] == "active"]
    removed_cycles = [row for row in ledger["cycle_exception"] if row["status"] == "removed"]
    return {
        "schema_version": 1,
        "scope": "bh-bptze.7 capability-boundary acceptance closeout",
        "measured_at_utc": MEASURED_AT,
        "measured_source_revision": SOURCE_REVISION,
        "definitions": {
            "fan_in": (
                "distinct source modules outside the selected slice with a static import into it"
            ),
            "complexity": (
                "AST decision nodes: if/for/while/try/handler/if-expression/match/"
                "comprehension/bool-op"
            ),
            "churn": f"Git numstat at each exact revision since {CHURN_SINCE}",
            "coverage": "non-integration statement coverage; no branch or per-test contexts",
            "proof_commit": (
                "The proof child changes only docs/scripts/tests; source metrics are pinned to "
                "its immutable parent source commit."
            ),
        },
        "repository_history": [
            {
                "label": "modular foundation",
                "revision": FOUNDATION_REVISION,
                "modules": 182,
                "edges": 2029,
                "scc_count": 6,
                "cyclic_modules": 85,
                "largest_scc": 65,
                "cyclic_edges": 288,
                "cyclic_symbols": 318,
                "provenance": "docs/proof/bh-inqwc.6-modular-baseline.md",
            },
            {
                "label": "capability pre-migration",
                "revision": PRE_MIGRATION_REVISION,
                "modules": 232,
                "edges": 2282,
                "scc_count": 6,
                "cyclic_modules": 85,
                "largest_scc": 65,
                "cyclic_edges": 278,
                "cyclic_symbols": 308,
                "provenance": "docs/design/capability-module-dependency-map.json",
            },
            {
                "label": "pre-runtime-registry cut",
                "revision": PRE_CUT_REVISION,
                "modules": 281,
                "edges": 2463,
                "scc_count": 5,
                "cyclic_modules": 82,
                "largest_scc": 64,
                "cyclic_edges": 272,
                "cyclic_symbols": 298,
                "provenance": (
                    "docs/proof/bh-bptze.14-plugin-runtime-registry.md and exact AST rerun"
                ),
            },
            {"label": "closeout source", "revision": SOURCE_REVISION, **graph},
        ],
        "material_reduction": {
            "historical_largest_scc": {"before": 65, "after": 35, "delta": -30},
            "historical_cyclic_modules": {"before": 85, "after": 59, "delta": -26},
            "historical_cyclic_edges": {"before": 288, "after": 155, "delta": -133},
            "immediate_largest_scc": {"before": 64, "after": 35, "delta": -29},
        },
        "closure_registry": {
            "present": 23,
            "absent": 0,
            "full_gate": "just check",
            "release_gate": "just check-all",
        },
        "exception_ledger": {
            "verified_commit": ledger["verified_commit"],
            "active_cycle_exceptions": len(active_cycles),
            "removed_cycle_audit_records": len(removed_cycles),
            "removed_ids": sorted(row["id"] for row in removed_cycles),
            "active_boundary_exceptions": sum(
                row["status"] == "active" for row in ledger["boundary_exception"]
            ),
            "active_facades": sum(row["status"] == "active" for row in ledger["facade"]),
            "resolved_entry_policy": (
                "Resolved entries are excluded from active exceptions and retained with "
                "status=removed as immutable audit records."
            ),
            "architecture_check_errors": list(checked.errors),
        },
        "modules": modules,
        "repository_execution": {
            "before": {
                "revision": PRE_MIGRATION_REVISION,
                "command": "COVERAGE_FILE=/tmp/bh-bptze-1.coverage just cov",
                "selected": 7448,
                "passed": 7436,
                "skipped": 12,
                "pytest_seconds": 269.12,
                "wall_seconds": None,
                "covered": 38592,
                "statements": 43477,
                "percent": 88.76417416105068,
                "note": "The historical source records pytest timing but not shell wall timing.",
            },
            "after": {
                "revision": SOURCE_REVISION,
                "command": "time env COVERAGE_FILE=/tmp/bh-bptze-7.coverage just cov",
                "selected": 7657,
                "passed": 7645,
                "skipped": 12,
                "warnings": 1,
                "pytest_seconds": 304.25,
                "wall_seconds": 307.011,
                "covered": 40679,
                "statements": 45826,
                "missing": 5147,
                "percent": 88.76838475974338,
            },
        },
    }


def _render(value: dict[str, Any]) -> str:
    return json.dumps(value, indent=2, sort_keys=True) + "\n"


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    rendered = _render(build_proof())
    if args.check:
        if not args.output.exists() or args.output.read_text(encoding="utf-8") != rendered:
            print(f"capability closeout drifted: {args.output}")
            return 1
        print(f"capability closeout: OK ({args.output})")
        return 0
    args.output.write_text(rendered, encoding="utf-8")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
