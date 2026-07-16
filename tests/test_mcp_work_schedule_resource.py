""" — schedule_payload core + beadhive://work/schedule/{epic} resource.

Tests that:
  * schedule_payload() returns {groups, singletons, coordinators, max_depth}.
  * schedule_payload() enriches groups with per-group model (tier) field.
  * schedule_payload() enriches coordinators with dispatch + model fields.
  * schedule_payload() raises ValueError when bd.json returns a non-list.
  * schedule_payload() singletons field is a plain list (JSON-serialisable).
  * beadhive://work/schedule/{epic} appears in the server's resource template list.
  * beadhive://work/schedule/{epic} returns the schedule_payload() result for a known epic.
  * beadhive://work/schedule/{epic} surfaces a ResourceError when ValueError is raised.
  * Default MIME type and annotations (readOnlyHint + idempotentHint).

MCP tests gated behind importorskip so CI stays green without the [mcp] extra installed.
"""

from __future__ import annotations

import asyncio
import json
from collections import namedtuple
from pathlib import Path

import pytest

from beadhive import bd as bd_mod
from beadhive import config as config_mod
from beadhive import mcp as mcp_mod
from beadhive import work as work_mod
from beadhive import worktree as worktree_mod

# ---- constants ---------------------------------------------------------------

_CP = namedtuple("CP", "returncode stdout stderr")

FAKE_ENTRY = {"provider": "github", "org": "myorg", "repo": "myrepo", "prefix": "mr"}
FAKE_MAIN = Path("/fake/main")
FAKE_EPIC = "mr-epic"

FAKE_PAYLOAD = {
    "groups": [{"kind": "chain", "ids": ["mr-1", "mr-2"], "reason": "linear", "model": "sonnet"}],
    "singletons": ["mr-3"],
    "coordinators": [],
    "max_depth": 2,
}

# ---- async helpers -----------------------------------------------------------


async def _read(server, uri: str):
    from fastmcp import Client

    async with Client(server) as client:
        return await client.read_resource(uri)


async def _list_resource_templates(server):
    from fastmcp import Client

    async with Client(server) as client:
        return await client.list_resource_templates()


# ---- schedule_payload() unit tests -------------------------------------------


def _bead(bead_id, *, labels=None, issue_type=None):
    """Minimal bead dict for schedule_payload() inputs."""
    b = {"id": bead_id, "labels": list(labels or []), "dependencies": [], "status": "open"}
    if issue_type:
        b["issue_type"] = issue_type
    return b


def _patch_schedule_deps(monkeypatch, beads: list):
    """Patch bd.json to return beads for any list --parent call; pin config to defaults."""
    def _fake_bd_run(cmd, **_kw):
        if "list" in cmd and "--parent" in cmd:
            return _CP(0, json.dumps(beads), "")
        return _CP(1, "", "unexpected")

    monkeypatch.setattr(bd_mod, "_run", _fake_bd_run)
    monkeypatch.setattr(config_mod, "load", lambda: {})
    monkeypatch.setattr(config_mod, "dispatch_mode", lambda cfg, entry: "fanout")
    monkeypatch.setattr(config_mod, "batch_max_size", lambda cfg, entry: 5)
    monkeypatch.setattr(config_mod, "dispatch_max_depth", lambda cfg, entry: 2)
    monkeypatch.setattr(config_mod, "dispatch_auto_budget", lambda cfg, entry: 8)
    monkeypatch.setattr(config_mod, "dispatch_max_beads_per_session", lambda cfg, entry: 8)


def test_schedule_payload_returns_required_keys(monkeypatch):
    """schedule_payload() returns a dict with groups, singletons, coordinators, max_depth."""
    _patch_schedule_deps(monkeypatch, [_bead("mr-1"), _bead("mr-2")])
    result = work_mod.schedule_payload(FAKE_EPIC, {}, FAKE_ENTRY, FAKE_MAIN)
    assert set(result.keys()) == {"groups", "singletons", "coordinators", "max_depth"}


def test_schedule_payload_singletons_is_list(monkeypatch):
    """schedule_payload() singletons is a plain list (not a tuple), JSON-serialisable."""
    _patch_schedule_deps(monkeypatch, [_bead("mr-1"), _bead("mr-2")])
    result = work_mod.schedule_payload(FAKE_EPIC, {}, FAKE_ENTRY, FAKE_MAIN)
    assert isinstance(result["singletons"], list)
    assert sorted(result["singletons"]) == ["mr-1", "mr-2"]


def test_schedule_payload_group_carries_model_field(monkeypatch):
    """schedule_payload() enriches each group dict with a model (tier) field."""
    beads = [_bead("mr-1", labels=["batch:g"]), _bead("mr-2", labels=["batch:g"])]
    _patch_schedule_deps(monkeypatch, beads)
    result = work_mod.schedule_payload(FAKE_EPIC, {}, FAKE_ENTRY, FAKE_MAIN)
    assert len(result["groups"]) == 1
    g = result["groups"][0]
    assert "model" in g
    assert "kind" in g and "ids" in g and "reason" in g


def test_schedule_payload_coordinator_carries_dispatch_and_model(monkeypatch):
    """schedule_payload() enriches coordinator entries with dispatch + model fields."""
    beads = [_bead("mr-ws.1", issue_type="epic", labels=["model:opus"])]
    _patch_schedule_deps(monkeypatch, beads)
    result = work_mod.schedule_payload(FAKE_EPIC, {}, FAKE_ENTRY, FAKE_MAIN)
    assert len(result["coordinators"]) == 1
    c = result["coordinators"][0]
    assert c["id"] == "mr-ws.1"
    assert "dispatch" in c
    assert "model" in c
    assert c["model"] == "opus"


def test_schedule_payload_max_depth_reflected(monkeypatch):
    """schedule_payload() max_depth reflects the configured dispatch max_depth."""
    _patch_schedule_deps(monkeypatch, [])
    monkeypatch.setattr(config_mod, "dispatch_max_depth", lambda cfg, entry: 0)
    result = work_mod.schedule_payload(FAKE_EPIC, {}, FAKE_ENTRY, FAKE_MAIN)
    assert result["max_depth"] == 0


def test_schedule_payload_raises_value_error_when_bd_fails(monkeypatch):
    """schedule_payload() raises ValueError when bd.json returns a non-list (bd failure)."""
    monkeypatch.setattr(bd_mod, "_run", lambda cmd, **_kw: _CP(1, "", "bd error"))
    with pytest.raises(ValueError, match="cannot list children"):
        work_mod.schedule_payload(FAKE_EPIC, {}, FAKE_ENTRY, FAKE_MAIN)


def test_schedule_payload_excludes_closed_beads(monkeypatch):
    """schedule_payload() filters out closed beads before scheduling."""
    beads = [_bead("mr-1"), {**_bead("mr-2"), "status": "closed"}]
    _patch_schedule_deps(monkeypatch, beads)
    result = work_mod.schedule_payload(FAKE_EPIC, {}, FAKE_ENTRY, FAKE_MAIN)
    all_ids = result["singletons"] + [i for g in result["groups"] for i in g["ids"]]
    assert "mr-2" not in all_ids
    assert "mr-1" in all_ids


# ---- beadhive://work/schedule/{epic} resource tests --------------------------------


def _patch_resource(monkeypatch, payload: dict):
    """Patch worktree.locate + work.schedule_payload for resource-level tests."""
    def _fake_locate(cfg, hive, bead, **kw):
        return FAKE_ENTRY, FAKE_MAIN, Path("/fake/wt"), "wt/bead/epic/mr-epic"

    monkeypatch.setattr(worktree_mod, "locate", _fake_locate)
    monkeypatch.setattr(
        work_mod,
        "schedule_payload",
        lambda epic, cfg, entry, main: payload,
    )
    monkeypatch.setattr(config_mod, "load", lambda: {})


def _patch_resource_error(monkeypatch):
    """Patch work.schedule_payload to raise ValueError (simulates missing epic)."""
    def _fake_locate(cfg, hive, bead, **kw):
        return FAKE_ENTRY, FAKE_MAIN, Path("/fake/wt"), "wt/bead/epic/mr-epic"

    monkeypatch.setattr(worktree_mod, "locate", _fake_locate)
    monkeypatch.setattr(
        work_mod,
        "schedule_payload",
        lambda epic, cfg, entry, main: (_ for _ in ()).throw(
            ValueError("cannot list children of bad-epic — is it an epic in this hive?")
        ),
    )
    monkeypatch.setattr(config_mod, "load", lambda: {})


# ---- registration check ------------------------------------------------------


def test_work_schedule_resource_is_registered():
    """beadhive://work/schedule/{epic} appears in the server's resource template list."""
    pytest.importorskip("fastmcp")
    server = mcp_mod.build_server()
    templates = asyncio.run(_list_resource_templates(server))
    uris = {str(t.uriTemplate) for t in templates}
    assert "beadhive://work/schedule/{epic}" in uris, (
        f"expected beadhive://work/schedule/{{epic}} in resource template list, got: {uris}"
    )


# ---- payload checks ----------------------------------------------------------


def test_work_schedule_resource_returns_payload(monkeypatch):
    """beadhive://work/schedule/<epic> returns the schedule_payload() dict."""
    pytest.importorskip("fastmcp")
    _patch_resource(monkeypatch, FAKE_PAYLOAD)
    server = mcp_mod.build_server()
    contents = asyncio.run(_read(server, f"beadhive://work/schedule/{FAKE_EPIC}"))
    assert contents, "expected at least one content block"
    data = json.loads(contents[0].text)
    assert isinstance(data, dict)
    assert set(data.keys()) == {"groups", "singletons", "coordinators", "max_depth"}
    assert data["max_depth"] == 2
    assert data["singletons"] == ["mr-3"]
    assert len(data["groups"]) == 1
    assert data["groups"][0]["kind"] == "chain"


def test_work_schedule_resource_raises_error_on_missing_epic(monkeypatch):
    """beadhive://work/schedule/<epic> raises McpError when schedule_payload raises ValueError.

    The server maps ValueError → ResourceError internally; the MCP client receives this
    as an mcp.shared.exceptions.McpError over the protocol.
    """
    pytest.importorskip("fastmcp")
    from mcp.shared.exceptions import McpError

    _patch_resource_error(monkeypatch)
    server = mcp_mod.build_server()
    with pytest.raises(McpError):
        asyncio.run(_read(server, "beadhive://work/schedule/bad-epic"))


# ---- annotation / mime checks ------------------------------------------------


def test_work_schedule_resource_has_json_mime_and_readonly_idempotent_annotations():
    """beadhive://work/schedule/{epic} defaults: json mime + readOnlyHint + idempotentHint."""
    pytest.importorskip("fastmcp")
    server = mcp_mod.build_server()
    templates = asyncio.run(_list_resource_templates(server))
    tmpl = next(
        (t for t in templates if str(t.uriTemplate) == "beadhive://work/schedule/{epic}"), None
    )
    assert tmpl is not None, "beadhive://work/schedule/{epic} not found in resource template list"
    assert tmpl.mimeType == "application/json"
    assert tmpl.annotations is not None
    assert tmpl.annotations.readOnlyHint is True
    assert tmpl.annotations.idempotentHint is True
