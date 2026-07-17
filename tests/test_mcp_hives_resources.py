""" / eybf.9 — hives resource dual-exposure (status, available, survey).

Tests that both resources:
  * are registered and readable via the in-process FastMCP Client;
  * return the same payload shape as the corresponding hive_status / hive_list tools,
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


def test_hive_list_resource_is_registered():
    """beadhive://hive/list appears in the server's resource list."""
    pytest.importorskip("fastmcp")
    server = mcp_mod.build_server()
    resources = asyncio.run(_list_resources(server))
    uris = {str(r.uri) for r in resources}
    assert "beadhive://hive/list" in uris


def test_hive_status_resource_is_registered():
    """beadhive://hive/status appears in the server's resource list."""
    pytest.importorskip("fastmcp")
    server = mcp_mod.build_server()
    resources = asyncio.run(_list_resources(server))
    uris = {str(r.uri) for r in resources}
    assert "beadhive://hive/status" in uris


# ---- payload checks: same cores as the tools ---------------------------------


def test_hive_list_resource_returns_same_payload_as_tool(monkeypatch):
    """beadhive://hive/list returns {candidates, registered} backed by hive.available(cfg)."""
    pytest.importorskip("fastmcp")
    _patch_hives(monkeypatch)
    server = mcp_mod.build_server()
    contents = asyncio.run(_read(server, "beadhive://hive/list"))
    assert contents, "expected at least one content block"
    data = json.loads(contents[0].text)
    assert data["candidates"] == ["github/acme/new"]
    assert data["registered"] == []


def test_hive_status_resource_returns_same_payload_as_tool(monkeypatch):
    """beadhive://hive/status returns {candidates, collisions, violations, hives}
    via registry.*."""
    pytest.importorskip("fastmcp")
    _patch_hives(monkeypatch)
    server = mcp_mod.build_server()
    contents = asyncio.run(_read(server, "beadhive://hive/status"))
    assert contents, "expected at least one content block"
    data = json.loads(contents[0].text)
    assert data["candidates"] == ["github/acme/new"]
    # Two hives share prefix 'dup' → one collision entry.
    assert data["collisions"] == [{"prefix": "dup", "hives": ["acme/one", "acme/two"]}]
    # Both hives violate the required 'ac-' prefix convention.
    assert len(data["violations"]) == 2
    assert {r["repo"] for r in data["hives"]} == {"one", "two"}


# ---- dual-expose: both tools remain registered --------------------------------


def test_hive_list_tool_still_registered_after_resource_added():
    """hive_list tool is still registered — dual-expose leaves tools intact."""
    pytest.importorskip("fastmcp")
    server = mcp_mod.build_server()
    tool_names = {t.name for t in asyncio.run(_list_tools(server))}
    assert "hive_list" in tool_names, "hive_list tool must remain registered"


def test_hive_status_tool_still_registered_after_resource_added():
    """hive_status tool is still registered — dual-expose leaves tools intact."""
    pytest.importorskip("fastmcp")
    server = mcp_mod.build_server()
    tool_names = {t.name for t in asyncio.run(_list_tools(server))}
    assert "hive_status" in tool_names, "hive_status tool must remain registered"


# ---- beadhive://hive/survey (eybf.9) -----------------------------------------------


def test_hives_survey_resource_is_registered():
    """beadhive://hive/survey appears in the server's resource list."""
    pytest.importorskip("fastmcp")
    server = mcp_mod.build_server()
    resources = asyncio.run(_list_resources(server))
    uris = {str(r.uri) for r in resources}
    assert "beadhive://hive/survey" in uris


def test_hives_survey_resource_returns_list_of_row_mappings(monkeypatch):
    """beadhive://hive/survey returns a list of row mappings via survey.collect_rows."""
    pytest.importorskip("fastmcp")

    fake_rows = [
        {"repo": "github/acme/one", "registered": True, "classification": "hive"},
        {"repo": "github/acme/two", "registered": False, "classification": "unregistered"},
    ]
    monkeypatch.setattr(survey_mod, "collect_rows", lambda cfg: fake_rows)
    monkeypatch.setattr(config_mod, "load", lambda: {})
    server = mcp_mod.build_server()
    contents = asyncio.run(_read(server, "beadhive://hive/survey"))
    assert contents, "expected at least one content block"
    data = json.loads(contents[0].text)
    assert isinstance(data, list), "expected a list of rows"
    assert all(isinstance(row, dict) for row in data), "expected each row to be a mapping"
    assert data == fake_rows
