"""Completeness and drift gates for every public transport projection."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastmcp import Client
from jsonschema import Draft202012Validator
from starlette.routing import Route, WebSocketRoute
from typer.testing import CliRunner

from beadhive import (
    cli,
    daemon_contract,
    host_daemon,
    mcp,
    operation_catalog,
    operator_api,
    remote_gateway,
)
from beadhive.transport_inventory import document, projections

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = ROOT / "docs" / "design" / "transport-projection-inventory-v1.json"
SCHEMA = ROOT / "docs" / "design" / "transport-projection-inventory-v1.schema.json"


def _route_inventory(routes) -> set[str]:
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


def test_checked_inventory_is_current_deterministic_and_schema_valid() -> None:
    first = document()
    assert first == document()
    assert first["inventory_version"] == "1.1.0"
    assert json.loads(ARTIFACT.read_text()) == first
    schema = json.loads(SCHEMA.read_text())
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(first)

    rows = first["projections"]
    keys = [(row["surface"], row["identifier"]) for row in rows]
    assert keys == sorted(keys)
    assert len(keys) == len(set(keys))


def test_generated_cli_and_mcp_inventory_exactly_covers_the_catalog() -> None:
    counts: dict[str, int] = {}
    for row in projections():
        counts[row.surface] = counts.get(row.surface, 0) + 1
    assert counts == {
        "cli": 213,
        "gateway": 18,
        "mcp-resource": 21,
        "mcp-tool": 10,
        "operator-api": 13,
    }

    rows = {(row.surface, row.identifier): row for row in projections()}
    catalog_rows = operation_catalog.operations()
    for operation in catalog_rows:
        if cli := operation.surfaces.get("cli"):
            assert rows["cli", cli["path"]].operation == operation.name
            for alias in cli["aliases"]:
                assert rows["cli", alias["path"]].operation == operation.name
        if mcp := operation.surfaces.get("mcp"):
            if tool := mcp.get("tool"):
                assert rows["mcp-tool", tool].operation == operation.name
            if resource := mcp.get("resource"):
                assert rows["mcp-resource", resource].operation == operation.name


def test_operator_inventory_matches_runtime_routes_and_checked_openapi() -> None:
    async def events(_request):  # pragma: no cover - only its route declaration is inspected
        raise AssertionError("inventory must not dispatch")

    api = operator_api.OperatorAPI(
        sources=object(),
        feed=object(),
        host_id="host-test",
        instance_id="instance-test",
        ready=lambda: True,
        events=events,
        activity_publisher=lambda *_args: None,
        activity_max_body_bytes=1,
    )
    runtime = host_daemon.DaemonRuntime()
    app = host_daemon.build_application(runtime=runtime, routes=api.routes())
    actual = _route_inventory(app.routes)
    declared = {row.identifier for row in projections() if row.surface == "operator-api"}
    assert actual == {row for row in declared if row != "OPTIONS *"}

    openapi = operator_api.openapi_document()
    openapi_methods = {
        f"{'WEBSOCKET' if method == 'x-beadhive-websocket' else method.upper()} {path}"
        for path, path_item in openapi["paths"].items()
        for method in path_item
        if method in {"get", "post", "put", "patch", "delete", "x-beadhive-websocket"}
    }
    normalized_declared = {
        identifier.replace(":path}", "}") for identifier in declared if identifier != "OPTIONS *"
    }
    assert openapi_methods == normalized_declared
    assert openapi["x-beadhive-secure-network-preflight"]["method"] == "OPTIONS"

    def resolve(pointer: str):
        value = openapi
        for token in pointer.removeprefix("/").split("/"):
            value = value[token.replace("~1", "/").replace("~0", "~")]
        return value

    for row in projections():
        if row.surface != "operator-api":
            continue
        for reference in (row.request_schema, row.result_schema):
            if reference.startswith("openapi:beadhive-host-openapi-v1.json#"):
                assert resolve(reference.split("#", 1)[1]) is not None

        method, path = row.identifier.split(" ", 1)
        if method == "OPTIONS":
            continue
        operation_key = "x-beadhive-websocket" if method == "WEBSOCKET" else method.lower()
        operation = openapi["paths"][path.replace(":path}", "}")][operation_key]
        assert operation["x-beadhive-catalog-projection"] == {
            "classification": row.classification,
            "shape": row.shape,
            "canonicalContracts": [
                {
                    "operation": contract.operation,
                    "requestSchema": contract.request_schema,
                    "resultSchema": contract.result_schema,
                    "privilege": contract.privilege,
                    "sideEffects": contract.side_effects,
                }
                for contract in row.canonical_contracts
            ],
            "transportOwner": row.transport_owner,
            "transportPrivilege": row.privilege,
            "sideEffects": row.side_effects,
            "reason": row.reason,
        }


def test_gateway_inventory_matches_every_registered_runtime_route() -> None:
    config = remote_gateway.DevelopmentGatewayConfig(
        issuer=remote_gateway.DEVELOPMENT_ISSUER,
        audience="beadhive-gateway-dev",
        app_origin="https://app-dev.beadhive.cloud",
        gateway_origin="https://gateway-dev.beadhive.cloud",
    )
    app = remote_gateway.build_development_gateway_application(
        config=config,
        verifier=object(),
        registry=remote_gateway.DevelopmentInstanceRegistry(instances={}),
    )
    actual = _route_inventory(app.routes)
    declared = {row.identifier for row in projections() if row.surface == "gateway"}
    assert actual == declared


def test_classifications_are_closed_and_composites_preserve_application_policy() -> None:
    catalog = {operation.name: operation for operation in operation_catalog.operations()}
    for row in projections():
        assert row.request_schema
        assert row.result_schema
        assert row.privilege
        assert row.availability
        assert row.compatibility
        assert row.reason
        assert row.transport_owner
        if row.classification == "catalog-entry":
            assert row.operation in catalog
            assert not row.composes
        elif row.classification == "composite":
            assert row.operation is None or row.operation in catalog
            assert row.composes
            for component_name in row.composes:
                component = catalog[component_name]
                assert component.privilege != "privileged"
                assert not component.constraints["hq_write"]
                assert not component.constraints["secret_material"]
                assert not component.constraints["interactive"]
                if row.surface in {"operator-api", "gateway"}:
                    assert component.kind == "read-resource"
        else:
            assert row.operation is None
            assert not row.composes

        expected_contract_names = (
            ((row.operation,) if row.operation else row.composes)
            if row.surface in {"operator-api", "gateway"}
            else ()
        )
        assert tuple(contract.operation for contract in row.canonical_contracts) == tuple(
            expected_contract_names
        )
        for contract in row.canonical_contracts:
            operation = catalog[contract.operation]
            assert contract.request_schema == f"catalog:{operation.name}#parameters"
            assert contract.result_schema == operation.result_schema
            assert contract.privilege == operation.privilege
            assert contract.side_effects == (
                "none" if operation.kind == "read-resource" else "application-mutation"
            )

        if row.surface.startswith("mcp"):
            assert row.privilege != "privileged"
            assert row.prompts is False


def test_http_projection_shapes_and_non_catalog_ownership_are_explicit() -> None:
    rows = {(row.surface, row.identifier): row for row in projections()}

    assert rows["operator-api", "GET /api/v1/hives/{hive_id:path}/work-items"].shape == "richer"
    assert rows["gateway", "GET /v1/instances/{stage}/{slug}/snapshot"].shape == "coarser"
    assert rows["gateway", "GET /v1/instances/{stage}/{slug}/snapshot"].composes == (
        "work.list",
        "work.schedule",
    )

    exclusions = {
        (row.surface, row.identifier)
        for row in projections()
        if row.classification == "explicit-exclusion"
    }
    assert exclusions == {
        ("operator-api", "GET /api/v1/runs/{run_id}/activity"),
        ("operator-api", "POST /api/v1/runs/{run_id}/activity"),
        ("gateway", "POST /v1/instances/{stage}/{slug}/commands/refresh"),
    }
    assert all(
        row.transport_owner == "bh-q0lol" for row in projections() if row.surface == "operator-api"
    )
    assert all(
        row.transport_owner == "gateway-contract"
        for row in projections()
        if row.surface == "gateway"
    )


def test_operator_manifest_itself_is_exactly_inventoried() -> None:
    manifest = {f"{route.method} {route.path}" for route in daemon_contract.NON_MCP_ROUTES}
    declared = {
        row.identifier.replace(":path}", "}")
        for row in projections()
        if row.surface == "operator-api" and row.identifier != "OPTIONS *"
    }
    assert declared == manifest


def test_cli_and_stdio_mcp_do_not_compose_network_transports(monkeypatch) -> None:
    def network_transport_is_a_failure(*_args, **_kwargs):
        raise AssertionError("direct transports must not compose a daemon or gateway")

    monkeypatch.setattr(host_daemon, "build_application", network_transport_is_a_failure)
    monkeypatch.setattr(
        remote_gateway, "build_development_gateway_application", network_transport_is_a_failure
    )

    cli_result = CliRunner().invoke(cli.app, ["--version"])
    assert cli_result.exit_code == 0

    async def discover_stdio() -> tuple[int, int]:
        async with Client(mcp.build_server()) as client:
            return len(await client.list_tools()), len(await client.list_resources()) + len(
                await client.list_resource_templates()
            )

    assert asyncio.run(discover_stdio()) == (10, 21)


def test_mcp_composite_declaration_fails_closed_on_unsafe_components(monkeypatch) -> None:
    monkeypatch.setattr(operation_catalog, "_MCP_COMPOSITES", {"hive.onboard": ("hq.push",)})
    with pytest.raises(ValueError, match="bypasses component policy.*hq.push"):
        operation_catalog.operations()
