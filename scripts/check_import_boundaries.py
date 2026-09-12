#!/usr/bin/env python3
"""Check Beadhive's Python dependency direction without importing product code."""

from __future__ import annotations

import argparse
import ast
import hashlib
import sys
import tomllib
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, order=True)
class ImportEdge:
    importer: str
    importer_path: str
    imported_module: str
    symbols: tuple[str, ...]


@dataclass(frozen=True, order=True)
class DynamicImportCall:
    importer: str
    importer_path: str
    function: str


@dataclass(frozen=True)
class PackageRole:
    kind: str
    owner: str
    public: bool


@dataclass(frozen=True)
class CheckResult:
    errors: tuple[str, ...]
    files: int
    imports: int
    cyclic_components: int
    cyclic_edges: int


_REQUIRED_EXCEPTION_FIELDS = {
    "id",
    "status",
    "target_owner_package",
    "successor",
    "reason",
    "consumer_inventory",
    "executable_test",
    "test_closure",
    "introduced_commit",
    "last_verified_commit",
    "expiry_trigger",
}
_VALID_STATUSES = {"active", "removable", "removed"}
_CONSUMER_GROUPS = {"production", "tests", "docs", "external"}
_KIND_FIELDS = {
    "cycle_exception": {"importer", "importer_path", "imported_module", "symbols"},
    "boundary_exception": {"importer", "importer_path", "imported_module", "symbol"},
    "facade": {"facade_path", "preserved_symbols"},
}
_FORBIDDEN_EXTERNAL_PREFIXES = (
    "fastmcp",
    "herdr",
    "httpx",
    "opentelemetry",
    "starlette",
    "typer",
    "uvicorn",
)
_FORBIDDEN_LEGACY_PREFIXES = (
    "beadhive.cli",
    "beadhive.gateway_read",
    "beadhive.herdr_",
    "beadhive.host_daemon",
    "beadhive.mcp",
    "beadhive.operator_api",
    "beadhive.operator_sse",
    "beadhive.otel",
    "beadhive.plugins",
    "beadhive.frame_bridge",
)


def _module_name(path: Path, source_root: Path) -> str:
    parts = path.relative_to(source_root).with_suffix("").parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _resolve_from(importer: str, is_package: bool, level: int, module: str | None) -> str:
    package = importer.split(".") if is_package else importer.split(".")[:-1]
    if not level:
        return module or ""
    keep = len(package) - level + 1
    base = package[: max(keep, 0)]
    if module:
        base.extend(module.split("."))
    return ".".join(base)


def collect_imports(
    source_root: Path,
) -> tuple[dict[str, Path], tuple[ImportEdge, ...], tuple[DynamicImportCall, ...]]:
    modules = {
        _module_name(path, source_root): path
        for path in sorted(source_root.rglob("*.py"))
        if "__pycache__" not in path.parts
    }
    grouped: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    nonliteral_calls: list[DynamicImportCall] = []
    for importer, path in sorted(modules.items()):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        nodes = tuple(ast.walk(tree))
        importlib_bindings: set[str] = set()
        import_module_bindings: set[str] = set()
        for node in nodes:
            if isinstance(node, ast.Import):
                importlib_bindings.update(
                    alias.asname or "importlib" for alias in node.names if alias.name == "importlib"
                )
            elif isinstance(node, ast.ImportFrom) and node.module == "importlib":
                import_module_bindings.update(
                    alias.asname or alias.name
                    for alias in node.names
                    if alias.name == "import_module"
                )

        def record(symbol: str, importer: str = importer, path: Path = path) -> None:
            if symbol.startswith("."):
                level = len(symbol) - len(symbol.lstrip("."))
                symbol = _resolve_from(
                    importer,
                    path.name == "__init__.py",
                    level,
                    symbol[level:] or None,
                )
            parts = symbol.split(".")
            candidates = [".".join(parts[:index]) for index in range(len(parts), 0, -1)]
            imported = next((candidate for candidate in candidates if candidate in modules), None)
            imported_module = imported or parts[0]
            key = (importer, path.relative_to(source_root.parent).as_posix(), imported_module)
            grouped[key].add(symbol)

        for node in nodes:
            if isinstance(node, ast.Import):
                symbols = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = _resolve_from(
                    importer,
                    path.name == "__init__.py",
                    node.level,
                    node.module,
                )
                symbols = [f"{base}.{alias.name}" if base else alias.name for alias in node.names]
            elif isinstance(node, ast.Call):
                function = ""
                if isinstance(node.func, ast.Name) and node.func.id == "__import__":
                    function = "__import__"
                elif isinstance(node.func, ast.Name) and node.func.id in import_module_bindings:
                    function = "importlib.import_module"
                elif (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "import_module"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in importlib_bindings
                ):
                    function = "importlib.import_module"
                if not function:
                    continue
                argument = node.args[0] if node.args else None
                if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                    if _role(importer).kind != "legacy":
                        record(argument.value)
                else:
                    nonliteral_calls.append(
                        DynamicImportCall(
                            importer,
                            path.relative_to(source_root.parent).as_posix(),
                            function,
                        )
                    )
                continue
            else:
                continue
            for symbol in symbols:
                record(symbol)
    edges = tuple(
        ImportEdge(importer, importer_path, imported_module, tuple(sorted(symbols)))
        for (importer, importer_path, imported_module), symbols in sorted(grouped.items())
    )
    return modules, edges, tuple(sorted(nonliteral_calls))


def _role(module: str) -> PackageRole:
    parts = module.split(".")
    try:
        root_index = parts.index("beadhive")
    except ValueError:
        return PackageRole("external", "", True)
    tail = parts[root_index + 1 :]
    if (
        len(tail) >= 3
        and tail[0] == "modules"
        and tail[2]
        in {
            "domain",
            "contracts",
            "application",
        }
    ):
        public = not any(part.startswith("_") for part in tail[3:])
        return PackageRole(tail[2], tail[1], public)
    if len(tail) >= 2 and tail[0] == "kernel":
        offset = 3 if len(tail) >= 3 and tail[2] == "contracts" else 2
        kind = "kernel_contracts" if offset == 3 else "kernel"
        public = not any(part.startswith("_") for part in tail[offset:])
        return PackageRole(kind, tail[1], public)
    if len(tail) >= 2 and tail[0] in {"adapters", "integrations"}:
        public = not any(part.startswith("_") for part in tail[2:])
        return PackageRole(tail[0][:-1], tail[1], public)
    if tail and tail[0] == "bootstrap":
        return PackageRole("bootstrap", "bootstrap", True)
    return PackageRole("legacy", "legacy", True)


def _is_forbidden_runtime(symbol: str) -> bool:
    if symbol.startswith(_FORBIDDEN_EXTERNAL_PREFIXES):
        return True
    if symbol.startswith(_FORBIDDEN_LEGACY_PREFIXES):
        return True
    parts = symbol.split(".")
    return len(parts) >= 2 and parts[0] == "beadhive" and parts[1].endswith("_plugin")


def _direction_allowed(importer: PackageRole, imported: PackageRole) -> bool:
    if imported.kind == "external":
        return True
    if imported.kind == "legacy":
        return False
    if importer.kind == "domain":
        return imported.kind == "domain" and importer.owner == imported.owner
    if importer.kind == "contracts":
        return (imported.owner == importer.owner and imported.kind in {"domain", "contracts"}) or (
            imported.kind in {"contracts", "kernel_contracts"} and imported.public
        )
    if importer.kind == "application":
        return (
            imported.owner == importer.owner
            and imported.kind in {"domain", "contracts", "application"}
        ) or (imported.kind in {"contracts", "kernel_contracts"} and imported.public)
    if importer.kind == "kernel_contracts":
        return imported.kind in {"contracts", "kernel_contracts"} and imported.public
    if importer.kind == "kernel":
        return (
            imported.kind in {"kernel", "kernel_contracts"} and imported.owner == importer.owner
        ) or (imported.kind in {"contracts", "kernel_contracts"} and imported.public)
    if importer.kind == "adapter":
        return (
            imported.kind in {"domain", "contracts", "application", "kernel", "kernel_contracts"}
            and imported.public
        ) or (imported.kind == "adapter" and imported.owner == importer.owner)
    if importer.kind == "integration":
        return (
            imported.kind in {"domain", "contracts", "application", "kernel", "kernel_contracts"}
            and imported.public
        ) or (imported.kind == "integration" and imported.owner == importer.owner)
    if importer.kind == "bootstrap":
        return imported.kind != "bootstrap" or imported.owner == importer.owner
    return True


def _strong_components(modules: dict[str, Path], edges: tuple[ImportEdge, ...]) -> list[set[str]]:
    adjacency = {module: set() for module in modules}
    for edge in edges:
        if edge.imported_module in modules:
            adjacency[edge.importer].add(edge.imported_module)
    index = 0
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: list[set[str]] = []

    def visit(module: str) -> None:
        nonlocal index
        indices[module] = lowlinks[module] = index
        index += 1
        stack.append(module)
        on_stack.add(module)
        for imported in sorted(adjacency[module]):
            if imported not in indices:
                visit(imported)
                lowlinks[module] = min(lowlinks[module], lowlinks[imported])
            elif imported in on_stack:
                lowlinks[module] = min(lowlinks[module], indices[imported])
        if lowlinks[module] != indices[module]:
            return
        component: set[str] = set()
        while True:
            member = stack.pop()
            on_stack.remove(member)
            component.add(member)
            if member == module:
                break
        components.append(component)

    for module in sorted(modules):
        if module not in indices:
            visit(module)
    return [
        component
        for component in components
        if len(component) > 1
        or any(edge.importer == edge.imported_module == next(iter(component)) for edge in edges)
    ]


def _cyclic_edges(
    modules: dict[str, Path], edges: tuple[ImportEdge, ...]
) -> tuple[list[set[str]], tuple[ImportEdge, ...]]:
    components = _strong_components(modules, edges)
    component_by_module = {
        module: index for index, component in enumerate(components) for module in component
    }
    cyclic = tuple(
        edge
        for edge in edges
        if edge.importer in component_by_module
        and component_by_module.get(edge.importer) == component_by_module.get(edge.imported_module)
    )
    return components, cyclic


def _cycle_digest(edges: tuple[ImportEdge, ...]) -> str:
    rows = [
        f"{edge.importer_path}|{edge.imported_module}|{','.join(edge.symbols)}"
        for edge in sorted(edges)
    ]
    return hashlib.sha256((("\n".join(rows) + "\n") if rows else "").encode()).hexdigest()


def _has_wildcard(value: Any) -> bool:
    if isinstance(value, str):
        return any(character in value for character in "*?[")
    if isinstance(value, list):
        return any(_has_wildcard(item) for item in value)
    if isinstance(value, dict):
        return any(_has_wildcard(item) for item in value.values())
    return False


def _validate_metadata(kind: str, item: dict[str, Any], errors: list[str]) -> None:
    required = _REQUIRED_EXCEPTION_FIELDS | _KIND_FIELDS[kind]
    missing = sorted(required - item.keys())
    if missing:
        errors.append(f"ledger {kind} {item.get('id', '<unknown>')}: missing {', '.join(missing)}")
    if item.get("status") not in _VALID_STATUSES:
        errors.append(f"ledger {kind} {item.get('id', '<unknown>')}: invalid status")
    for field in sorted(
        required - {"status", "consumer_inventory", "symbols", "preserved_symbols"}
    ):
        if field in item and (not isinstance(item[field], str) or not item[field].strip()):
            errors.append(
                f"ledger {kind} {item.get('id', '<unknown>')}: {field} must be a non-empty string"
            )
    for field in sorted({"symbols", "preserved_symbols"} & required):
        value = item.get(field)
        if field in item and (
            not isinstance(value, list)
            or not value
            or any(not isinstance(entry, str) or not entry.strip() for entry in value)
        ):
            errors.append(
                f"ledger {kind} {item.get('id', '<unknown>')}: "
                f"{field} must be a non-empty string list"
            )
    inventory = item.get("consumer_inventory")
    if "consumer_inventory" in item and (
        not isinstance(inventory, dict)
        or set(inventory) != _CONSUMER_GROUPS
        or any(not isinstance(value, str) or not value.strip() for value in inventory.values())
    ):
        errors.append(
            f"ledger {kind} {item.get('id', '<unknown>')}: consumer_inventory must name "
            "non-empty production, tests, docs, and external groups"
        )
    if _has_wildcard(item):
        errors.append(f"ledger {kind} {item.get('id', '<unknown>')}: wildcards are forbidden")


def check(source_root: Path, ledger_path: Path) -> CheckResult:
    errors: list[str] = []
    modules, edges, nonliteral_calls = collect_imports(source_root)
    try:
        ledger = tomllib.loads(ledger_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return CheckResult((f"cannot load exception ledger {ledger_path}: {exc}",), 0, 0, 0, 0)
    if ledger.get("format_version") != 1:
        errors.append("ledger format_version must be 1")

    exception_keys: set[tuple[str, str, str, tuple[str, ...]]] = set()
    for item in ledger.get("cycle_exception", []):
        _validate_metadata("cycle_exception", item, errors)
        if item.get("status") != "active":
            continue
        key = (
            str(item.get("importer", "")),
            str(item.get("importer_path", "")),
            str(item.get("imported_module", "")),
            tuple(sorted(item.get("symbols", []))),
        )
        if key in exception_keys:
            errors.append(f"ledger cycle_exception {item.get('id')}: duplicate edge")
        exception_keys.add(key)
    boundary_exceptions: set[tuple[str, str, str, str]] = set()
    for item in ledger.get("boundary_exception", []):
        _validate_metadata("boundary_exception", item, errors)
        if item.get("status") == "active":
            boundary_exceptions.add(
                (
                    str(item.get("importer_path", "")),
                    str(item.get("importer", "")),
                    str(item.get("imported_module", "")),
                    str(item.get("symbol", "")),
                )
            )
    for item in ledger.get("facade", []):
        _validate_metadata("facade", item, errors)
        facade_path = item.get("facade_path")
        if facade_path and not (source_root.parent / str(facade_path)).is_file():
            errors.append(f"ledger facade {item.get('id')}: missing {facade_path}")

    used_boundary_exceptions: set[tuple[str, str, str, str]] = set()
    for edge in edges:
        importer_role = _role(edge.importer)
        imported_role = _role(edge.imported_module)
        if importer_role.kind != "legacy":
            for symbol in edge.symbols:
                violation = ""
                if importer_role.kind in {"domain", "application"} and _is_forbidden_runtime(
                    symbol
                ):
                    violation = (
                        f"{edge.importer_path}: forbidden {importer_role.kind} runtime import "
                        f"{edge.importer} -> {symbol}"
                    )
                elif not _direction_allowed(importer_role, imported_role):
                    violation = (
                        f"{edge.importer_path}: forbidden dependency direction "
                        f"{importer_role.kind}/{importer_role.owner} -> "
                        f"{imported_role.kind}/{imported_role.owner}: {symbol}"
                    )
                if not violation:
                    continue
                exception_key = (
                    edge.importer_path,
                    edge.importer,
                    edge.imported_module,
                    symbol,
                )
                if exception_key in boundary_exceptions:
                    used_boundary_exceptions.add(exception_key)
                else:
                    errors.append(violation)
    for call in nonliteral_calls:
        importer_role = _role(call.importer)
        if importer_role.kind != "legacy":
            errors.append(
                f"{call.importer_path}: nonliteral dynamic import cannot be verified in "
                f"{importer_role.kind}/{importer_role.owner}: {call.function}"
            )
    for key in sorted(boundary_exceptions - used_boundary_exceptions):
        errors.append(f"stale boundary exception (exact violating edge not found): {key}")

    components, current_cyclic_edges = _cyclic_edges(modules, edges)
    cycle_snapshot = ledger.get("cycle_snapshot", {})
    expected_digest = cycle_snapshot.get("sha256")
    actual_digest = _cycle_digest(current_cyclic_edges)
    if expected_digest != actual_digest:
        errors.append(
            "cycle snapshot changed: "
            f"expected {expected_digest}, got {actual_digest}; "
            "update only with reviewed exact edges"
        )
    expected_counts = {
        "components": len(components),
        "modules": sum(len(component) for component in components),
        "edges": len(current_cyclic_edges),
        "symbols": sum(len(edge.symbols) for edge in current_cyclic_edges),
    }
    for name, actual in expected_counts.items():
        if cycle_snapshot.get(name) != actual:
            errors.append(
                f"cycle snapshot {name} changed: expected {cycle_snapshot.get(name)}, got {actual}"
            )

    current_keys = {
        (edge.importer, edge.importer_path, edge.imported_module, edge.symbols)
        for edge in current_cyclic_edges
    }
    for key in sorted(exception_keys - current_keys):
        errors.append(f"stale cycle exception (edge or symbols changed): {key}")
    effective_edges = tuple(
        edge
        for edge in edges
        if (edge.importer, edge.importer_path, edge.imported_module, edge.symbols)
        not in exception_keys
    )
    remaining_components = _strong_components(modules, effective_edges)
    for component in sorted(remaining_components, key=lambda value: sorted(value)):
        errors.append("unowned import cycle: " + " -> ".join(sorted(component)))

    return CheckResult(
        tuple(sorted(set(errors))),
        len(modules),
        len(edges),
        len(components),
        len(current_cyclic_edges),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path("src"))
    parser.add_argument(
        "--ledger",
        type=Path,
        default=Path("docs/design/import-boundary-exceptions.toml"),
    )
    args = parser.parse_args(argv)
    try:
        result = check(args.source_root, args.ledger)
    except (OSError, SyntaxError) as exc:
        print(f"import-boundary-check: ERROR: {exc}", file=sys.stderr)
        return 2
    if result.errors:
        print("import-boundary-check: FAILED", file=sys.stderr)
        for error in result.errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(
        "import-boundary-check: OK "
        f"({result.files} files, {result.imports} import edges, "
        f"{result.cyclic_components} owned legacy cycles/{result.cyclic_edges} edges)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
