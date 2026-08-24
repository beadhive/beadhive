"""— beadhive://doctor structured-diagnostics resource.

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
from test_work import fakebd, hive  # noqa: F401 — fixtures resolved by name

# The section keys beadhive://doctor exposes (kept in lockstep with doctor.doctor_payload).
_SECTIONS = {
    "config",
    "providers",
    "orgs",
    "hives",
    "inventory",
    "disk_usage",
    "worktree_disk_usage",
    "fleet_health",
    "worktrees",
    "molecules",
    "prefix_mismatches",
    "store_engine",
    "group_auth",
    "mcp",
    "seats",
    "install",
    "observability",
    "warnings",
}

# The version envelope `doctor_payload` wraps those sections in (bh-0olv9.2). Asserted HERE
# because the MCP resource and `bh doctor --json` serve the same object: a version added for the
# CLI consumer and missing from the resource would be two contracts for one payload.
_ENVELOPE = {"schema_version": 1, "command": "doctor"}


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
    fake = {
        **_ENVELOPE,
        **{k: [] if k in ("providers", "orgs", "hives", "warnings") else {} for k in _SECTIONS},
    }
    monkeypatch.setattr(doctor_mod, "doctor_payload", lambda: fake)

    server = mcp_mod.build_server()
    contents = asyncio.run(_read(server, "beadhive://doctor"))
    assert contents, "expected at least one content block"
    data = json.loads(contents[0].text)
    assert set(data.keys()) == _SECTIONS | set(_ENVELOPE)
    assert {k: data[k] for k in _ENVELOPE} == _ENVELOPE


def test_real_doctor_payload_carries_the_version_envelope(hive, fakebd):  # noqa: F811
    """The envelope on the REAL builder, not just on a fake — the resource's contract is only
    versioned if `doctor_payload` itself stamps it (see its docstring)."""
    payload = doctor_mod.doctor_payload()
    assert {k: payload[k] for k in _ENVELOPE} == _ENVELOPE
