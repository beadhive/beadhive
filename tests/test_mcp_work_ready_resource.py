""" — beadhive://work/ready resource.

Tests that the resource:
  * is registered and readable via the in-process FastMCP Client;
  * returns the same JSON shape as `ws work ready --json` (a list of bead dicts) via
    bd.json(["ready"], cwd);
  * returns an empty list when bd.json returns None (bd exits non-zero).

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


async def _read(server, uri: str):
    from fastmcp import Client

    async with Client(server) as client:
        return await client.read_resource(uri)


async def _list_resources(server):
    from fastmcp import Client

    async with Client(server) as client:
        return await client.list_resources()


def _patch_ready(monkeypatch, payload):
    """Monkeypatch bd.run so bd.json(["ready"], cwd) returns payload.

    Also pins registry.rig_dir_for to a fixed cwd so rig_dir_for(cfg, rig="") doesn't
    hit the filesystem."""
    monkeypatch.setattr(
        bd_mod,
        "_run",
        lambda cmd, **_kw: _CP(0, json.dumps(payload), ""),
    )
    monkeypatch.setattr(registry_mod, "hive_dir_for", lambda cfg, hive="": Path("/fake/hive"))


# ---- registration check ------------------------------------------------------


def test_work_ready_resource_is_registered():
    """beadhive://work/ready appears in the server's resource list."""
    pytest.importorskip("fastmcp")
    server = mcp_mod.build_server()
    resources = asyncio.run(_list_resources(server))
    uris = {str(r.uri) for r in resources}
    assert "beadhive://work/ready" in uris


# ---- payload checks ----------------------------------------------------------


def test_work_ready_resource_returns_list_of_bead_dicts(monkeypatch):
    """beadhive://work/ready returns the same list of bead dicts as bd ready --json."""
    pytest.importorskip("fastmcp")
    beads = [
        {"id": "", "title": "first ready bead", "status": "open"},
        {"id": "", "title": "second ready bead", "status": "open"},
    ]
    _patch_ready(monkeypatch, beads)
    server = mcp_mod.build_server()
    contents = asyncio.run(_read(server, "beadhive://work/ready"))
    assert contents, "expected at least one content block"
    data = json.loads(contents[0].text)
    assert isinstance(data, list), "beadhive://work/ready must return a list"
    assert len(data) == 2
    assert data[0]["id"] == ""
    assert data[1]["id"] == ""


def test_work_ready_resource_returns_empty_list_when_bd_fails(monkeypatch):
    """When bd exits non-zero (bd.json → None), beadhive://work/ready returns an empty list."""
    pytest.importorskip("fastmcp")
    monkeypatch.setattr(
        bd_mod, "_run", lambda cmd, **_kw: _CP(1, "", "bd error")
    )
    monkeypatch.setattr(registry_mod, "hive_dir_for", lambda cfg, hive="": Path("/fake/hive"))
    server = mcp_mod.build_server()
    contents = asyncio.run(_read(server, "beadhive://work/ready"))
    assert contents, "expected at least one content block"
    data = json.loads(contents[0].text)
    assert data == [], f"expected empty list on bd failure, got {data!r}"


def test_work_ready_resource_returns_empty_list_on_empty_bd_output(monkeypatch):
    """When bd returns an empty list (no ready beads), resource returns []."""
    pytest.importorskip("fastmcp")
    _patch_ready(monkeypatch, [])
    server = mcp_mod.build_server()
    contents = asyncio.run(_read(server, "beadhive://work/ready"))
    assert contents, "expected at least one content block"
    data = json.loads(contents[0].text)
    assert data == []


# ---- annotation / mime checks ------------------------------------------------


def test_work_ready_resource_has_json_mime_and_readonly_idempotent_annotations():
    """beadhive://work/ready defaults: application/json + readOnlyHint + idempotentHint=True."""
    pytest.importorskip("fastmcp")
    server = mcp_mod.build_server()
    resources = asyncio.run(_list_resources(server))
    res = next((r for r in resources if str(r.uri) == "beadhive://work/ready"), None)
    assert res is not None, "beadhive://work/ready not found in resource list"
    assert res.mimeType == "application/json"
    assert res.annotations is not None
    assert res.annotations.readOnlyHint is True
    assert res.annotations.idempotentHint is True
