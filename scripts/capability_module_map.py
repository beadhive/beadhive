#!/usr/bin/env python3
"""Generate the exact pre-migration capability dependency map for bh-bptze.1."""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
import tempfile
from collections import Counter
from collections.abc import Iterable
from functools import cache
from pathlib import Path
from typing import Any

from check_import_boundaries import ImportEdge, _cyclic_edges, collect_imports

ROOT = Path(__file__).resolve().parents[1]
SOURCE_REVISION = "287061f089764dac29b5f584ebcdbb5f25f86c26"
DEFAULT_OUTPUT = ROOT / "docs/design/capability-module-dependency-map.json"
CHURN_SINCE = "2026-06-03T00:00:00Z"

SLICES = {
    "hives": (
        "src/beadhive/hive.py",
        "src/beadhive/hive_identity.py",
        "src/beadhive/hive_ready.py",
        "src/beadhive/onboard.py",
        "src/beadhive/registry.py",
        "src/beadhive/retire.py",
    ),
    "worktrees": (
        "src/beadhive/gitworkspace.py",
        "src/beadhive/gitworkspace_plugin.py",
        "src/beadhive/worktree.py",
        "src/beadhive/worktree_cleanup.py",
        "src/beadhive/worktree_git.py",
        "src/beadhive/worktree_inventory.py",
        "src/beadhive/worktree_merge.py",
        "src/beadhive/worktree_verify.py",
        "src/beadhive/wt_status.py",
    ),
    "work": (
        "src/beadhive/validate.py",
        "src/beadhive/validation_admission.py",
        "src/beadhive/work.py",
        "src/beadhive/work_assignment.py",
        "src/beadhive/work_dispatch.py",
        "src/beadhive/work_group.py",
        "src/beadhive/work_guards.py",
        "src/beadhive/work_intake.py",
        "src/beadhive/work_logic.py",
        "src/beadhive/work_merge.py",
        "src/beadhive/work_metrics.py",
        "src/beadhive/work_next.py",
        "src/beadhive/work_reads.py",
        "src/beadhive/work_refine.py",
        "src/beadhive/work_show.py",
        "src/beadhive/work_submission.py",
    ),
    "planning": (
        "src/beadhive/molecule.py",
        "src/beadhive/plan.py",
        "src/beadhive/plan_repair.py",
        "src/beadhive/report.py",
        "src/beadhive/report_target.py",
        "src/beadhive/triage.py",
        "src/beadhive/triage_store.py",
    ),
    "state": (
        "src/beadhive/agent_run_summary.py",
        "src/beadhive/agent_run_summary_reader.py",
        "src/beadhive/public_readers.py",
        "src/beadhive/run_journal.py",
        "src/beadhive/state.py",
        "src/beadhive/state_stream.py",
        "src/beadhive/state_stream_epic_schedule.py",
        "src/beadhive/state_stream_gate_projection.py",
        "src/beadhive/state_stream_polling.py",
        "src/beadhive/state_stream_process.py",
        "src/beadhive/validation_ledger.py",
        "src/beadhive/validation_records.py",
    ),
}

TEST_PATTERNS = {
    "hives": ("hive", "onboard", "registry", "retire"),
    "worktrees": ("worktree", "wt_status", "workspace_config"),
    "work": ("work", "validation_admission"),
    "planning": ("molecule", "plan", "report", "triage"),
    "state": ("state", "validation_ledger", "validation_records"),
}


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@cache
def _revision_files(prefix: str) -> tuple[str, ...]:
    output = _git("ls-tree", "-r", "--name-only", SOURCE_REVISION, "--", prefix)
    return tuple(path for path in output.splitlines() if path.endswith(".py"))


@cache
def _source(path: str) -> str:
    return _git("show", f"{SOURCE_REVISION}:{path}")


def _module(path: str) -> str:
    parts = Path(path).with_suffix("").parts
    parts = parts[1:] if parts and parts[0] == "src" else parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _resolve_from(importer: str, is_package: bool, level: int, module: str | None) -> str:
    package = importer.split(".") if is_package else importer.split(".")[:-1]
    if not level:
        return module or ""
    base = package[: max(len(package) - level + 1, 0)]
    if module:
        base.extend(module.split("."))
    return ".".join(base)


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


def _materialized_import_graph() -> tuple[
    dict[str, Path], tuple[ImportEdge, ...], tuple[Any, ...], list[set[str]], tuple[ImportEdge, ...]
]:
    with tempfile.TemporaryDirectory(prefix="bh-bptze-map-") as tmp:
        source_root = Path(tmp) / "src"
        for path in _revision_files("src/beadhive"):
            destination = Path(tmp) / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(_source(path), encoding="utf-8")
        modules, edges, dynamic = collect_imports(source_root)
        components, cyclic = _cyclic_edges(modules, edges)
    return modules, edges, dynamic, components, cyclic


def _churn(paths: tuple[str, ...]) -> dict[str, int]:
    output = _git(
        "log",
        SOURCE_REVISION,
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
            continue
        fields = line.split("\t")
        if len(fields) >= 3 and fields[0].isdigit() and fields[1].isdigit():
            additions += int(fields[0])
            deletions += int(fields[1])
    return {"commits": len(commits), "additions": additions, "deletions": deletions}


def _edge_rows(edges: Iterable[ImportEdge]) -> list[dict[str, Any]]:
    return [
        {
            "importer": edge.importer,
            "imported_module": edge.imported_module,
            "symbols": list(edge.symbols),
        }
        for edge in sorted(edges)
    ]


def _call_name(node: ast.AST) -> tuple[str, ...]:
    if isinstance(node, ast.Name):
        return (node.id,)
    if isinstance(node, ast.Attribute):
        return (*_call_name(node.value), node.attr)
    return ()


def _call_coupling(
    all_paths: tuple[str, ...], modules: set[str], selected: set[str]
) -> dict[str, Any]:
    inbound: Counter[tuple[str, str, str]] = Counter()
    outbound: Counter[tuple[str, str, str]] = Counter()
    for path in all_paths:
        importer = _module(path)
        tree = ast.parse(_source(path), filename=path)
        aliases: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    aliases[alias.asname or alias.name.split(".")[0]] = alias.name
            elif isinstance(node, ast.ImportFrom):
                base = _resolve_from(
                    importer,
                    path.endswith("/__init__.py"),
                    node.level,
                    node.module,
                )
                for alias in node.names:
                    target = f"{base}.{alias.name}" if base else alias.name
                    aliases[alias.asname or alias.name] = target
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            parts = _call_name(node.func)
            if not parts or parts[0] not in aliases:
                continue
            target = ".".join((aliases[parts[0]], *parts[1:]))
            candidates = sorted(
                (
                    module
                    for module in modules
                    if target == module or target.startswith(module + ".")
                ),
                key=len,
                reverse=True,
            )
            if not candidates:
                continue
            target_module = candidates[0]
            symbol = target.removeprefix(target_module).lstrip(".") or "<module>"
            key = (importer, target_module, symbol)
            if importer not in selected and target_module in selected:
                inbound[key] += 1
            elif importer in selected and target_module not in selected:
                outbound[key] += 1

    def rows(counter: Counter[tuple[str, str, str]]) -> list[dict[str, Any]]:
        return [
            {"caller": caller, "target_module": target, "symbol": symbol, "sites": count}
            for (caller, target, symbol), count in sorted(counter.items())
        ]

    return {
        "inbound_sites": sum(inbound.values()),
        "outbound_sites": sum(outbound.values()),
        "inbound": rows(inbound),
        "outbound": rows(outbound),
    }


def _test_inventory(slice_name: str) -> tuple[str, ...]:
    patterns = TEST_PATTERNS[slice_name]
    return tuple(
        path
        for path in _revision_files("tests")
        if Path(path).name.startswith("test_")
        and any(pattern in Path(path).stem for pattern in patterns)
    )


def _import_aliases(path: str, tree: ast.AST) -> dict[str, str]:
    importer = _module(path)
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                aliases[alias.asname or alias.name.split(".")[0]] = alias.name
        elif isinstance(node, ast.ImportFrom):
            base = _resolve_from(
                importer,
                path.endswith("/__init__.py"),
                node.level,
                node.module,
            )
            for alias in node.names:
                target = f"{base}.{alias.name}" if base else alias.name
                aliases[alias.asname or alias.name] = target
    return aliases


@cache
def _source_namespaces() -> dict[str, dict[str, str]]:
    namespaces: dict[str, dict[str, str]] = {}
    for path in _revision_files("src/beadhive"):
        tree = ast.parse(_source(path), filename=path)
        namespaces[_module(path)] = _import_aliases(path, tree)
    return namespaces


def _resolved_reference(
    node: ast.AST,
    aliases: dict[str, str],
    namespaces: dict[str, dict[str, str]],
) -> str | None:
    parts = _call_name(node)
    if not parts or parts[0] not in aliases:
        return None
    resolved = aliases[parts[0]]
    for part in parts[1:]:
        resolved = namespaces.get(resolved, {}).get(part, f"{resolved}.{part}")
    return resolved


def _string(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _selected_target(reference: str | None, selected: set[str]) -> tuple[str, str] | None:
    if reference is None:
        return None
    candidates = [
        module for module in selected if reference == module or reference.startswith(f"{module}.")
    ]
    if not candidates:
        return None
    module = max(candidates, key=len)
    return module, reference.removeprefix(module).lstrip(".") or "<module>"


def _dynamic_seams_in_source(
    source: str,
    path: str,
    selected: set[str],
    namespaces: dict[str, dict[str, str]],
) -> list[dict[str, Any]]:
    tree = ast.parse(source, filename=path)
    aliases = _import_aliases(path, tree)
    records: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        raw_call = ".".join(_call_name(node.func))
        resolved_call = _resolved_reference(node.func, aliases, namespaces)
        operation = ""
        reference: str | None = None
        if raw_call in {"monkeypatch.setattr", "monkeypatch.delattr"} and node.args:
            operation = raw_call
            reference = _string(node.args[0]) or _resolved_reference(
                node.args[0], aliases, namespaces
            )
            if _string(node.args[0]) is None and len(node.args) > 1:
                attribute = _string(node.args[1])
                reference = f"{reference}.{attribute}" if reference and attribute else None
        elif resolved_call in {"unittest.mock.patch", "mock.patch"} and node.args:
            operation = "patch"
            reference = _string(node.args[0])
        elif (
            resolved_call in {"unittest.mock.patch.object", "mock.patch.object"}
            or raw_call in {"mocker.patch.object"}
        ) and len(node.args) > 1:
            operation = "patch.object"
            reference = _resolved_reference(node.args[0], aliases, namespaces)
            attribute = _string(node.args[1])
            reference = f"{reference}.{attribute}" if reference and attribute else None
        elif raw_call == "mocker.patch" and node.args:
            operation = "mocker.patch"
            reference = _string(node.args[0])
        elif raw_call == "getattr" and len(node.args) > 1:
            operation = "getattr"
            reference = _resolved_reference(node.args[0], aliases, namespaces)
            attribute = _string(node.args[1])
            reference = f"{reference}.{attribute}" if reference and attribute else None
        elif resolved_call == "importlib.import_module" and node.args:
            operation = "importlib.import_module"
            reference = _string(node.args[0])
        elif raw_call == "__import__" and node.args:
            operation = "__import__"
            reference = _string(node.args[0])
        target = _selected_target(reference, selected)
        if target is None:
            continue
        target_module, target_symbol = target
        expression = ast.get_source_segment(source, node) or ""
        records.append(
            {
                "path": path,
                "line": node.lineno,
                "call": raw_call,
                "operation": operation,
                "target_module": target_module,
                "target_symbol": target_symbol,
                "expression": " ".join(expression.split())[:500],
            }
        )
    return records


def _dynamic_test_seams(paths: tuple[str, ...], selected: set[str]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    namespaces = _source_namespaces()
    for path in paths:
        source = _source(path)
        records.extend(_dynamic_seams_in_source(source, path, selected, namespaces))
    return sorted(records, key=lambda row: (row["path"], row["line"], row["expression"]))


def _slice_metrics(
    name: str,
    paths: tuple[str, ...],
    all_paths: tuple[str, ...],
    all_test_paths: tuple[str, ...],
    modules: dict[str, Path],
    edges: tuple[ImportEdge, ...],
    components: list[set[str]],
) -> dict[str, Any]:
    selected = {_module(path) for path in paths}
    physical = nonblank = functions = classes = decisions = 0
    for path in paths:
        source = _source(path)
        physical += len(source.splitlines())
        nonblank += sum(bool(line.strip()) for line in source.splitlines())
        tree = ast.parse(source, filename=path)
        functions += sum(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) for node in ast.walk(tree)
        )
        classes += sum(isinstance(node, ast.ClassDef) for node in ast.walk(tree))
        decisions += _decision_nodes(tree)
    inbound = tuple(
        edge for edge in edges if edge.importer not in selected and edge.imported_module in selected
    )
    outbound = tuple(
        edge
        for edge in edges
        if edge.importer in selected
        and edge.imported_module in modules
        and edge.imported_module not in selected
    )
    sccs = [sorted(component) for component in components if component & selected]
    tests = _test_inventory(name)
    return {
        "source_paths": list(paths),
        "source_modules": sorted(selected),
        "shape": {
            "physical_lines": physical,
            "nonblank_lines": nonblank,
            "functions": functions,
            "classes": classes,
            "decision_nodes": decisions,
        },
        "churn_since": CHURN_SINCE,
        "churn": _churn(paths),
        "fan_in": {
            "distinct_external_importers": len({edge.importer for edge in inbound}),
            "import_edges": len(inbound),
            "imported_symbols": sum(len(edge.symbols) for edge in inbound),
        },
        "inbound_imports": _edge_rows(inbound),
        "outbound_imports": _edge_rows(outbound),
        "call_coupling": _call_coupling(all_paths, set(modules), selected),
        "owned_scc_intersections": sccs,
        "current_test_files": list(tests),
        "dynamic_test_scope": {
            "python_files": len(all_test_paths),
            "paths_ref": "repository.test_inventory.python_paths",
        },
        "dynamic_test_seams": _dynamic_test_seams(all_test_paths, selected),
    }


def build_map() -> dict[str, Any]:
    all_paths = _revision_files("src/beadhive")
    all_test_paths = _revision_files("tests")
    modules, edges, dynamic, components, cyclic = _materialized_import_graph()
    slices = {
        name: _slice_metrics(
            name,
            paths,
            all_paths,
            all_test_paths,
            modules,
            edges,
            components,
        )
        for name, paths in SLICES.items()
    }
    return {
        "schema_version": 1,
        "scope": "bh-bptze.1 exact-tip pre-migration capability map",
        "measured_revision": SOURCE_REVISION,
        "definitions": {
            "fan_in": "distinct source modules outside the slice with a static import into it",
            "call_coupling": "AST call sites resolved through explicit import aliases",
            "churn": f"git numstat at the measured revision since {CHURN_SINCE}",
            "dynamic_test_seams": (
                "AST-semantically resolved monkeypatch/patch/getattr/import targets across every "
                "exact-revision Python test/support file; current_test_files is the narrower "
                "legacy characterization closure"
            ),
            "coverage": "recorded separately in the human evidence because it is executed data",
        },
        "repository": {
            "python_modules": len(modules),
            "import_edges": len(edges),
            "dynamic_import_calls": [
                {
                    "importer": call.importer,
                    "path": call.importer_path,
                    "function": call.function,
                }
                for call in dynamic
            ],
            "owned_cyclic_sccs": len(components),
            "cyclic_modules": sum(len(component) for component in components),
            "scc_sizes": sorted((len(component) for component in components), reverse=True),
            "sccs": [
                sorted(component)
                for component in sorted(components, key=lambda c: (-len(c), sorted(c)))
            ],
            "cyclic_edges": len(cyclic),
            "cyclic_symbols": sum(len(edge.symbols) for edge in cyclic),
            "test_inventory": {
                "python_files": len(all_test_paths),
                "python_paths": list(all_test_paths),
            },
        },
        "slices": slices,
    }


def _render(value: dict[str, Any]) -> str:
    return json.dumps(value, indent=2, sort_keys=True) + "\n"


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    rendered = _render(build_map())
    if args.check:
        if not args.output.exists() or args.output.read_text(encoding="utf-8") != rendered:
            print(f"capability module map drifted: {args.output}")
            return 1
        print(f"capability module map: OK ({args.output})")
        return 0
    args.output.write_text(rendered, encoding="utf-8")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
