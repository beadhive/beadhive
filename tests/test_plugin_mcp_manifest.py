""" — plugin MCP manifest shape tests.

Assert that plugins/agf/.mcp.json is valid JSON, declares mcpServers.ws with
command "ws-mcp" and args [], and that plugin.json carries the expected version
bump (0.4.0 -> 0.4.1).

Background: 'ws mcp serve' is gated behind the ws setup-check cache and exits 1
before the MCP handshake when the cache is absent/stale, producing a -32000 error
in the client.  'ws-mcp' (the dedicated console-script entry-point) has no such
gate and answers initialize cleanly; it is always installed alongside 'ws'.
"""

from __future__ import annotations

import json
from pathlib import Path

# Locate the plugin root relative to this test file's package anchor.
_PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "plugins" / "agf"
_MCP_JSON = _PLUGIN_ROOT / ".mcp.json"
_PLUGIN_JSON = _PLUGIN_ROOT / ".claude-plugin" / "plugin.json"


def test_mcp_json_exists():
    """plugins/agf/.mcp.json must exist."""
    assert _MCP_JSON.is_file(), f"{_MCP_JSON} not found"


def test_mcp_json_is_valid_json():
    """.mcp.json must be parseable JSON."""
    data = json.loads(_MCP_JSON.read_text())
    assert isinstance(data, dict)


def test_mcp_json_declares_ws_server():
    """.mcp.json must declare mcpServers.ws."""
    data = json.loads(_MCP_JSON.read_text())
    assert "mcpServers" in data, "missing top-level 'mcpServers' key"
    assert "ws" in data["mcpServers"], "missing 'ws' entry under mcpServers"


def test_mcp_json_ws_command():
    """mcpServers.ws.command must be 'ws-mcp' (ungated console-script entry-point)."""
    data = json.loads(_MCP_JSON.read_text())
    assert data["mcpServers"]["ws"]["command"] == "ws-mcp"


def test_mcp_json_ws_args():
    """mcpServers.ws.args must be [] (ws-mcp takes no subcommand args)."""
    data = json.loads(_MCP_JSON.read_text())
    assert data["mcpServers"]["ws"]["args"] == []


def test_plugin_json_version_bumped():
    """plugin.json version must be 0.4.1 (patch bump from 0.4.0 for the ws-mcp fix)."""
    data = json.loads(_PLUGIN_JSON.read_text())
    assert data["version"] == "0.4.1", f"expected 0.4.1, got {data['version']}"
