""" — plugin MCP manifest shape tests.

Assert that plugins/bh/.mcp.json is valid JSON, declares mcpServers.bh with
command "bh-mcp" and args [], and that plugin.json carries the expected version
bump (0.4.0 -> 0.4.1).

Background: 'bh mcp serve' is gated behind the bh setup-check cache and exits 1
before the MCP handshake when the cache is absent/stale, producing a -32000 error
in the client.  'bh-mcp' (the dedicated console-script entry-point) has no such
gate and answers initialize cleanly; it is always installed alongside 'bh'.
"""

from __future__ import annotations

import json
from pathlib import Path

# Locate the plugin root relative to this test file's package anchor.
_PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "plugins" / "bh"
_MCP_JSON = _PLUGIN_ROOT / ".mcp.json"
_PLUGIN_JSON = _PLUGIN_ROOT / ".claude-plugin" / "plugin.json"


def test_mcp_json_exists():
    """plugins/bh/.mcp.json must exist."""
    assert _MCP_JSON.is_file(), f"{_MCP_JSON} not found"


def test_mcp_json_is_valid_json():
    """.mcp.json must be parseable JSON."""
    data = json.loads(_MCP_JSON.read_text())
    assert isinstance(data, dict)


def test_mcp_json_declares_bh_server():
    """.mcp.json must declare mcpServers.bh."""
    data = json.loads(_MCP_JSON.read_text())
    assert "mcpServers" in data, "missing top-level 'mcpServers' key"
    assert "bh" in data["mcpServers"], "missing 'bh' entry under mcpServers"


def test_mcp_json_bh_command():
    """mcpServers.bh.command must be 'bh-mcp' (ungated console-script entry-point)."""
    data = json.loads(_MCP_JSON.read_text())
    assert data["mcpServers"]["bh"]["command"] == "bh-mcp"


def test_mcp_json_bh_args():
    """mcpServers.bh.args must be [] (bh-mcp takes no subcommand args)."""
    data = json.loads(_MCP_JSON.read_text())
    assert data["mcpServers"]["bh"]["args"] == []


def test_plugin_json_version_bumped():
    """plugin.json version must be 0.6.0 (bumped for the agf -> bh plugin rename)."""
    data = json.loads(_PLUGIN_JSON.read_text())
    assert data["version"] == "0.6.0", f"expected 0.6.0, got {data['version']}"
