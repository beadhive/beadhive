""" — change notifications: mutating tools emit resources/updated.

Proves the wiring (not the fuller integration behavior — that's res-test-notify / .20): a
mutating tool, invoked over the in-process FastMCP transport, sends an MCP
`resources/updated` notification for each URI it invalidates. Each test stubs the tool's
core so it exercises ONLY the notify wiring — no real config write / bd / git / network.

The `ctx: Context` param FastMCP injects is what lets a sync-shaped tool `await
_notify_updated`; a message handler on the client captures the notifications.
"""

from __future__ import annotations

import asyncio

import pytest

from beadhive import config as config_mod
from beadhive import mcp as mcp_mod
from beadhive import registry as registry_mod
from beadhive import rig as rig_mod


def _call_capturing(server, tool: str, args: dict):
    """Call *tool* over the in-memory transport, capturing every resources/updated URI.

    Returns (result, captured_uris) — captured_uris are the normalized URI strings the server
    pushed as `notifications/resources/updated` during the call.
    """
    from fastmcp import Client
    from mcp.types import ResourceUpdatedNotification

    captured: list[str] = []

    async def handler(msg):
        # FastMCP wraps server notifications in a ServerNotification whose `.root` is the
        # concrete notification; unwrap and keep only resource-updated URIs.
        root = getattr(msg, "root", msg)
        if isinstance(root, ResourceUpdatedNotification):
            captured.append(str(root.params.uri))

    async def call():
        async with Client(server, message_handler=handler) as client:
            return await client.call_tool(tool, args)

    result = asyncio.run(call())
    return result, captured


def test_config_set_emits_config_and_per_key_updated(monkeypatch):
    """config_set on a successful write → resources/updated for beadhive://config + /{key}."""
    pytest.importorskip("fastmcp")
    monkeypatch.setattr(
        config_mod,
        "set_value",
        lambda key, raw, as_json=False, cfg=None: {
            "ok": True,
            "problems": [],
            "old": None,
            "new": raw,
        },
    )
    server = mcp_mod.build_server()

    result, uris = _call_capturing(
        server, "config_set", {"key": "otel.protocol", "value": "grpc"}
    )

    assert result.data["ok"] is True
    assert uris == ["beadhive://config", "beadhive://config/otel.protocol"]


def test_config_set_failed_write_emits_nothing(monkeypatch):
    """A validation failure (ok=false, nothing written) must NOT emit a stale invalidation."""
    pytest.importorskip("fastmcp")
    monkeypatch.setattr(
        config_mod,
        "set_value",
        lambda key, raw, as_json=False, cfg=None: {
            "ok": False,
            "problems": ["bad value"],
            "old": None,
            "new": None,
        },
    )
    server = mcp_mod.build_server()

    result, uris = _call_capturing(
        server, "config_set", {"key": "otel.protocol", "value": "nope"}
    )

    assert result.data["ok"] is False
    assert uris == []


def test_rig_add_emits_rigs_resources(monkeypatch):
    """rig_add → resources/updated for beadhive://rigs/status, beadhive://rigs/available, beadhive://rigs/survey."""
    pytest.importorskip("fastmcp")
    monkeypatch.setattr(rig_mod, "add", lambda rig_id, **kw: None)
    monkeypatch.setattr(
        registry_mod, "find_entry", lambda cfg, p, o, r: {"prefix": "ws", "kind": "personal"}
    )
    monkeypatch.setattr(config_mod, "load", lambda: {})
    server = mcp_mod.build_server()

    _result, uris = _call_capturing(
        server, "rig_add", {"provider": "github", "org": "acme", "repo": "tools"}
    )

    assert uris == ["beadhive://rigs/status", "beadhive://rigs/available", "beadhive://rigs/survey"]
