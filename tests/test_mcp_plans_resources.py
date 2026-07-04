""" — ws://plans + ws://plan/{ref} resources.

Tests that both resources:
  * are registered and readable via the in-process FastMCP Client;
  * return the expected JSON payloads via bd.json(["swarm", ...], cwd);
  * return None when bd exits non-zero.

All tests gated behind importorskip so CI stays green without the [mcp] extra installed.
"""

from __future__ import annotations

import asyncio
import json
from collections import namedtuple
from pathlib import Path

import pytest

from ws import bd as bd_mod
from ws import mcp as mcp_mod
from ws import plan as plan_mod

# ---- helpers -----------------------------------------------------------------

_CP = namedtuple("CP", "returncode stdout stderr")

SWARM_LIST = [
    {"ref": "", "title": "eybf epic", "status": "in_progress"},
    {"ref": "", "title": "jnv epic", "status": "done"},
]

SWARM_STATUS = {
    "ref": "",
    "title": "eybf epic",
    "status": "in_progress",
    "members": 13,
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


def _patch_bd(monkeypatch, swarm_ref: str, list_payload, status_payload):
    """Monkeypatch bd.run so swarm list/status return the given payloads.

    Also pins plan._rig_dir to a fixed cwd so _rig_dir(cfg, rig="") doesn't
    hit the filesystem.
    """

    def _fake_run(cmd, **_kw):
        # bd.json calls: ["bd", "-C", cwd, "swarm", "list"/"status", ..., "--json"]
        if "swarm" in cmd and "list" in cmd:
            return _CP(0, json.dumps(list_payload), "")
        if "swarm" in cmd and "status" in cmd and swarm_ref in cmd:
            return _CP(0, json.dumps(status_payload), "")
        return _CP(1, "", "not found")

    monkeypatch.setattr(bd_mod, "run", _fake_run)
    monkeypatch.setattr(plan_mod, "_rig_dir", lambda cfg, rig="": Path("/fake/rig"))


# ---- registration checks -----------------------------------------------------


def test_plans_resource_is_registered():
    """ws://plans appears in the server's resource list.

    FastMCP normalizes host-only URIs to include a trailing slash (same as ws://config/).
    Check for the normalized form ws://plans/.
    """
    pytest.importorskip("fastmcp")
    server = mcp_mod.build_server()
    resources = asyncio.run(_list_resources(server))
    uris = {str(r.uri) for r in resources}
    # FastMCP normalizes ws://plans → ws://plans/ (pydantic AnyUrl host-only form)
    assert "ws://plans/" in uris, f"expected ws://plans/ in resource list, got: {uris}"


def test_plan_ref_resource_is_registered():
    """ws://plan/{ref} appears in the server's resource template list."""
    pytest.importorskip("fastmcp")
    server = mcp_mod.build_server()
    templates = asyncio.run(_list_resource_templates(server))
    uris = {str(t.uriTemplate) for t in templates}
    assert "ws://plan/{ref}" in uris, (
        f"expected ws://plan/{{ref}} in resource template list, got: {uris}"
    )


# ---- payload checks: ws://plans ----------------------------------------------


def test_plans_resource_returns_swarm_list(monkeypatch):
    """ws://plans returns the swarm list via bd.json(["swarm", "list"], cwd)."""
    pytest.importorskip("fastmcp")
    _patch_bd(monkeypatch, "", SWARM_LIST, SWARM_STATUS)
    server = mcp_mod.build_server()
    contents = asyncio.run(_read(server, "ws://plans"))
    assert contents, "expected at least one content block"
    data = json.loads(contents[0].text)
    assert isinstance(data, list), f"ws://plans must return a list, got: {type(data)}"
    assert len(data) == 2
    assert data[0]["ref"] == ""
    assert data[1]["ref"] == ""


def test_plans_resource_returns_none_when_bd_fails(monkeypatch):
    """When bd exits non-zero, ws://plans returns None."""
    pytest.importorskip("fastmcp")
    monkeypatch.setattr(bd_mod, "run", lambda cmd, **_kw: _CP(1, "", "bd error"))
    monkeypatch.setattr(plan_mod, "_rig_dir", lambda cfg, rig="": Path("/fake/rig"))
    server = mcp_mod.build_server()
    contents = asyncio.run(_read(server, "ws://plans"))
    assert contents, "expected at least one content block"
    data = json.loads(contents[0].text)
    assert data is None, f"expected None on bd failure, got {data!r}"


# ---- payload checks: ws://plan/{ref} -----------------------------------------


def test_plan_ref_resource_returns_molecule_status(monkeypatch):
    """ws://plan/<ref> returns the molecule status via bd.json(["swarm","status",ref], cwd)."""
    pytest.importorskip("fastmcp")
    ref = ""
    _patch_bd(monkeypatch, ref, SWARM_LIST, SWARM_STATUS)
    server = mcp_mod.build_server()
    contents = asyncio.run(_read(server, f"ws://plan/{ref}"))
    assert contents, "expected at least one content block"
    data = json.loads(contents[0].text)
    assert isinstance(data, dict), f"ws://plan/<ref> must return a dict, got: {type(data)}"
    assert data["ref"] == ref
    assert data["status"] == "in_progress"
    assert data["members"] == 13


def test_plan_ref_resource_returns_none_when_not_found(monkeypatch):
    """When bd exits non-zero (ref not found), ws://plan/<ref> returns None."""
    pytest.importorskip("fastmcp")
    monkeypatch.setattr(bd_mod, "run", lambda cmd, **_kw: _CP(1, "", "not found"))
    monkeypatch.setattr(plan_mod, "_rig_dir", lambda cfg, rig="": Path("/fake/rig"))
    server = mcp_mod.build_server()
    contents = asyncio.run(_read(server, "ws://plan/"))
    assert contents, "expected at least one content block"
    data = json.loads(contents[0].text)
    assert data is None, f"expected None when ref not found, got {data!r}"


# ---- annotation / mime checks ------------------------------------------------


def test_plans_resource_has_json_mime_and_readonly_idempotent_annotations():
    """ws://plans defaults: application/json + readOnlyHint=True + idempotentHint=True.

    FastMCP normalizes host-only URIs to include a trailing slash (ws://plans/).
    """
    pytest.importorskip("fastmcp")
    server = mcp_mod.build_server()
    resources = asyncio.run(_list_resources(server))
    # FastMCP normalizes ws://plans → ws://plans/ (pydantic AnyUrl host-only form)
    res = next((r for r in resources if str(r.uri) == "ws://plans/"), None)
    assert res is not None, "ws://plans/ not found in resource list"
    assert res.mimeType == "application/json"
    assert res.annotations is not None
    assert res.annotations.readOnlyHint is True
    assert res.annotations.idempotentHint is True


def test_plan_ref_resource_has_json_mime_and_readonly_idempotent_annotations():
    """ws://plan/{ref} defaults: application/json + readOnlyHint=True + idempotentHint=True."""
    pytest.importorskip("fastmcp")
    server = mcp_mod.build_server()
    templates = asyncio.run(_list_resource_templates(server))
    tmpl = next((t for t in templates if str(t.uriTemplate) == "ws://plan/{ref}"), None)
    assert tmpl is not None, "ws://plan/{ref} not found in resource template list"
    assert tmpl.mimeType == "application/json"
    assert tmpl.annotations is not None
    assert tmpl.annotations.readOnlyHint is True
    assert tmpl.annotations.idempotentHint is True
