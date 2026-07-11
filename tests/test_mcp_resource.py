""" — _measured_resource seam: registration, otel metrics, defaults.

Tests the resource registrar helper and the beadhive://probe/health probe that proves it works.

Two halves:
  * structural checks — the probe URI appears in the resource list, returns JSON content,
    and carries the correct mime_type + readOnly/idempotent annotations (all gated on
    importorskip so CI stays green without the [mcp] extra);
  * otel instrumentation — same mock-meter pattern as test_mcp.py: reads the probe resource
    via an in-process FastMCP Client and asserts that ws.mcp.resource.invocations counter +
    ws.mcp.resource.duration histogram are recorded when otel is on, and the instrument cache
    is untouched when otel is off (zero overhead).
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pytest

from beadhive import mcp as mcp_mod
from beadhive import otel as otel_mod

# ---- helpers -----------------------------------------------------------------


def _force_otel_on(monkeypatch) -> MagicMock:
    """Force otel active with a fresh mocked meter; return it for assertions."""
    meter = MagicMock(name="meter")
    monkeypatch.setattr(otel_mod, "_initialized", True)
    monkeypatch.setattr(otel_mod, "get_meter", lambda *a, **k: meter)
    monkeypatch.setattr(otel_mod, "_instruments", {})
    return meter


async def _read(server, uri: str):
    from fastmcp import Client

    async with Client(server) as client:
        return await client.read_resource(uri)


async def _list(server):
    from fastmcp import Client

    async with Client(server) as client:
        return await client.list_resources()


# ---- registration + structural checks ----------------------------------------


def test_probe_health_resource_is_registered():
    """beadhive://probe/health appears in the server's resource list."""
    pytest.importorskip("fastmcp")
    server = mcp_mod.build_server()
    resources = asyncio.run(_list(server))
    uris = {str(r.uri) for r in resources}
    assert "beadhive://probe/health" in uris


def test_probe_health_returns_json_content():
    """Reading beadhive://probe/health returns application/json with a 'status' field."""
    pytest.importorskip("fastmcp")
    server = mcp_mod.build_server()
    contents = asyncio.run(_read(server, "beadhive://probe/health"))
    assert contents, "expected at least one content block"
    data = json.loads(contents[0].text)
    assert data.get("status") == "ok"


def test_probe_resource_has_json_mime_and_readonly_idempotent_annotations():
    """_measured_resource defaults: application/json + readOnlyHint=True + idempotentHint=True."""
    pytest.importorskip("fastmcp")
    server = mcp_mod.build_server()
    resources = asyncio.run(_list(server))
    probe = next((r for r in resources if str(r.uri) == "beadhive://probe/health"), None)
    assert probe is not None, "beadhive://probe/health not found in resource list"
    assert probe.mimeType == "application/json"
    assert probe.annotations is not None
    assert probe.annotations.readOnlyHint is True
    assert probe.annotations.idempotentHint is True


def test_config_resource_is_registered():
    """beadhive://config appears in the server's resource list."""
    pytest.importorskip("fastmcp")
    server = mcp_mod.build_server()
    resources = asyncio.run(_list(server))
    uris = {str(r.uri) for r in resources}
    assert "beadhive://config" in uris


def test_config_resource_returns_mapping():
    """Reading beadhive://config returns application/json with a dict of top-level config keys."""
    pytest.importorskip("fastmcp")
    server = mcp_mod.build_server()
    contents = asyncio.run(_read(server, "beadhive://config"))
    assert contents, "expected at least one content block"
    data = json.loads(contents[0].text)
    assert isinstance(data, dict), "config should return a dict/mapping"
    # Verify it contains at least some expected top-level keys from KNOWN_SECTIONS
    assert any(
        key in data
        for key in [
            "delimiter",
            "providers",
            "orgs",
            "exclude",
            "dimensions",
            "work",
            "managed_repos",
            "log",
            "otel",
        ]
    ), f"config dict should contain at least one expected top-level key, got: {list(data.keys())}"


def test_config_resource_has_json_mime_and_readonly_idempotent_annotations():
    """beadhive://config defaults: application/json + readOnlyHint + idempotentHint=True."""
    pytest.importorskip("fastmcp")
    server = mcp_mod.build_server()
    resources = asyncio.run(_list(server))
    config_res = next((r for r in resources if str(r.uri) == "beadhive://config"), None)
    assert config_res is not None, "beadhive://config not found in resource list"
    assert config_res.mimeType == "application/json"
    assert config_res.annotations is not None
    assert config_res.annotations.readOnlyHint is True
    assert config_res.annotations.idempotentHint is True


# ---- otel instrumentation checks ---------------------------------------------


def test_resource_emits_ok_counter_and_latency_when_otel_on(monkeypatch):
    """Reading a resource records ws.mcp.resource.invocations + ws.mcp.resource.duration."""
    pytest.importorskip("fastmcp")
    meter = _force_otel_on(monkeypatch)
    server = mcp_mod.build_server()

    asyncio.run(_read(server, "beadhive://probe/health"))

    # Counter: ws.mcp.resource.invocations, resource=beadhive://probe/health, outcome=ok.
    meter.create_counter.assert_called_once()
    assert meter.create_counter.call_args.args[0] == "bh.mcp.resource.invocations"
    meter.create_counter.return_value.add.assert_called_once_with(
        1, {"bh.mcp.resource": "beadhive://probe/health", "bh.mcp.outcome": "ok"}
    )
    # Histogram: ws.mcp.resource.duration with same tags and a non-negative duration.
    meter.create_histogram.assert_called_once()
    assert meter.create_histogram.call_args.args[0] == "bh.mcp.resource.duration"
    rec = meter.create_histogram.return_value.record.call_args
    assert rec.args[1] == {"bh.mcp.resource": "beadhive://probe/health", "bh.mcp.outcome": "ok"}
    assert rec.args[0] >= 0.0


def test_resource_invocation_is_noop_when_otel_off():
    """When otel is off, reading a resource works fine but never touches the instrument cache."""
    pytest.importorskip("fastmcp")
    assert not otel_mod.is_active()  # off by default in tests
    server = mcp_mod.build_server()
    contents = asyncio.run(_read(server, "beadhive://probe/health"))
    assert contents  # content returned correctly
    assert otel_mod._instruments == {}  # zero overhead — nothing cached when off


def test_config_resource_emits_ok_counter_and_latency_when_otel_on(monkeypatch):
    """Reading beadhive://config records ws.mcp.resource.invocations + ws.mcp.resource.duration."""
    pytest.importorskip("fastmcp")
    meter = _force_otel_on(monkeypatch)
    server = mcp_mod.build_server()

    asyncio.run(_read(server, "beadhive://config"))

    # Counter: ws.mcp.resource.invocations, resource=beadhive://config, outcome=ok.
    meter.create_counter.assert_called_once()
    assert meter.create_counter.call_args.args[0] == "bh.mcp.resource.invocations"
    meter.create_counter.return_value.add.assert_called_once_with(
        1, {"bh.mcp.resource": "beadhive://config", "bh.mcp.outcome": "ok"}
    )
    # Histogram: ws.mcp.resource.duration with same tags and a non-negative duration.
    meter.create_histogram.assert_called_once()
    assert meter.create_histogram.call_args.args[0] == "bh.mcp.resource.duration"
    rec = meter.create_histogram.return_value.record.call_args
    assert rec.args[1] == {"bh.mcp.resource": "beadhive://config", "bh.mcp.outcome": "ok"}
    assert rec.args[0] >= 0.0


# ---- beadhive://work/intake resource -----------------------------------------------


def test_work_intake_resource_is_registered():
    """beadhive://work/intake appears in the server's resource list."""
    pytest.importorskip("fastmcp")
    server = mcp_mod.build_server()
    resources = asyncio.run(_list(server))
    uris = {str(r.uri) for r in resources}
    assert "beadhive://work/intake" in uris


def test_work_intake_resource_returns_rows_and_dupes(monkeypatch):
    """Reading beadhive://work/intake returns a dict with 'rows' and 'dupes' keys."""
    pytest.importorskip("fastmcp")
    from beadhive import triage as triage_mod

    monkeypatch.setattr(
        triage_mod,
        "intake_payload",
        lambda *a, **kw: {"rows": [], "dupes": []},
    )
    server = mcp_mod.build_server()
    contents = asyncio.run(_read(server, "beadhive://work/intake"))
    assert contents, "expected at least one content block"
    data = json.loads(contents[0].text)
    assert "rows" in data, f"expected 'rows' key in payload, got: {list(data.keys())}"
    assert "dupes" in data, f"expected 'dupes' key in payload, got: {list(data.keys())}"


# ---- beadhive://config/{key} template resource ------------------------------------


def test_config_key_resource_is_registered():
    """beadhive://config/{key} template resource can be read with a concrete key."""
    pytest.importorskip("fastmcp")
    server = mcp_mod.build_server()
    # Template resources are read with concrete URIs, not listed
    contents = asyncio.run(_read(server, "beadhive://config/otel.protocol"))
    assert contents, "expected at least one content block"
    data = json.loads(contents[0].text)
    assert isinstance(data, dict), "config key resource should return a dict"


def test_config_key_resource_reads_scalar_key():
    """Reading beadhive://config/<scalar.key> returns {ok, problems, value} shape."""
    pytest.importorskip("fastmcp")
    server = mcp_mod.build_server()
    # otel.protocol is a scalar string key
    contents = asyncio.run(_read(server, "beadhive://config/otel.protocol"))
    assert contents, "expected at least one content block"
    data = json.loads(contents[0].text)
    assert "ok" in data, f"expected 'ok' key in payload, got: {list(data.keys())}"
    assert "problems" in data, f"expected 'problems' key in payload, got: {list(data.keys())}"
    assert "value" in data, f"expected 'value' key in payload, got: {list(data.keys())}"
    assert data["ok"] is True, "ok should be True for an existing dotted key"
    assert isinstance(data["problems"], list), "problems should be a list"
    assert len(data["problems"]) == 0, "problems should be empty for existing key"
    assert isinstance(data["value"], str), "value should be a string for otel.protocol"


def test_config_key_resource_reads_map_key():
    """Reading beadhive://config/<map.key> returns {ok, problems, value} with map value."""
    pytest.importorskip("fastmcp")
    server = mcp_mod.build_server()
    # exclude.repos is a map/list key
    contents = asyncio.run(_read(server, "beadhive://config/exclude.repos"))
    assert contents, "expected at least one content block"
    data = json.loads(contents[0].text)
    assert "ok" in data, f"expected 'ok' key in payload, got: {list(data.keys())}"
    assert "problems" in data, f"expected 'problems' key in payload, got: {list(data.keys())}"
    assert "value" in data, f"expected 'value' key in payload, got: {list(data.keys())}"
    assert data["ok"] is True, "ok should be True for an existing dotted key"
    assert isinstance(data["problems"], list), "problems should be a list"
    assert len(data["problems"]) == 0, "problems should be empty for existing key"
    assert isinstance(data["value"], list), "value should be a list for exclude.repos"


def test_config_key_resource_returns_valid_json():
    """beadhive://config/{key} resource returns valid json with {ok, problems, value}."""
    pytest.importorskip("fastmcp")
    server = mcp_mod.build_server()
    contents = asyncio.run(_read(server, "beadhive://config/otel.protocol"))
    assert contents, "expected at least one content block"
    assert contents[0].mimeType == "application/json"
    data = json.loads(contents[0].text)
    # Verify shape: {ok, problems, value}
    assert "ok" in data, f"expected 'ok' key in payload, got: {list(data.keys())}"
    assert "problems" in data, f"expected 'problems' key in payload, got: {list(data.keys())}"
    assert "value" in data, f"expected 'value' key in payload, got: {list(data.keys())}"


# ---- beadhive://worktrees resource -------------------------------------------------


def test_worktrees_resource_is_registered():
    """beadhive://worktrees appears in the resource list."""
    pytest.importorskip("fastmcp")
    server = mcp_mod.build_server()
    resources = asyncio.run(_list(server))
    uris = {str(r.uri) for r in resources}
    assert "beadhive://worktrees" in uris


def test_worktrees_resource_returns_list(monkeypatch):
    """Reading beadhive://worktrees returns a JSON list of WtStatus row dicts."""
    pytest.importorskip("fastmcp")
    from beadhive import worktree as worktree_mod

    _wt_row = {
        "rig": "workspace",
        "leaf": "-1",
        "branch": "wt/bead/issue/",
        "path": "/fake/path/-1",
        "bead_id": "",
        "classification": "active",
        "merged": False,
        "dirty": False,
        "safe": False,
    }
    monkeypatch.setattr(worktree_mod, "status_rows", lambda rig="": [_FakeWtStatus(_wt_row)])
    server = mcp_mod.build_server()
    contents = asyncio.run(_read(server, "beadhive://worktrees"))
    assert contents, "expected at least one content block"
    data = json.loads(contents[0].text)
    assert isinstance(data, list), f"beadhive://worktrees must return a list, got {type(data)}"
    assert len(data) == 1
    row = data[0]
    assert row["rig"] == "workspace"
    assert row["classification"] == "active"
    assert row["bead_id"] == ""
    assert row["merged"] is False
    assert row["dirty"] is False
    assert row["safe"] is False


def test_worktrees_resource_returns_empty_list_when_no_worktrees(monkeypatch):
    """When no managed worktrees exist, beadhive://worktrees returns an empty list."""
    pytest.importorskip("fastmcp")
    from beadhive import worktree as worktree_mod

    monkeypatch.setattr(worktree_mod, "status_rows", lambda rig="": [])
    server = mcp_mod.build_server()
    contents = asyncio.run(_read(server, "beadhive://worktrees"))
    assert contents, "expected at least one content block"
    data = json.loads(contents[0].text)
    assert data == [], f"expected empty list, got {data!r}"


def test_worktrees_resource_row_shape(monkeypatch):
    """Each row in beadhive://worktrees has the full WtStatus as_dict() shape."""
    pytest.importorskip("fastmcp")
    from beadhive import worktree as worktree_mod

    _wt_row = {
        "rig": "workspace",
        "leaf": "-2",
        "branch": "wt/bead/epic/",
        "path": "/fake/path/-2",
        "bead_id": "",
        "classification": "safe",
        "merged": True,
        "dirty": False,
        "safe": True,
    }
    monkeypatch.setattr(worktree_mod, "status_rows", lambda rig="": [_FakeWtStatus(_wt_row)])
    server = mcp_mod.build_server()
    contents = asyncio.run(_read(server, "beadhive://worktrees"))
    data = json.loads(contents[0].text)
    assert len(data) == 1
    row = data[0]
    _EXPECTED_KEYS = {
        "rig", "leaf", "branch", "path", "bead_id", "classification", "merged", "dirty", "safe"
    }
    assert _EXPECTED_KEYS.issubset(row.keys()), (
        f"row missing keys: {_EXPECTED_KEYS - set(row.keys())}"
    )
    assert row["safe"] is True
    assert row["merged"] is True
    assert row["classification"] == "safe"


def test_worktrees_resource_has_json_mime_and_readonly_idempotent_annotations():
    """beadhive://worktrees defaults: application/json + readOnlyHint=True + idempotentHint=True."""
    pytest.importorskip("fastmcp")
    server = mcp_mod.build_server()
    resources = asyncio.run(_list(server))
    res = next((r for r in resources if str(r.uri) == "beadhive://worktrees"), None)
    assert res is not None, "beadhive://worktrees not found in resource list"
    assert res.mimeType == "application/json"
    assert res.annotations is not None
    assert res.annotations.readOnlyHint is True
    assert res.annotations.idempotentHint is True


class _FakeWtStatus:
    """Minimal WtStatus stand-in for MCP resource tests — returns the dict from as_dict()."""

    def __init__(self, d: dict):
        self._d = d

    def as_dict(self) -> dict:
        return dict(self._d)


# ---- beadhive://hq/intake resource -------------------------------------------------


def test_hq_intake_resource_is_registered():
    """beadhive://hq/intake appears in the server's resource list."""
    pytest.importorskip("fastmcp")
    server = mcp_mod.build_server()
    resources = asyncio.run(_list(server))
    uris = {str(r.uri) for r in resources}
    assert "beadhive://hq/intake" in uris


def test_hq_intake_resource_returns_list(monkeypatch, tmp_path):
    """Reading beadhive://hq/intake returns a list of untriaged intake rows via bd.json."""
    pytest.importorskip("fastmcp")
    from beadhive import bd as bd_mod
    from beadhive import hub as hub_mod

    fake_beads = tmp_path / ".beads"
    fake_beads.mkdir()

    monkeypatch.setattr(hub_mod, "_aggregation_target", lambda: (tmp_path, "hub"))
    monkeypatch.setattr(bd_mod, "json", lambda args, cwd: [{"id": "bc-1", "title": "test"}])

    server = mcp_mod.build_server()
    contents = asyncio.run(_read(server, "beadhive://hq/intake"))
    assert contents, "expected at least one content block"
    data = json.loads(contents[0].text)
    assert isinstance(data, list), f"expected a list, got: {type(data)}"
    assert len(data) == 1
    assert data[0]["id"] == "bc-1"


def test_hq_intake_resource_returns_empty_when_no_hub(monkeypatch, tmp_path):
    """Reading beadhive://hq/intake returns an empty list when the hub store is absent."""
    pytest.importorskip("fastmcp")
    from beadhive import hub as hub_mod

    # tmp_path exists but has no .beads subdir — simulates missing/uninitialized hub
    monkeypatch.setattr(hub_mod, "_aggregation_target", lambda: (tmp_path, "hub"))

    server = mcp_mod.build_server()
    contents = asyncio.run(_read(server, "beadhive://hq/intake"))
    assert contents, "expected at least one content block"
    data = json.loads(contents[0].text)
    assert data == [], f"expected empty list for missing hub, got: {data}"
