"""Catalog-derived FastMCP registration and safety-policy regression tests."""

from __future__ import annotations

import asyncio
import inspect

import pytest

from beadhive import config as config_mod
from beadhive import host_daemon
from beadhive import mcp as mcp_mod
from beadhive import operation_catalog as catalog


def _live_inventory(server):
    from fastmcp import Client

    async def read():
        async with Client(server) as client:
            tools = {tool.name: tool for tool in await client.list_tools()}
            resources = {str(resource.uri) for resource in await client.list_resources()}
            resources.update(
                str(template.uriTemplate) for template in await client.list_resource_templates()
            )
            return tools, resources

    return asyncio.run(read())


def _registered_components(server):
    return (
        asyncio.run(server.list_tools()),
        [
            *asyncio.run(server.list_resources()),
            *asyncio.run(server.list_resource_templates()),
        ],
    )


def _server_contract(server):
    tools, resources = _registered_components(server)
    return {
        "tools": {
            tool.name: {
                "schema": tool.parameters,
                "operation": tool.fn.bh_catalog_operation,
                "parameters": tool.fn.bh_catalog_parameters,
                "composes": tool.fn.bh_catalog_composes,
                "strict_bd": tool.fn.bh_strict_bd,
                "model_safe_errors": tool.fn.bh_model_safe_errors,
            }
            for tool in tools
        },
        "resources": {
            str(getattr(resource, "uri", getattr(resource, "uri_template", ""))): {
                "schema": getattr(resource, "parameters", None),
                "operation": resource.fn.bh_catalog_operation,
                "parameters": resource.fn.bh_catalog_parameters,
                "strict_bd": getattr(resource.fn, "bh_strict_bd", False),
                "model_safe_errors": resource.fn.bh_model_safe_errors,
            }
            for resource in resources
        },
    }


def test_mcp_composition_root_contains_no_hand_typed_registration_uri_or_bare_tool():
    source = inspect.getsource(mcp_mod)

    assert '@resource("beadhive://' not in source
    assert "    @tool\n" not in source


def test_catalog_mutation_changes_live_tool_name_signature_and_resource_uri(monkeypatch):
    """The catalog is causative: changing it changes the built server without handler edits."""
    pytest.importorskip("fastmcp")
    monkeypatch.setitem(
        catalog._MCP_TOOLS,
        "plan.file",
        ("catalog_plan_file", ("spec", "dry_run")),
    )
    monkeypatch.setitem(
        catalog._MCP_RESOURCES,
        "probe.health",
        ("beadhive://catalog/probe", ()),
    )

    tools, resources = _live_inventory(mcp_mod.build_server())

    assert "plan_file" not in tools
    assert list(tools["catalog_plan_file"].inputSchema["properties"]) == ["spec", "dry_run"]
    assert "beadhive://probe/health" not in resources
    assert "beadhive://catalog/probe" in resources


def test_generated_registration_keeps_measurement_strictness_and_handler_behavior(
    monkeypatch,
):
    """Generation changes transport metadata, not the measured/strict application handler."""
    pytest.importorskip("fastmcp")
    from fastmcp import Client

    monkeypatch.setitem(catalog._MCP_TOOLS, "plan.check", ("catalog_plan_check", ("spec",)))
    measured = []
    monkeypatch.setattr(
        mcp_mod.otel,
        "record_mcp_invocation",
        lambda name, outcome, seconds: measured.append((name, outcome, seconds)),
    )
    server = mcp_mod.build_server()
    registered_tools, _resources = _registered_components(server)
    generated = next(tool for tool in registered_tools if tool.name == "catalog_plan_check")

    async def call():
        async with Client(server) as client:
            return await client.call_tool(
                "catalog_plan_check",
                {
                    "spec": {
                        "epic": {"title": "Derived surface"},
                        "issues": [
                            {"handle": "a", "title": "Keep behavior", "acceptance": "works"}
                        ],
                    }
                },
            )

    result = asyncio.run(call())

    assert result.data["valid"] is True
    assert generated.fn.bh_catalog_operation == "plan.check"
    assert generated.fn.bh_catalog_parameters == ("spec",)
    assert generated.fn.bh_strict_bd is True
    assert [(name, outcome) for name, outcome, _seconds in measured] == [
        ("catalog_plan_check", "ok")
    ]


def test_generated_surface_excludes_privilege_at_catalog_registry_and_schema_layers():
    """No HQ write, secret action, prompt, or override parameter is model-reachable."""
    pytest.importorskip("fastmcp")
    server = mcp_mod.build_server()
    registered_tools, _resources = _registered_components(server)
    operations = {operation.name: operation for operation in catalog.operations()}
    projected = {
        operation.name: operation.surfaces["mcp"]
        for operation in operations.values()
        if "mcp" in operation.surfaces
    }

    # Declaration layer: positive allowlist only, with every unsafe capability absent.
    for name, projection in projected.items():
        operation = operations[name]
        assert projection["allowlisted"] is True
        assert operation.privilege != "privileged"
        assert not operation.constraints["hq_write"]
        assert not operation.constraints["secret_material"]
        assert not operation.constraints["interactive"]

    # Registration layer: every callable is traceable to one safe allowlisted operation.
    assert registered_tools
    for tool in registered_tools:
        operation = operations[tool.fn.bh_catalog_operation]
        assert operation.name in projected
        assert not operation.name.startswith("hq.")
        assert operation.privilege != "privileged"
        assert not operation.constraints["secret_material"]

    # Wire-schema layer: override flags and secret/auth material never enter model input.
    properties = {
        name for tool in registered_tools for name in tool.parameters.get("properties", {})
    }
    assert properties.isdisjoint({"force", "yes", "skip_check", "token", "secret"})


def test_projection_fails_closed_when_allowlist_is_mutated_with_unsafe_entries(monkeypatch):
    """Positive permission is insufficient when a malformed declaration violates policy."""
    monkeypatch.setitem(catalog._MCP_TOOLS, "hq.push", ("hq_push", ("dry_run",)))
    with pytest.raises(catalog.MCPProjectionError, match="hq_write"):
        catalog.mcp_tool_projection("hq.push")

    monkeypatch.setitem(catalog._MCP_TOOLS, "dep.auth", ("dep_auth", ("name", "check")))
    with pytest.raises(catalog.MCPProjectionError, match="secret_material"):
        catalog.mcp_tool_projection("dep.auth")

    monkeypatch.setitem(catalog._MCP_TOOLS, "dep.install", ("dep_install", ("name",)))
    with pytest.raises(catalog.MCPProjectionError, match="interactive"):
        catalog.mcp_tool_projection("dep.install")

    _tool_name, parameters = catalog._MCP_TOOLS["hive.onboard"]
    monkeypatch.setitem(
        catalog._MCP_TOOLS,
        "hive.onboard",
        (_tool_name, (*parameters, "force")),
    )
    with pytest.raises(catalog.MCPProjectionError, match="privileged MCP parameters"):
        catalog.mcp_tool_projection("hive.onboard")


def test_composite_allowlist_is_small_explicit_and_references_catalog_operations():
    operations = {operation.name: operation for operation in catalog.operations()}
    composites = {
        name: projection["composes"]
        for name, operation in operations.items()
        if (projection := operation.surfaces.get("mcp")) and projection.get("composes")
    }

    assert composites == {"hive.onboard": ["hive.init", "sync"]}
    assert set(composites["hive.onboard"]) <= set(operations)
    assert operations["hive.onboard"].surfaces["mcp"]["granularity"]["mode"] == "coarse"


def test_registration_plan_carries_the_explicit_composite_declaration():
    tool_plan, resource_plan = mcp_mod._registration_plan(mcp_mod._handler_bindings())

    assert len(tool_plan) == 10
    assert len(resource_plan) == 21
    assert {binding.operation: binding.composes for binding in tool_plan if binding.composes} == {
        "hive.onboard": ("hive.init", "sync")
    }


def test_stdio_and_host_daemon_http_use_identical_contracts_and_envelopes():
    pytest.importorskip("fastmcp")
    stdio_server = mcp_mod.build_server()
    captured = []

    def server_factory():
        server = mcp_mod.build_server()
        captured.append(server)
        return server

    app = host_daemon.build_application(enable_mcp_http=True, mcp_server_factory=server_factory)

    assert any(getattr(route, "path", None) == "/mcp" for route in app.routes)
    assert len(captured) == 1
    assert _server_contract(captured[0]) == _server_contract(stdio_server)
    assert _server_contract(stdio_server)["tools"]["hive_onboard"]["composes"] == (
        "hive.init",
        "sync",
    )


def test_catalog_drives_completed_mutation_notifications(monkeypatch):
    pytest.importorskip("fastmcp")
    from mcp.types import ResourceUpdatedNotification

    monkeypatch.setitem(
        catalog._MCP_NOTIFICATION_URIS,
        "config.set",
        ["beadhive://catalog/config", "beadhive://catalog/config/{key}"],
    )
    monkeypatch.setattr(
        config_mod,
        "set_value",
        lambda key, raw, as_json=False, cfg=None: {
            "ok": True,
            "problems": [],
            "old": None,
            "new": raw,
        },
    )
    captured = []

    async def handler(message):
        root = getattr(message, "root", message)
        if isinstance(root, ResourceUpdatedNotification):
            captured.append(str(root.params.uri))

    async def call():
        from fastmcp import Client

        async with Client(mcp_mod.build_server(), message_handler=handler) as client:
            return await client.call_tool("config_set", {"key": "otel.protocol", "value": "grpc"})

    result = asyncio.run(call())

    assert result.data["ok"] is True
    assert captured == ["beadhive://catalog/config", "beadhive://catalog/config/otel.protocol"]
