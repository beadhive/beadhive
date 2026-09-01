#!/usr/bin/env python3
"""Generate the exact legacy-config caller and patch-point migration ledger."""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LEDGER = ROOT / "docs/design/config-consumer-migration-ledger.json"


def _imports_name(tree: ast.AST, name: str) -> bool:
    absolute = f"beadhive.{name}"
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(
            alias.name == absolute or alias.name.startswith(f"{absolute}.") for alias in node.names
        ):
            return True
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.module == "beadhive" and any(alias.name == name for alias in node.names):
            return True
        if node.module == absolute or (node.level and node.module == name):
            return True
        if node.level and node.module is None and any(alias.name == name for alias in node.names):
            return True
    return False


def _python_files(directory: Path) -> list[Path]:
    return sorted(path for path in directory.rglob("*.py") if "__pycache__" not in path.parts)


def _relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _importers(directory: Path, name: str) -> list[str]:
    result = []
    for path in _python_files(directory):
        if _imports_name(ast.parse(path.read_text(encoding="utf-8")), name):
            result.append(_relative(path))
    return result


def _string_constants(call: ast.Call) -> list[tuple[int, str]]:
    values = []
    for node in (*call.args, *(keyword.value for keyword in call.keywords)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            values.append((node.lineno, node.value))
    return values


def _module_name(path: Path) -> str:
    relative = path.relative_to(ROOT / "src").with_suffix("")
    parts = relative.parts[:-1] if relative.name == "__init__" else relative.parts
    return ".".join(parts)


def _adapter_aliases() -> dict[str, dict[str, str]]:
    """Map production ``module.attribute`` seams to their dynamic facade target."""
    routes: dict[str, dict[str, str]] = {}
    for path in _python_files(ROOT / "src/beadhive"):
        aliases: dict[str, str] = {}
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.module != "config_consumer_ports" and node.module != (
                "beadhive.config_consumer_ports"
            ):
                continue
            for alias in node.names:
                aliases[alias.asname or alias.name] = "beadhive.config"
        if aliases:
            routes[_module_name(path)] = aliases
    return routes


def _beadhive_module_imports(tree: ast.AST) -> dict[str, str]:
    imports: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("beadhive."):
                    imports[alias.asname or alias.name] = alias.name
        elif (
            isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("beadhive")
        ):
            for alias in node.names:
                imports[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return imports


def _attribute_parts(node: ast.AST) -> tuple[str, ...] | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return tuple(reversed(parts))


def _resolve_imported_expression(node: ast.AST, module_imports: dict[str, str]) -> str | None:
    parts = _attribute_parts(node)
    if parts is None:
        return None
    for length in range(len(parts), 0, -1):
        prefix = ".".join(parts[:length])
        imported = module_imports.get(prefix)
        if imported is not None:
            suffix = ".".join(parts[length:])
            return f"{imported}.{suffix}" if suffix else imported
    return None


def _indirect_adapter_patch(
    receiver: ast.AST,
    attribute: str,
    module_imports: dict[str, str],
    adapter_aliases: dict[str, dict[str, str]],
) -> tuple[str, str] | None:
    resolved = _resolve_imported_expression(receiver, module_imports)
    if resolved is None or "." not in resolved:
        return None
    module, adapter_attribute = resolved.rsplit(".", 1)
    facade = adapter_aliases.get(module, {}).get(adapter_attribute)
    if facade is None:
        return None
    return f"{module}.{adapter_attribute}.{attribute}", f"{facade}.{attribute}"


def _patch_points() -> list[dict[str, Any]]:
    points: set[tuple[str, int, str, str, str]] = set()
    adapter_aliases = _adapter_aliases()
    for path in _python_files(ROOT / "tests"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        module_imports = _beadhive_module_imports(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for line, value in _string_constants(node):
                if value == "beadhive.config" or value.startswith("beadhive.config."):
                    points.add((_relative(path), line, value, value, "string-facade"))
            if not (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "setattr"
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)
            ):
                continue
            attribute = node.args[1].value
            receiver = node.args[0]
            if isinstance(receiver, ast.Name) and receiver.id in {
                "config",
                "config_mod",
                "cfg_mod",
                "_config",
            }:
                target = f"beadhive.config.{attribute}"
                points.add((_relative(path), node.lineno, target, target, "direct-facade"))
                continue
            indirect = _indirect_adapter_patch(receiver, attribute, module_imports, adapter_aliases)
            if indirect is not None:
                target, facade_target = indirect
                points.add(
                    (
                        _relative(path),
                        node.lineno,
                        target,
                        facade_target,
                        "indirect-adapter",
                    )
                )
    return [
        {
            "path": path,
            "line": line,
            "target": target,
            "facade_target": facade_target,
            "kind": kind,
        }
        for path, line, target, facade_target, kind in sorted(points)
    ]


def build_ledger() -> dict[str, Any]:
    production_config = _importers(ROOT / "src/beadhive", "config")
    production_schema = _importers(ROOT / "src/beadhive", "config_schema")
    test_config = _importers(ROOT / "tests", "config")
    test_schema = _importers(ROOT / "tests", "config_schema")
    patch_points = _patch_points()
    return {
        "schema_version": 2,
        "baseline": {
            "revision": "5e47c61107a67a26676fc5890a89f0fd0b715c78",
            "production_config_importers": 95,
            "test_config_importers": 135,
            "historical_test_blast_radius": 129,
            "production_config_schema_importers": 8,
        },
        "current": {
            "production_config_importers": production_config,
            "production_config_importer_count": len(production_config),
            "production_config_schema_importers": production_schema,
            "production_config_schema_importer_count": len(production_schema),
            "test_config_importers": test_config,
            "test_config_importer_count": len(test_config),
            "test_config_schema_importers": test_schema,
            "test_config_schema_importer_count": len(test_schema),
            "config_patch_points": patch_points,
            "config_patch_point_count": len(patch_points),
            "remaining_broad_dependencies": [
                {
                    "path": path,
                    "owner": "bh-18hud.5 compatibility migration ledger",
                    "rationale": (
                        "retained legacy capability outside the work/telemetry/plugin migration "
                        "batch; removal requires a separately bounded port and its reverse-"
                        "dependent compatibility closure"
                    ),
                }
                for path in production_config
            ],
        },
        "compatibility": {
            "facades_retained": ["beadhive.config", "beadhive.config_schema"],
            "successor_contracts": "beadhive.modules.config.contracts",
            "consumer_port": (
                "beadhive.modules.config.application.consumer_settings.CapabilitySettings"
            ),
            "legacy_adapter": "beadhive.config_consumer_ports",
        },
        "removal_prerequisites": [
            "production_config_importers contains only the approved outward legacy adapter",
            "production_config_schema_importers is empty",
            (
                "config_patch_point_count equals the generated config_patch_points length, and "
                "the list is empty or every remaining point has an approved replacement contract"
            ),
            (
                "CLI, MCP, YAML, environment, schema, facade, and integration compatibility "
                "closures are green"
            ),
            "a separate reviewed compatibility change authorizes facade removal",
        ],
        "selective_ci_graduation_claimed": False,
    }


def _render(ledger: dict[str, Any]) -> str:
    return json.dumps(ledger, indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--output", type=Path, default=DEFAULT_LEDGER)
    args = parser.parse_args()
    rendered = _render(build_ledger())
    if args.check:
        if not args.output.exists() or args.output.read_text(encoding="utf-8") != rendered:
            print(f"config dependency ledger drifted: {args.output}")
            return 1
        print(f"config dependency ledger: OK ({args.output})")
        return 0
    args.output.write_text(rendered, encoding="utf-8")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
