""" — plugin MCP manifest shape tests.

Assert that plugins/agf/.mcp.json is valid JSON, declares mcpServers.ws with
command "ws" and args ["mcp", "serve"], and that plugin.json carries the
expected version bump (0.3.0 -> 0.4.0).
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
    """mcpServers.ws.command must be 'ws'."""
    data = json.loads(_MCP_JSON.read_text())
    assert data["mcpServers"]["ws"]["command"] == "ws"


def test_mcp_json_ws_args():
    """mcpServers.ws.args must be ['mcp', 'serve']."""
    data = json.loads(_MCP_JSON.read_text())
    assert data["mcpServers"]["ws"]["args"] == ["mcp", "serve"]


def test_plugin_json_version_bumped():
    """plugin.json version must be 0.4.0 (bumped from 0.3.0 to signal MCP config ship)."""
    data = json.loads(_PLUGIN_JSON.read_text())
    assert data["version"] == "0.4.0", f"expected 0.4.0, got {data['version']}"
