""" — integration test: resources/updated round-trip.

Proves the subscribe → mutate → notified loop end-to-end using the in-process FastMCP
Client pattern established in tests/test_mcp_notify.py.

FastMCP 3.4.x advertises ``resources.subscribe=False`` in its server capabilities, so
the in-process subscription mechanism is a ``message_handler`` callback registered on the
``Client`` — that IS how a client "subscribes" to notifications in this transport. The test:

  1. Registers a notification handler (the subscription) on the in-process FastMCP Client.
  2. Calls ``config_set`` with a valid key/value that returns ok=True.
  3. Asserts a ``resources/updated`` notification for ``ws://config`` is received.

pydantic's ``AnyUrl`` normalizes a host-only URI to a trailing slash on the wire
(``ws://config`` → ``ws://config/``), matching the convention from test_mcp_resource.py.
``config.set_value`` is stubbed to return ok=True without touching the filesystem.
"""

from __future__ import annotations

import asyncio

import pytest

from ws import config as config_mod
from ws import mcp as mcp_mod


def test_config_set_emits_resources_updated_for_config(monkeypatch):
    """subscribe(message_handler) + config_set → resources/updated notification for ws://config.

    End-to-end loop: the in-process FastMCP Client registers a notification sink
    (the message_handler — the in-process subscription equivalent), calls config_set
    with a key/value pair that succeeds (ok=True), and asserts the resulting
    resources/updated notification carries the ws://config URI.
    """
    pytest.importorskip("fastmcp")
    from fastmcp import Client
    from mcp.types import ResourceUpdatedNotification

    # Stub set_value so the tool call returns ok=True without touching ~/.ws/config.yaml.
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
    captured: list[str] = []

    async def handler(msg):
        # FastMCP wraps server notifications in a ServerNotification whose .root is the
        # concrete notification type; unwrap and capture only resource-updated URIs.
        root = getattr(msg, "root", msg)
        if isinstance(root, ResourceUpdatedNotification):
            captured.append(str(root.params.uri))

    async def run():
        # 1. Connect with a notification handler — the in-process subscription mechanism.
        #    FastMCP 3.4.x does not support resources/subscribe over this transport
        #    (capabilities.resources.subscribe=False); message_handler is the subscription.
        async with Client(server, message_handler=handler) as client:
            # 2. Call config_set with a valid key/value (ok=True path).
            result = await client.call_tool("config_set", {"key": "otel.protocol", "value": "grpc"})
            assert result.data["ok"] is True

    asyncio.run(run())

    # 3. Assert resources/updated was received for ws://config.
    #    pydantic AnyUrl normalizes a host-only URI to include a trailing slash.
    assert "ws://config/" in captured
