""" / eybf.9 — hives resource dual-exposure (status, available, survey).

Tests that both resources:
  * are registered and readable via the in-process FastMCP Client;
  * return the same payload shape as the corresponding hives_status / hives_available tools,
    backed by the same hive.available(cfg) / registry.* cores;
  * do NOT remove the existing tools from the registry (dual-expose assertion).

All tests gated behind importorskip so CI stays green without the [mcp] extra installed.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from beadhive import config as config_mod
from beadhive import hive as hive_mod
from beadhive import mcp as mcp_mod
from beadhive import survey as survey_mod

# ---- helpers -----------------------------------------------------------------


async def _read(server, uri: str):
    from fastmcp import Client

    async with Client(server) as client:
        return await client.read_resource(uri)


async def _list_resources(server):
    from fastmcp import Client

    async with Client(server) as client:
        return await client.list_resources()


async def _list_tools(server):
    from fastmcp import Client

    async with Client(server) as client:
        return await client.list_tools()


def _patch_hives(monkeypatch):
    """Monkeypatch config.load + hive.available with minimal collision/violation fixtures."""
    cfg = {
        "orgs": {"acme": {"code": "ac", "policy": "required"}},
        "managed_repos": [
            {"provider": "github", "org": "acme", "repo": "one", "prefix": "dup", "kind": ""},
            {"provider": "github", "org": "acme", "repo": "two", "prefix": "dup", "kind": ""},
        ],
    }
    monkeypatch.setattr(config_mod, "load", lambda: cfg)
    monkeypatch.setattr(
        hive_mod, "available", lambda c: {"candidates": ["github/acme/new"], "registered": []}
    )
    return cfg


# ---- registration checks -----------------------------------------------------


def test_hives_available_resource_is_registered():
    """beadhive://hives/available appears in the server's resource list."""
    pytest.importorskip("fastmcp")
    server = mcp_mod.build_server()
    resources = asyncio.run(_list_resources(server))
    uris = {str(r.uri) for r in resources}
    assert "beadhive://hives/available" in uris


def test_hives_status_resource_is_registered():
    """beadhive://hives/status appears in the server's resource list."""
    pytest.importorskip("fastmcp")
    server = mcp_mod.build_server()
    resources = asyncio.run(_list_resources(server))
    uris = {str(r.uri) for r in resources}
    assert "beadhive://hives/status" in uris


# ---- payload checks: same cores as the tools ---------------------------------


def test_hives_available_resource_returns_same_payload_as_tool(monkeypatch):
    """beadhive://hives/available returns {candidates, registered} backed by hive.available(cfg)."""
    pytest.importorskip("fastmcp")
    _patch_hives(monkeypatch)
    server = mcp_mod.build_server()
    contents = asyncio.run(_read(server, "beadhive://hives/available"))
    assert contents, "expected at least one content block"
    data = json.loads(contents[0].text)
    assert data["candidates"] == ["github/acme/new"]
    assert data["registered"] == []


def test_hives_status_resource_returns_same_payload_as_tool(monkeypatch):
    """beadhive://hives/status returns {candidates, collisions, violations, hives}
    via registry.*."""
    pytest.importorskip("fastmcp")
    _patch_hives(monkeypatch)
    server = mcp_mod.build_server()
    contents = asyncio.run(_read(server, "beadhive://hives/status"))
    assert contents, "expected at least one content block"
    data = json.loads(contents[0].text)
    assert data["candidates"] == ["github/acme/new"]
    # Two hives share prefix 'dup' → one collision entry.
    assert data["collisions"] == [{"prefix": "dup", "hives": ["acme/one", "acme/two"]}]
    # Both hives violate the required 'ac-' prefix convention.
    assert len(data["violations"]) == 2
    assert {r["repo"] for r in data["hives"]} == {"one", "two"}


# ---- dual-expose: both tools remain registered --------------------------------


def test_hives_available_tool_still_registered_after_resource_added():
    """hives_available tool is still registered — dual-expose leaves tools intact."""
    pytest.importorskip("fastmcp")
    server = mcp_mod.build_server()
    tool_names = {t.name for t in asyncio.run(_list_tools(server))}
    assert "hives_available" in tool_names, "hives_available tool must remain registered"


def test_hives_status_tool_still_registered_after_resource_added():
    """hives_status tool is still registered — dual-expose leaves tools intact."""
    pytest.importorskip("fastmcp")
    server = mcp_mod.build_server()
    tool_names = {t.name for t in asyncio.run(_list_tools(server))}
    assert "hives_status" in tool_names, "hives_status tool must remain registered"


# ---- beadhive://hives/survey (eybf.9) -----------------------------------------------


def test_hives_survey_resource_is_registered():
    """beadhive://hives/survey appears in the server's resource list."""
    pytest.importorskip("fastmcp")
    server = mcp_mod.build_server()
    resources = asyncio.run(_list_resources(server))
    uris = {str(r.uri) for r in resources}
    assert "beadhive://hives/survey" in uris


def test_hives_survey_resource_returns_list_of_row_mappings(monkeypatch):
    """beadhive://hives/survey returns a list of row mappings via survey.collect_rows."""
    pytest.importorskip("fastmcp")

    fake_rows = [
        {"repo": "github/acme/one", "registered": True, "classification": "hive"},
        {"repo": "github/acme/two", "registered": False, "classification": "unregistered"},
    ]
    monkeypatch.setattr(survey_mod, "collect_rows", lambda cfg: fake_rows)
    monkeypatch.setattr(config_mod, "load", lambda: {})
    server = mcp_mod.build_server()
    contents = asyncio.run(_read(server, "beadhive://hives/survey"))
    assert contents, "expected at least one content block"
    data = json.loads(contents[0].text)
    assert isinstance(data, list), "expected a list of rows"
    assert all(isinstance(row, dict) for row in data), "expected each row to be a mapping"
    assert data == fake_rows
