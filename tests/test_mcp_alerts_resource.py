"""beadhive://alerts resource and change notifications."""

from __future__ import annotations

import asyncio
import json

import pytest

from beadhive import alerts, mcp


async def _resources(server):
    from fastmcp import Client

    async with Client(server) as client:
        return await client.list_resources()


async def _read(server):
    from fastmcp import Client

    async with Client(server) as client:
        return await client.read_resource("beadhive://alerts")


def test_alerts_resource_is_registered_and_returns_normalized_rows(monkeypatch):
    pytest.importorskip("fastmcp")
    rows = [{"severity": "warning", "code": "doctor.warning", "message": "x", "remediation": "y"}]
    monkeypatch.setattr(alerts, "active", lambda: rows)
    server = mcp.build_server()

    assert "beadhive://alerts" in {str(row.uri) for row in asyncio.run(_resources(server))}
    contents = asyncio.run(_read(server))
    assert json.loads(contents[0].text) == rows


def test_alert_notification_only_fires_after_a_subscribed_state_changes(monkeypatch):
    mcp._alerts_fingerprint = None
    rows = [
        [],
        [],
        [{"severity": "warning", "code": "doctor.warning", "message": "x", "remediation": "y"}],
    ]
    monkeypatch.setattr(alerts, "active", lambda: rows.pop(0))
    notified = []

    async def fake_notify(_ctx, uris):
        notified.extend(uris)

    monkeypatch.setattr(mcp, "_notify_updated", fake_notify)
    assert mcp._observe_alerts() is False  # initial resource read
    asyncio.run(mcp._notify_alerts_if_changed(None))
    asyncio.run(mcp._notify_alerts_if_changed(None))
    assert notified == ["beadhive://alerts"]
