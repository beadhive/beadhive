"""beadhive://toolchain/* resources + the toolchain_exec tool (bh-d0kb, knowledge-only).

The MCP surface shares the CLI's payload producers (toolchain.list_payload /
show_payload), so these tests pin that shape identity plus the tool's exec seam:

- both resources are registered (list concrete, show/{name} a template);
- list returns exactly toolchain.list_payload's {declared, registry} payload;
- show/{name} runs the template's entrypoints_cmd through the faked run() seam;
- show on an unknown name surfaces a clean error (no partial payload);
- toolchain_exec passes argv through with capture and returns {exit_code, stdout, stderr};
- toolchain_exec refuses an empty argv with a ToolError.

All gated behind importorskip so CI stays green without the fastmcp extra installed.
"""

from __future__ import annotations

import asyncio
import json
from collections import namedtuple
from pathlib import Path

import pytest

from beadhive import config, toolchain
from beadhive import mcp as mcp_mod
from beadhive import registry as registry_mod

_CP = namedtuple("CP", "returncode stdout stderr")
_CFG = {"worktrees": {"toolchain": "just"}}


@pytest.fixture
def stubbed(monkeypatch):
    monkeypatch.setattr(config, "load", lambda: _CFG)
    monkeypatch.setattr(registry_mod, "current_hive", lambda cfg: {})
    monkeypatch.setattr(registry_mod, "hive_dir_for", lambda cfg, hive="": Path("/fake/hive"))


def _fake_run(monkeypatch, returncode=0, stdout="", stderr=""):
    calls: list[tuple[list, dict]] = []

    def fake(cmd, **kw):
        calls.append((cmd, kw))
        return _CP(returncode, stdout, stderr)

    monkeypatch.setattr(toolchain, "run", fake)
    return calls


async def _read(server, uri: str):
    from fastmcp import Client

    async with Client(server) as client:
        return await client.read_resource(uri)


def _call(server, tool, args):
    async def call():
        from fastmcp import Client

        async with Client(server) as client:
            return await client.call_tool(tool, args)

    return asyncio.run(call())


# ---- registration ------------------------------------------------------------


def test_toolchain_resources_are_registered():
    pytest.importorskip("fastmcp")
    from fastmcp import Client

    server = mcp_mod.build_server()

    async def go():
        async with Client(server) as client:
            resources = {str(r.uri) for r in await client.list_resources()}
            templates = {str(t.uriTemplate) for t in await client.list_resource_templates()}
        return resources, templates

    resources, templates = asyncio.run(go())
    assert "beadhive://toolchain/list" in resources
    assert "beadhive://toolchain/show/{name}" in templates


# ---- beadhive://toolchain/list -----------------------------------------------


def test_toolchain_list_resource_shares_the_cli_payload(stubbed):
    """The resource returns exactly toolchain.list_payload — the same producer behind
    `bh toolchain list --json`, so the two shapes can never drift."""
    pytest.importorskip("fastmcp")
    server = mcp_mod.build_server()
    contents = asyncio.run(_read(server, "beadhive://toolchain/list"))
    data = json.loads(contents[0].text)
    assert data == toolchain.list_payload(_CFG, {})
    assert data["declared"] == ["just"]


# ---- beadhive://toolchain/show/{name} ----------------------------------------


def test_toolchain_show_resource_runs_entrypoints_cmd(stubbed, monkeypatch):
    pytest.importorskip("fastmcp")
    calls = _fake_run(monkeypatch, stdout="check\nlint\n")
    server = mcp_mod.build_server()
    contents = asyncio.run(_read(server, "beadhive://toolchain/show/just"))
    data = json.loads(contents[0].text)
    ((cmd, kw),) = calls
    assert cmd == ["just", "--list"]
    assert kw["cwd"] == str(Path("/fake/hive"))
    assert data["entrypoints"] == "check\nlint\n"
    assert data["suggestions"]["validate_cmd"] == "just check"


def test_toolchain_show_resource_unknown_name_errors(stubbed, monkeypatch):
    pytest.importorskip("fastmcp")
    calls = _fake_run(monkeypatch)
    server = mcp_mod.build_server()
    with pytest.raises(Exception, match="unknown toolchain 'gradle'"):
        asyncio.run(_read(server, "beadhive://toolchain/show/gradle"))
    assert not calls


# ---- toolchain_exec tool -----------------------------------------------------


def test_toolchain_exec_tool_passes_argv_and_returns_exit_code(stubbed, monkeypatch):
    pytest.importorskip("fastmcp")
    calls = _fake_run(monkeypatch, returncode=3, stdout="out", stderr="err")
    server = mcp_mod.build_server()
    result = _call(server, "toolchain_exec", {"argv": ["npm", "run", "lint"]})
    assert result.data == {"exit_code": 3, "stdout": "out", "stderr": "err"}
    ((cmd, kw),) = calls
    assert cmd == ["npm", "run", "lint"]
    assert kw["cwd"] == str(Path("/fake/hive"))
    assert kw["capture"] is True


def test_toolchain_exec_tool_refuses_empty_argv(stubbed, monkeypatch):
    pytest.importorskip("fastmcp")
    from fastmcp.exceptions import ToolError

    calls = _fake_run(monkeypatch)
    server = mcp_mod.build_server()
    with pytest.raises(ToolError, match="empty argv"):
        _call(server, "toolchain_exec", {"argv": []})
    assert not calls
