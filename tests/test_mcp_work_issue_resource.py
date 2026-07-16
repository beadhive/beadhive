"""beadhive://work/issue/{id} template resource.

Tests that the resource:
  * is registered and readable via the in-process FastMCP Client;
  * returns the normalized bead dict via bd.show(bead, cwd) for a known id;
  * returns None when the bead is not found (bd.json returns None).

All tests gated behind importorskip so CI stays green without the [mcp] extra installed.
"""

from __future__ import annotations

import asyncio
import json
from collections import namedtuple
from pathlib import Path

import pytest

from beadhive import bd as bd_mod
from beadhive import mcp as mcp_mod
from beadhive import registry as registry_mod

# ---- helpers -----------------------------------------------------------------

_CP = namedtuple("CP", "returncode stdout stderr")

KNOWN_ID = "bh-abc.1"
KNOWN_BEAD = {
    "id": KNOWN_ID,
    "title": "a known bead",
    "status": "open",
    "type": "issue",
}


async def _read(server, uri: str):
    from fastmcp import Client

    async with Client(server) as client:
        return await client.read_resource(uri)


async def _list_resources(server):
    from fastmcp import Client

    async with Client(server) as client:
        return await client.list_resources()


async def _list_resource_templates(server):
    from fastmcp import Client

    async with Client(server) as client:
        return await client.list_resource_templates()


def _patch_show(monkeypatch, bead_id: str, payload):
    """Monkeypatch bd.run so bd.json(["show", bead_id], cwd) returns payload.

    Also pins registry.hive_dir_for to a fixed cwd so hive_dir_for(cfg, hive="") doesn't
    hit the filesystem.
    """

    def _fake_run(cmd, **_kw):
        # bd.json calls: ["bd", "-C", cwd, "show", bead_id, "--json"]
        if "show" in cmd and bead_id in cmd:
            return _CP(0, json.dumps(payload), "")
        return _CP(1, "", "not found")

    monkeypatch.setattr(bd_mod, "_run", _fake_run)
    monkeypatch.setattr(registry_mod, "hive_dir_for", lambda cfg, hive="": Path("/fake/hive"))


# ---- registration check ------------------------------------------------------


def test_work_issue_resource_is_registered():
    """beadhive://work/issue/{id} appears in the server's resource template list."""
    pytest.importorskip("fastmcp")
    server = mcp_mod.build_server()
    # Template resources (URI with {param}) are in list_resource_templates, not list_resources.
    templates = asyncio.run(_list_resource_templates(server))
    uris = {str(t.uriTemplate) for t in templates}
    assert "beadhive://work/issue/{id}" in uris, (
        f"expected beadhive://work/issue/{{id}} in resource template list, got: {uris}"
    )


# ---- payload checks ----------------------------------------------------------


def test_work_issue_resource_returns_bead_dict(monkeypatch):
    """beadhive://work/issue/<id> returns the normalized bead dict for a known id."""
    pytest.importorskip("fastmcp")
    _patch_show(monkeypatch, KNOWN_ID, KNOWN_BEAD)
    server = mcp_mod.build_server()
    contents = asyncio.run(_read(server, f"beadhive://work/issue/{KNOWN_ID}"))
    assert contents, "expected at least one content block"
    data = json.loads(contents[0].text)
    assert isinstance(data, dict), f"expected a dict, got: {type(data)}"
    assert data["id"] == KNOWN_ID
    assert data["title"] == "a known bead"
    assert data["status"] == "open"


def test_work_issue_resource_normalizes_single_element_list(monkeypatch):
    """bd.show normalizes a 1-list from bd.json; resource returns the inner dict."""
    pytest.importorskip("fastmcp")
    # bd show sometimes returns a list with one element instead of a bare dict
    _patch_show(monkeypatch, KNOWN_ID, [KNOWN_BEAD])
    server = mcp_mod.build_server()
    contents = asyncio.run(_read(server, f"beadhive://work/issue/{KNOWN_ID}"))
    assert contents, "expected at least one content block"
    data = json.loads(contents[0].text)
    assert isinstance(data, dict), "resource must unwrap single-element list"
    assert data["id"] == KNOWN_ID


def test_work_issue_resource_returns_none_when_not_found(monkeypatch):
    """When bd.json returns None (bead not found), the resource returns None."""
    pytest.importorskip("fastmcp")
    monkeypatch.setattr(bd_mod, "_run", lambda cmd, **_kw: _CP(1, "", "not found"))
    monkeypatch.setattr(registry_mod, "hive_dir_for", lambda cfg, hive="": Path("/fake/hive"))
    server = mcp_mod.build_server()
    contents = asyncio.run(_read(server, f"beadhive://work/issue/{KNOWN_ID}"))
    assert contents, "expected at least one content block"
    data = json.loads(contents[0].text)
    assert data is None, f"expected None when bead not found, got {data!r}"
