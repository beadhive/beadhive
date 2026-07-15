""" — beadhive://doctor structured-diagnostics resource.

Tests that the resource:
  * is registered and readable via the in-process FastMCP Client;
  * returns doctor.doctor_payload() verbatim (the section-keyed structured dict).

Gated behind importorskip so CI stays green without the [mcp] extra installed.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from beadhive import doctor as doctor_mod
from beadhive import mcp as mcp_mod

# The section keys beadhive://doctor exposes (kept in lockstep with doctor.doctor_payload).
_SECTIONS = {
    "config",
    "providers",
    "orgs",
    "rigs",
    "inventory",
    "disk_usage",
    "fleet_health",
    "worktrees",
    "molecules",
    "group_auth",
    "mcp",
    "observability",
    "warnings",
}


async def _read(server, uri: str):
    from fastmcp import Client

    async with Client(server) as client:
        return await client.read_resource(uri)


async def _list_resources(server):
    from fastmcp import Client

    async with Client(server) as client:
        return await client.list_resources()


def test_doctor_resource_is_registered():
    """beadhive://doctor appears in the server's resource list."""
    pytest.importorskip("fastmcp")
    server = mcp_mod.build_server()
    resources = asyncio.run(_list_resources(server))
    uris = {str(r.uri) for r in resources}
    assert "beadhive://doctor" in uris


def test_doctor_resource_returns_payload_section_keys(monkeypatch):
    """Reading beadhive://doctor returns doctor.doctor_payload() with every section key."""
    pytest.importorskip("fastmcp")
    fake = {k: [] if k in ("providers", "orgs", "rigs", "warnings") else {} for k in _SECTIONS}
    monkeypatch.setattr(doctor_mod, "doctor_payload", lambda: fake)

    server = mcp_mod.build_server()
    contents = asyncio.run(_read(server, "beadhive://doctor"))
    assert contents, "expected at least one content block"
    data = json.loads(contents[0].text)
    assert set(data.keys()) == _SECTIONS
