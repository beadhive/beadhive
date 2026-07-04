""" / eybf.9 — rigs resource dual-exposure (status, available, survey).

Tests that both resources:
  * are registered and readable via the in-process FastMCP Client;
  * return the same payload shape as the corresponding rigs_status / rigs_available tools,
    backed by the same rig.available(cfg) / registry.* cores;
  * do NOT remove the existing tools from the registry (dual-expose assertion).

All tests gated behind importorskip so CI stays green without the [mcp] extra installed.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from ws import config as config_mod
from ws import mcp as mcp_mod
from ws import rig as rig_mod
from ws import survey as survey_mod

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


def _patch_rigs(monkeypatch):
    """Monkeypatch config.load + rig.available with minimal collision/violation fixtures."""
    cfg = {
        "orgs": {"acme": {"code": "ac", "policy": "required"}},
        "managed_repos": [
            {"provider": "github", "org": "acme", "repo": "one", "prefix": "dup", "kind": ""},
            {"provider": "github", "org": "acme", "repo": "two", "prefix": "dup", "kind": ""},
        ],
    }
    monkeypatch.setattr(config_mod, "load", lambda: cfg)
    monkeypatch.setattr(
        rig_mod, "available", lambda c: {"candidates": ["github/acme/new"], "registered": []}
    )
    return cfg


# ---- registration checks -----------------------------------------------------


def test_rigs_available_resource_is_registered():
    """ws://rigs/available appears in the server's resource list."""
    pytest.importorskip("fastmcp")
    server = mcp_mod.build_server()
    resources = asyncio.run(_list_resources(server))
    uris = {str(r.uri) for r in resources}
    assert "ws://rigs/available" in uris


def test_rigs_status_resource_is_registered():
    """ws://rigs/status appears in the server's resource list."""
    pytest.importorskip("fastmcp")
    server = mcp_mod.build_server()
    resources = asyncio.run(_list_resources(server))
    uris = {str(r.uri) for r in resources}
    assert "ws://rigs/status" in uris


# ---- payload checks: same cores as the tools ---------------------------------


def test_rigs_available_resource_returns_same_payload_as_tool(monkeypatch):
    """ws://rigs/available returns {candidates, registered} backed by rig.available(cfg)."""
    pytest.importorskip("fastmcp")
    _patch_rigs(monkeypatch)
    server = mcp_mod.build_server()
    contents = asyncio.run(_read(server, "ws://rigs/available"))
    assert contents, "expected at least one content block"
    data = json.loads(contents[0].text)
    assert data["candidates"] == ["github/acme/new"]
    assert data["registered"] == []


def test_rigs_status_resource_returns_same_payload_as_tool(monkeypatch):
    """ws://rigs/status returns {candidates, collisions, violations, rigs} via registry.*."""
    pytest.importorskip("fastmcp")
    _patch_rigs(monkeypatch)
    server = mcp_mod.build_server()
    contents = asyncio.run(_read(server, "ws://rigs/status"))
    assert contents, "expected at least one content block"
    data = json.loads(contents[0].text)
    assert data["candidates"] == ["github/acme/new"]
    # Two rigs share prefix 'dup' → one collision entry.
    assert data["collisions"] == [{"prefix": "dup", "rigs": ["acme/one", "acme/two"]}]
    # Both rigs violate the required 'ac-' prefix convention.
    assert len(data["violations"]) == 2
    assert {r["repo"] for r in data["rigs"]} == {"one", "two"}


# ---- dual-expose: both tools remain registered --------------------------------


def test_rigs_available_tool_still_registered_after_resource_added():
    """rigs_available tool is still registered — dual-expose leaves tools intact."""
    pytest.importorskip("fastmcp")
    server = mcp_mod.build_server()
    tool_names = {t.name for t in asyncio.run(_list_tools(server))}
    assert "rigs_available" in tool_names, "rigs_available tool must remain registered"


def test_rigs_status_tool_still_registered_after_resource_added():
    """rigs_status tool is still registered — dual-expose leaves tools intact."""
    pytest.importorskip("fastmcp")
    server = mcp_mod.build_server()
    tool_names = {t.name for t in asyncio.run(_list_tools(server))}
    assert "rigs_status" in tool_names, "rigs_status tool must remain registered"


# ---- ws://rigs/survey (eybf.9) -----------------------------------------------


def test_rigs_survey_resource_is_registered():
    """ws://rigs/survey appears in the server's resource list."""
    pytest.importorskip("fastmcp")
    server = mcp_mod.build_server()
    resources = asyncio.run(_list_resources(server))
    uris = {str(r.uri) for r in resources}
    assert "ws://rigs/survey" in uris


def test_rigs_survey_resource_returns_list_of_row_mappings(monkeypatch):
    """ws://rigs/survey returns a list of row mappings via survey.collect_rows."""
    pytest.importorskip("fastmcp")

    fake_rows = [
        {"repo": "github/acme/one", "registered": True, "classification": "rig"},
        {"repo": "github/acme/two", "registered": False, "classification": "unregistered"},
    ]
    monkeypatch.setattr(survey_mod, "collect_rows", lambda cfg: fake_rows)
    monkeypatch.setattr(config_mod, "load", lambda: {})
    server = mcp_mod.build_server()
    contents = asyncio.run(_read(server, "ws://rigs/survey"))
    assert contents, "expected at least one content block"
    data = json.loads(contents[0].text)
    assert isinstance(data, list), "expected a list of rows"
    assert all(isinstance(row, dict) for row in data), "expected each row to be a mapping"
    assert data == fake_rows
