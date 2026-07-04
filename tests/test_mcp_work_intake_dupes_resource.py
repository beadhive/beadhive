""" — ws://work/intake/dupes resource.

Tests that the resource:
  * is registered and readable via the in-process FastMCP Client;
  * returns a list of dupe-pair dicts with the expected shape (issue_a_id,
    issue_b_id, similarity) via triage.find_dupes / triage.dupes_touching;
  * scopes results to the intake queue (only pairs where one side is an intake bead);
  * returns an empty list when no pairs exist.

All tests gated behind importorskip so CI stays green without the [mcp] extra installed.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ws import mcp as mcp_mod
from ws import plan as plan_mod
from ws import triage as triage_mod

# ---- helpers -----------------------------------------------------------------


async def _read(server, uri: str):
    from fastmcp import Client

    async with Client(server) as client:
        return await client.read_resource(uri)


async def _list_resources(server):
    from fastmcp import Client

    async with Client(server) as client:
        return await client.list_resources()


def _patch_triage(monkeypatch, pairs, intake_rows):
    """Monkeypatch triage helpers and pin plan._rig_dir to a fixed path."""
    monkeypatch.setattr(plan_mod, "_rig_dir", lambda cfg, rig="": Path("/fake/rig"))
    monkeypatch.setattr(triage_mod, "find_dupes", lambda cwd, **_kw: pairs)
    monkeypatch.setattr(triage_mod, "list_intake", lambda cwd, **_kw: intake_rows)


# ---- registration check -------------------------------------------------------


def test_work_intake_dupes_resource_is_registered():
    """ws://work/intake/dupes appears in the server's resource list."""
    pytest.importorskip("fastmcp")
    server = mcp_mod.build_server()
    resources = asyncio.run(_list_resources(server))
    uris = {str(r.uri) for r in resources}
    assert "ws://work/intake/dupes" in uris


# ---- payload checks -----------------------------------------------------------


def test_work_intake_dupes_resource_returns_list(monkeypatch):
    """ws://work/intake/dupes returns a JSON list."""
    pytest.importorskip("fastmcp")
    _patch_triage(monkeypatch, pairs=[], intake_rows=[])
    server = mcp_mod.build_server()
    contents = asyncio.run(_read(server, "ws://work/intake/dupes"))
    assert contents, "expected at least one content block"
    data = json.loads(contents[0].text)
    assert isinstance(data, list), f"expected list, got {type(data)}"


def test_work_intake_dupes_resource_returns_pair_shape(monkeypatch):
    """ws://work/intake/dupes returns pair dicts with issue_a_id / issue_b_id / similarity."""
    pytest.importorskip("fastmcp")
    pairs = [
        {"issue_a_id": "", "issue_b_id": "", "similarity": 0.9},
    ]
    intake_rows = [{"id": ""}]
    _patch_triage(monkeypatch, pairs=pairs, intake_rows=intake_rows)
    server = mcp_mod.build_server()
    contents = asyncio.run(_read(server, "ws://work/intake/dupes"))
    assert contents, "expected at least one content block"
    data = json.loads(contents[0].text)
    assert isinstance(data, list), f"expected list, got {type(data)}"
    assert len(data) == 1, f"expected 1 pair, got {len(data)}"
    pair = data[0]
    assert "issue_a_id" in pair, f"pair missing 'issue_a_id': {pair}"
    assert "issue_b_id" in pair, f"pair missing 'issue_b_id': {pair}"
    assert "similarity" in pair, f"pair missing 'similarity': {pair}"
    assert pair["issue_a_id"] == ""
    assert pair["issue_b_id"] == ""


def test_work_intake_dupes_resource_scopes_to_intake(monkeypatch):
    """Only pairs where one side is an intake bead are returned."""
    pytest.importorskip("fastmcp")
    pairs = [
        # touching intake bead — should be included
        {"issue_a_id": "", "issue_b_id": "", "similarity": 0.8},
        # no intake bead on either side — should be excluded
        {"issue_a_id": "", "issue_b_id": "", "similarity": 0.7},
    ]
    intake_rows = [{"id": ""}]
    _patch_triage(monkeypatch, pairs=pairs, intake_rows=intake_rows)
    server = mcp_mod.build_server()
    contents = asyncio.run(_read(server, "ws://work/intake/dupes"))
    assert contents, "expected at least one content block"
    data = json.loads(contents[0].text)
    assert len(data) == 1, f"expected 1 scoped pair, got {len(data)}: {data}"
    assert data[0]["issue_a_id"] == ""


def test_work_intake_dupes_resource_returns_empty_list_when_no_pairs(monkeypatch):
    """ws://work/intake/dupes returns [] when find_dupes yields nothing."""
    pytest.importorskip("fastmcp")
    _patch_triage(monkeypatch, pairs=, intake_rows=[{"id": ""}])
    server = mcp_mod.build_server()
    contents = asyncio.run(_read(server, "ws://work/intake/dupes"))
    assert contents, "expected at least one content block"
    data = json.loads(contents[0].text)
    assert data == [], f"expected empty list, got {data!r}"


# ---- annotation / mime checks -------------------------------------------------


def test_work_intake_dupes_resource_has_json_mime_and_readonly_idempotent_annotations():
    """ws://work/intake/dupes defaults: application/json + readOnly + idempotent annotations."""
    pytest.importorskip("fastmcp")
    server = mcp_mod.build_server()
    resources = asyncio.run(_list_resources(server))
    res = next((r for r in resources if str(r.uri) == "ws://work/intake/dupes"), None)
    assert res is not None, "ws://work/intake/dupes not found in resource list"
    assert res.mimeType == "application/json"
    assert res.annotations is not None
    assert res.annotations.readOnlyHint is True
    assert res.annotations.idempotentHint is True
