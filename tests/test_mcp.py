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
import sys

import pytest

from ws import mcp as mcp_mod


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
    assert "install" in msg and "ws[mcp]" in msg


def test_main_without_fastmcp_returns_error_and_hints(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "fastmcp", None)
    code = mcp_mod.main()
    assert code == 1
    err = capsys.readouterr().err.lower()
    assert "install" in err and "ws[mcp]" in err


# The complex-input tools jnv.3 exposes — and nothing else (simple/bulk CLI-only
# commands stay off the MCP surface).
_SELECTED_TOOLS = {"plan_check", "plan_file", "work_refine", "bd_create"}


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
    # (and refuses to file). Fails before any bd/git call, so no rig fixture is needed.
    bad = {"epic": {"title": "E"}, "issues": [{"handle": "a", "title": "no acceptance"}]}

    async def call():
        async with Client(server) as client:
            return await client.call_tool("plan_file", {"spec": bad})

    with pytest.raises(ToolError) as excinfo:
        asyncio.run(call())
    msg = str(excinfo.value).lower()
    assert "invalid molecule spec" in msg
    assert "acceptance" in msg
