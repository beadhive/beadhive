"""Self-checks for the FastMCP stdio server (scaffold jnv.2 + tools jnv.3).

Two halves:
  * the absent-`fastmcp` path — must fail gracefully with an install hint and never
    crash the importer (runs everywhere, even if the dev env has no `fastmcp`);
  * the present-`fastmcp` path — in-process `Client(server)` checks against the real
    server: the tool list is exactly the selected complex-input tools, plus a happy
    path (`plan_check` valid spec → structured output) and an error-mapping path
    (`plan_file` invalid spec → MoleculeError surfaced as a `ToolError`). All gated
    behind `importorskip` so `just check` stays green without the extra installed.
"""

from __future__ import annotations

import asyncio
import json
import sys
from unittest.mock import MagicMock

import pytest

from beadhive import config as config_mod
from beadhive import hive as hive_mod
from beadhive import mcp as mcp_mod
from beadhive import otel as otel_mod
from beadhive import registry as registry_mod


def test_importing_ws_mcp_does_not_require_fastmcp():
    # The module imports cleanly even with the extra absent: fastmcp is imported lazily.
    assert "fastmcp" not in sys.modules or sys.modules.get("fastmcp") is not None
    assert hasattr(mcp_mod, "build_server")


def test_build_server_without_fastmcp_raises_friendly(monkeypatch):
    # Force the lazy import to fail regardless of whether the extra is installed.
    monkeypatch.setitem(sys.modules, "fastmcp", None)
    with pytest.raises(mcp_mod.MCPUnavailable) as excinfo:
        mcp_mod.build_server()
    msg = str(excinfo.value).lower()
    assert "fastmcp" in msg
    assert "install" in msg and "beadhive[otel]" in msg
    assert "ws[mcp]" not in msg


def test_main_without_fastmcp_returns_error_and_hints(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "fastmcp", None)
    code = mcp_mod.main()
    assert code == 1
    err = capsys.readouterr().err.lower()
    assert "install" in err and "beadhive[otel]" in err
    assert "ws[mcp]" not in err


# The complex-input tools the MCP surface exposes — and nothing else (simple/bulk CLI-only
# commands stay off the surface). jnv.3 seeded the planning/work tools; jpp4.8 added
# `hives_available`; jpp4.4 added the four control-plane tools. Note the deliberate absences:
# `config_get` (a scalar read) and `hive_rm` (destructive) are intentionally CLI-only.
_SELECTED_TOOLS = {
    "plan_check",
    "plan_file",
    "work_refine",
    "bd_create",
    "hives_available",
    "config_set",
    "hive_add",
    "hive_onboard",
    "hives_status",
}


def test_in_memory_lists_exactly_the_selected_tools():
    pytest.importorskip("fastmcp")
    from fastmcp import Client

    server = mcp_mod.build_server()

    async def handshake():
        # `async with` performs the MCP initialize handshake over the in-memory transport.
        async with Client(server) as client:
            await client.ping()
            tools = await client.list_tools()
            return {t.name for t in tools}

    assert asyncio.run(handshake()) == _SELECTED_TOOLS


def test_plan_check_happy_path_returns_structured_validation():
    pytest.importorskip("fastmcp")
    from fastmcp import Client

    server = mcp_mod.build_server()
    spec = {
        "epic": {"title": "Demo epic"},
        "issues": [{"handle": "a", "title": "do a thing", "acceptance": "it works"}],
    }

    async def call():
        async with Client(server) as client:
            return await client.call_tool("plan_check", {"spec": spec})

    result = asyncio.run(call())
    # Structured output (not a raw CLI string): {valid, problems}.
    assert result.data == {"valid": True, "problems": []}
    assert result.structured_content["valid"] is True


def test_plan_file_invalid_spec_maps_to_tool_error():
    pytest.importorskip("fastmcp")
    from fastmcp import Client
    from fastmcp.exceptions import ToolError

    server = mcp_mod.build_server()
    # Missing 'acceptance' → molecule.MoleculeError, which the wrapper maps to a ToolError
    # (and refuses to file). Fails before any bd/git call, so no hive fixture is needed.
    bad = {"epic": {"title": "E"}, "issues": [{"handle": "a", "title": "no acceptance"}]}

    async def call():
        async with Client(server) as client:
            return await client.call_tool("plan_file", {"spec": bad})

    with pytest.raises(ToolError) as excinfo:
        asyncio.run(call())
    msg = str(excinfo.value).lower()
    assert "invalid molecule spec" in msg
    assert "acceptance" in msg


# ---- control-plane tools (jpp4.4): config_set / hive_add / hive_onboard / hives_status ----
#
# Thin wrappers over the jpp4.1/.2/.3/.8 cores: each test stubs the core so it exercises the
# wrapper's translation + structured return + error mapping WITHOUT touching ~/.ws/config.yaml,
# git, or the network.


def _call(server, tool, args):
    """Run a single in-memory tool call and return the FastMCP result."""

    async def call():
        from fastmcp import Client

        async with Client(server) as client:
            return await client.call_tool(tool, args)

    return asyncio.run(call())


def test_config_get_and_hive_rm_are_not_registered():
    pytest.importorskip("fastmcp")
    from fastmcp import Client

    server = mcp_mod.build_server()

    async def names():
        async with Client(server) as client:
            return {t.name for t in await client.list_tools()}

    tools = asyncio.run(names())
    # Intentionally CLI-only: a scalar read and a destructive unregister.
    assert "config_get" not in tools
    assert "hive_rm" not in tools


def test_config_set_routes_structured_value_through_json_path(monkeypatch):
    pytest.importorskip("fastmcp")
    captured = {}

    def fake_set_value(key, raw, as_json=False, cfg=None):
        captured.update(key=key, raw=raw, as_json=as_json)
        return {"ok": True, "problems": [], "old": None, "new": raw}

    monkeypatch.setattr(config_mod, "set_value", fake_set_value)
    server = mcp_mod.build_server()

    # A structured (dict) value must round-trip exactly via the jpp4.1 --json path.
    result = _call(server, "config_set", {"key": "otel.headers", "value": {"x-token": "abc"}})

    assert captured["key"] == "otel.headers"
    assert captured["as_json"] is True
    assert json.loads(captured["raw"]) == {"x-token": "abc"}
    assert result.data["ok"] is True


def test_config_set_string_value_uses_cli_coercion(monkeypatch):
    pytest.importorskip("fastmcp")
    captured = {}

    def fake_set_value(key, raw, as_json=False, cfg=None):
        captured.update(raw=raw, as_json=as_json)
        return {"ok": True, "problems": [], "old": None, "new": raw}

    monkeypatch.setattr(config_mod, "set_value", fake_set_value)
    server = mcp_mod.build_server()

    # A bare string defers to the core's CLI-parity coercion (as_json stays False).
    _call(server, "config_set", {"key": "otel.protocol", "value": "grpc"})
    assert captured == {"raw": "grpc", "as_json": False}


def test_hive_add_returns_effective_registered_entry(monkeypatch):
    pytest.importorskip("fastmcp")
    calls = {}

    monkeypatch.setattr(hive_mod, "add", lambda hive_id, **kw: calls.update(hive_id=hive_id, **kw))
    monkeypatch.setattr(
        registry_mod,
        "find_entry",
        lambda cfg, p, o, r: {"prefix": "ws", "kind": "personal"},
    )
    monkeypatch.setattr(config_mod, "load", lambda: {})
    server = mcp_mod.build_server()

    result = _call(server, "hive_add", {"provider": "github", "org": "acme", "repo": "tools"})
    assert calls["hive_id"] == "github/acme/tools"
    assert result.data == {"prefix": "ws", "kind": "personal", "registered": True}


def test_hive_add_missing_field_maps_to_tool_error():
    pytest.importorskip("fastmcp")
    from fastmcp.exceptions import ToolError

    server = mcp_mod.build_server()
    with pytest.raises(ToolError) as excinfo:
        _call(server, "hive_add", {"provider": "github", "org": "acme", "repo": "  "})
    assert "repo" in str(excinfo.value).lower()


def test_hive_onboard_clone_path_returns_structured_report(monkeypatch, tmp_path):
    pytest.importorskip("fastmcp")
    seen = {}

    # workspace_root() is imported into the mcp namespace, so patch it there.
    monkeypatch.setattr(mcp_mod, "workspace_root", lambda: str(tmp_path))
    monkeypatch.setattr(config_mod, "load", lambda: {})
    monkeypatch.setattr(
        registry_mod, "derive_prefix", lambda *a, **k: ("tools", ["note: long prefix"])
    )
    monkeypatch.setattr(
        hive_mod, "onboard", lambda hive_id, **kw: seen.update(hive_id=hive_id, **kw)
    )
    monkeypatch.setattr(
        registry_mod, "find_entry", lambda cfg, p, o, r: {"prefix": "tools", "kind": ""}
    )
    server = mcp_mod.build_server()

    # target = tmp_path/github/acme/tools does not exist → cloned via clone_url.
    result = _call(
        server,
        "hive_onboard",
        {
            "provider": "github",
            "org": "acme",
            "repo": "tools",
            "clone_url": "https://example/acme/tools.git",
        },
    )
    assert seen["hive_id"] == "github/acme/tools"
    assert seen["clone_url"] == "https://example/acme/tools.git"
    assert result.data == {
        "cloned": True,
        "registered": True,
        "prefix": "tools",
        "synced": True,
        "warnings": ["note: long prefix"],
    }


def test_hive_onboard_absent_without_clone_url_errors(monkeypatch, tmp_path):
    pytest.importorskip("fastmcp")
    from fastmcp.exceptions import ToolError

    monkeypatch.setattr(mcp_mod, "workspace_root", lambda: str(tmp_path))
    server = mcp_mod.build_server()
    with pytest.raises(ToolError) as excinfo:
        _call(server, "hive_onboard", {"provider": "github", "org": "acme", "repo": "tools"})
    assert "clone_url" in str(excinfo.value)


def test_hives_status_aggregates_candidates_collisions_violations(monkeypatch):
    pytest.importorskip("fastmcp")
    cfg = {
        "orgs": {"acme": {"code": "ac", "policy": "required"}},
        "managed_repos": [
            {"provider": "github", "org": "acme", "repo": "one", "prefix": "dup", "kind": ""},
            {"provider": "github", "org": "acme", "repo": "two", "prefix": "dup", "kind": ""},
        ],
    }
    monkeypatch.setattr(config_mod, "load", lambda: cfg)
    # hive.available reads the git-workspace lock file — stub it to a fixed candidate set.
    monkeypatch.setattr(
        hive_mod, "available", lambda c: {"candidates": ["github/acme/new"], "registered": []}
    )
    server = mcp_mod.build_server()

    result = _call(server, "hives_status", {})
    data = result.data
    assert data["candidates"] == ["github/acme/new"]
    # Two hives share prefix 'dup' → one collision; both break the required 'ac-' convention.
    assert data["collisions"] == [{"prefix": "dup", "hives": ["acme/one", "acme/two"]}]
    assert len(data["violations"]) == 2
    assert {r["repo"] for r in data["hives"]} == {"one", "two"}


# ---- otel instrumentation: counter + latency per MCP tool call --------------
#
# The OTel SDK is an optional extra, so these tests drive a **mocked meter** (mirroring
# test_otel_instrument.py) and assert the per-tool metric emission via the in-memory
# FastMCP Client transport. Three cases: ok-outcome, error-outcome, and otel-off no-op.


def _force_otel_on(monkeypatch) -> MagicMock:
    """Force otel active with a fresh mocked meter; return it for assertions."""
    meter = MagicMock(name="meter")
    monkeypatch.setattr(otel_mod, "_initialized", True)
    monkeypatch.setattr(otel_mod, "get_meter", lambda *a, **k: meter)
    # Replace the instrument cache so each test starts fresh and monkeypatch restores it.
    monkeypatch.setattr(otel_mod, "_instruments", {})
    return meter


def test_tool_emits_ok_counter_and_latency_when_otel_on(monkeypatch):
    pytest.importorskip("fastmcp")
    from fastmcp import Client

    meter = _force_otel_on(monkeypatch)
    server = mcp_mod.build_server()
    spec = {
        "epic": {"title": "Demo epic"},
        "issues": [{"handle": "a", "title": "do a thing", "acceptance": "it works"}],
    }

    async def call():
        async with Client(server) as client:
            return await client.call_tool("plan_check", {"spec": spec})

    asyncio.run(call())

    # Counter: ws.mcp.tool.invocations, tool=plan_check, outcome=ok.
    meter.create_counter.assert_called_once()
    assert meter.create_counter.call_args.args[0] == "bh.mcp.tool.invocations"
    meter.create_counter.return_value.add.assert_called_once_with(
        1, {"bh.mcp.tool": "plan_check", "bh.mcp.outcome": "ok"}
    )
    # Histogram: ws.mcp.tool.duration with same tags; duration is non-negative.
    meter.create_histogram.assert_called_once()
    assert meter.create_histogram.call_args.args[0] == "bh.mcp.tool.duration"
    rec = meter.create_histogram.return_value.record.call_args
    assert rec.args[1] == {"bh.mcp.tool": "plan_check", "bh.mcp.outcome": "ok"}
    assert rec.args[0] >= 0.0


def test_tool_error_emits_error_outcome(monkeypatch):
    pytest.importorskip("fastmcp")
    from fastmcp import Client
    from fastmcp.exceptions import ToolError

    meter = _force_otel_on(monkeypatch)
    server = mcp_mod.build_server()
    bad = {"epic": {"title": "E"}, "issues": [{"handle": "a", "title": "no acceptance"}]}

    async def call():
        async with Client(server) as client:
            return await client.call_tool("plan_file", {"spec": bad})

    with pytest.raises(ToolError):
        asyncio.run(call())

    # Counter must record outcome=error even though the ToolError was re-raised.
    meter.create_counter.return_value.add.assert_called_once_with(
        1, {"bh.mcp.tool": "plan_file", "bh.mcp.outcome": "error"}
    )


def test_tool_invocation_is_noop_when_otel_off():
    pytest.importorskip("fastmcp")
    from fastmcp import Client

    assert not otel_mod.is_active()  # otel is off by default
    server = mcp_mod.build_server()
    spec = {
        "epic": {"title": "Demo epic"},
        "issues": [{"handle": "a", "title": "do a thing", "acceptance": "it works"}],
    }

    async def call():
        async with Client(server) as client:
            return await client.call_tool("plan_check", {"spec": spec})

    result = asyncio.run(call())
    assert result.data["valid"] is True
    assert otel_mod._instruments == {}  # nothing cached — zero overhead when off
