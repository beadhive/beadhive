#!/usr/bin/env python3
"""Render exact before/after evidence for the installed transport composition roots."""

from __future__ import annotations

import argparse
import ast
import asyncio
import io
import json
import subprocess
import tarfile
import tempfile
from functools import cache
from pathlib import Path
from typing import Any

from check_import_boundaries import _cyclic_edges, _strong_components, collect_imports

from beadhive.transport_inventory import (
    composition_roots,
    projections,
    registration_drift,
    validate_registration_drift,
)

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "docs" / "proof" / "bh-3qkmk.5-transport-composition.json"
INTEGRATION_BASE = "6461a048c1b81ae2e3cf9071cb9384d8579f9ac2"
BEFORE_MODULES = {
    "cli": "beadhive.cli",
    # Historical baseline at INTEGRATION_BASE, before the Frame Bridge rename.
    "gateway": "beadhive.remote_gateway_runtime",
    "mcp": "beadhive.mcp",
    "operator-api": "beadhive.host_daemon_entrypoint",
}
REGISTRATION_OBSERVATION = {
    "cli": "live Typer command tree",
    "gateway": "live Starlette route table",
    "mcp": "live FastMCP tools, resources, and resource templates",
    "operator-api": "live Starlette route table",
}


def _checkout(revision: str, destination: Path) -> Path:
    archive = subprocess.run(
        ["git", "archive", revision, "src/beadhive"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
        bundle.extractall(destination, filter="data")
    return destination / "src"


def _shape(source_root: Path, module: str) -> dict[str, Any]:
    modules, edges, _dynamic = collect_imports(source_root)
    path = modules[module]
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    direct = sorted(
        {
            edge.imported_module
            for edge in edges
            if edge.importer == module
            and edge.imported_module != module
            and edge.imported_module.startswith("beadhive")
        }
    )
    components = _strong_components(modules, edges)
    component = next((sorted(row) for row in components if module in row), [])
    _all_components, cyclic_edges = _cyclic_edges(modules, edges)
    touching = [
        f"{edge.importer}->{edge.imported_module}"
        for edge in cyclic_edges
        if edge.importer == module or edge.imported_module == module
    ]
    return {
        "module": module,
        "physical_lines": len(source.splitlines()),
        "nonblank_lines": sum(bool(line.strip()) for line in source.splitlines()),
        "top_level_functions": sum(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) for node in tree.body
        ),
        "top_level_classes": sum(isinstance(node, ast.ClassDef) for node in tree.body),
        "direct_beadhive_dependencies": direct,
        "direct_beadhive_dependency_count": len(direct),
        "cycle_member": bool(component),
        "cycle_component": component,
        "cyclic_edges_touching_root": touching,
    }


def _route_inventory(routes: Any) -> set[str]:
    from starlette.routing import Route, WebSocketRoute

    result = {
        f"{method} {route.path}"
        for route in routes
        if isinstance(route, Route)
        for method in route.methods or ()
        if method != "HEAD"
    }
    result.update(
        f"WEBSOCKET {route.path}" for route in routes if isinstance(route, WebSocketRoute)
    )
    return result


@cache
def _declared_registration_sets() -> dict[str, frozenset[str]]:
    rows = projections()
    return {
        "cli": frozenset(row.identifier for row in rows if row.surface == "cli"),
        "mcp": frozenset(
            f"tool {row.identifier}" if row.surface == "mcp-tool" else f"resource {row.identifier}"
            for row in rows
            if row.surface in {"mcp-tool", "mcp-resource"}
        ),
        "operator-api": frozenset(
            row.identifier
            for row in rows
            if row.surface == "operator-api" and row.identifier != "OPTIONS *"
        ),
        "gateway": frozenset(row.identifier for row in rows if row.surface == "gateway"),
    }


def _registration_comparison(surface: str, declared_count: int) -> dict[str, Any]:
    excluded = ["OPTIONS *"] if surface == "operator-api" else []
    reason = (
        "q0lol secure-network preflight is an OpenAPI contract extension, not a "
        "registered Starlette route"
        if excluded
        else None
    )
    return {
        "declared_source": "checked transport projection inventory",
        "observed_source": REGISTRATION_OBSERVATION[surface],
        "projection_declaration_count": declared_count + len(excluded),
        "excluded_declarations": excluded,
        "exclusion_reason": reason,
    }


@cache
def _observed_registration_sets() -> dict[str, frozenset[str]]:
    from typer.main import get_command

    from beadhive import cli, frame_bridge, host_daemon, mcp, operator_api

    def cli_inventory() -> frozenset[str]:
        found: set[str] = set()

        def walk(command: Any, path: tuple[str, ...] = ()) -> None:
            for name, child in command.commands.items():
                child_path = (*path, name)
                if getattr(child, "commands", None) is not None:
                    walk(child, child_path)
                else:
                    found.add(" ".join(child_path))

        walk(get_command(cli.app))
        return frozenset(found)

    async def mcp_inventory() -> frozenset[str]:
        server = mcp.build_server()
        tools = await server.list_tools()
        resources = [*await server.list_resources(), *await server.list_resource_templates()]
        registrations = [*(f"tool {tool.name}" for tool in tools)]
        for resource in resources:
            identifier = getattr(resource, "uri", None) or resource.uri_template
            registrations.append(f"resource {identifier}")
        return frozenset(registrations)

    async def events(_request: Any) -> None:
        raise AssertionError("registration observation must not dispatch")

    operator = operator_api.OperatorAPI(
        sources=object(),
        feed=object(),
        host_id="evidence-host",
        instance_id="evidence-instance",
        ready=lambda: True,
        events=events,
        activity_publisher=lambda *_args: None,
        activity_max_body_bytes=1,
    )
    operator_app = host_daemon.build_application(
        runtime=host_daemon.DaemonRuntime(), routes=operator.routes()
    )
    gateway_app = frame_bridge.build_development_frame_bridge_application(
        config=frame_bridge.DevelopmentFrameBridgeConfig(
            issuer=frame_bridge.DEVELOPMENT_ISSUER,
            audience="beadhive-gateway-dev",
            app_origin="https://app-dev.beadhive.cloud",
            gateway_origin="https://gateway-dev.beadhive.cloud",
        ),
        verifier=object(),
        registry=frame_bridge.DevelopmentInstanceRegistry(instances={}),
    )
    return {
        "cli": cli_inventory(),
        "mcp": asyncio.run(mcp_inventory()),
        "operator-api": frozenset(_route_inventory(operator_app.routes)),
        "gateway": frozenset(_route_inventory(gateway_app.routes)),
    }


def document() -> dict[str, Any]:
    current_source = ROOT / "src"
    declared = _declared_registration_sets()
    observed = _observed_registration_sets()
    with tempfile.TemporaryDirectory(prefix="bh-3qkmk-5-before-") as temporary:
        before_source = _checkout(INTEGRATION_BASE, Path(temporary))
        roots = []
        for root in composition_roots():
            drift = registration_drift(root.surface, declared[root.surface], observed[root.surface])
            validate_registration_drift(drift)
            roots.append(
                {
                    "surface": root.surface,
                    "before": _shape(before_source, BEFORE_MODULES[root.surface]),
                    "after": _shape(current_source, root.module),
                    "test_closure": list(root.test_closure),
                    "registration_drift": drift
                    | {
                        "comparison": _registration_comparison(
                            root.surface, len(declared[root.surface])
                        ),
                        "runtime_proof": root.registration_drift_test,
                    },
                }
            )
    modules, edges, _dynamic = collect_imports(current_source)
    components, cyclic_edges = _cyclic_edges(modules, edges)
    return {
        "format_version": 1,
        "bead": "bh-3qkmk.5",
        "integration_base": INTEGRATION_BASE,
        "validation_cadence": "strict",
        "north_star": (
            "installed bootstrap depends outward-to-inward on transport adapters and the "
            "canonical operation kernel; reusable production code never imports bootstrap"
        ),
        "invariants": [
            "catalog operation identity and transport eligibility are unchanged",
            "CLI and MCP stdio remain independent of daemon and gateway availability",
            (
                "bh-q0lol retains daemon authentication, streaming, sessions, OpenAPI runtime, "
                "and supervision"
            ),
            (
                "gateway wire, authentication, admission, rate-limit, status, and streaming "
                "policy remain gateway-owned"
            ),
        ],
        "non_goals": [
            "move legacy compatibility handler bodies",
            "change public transport behavior or wire contracts",
            "replace daemon or Frame Bridge runtime ownership",
            "graduate a selective merge gate",
        ],
        "current_graph": {
            "python_files": len(modules),
            "import_edges": len(edges),
            "cyclic_components": len(components),
            "cyclic_edges": len(cyclic_edges),
        },
        "roots": roots,
        "artifact_drift_gate": "just transport-artifact-check",
        "full_gate": "bh work check bh-3qkmk.5",
    }


def render() -> str:
    return json.dumps(document(), indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    rendered = render()
    if args.check:
        if not TARGET.is_file() or TARGET.read_text(encoding="utf-8") != rendered:
            parser.error(f"{TARGET.relative_to(ROOT)} is stale; render it without --check")
        return 0
    TARGET.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
