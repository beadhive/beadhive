""" — beadhive://labels/validation resource (labels plane).

Tests that the resource:
  * is registered in the server's resource list;
  * returns the correct structured shape {has_violations, required_violations,
    issue_problems, db_ok} assembled from validate.* + registry.required_violations;
  * applies _measured_resource defaults (application/json + readOnly/idempotent).

All tests gated behind importorskip so CI stays green without the [mcp] extra installed.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from beadhive import config as config_mod
from beadhive import mcp as mcp_mod
from beadhive import validate as validate_mod

# ---- helpers -----------------------------------------------------------------


async def _read(server, uri: str):
    from fastmcp import Client

    async with Client(server) as client:
        return await client.read_resource(uri)


async def _list_resources(server):
    from fastmcp import Client

    async with Client(server) as client:
        return await client.list_resources()


# ---- registration + structural checks ----------------------------------------


def test_labels_validation_resource_is_registered():
    """beadhive://labels/validation appears in the server's resource list."""
    pytest.importorskip("fastmcp")
    server = mcp_mod.build_server()
    resources = asyncio.run(_list_resources(server))
    uris = {str(r.uri) for r in resources}
    assert "beadhive://labels/validation" in uris


def test_labels_validation_resource_has_json_mime_and_readonly_idempotent_annotations():
    """_measured_resource defaults: application/json + readOnlyHint=True + idempotentHint=True."""
    pytest.importorskip("fastmcp")
    server = mcp_mod.build_server()
    resources = asyncio.run(_list_resources(server))
    res = next((r for r in resources if str(r.uri) == "beadhive://labels/validation"), None)
    assert res is not None, "beadhive://labels/validation not found in resource list"
    assert res.mimeType == "application/json"
    assert res.annotations is not None
    assert res.annotations.readOnlyHint is True
    assert res.annotations.idempotentHint is True


# ---- payload shape -----------------------------------------------------------


def test_labels_validation_resource_returns_findings_shape(monkeypatch):
    """beadhive://labels/validation returns the four-key findings shape."""
    pytest.importorskip("fastmcp")
    monkeypatch.setattr(config_mod, "load", lambda: {"managed_repos": [], "orgs": {}})
    monkeypatch.setattr(validate_mod, "_issue_checks", lambda cfg, cwd=None: ([], True))
    monkeypatch.setattr(validate_mod, "has_violations", lambda cfg=None, cwd=None: False)

    server = mcp_mod.build_server()
    contents = asyncio.run(_read(server, "beadhive://labels/validation"))
    assert contents, "expected at least one content block"
    data = json.loads(contents[0].text)

    assert "has_violations" in data, f"missing 'has_violations' key; got: {list(data.keys())}"
    assert "required_violations" in data, (
        f"missing 'required_violations' key; got: {list(data.keys())}"
    )
    assert "issue_problems" in data, f"missing 'issue_problems' key; got: {list(data.keys())}"
    assert "db_ok" in data, f"missing 'db_ok' key; got: {list(data.keys())}"


def test_labels_validation_resource_reports_clean_when_no_violations(monkeypatch):
    """has_violations=False + empty lists when registry and db are clean."""
    pytest.importorskip("fastmcp")
    monkeypatch.setattr(config_mod, "load", lambda: {"managed_repos": [], "orgs": {}})
    monkeypatch.setattr(validate_mod, "_issue_checks", lambda cfg, cwd=None: ([], True))
    monkeypatch.setattr(validate_mod, "has_violations", lambda cfg=None, cwd=None: False)

    server = mcp_mod.build_server()
    contents = asyncio.run(_read(server, "beadhive://labels/validation"))
    data = json.loads(contents[0].text)

    assert data["has_violations"] is False
    assert data["required_violations"] == []
    assert data["issue_problems"] == []
    assert data["db_ok"] is True


def test_labels_validation_resource_reports_violations_from_registry(monkeypatch):
    """has_violations=True + required_violations populated when registry has prefix violations."""
    pytest.importorskip("fastmcp")
    cfg = {
        "managed_repos": [
            {"provider": "github", "org": "acme", "repo": "myrepo", "prefix": "bad-prefix"}
        ],
        "orgs": {"acme": {"code": "ac", "policy": "required"}},
    }
    monkeypatch.setattr(config_mod, "load", lambda: cfg)
    monkeypatch.setattr(validate_mod, "_issue_checks", lambda c, cwd=None: ([], True))
    monkeypatch.setattr(validate_mod, "has_violations", lambda cfg=None, cwd=None: True)

    server = mcp_mod.build_server()
    contents = asyncio.run(_read(server, "beadhive://labels/validation"))
    data = json.loads(contents[0].text)

    assert data["has_violations"] is True
    assert len(data["required_violations"]) > 0
    assert data["issue_problems"] == []
    assert data["db_ok"] is True


def test_labels_validation_resource_reports_db_unavailable(monkeypatch):
    """db_ok=False + has_violations consistent when bd is unreachable."""
    pytest.importorskip("fastmcp")
    monkeypatch.setattr(config_mod, "load", lambda: {"managed_repos": [], "orgs": {}})
    monkeypatch.setattr(validate_mod, "_issue_checks", lambda cfg, cwd=None: ([], False))
    monkeypatch.setattr(validate_mod, "has_violations", lambda cfg=None, cwd=None: False)

    server = mcp_mod.build_server()
    contents = asyncio.run(_read(server, "beadhive://labels/validation"))
    data = json.loads(contents[0].text)

    assert data["db_ok"] is False
    assert data["issue_problems"] == []


def test_labels_validation_resource_reports_issue_problems(monkeypatch):
    """issue_problems populated + has_violations=True when per-bead checks find problems."""
    pytest.importorskip("fastmcp")
    fake_problems = ["\tunknown hive prefix (not registered)"]
    monkeypatch.setattr(config_mod, "load", lambda: {"managed_repos": [], "orgs": {}})
    monkeypatch.setattr(
        validate_mod, "_issue_checks", lambda cfg, cwd=None: (fake_problems, True)
    )
    monkeypatch.setattr(validate_mod, "has_violations", lambda cfg=None, cwd=None: True)

    server = mcp_mod.build_server()
    contents = asyncio.run(_read(server, "beadhive://labels/validation"))
    data = json.loads(contents[0].text)

    assert data["has_violations"] is True
    assert data["issue_problems"] == fake_problems
    assert data["db_ok"] is True
