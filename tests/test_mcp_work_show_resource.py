""" — show_payload core + ws://work/show/{id} resource.

Tests that:
  * show_payload() returns {base, max_commits, commits} using commit_rows + flag_rows.
  * show_payload() truncates the base SHA to 7 chars.
  * show_payload() returns empty commits and empty base when base cannot be resolved.
  * ws://work/show/{id} appears in the server's resource template list.
  * ws://work/show/{id} returns the show_payload() result for a known bead.
  * ws://work/show/{id} returns empty commits when the branch cannot be resolved.
  * Default MIME type and annotations (readOnlyHint + idempotentHint).

MCP tests gated behind importorskip so CI stays green without the [mcp] extra installed.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ws import config as config_mod
from ws import mcp as mcp_mod
from ws import work_show as work_show_mod
from ws import worktree as worktree_mod

# ---- helpers -----------------------------------------------------------------

FAKE_SHA = "abc1234def5678"  # 14-char sha for truncation testing
FAKE_ENTRY = {"provider": "github", "org": "myorg", "repo": "myrepo", "prefix": "mr"}
FAKE_BRANCH = "wt/bead/issue/mr-1"
FAKE_BEAD = "mr-1"


async def _read(server, uri: str):
    from fastmcp import Client

    async with Client(server) as client:
        return await client.read_resource(uri)


async def _list_resource_templates(server):
    from fastmcp import Client

    async with Client(server) as client:
        return await client.list_resource_templates()


# ---- show_payload() unit tests -----------------------------------------------


def _patch_show_payload_deps(
    monkeypatch,
    base: str = FAKE_SHA,
    max_commits: int = 10,
    commit_rows: list | None = None,
):
    """Monkeypatch the pure producers show_payload() delegates to."""
    if commit_rows is None:
        commit_rows = []

    monkeypatch.setattr(
        worktree_mod,
        "integration_base",
        lambda entry, bead, integration: "main",
    )
    monkeypatch.setattr(
        worktree_mod,
        "base_of",
        lambda entry, branch, integration: base,
    )
    monkeypatch.setattr(
        worktree_mod,
        "commit_rows",
        lambda entry, base, branch: commit_rows,
    )
    monkeypatch.setattr(
        config_mod,
        "integration_branch",
        lambda cfg, entry: "main",
    )
    monkeypatch.setattr(
        config_mod,
        "max_commits",
        lambda cfg, entry: max_commits,
    )


def test_show_payload_returns_correct_shape(monkeypatch):
    """show_payload() returns a dict with base, max_commits, and commits keys."""
    rows = [{"sha": FAKE_SHA, "subject": "feat: x", "flags": {}}]
    _patch_show_payload_deps(monkeypatch, base=FAKE_SHA, max_commits=8, commit_rows=rows)
    payload = work_show_mod.show_payload({}, FAKE_ENTRY, FAKE_BEAD, FAKE_BRANCH)
    assert set(payload.keys()) == {"base", "max_commits", "commits"}


def test_show_payload_truncates_base_to_7_chars(monkeypatch):
    """show_payload() abbreviates the base SHA to 7 characters."""
    _patch_show_payload_deps(monkeypatch, base=FAKE_SHA)
    payload = work_show_mod.show_payload({}, FAKE_ENTRY, FAKE_BEAD, FAKE_BRANCH)
    assert payload["base"] == FAKE_SHA[:7]


def test_show_payload_returns_max_commits(monkeypatch):
    """show_payload() returns the configured max_commits value."""
    _patch_show_payload_deps(monkeypatch, base=FAKE_SHA, max_commits=5)
    payload = work_show_mod.show_payload({}, FAKE_ENTRY, FAKE_BEAD, FAKE_BRANCH)
    assert payload["max_commits"] == 5


def test_show_payload_passes_commit_rows_through_flag_rows(monkeypatch):
    """show_payload() runs commit rows through flag_rows before including them."""
    raw_rows = [{"sha": FAKE_SHA, "subject": "feat: y", "flags": {}}]
    _patch_show_payload_deps(monkeypatch, base=FAKE_SHA, commit_rows=raw_rows)
    # flag_rows adds flags; we verify the rows pass through (flag_rows is real here)
    payload = work_show_mod.show_payload({}, FAKE_ENTRY, FAKE_BEAD, FAKE_BRANCH)
    assert isinstance(payload["commits"], list)
    assert len(payload["commits"]) == len(raw_rows)


def test_show_payload_empty_base_returns_empty_commits(monkeypatch):
    """When base_of returns '' (branch/integration absent), commits is [] and base is ''."""
    _patch_show_payload_deps(monkeypatch, base="")
    payload = work_show_mod.show_payload({}, FAKE_ENTRY, FAKE_BEAD, FAKE_BRANCH)
    assert payload["base"] == ""
    assert payload["commits"] == []


# ---- ws://work/show/{id} resource tests -------------------------------------


def _patch_resource(monkeypatch, payload: dict):
    """Monkeypatch worktree.locate + work_show.show_payload for resource tests."""
    def _fake_locate(cfg, rig, bead, **kw):
        return FAKE_ENTRY, Path("/fake/main"), Path("/fake/wt"), FAKE_BRANCH

    monkeypatch.setattr(worktree_mod, "locate", _fake_locate)
    monkeypatch.setattr(
        work_show_mod,
        "show_payload",
        lambda cfg, entry, bead, branch: payload,
    )
    monkeypatch.setattr(config_mod, "load", lambda: {})


# ---- registration check ------------------------------------------------------


def test_work_show_resource_is_registered():
    """ws://work/show/{id} appears in the server's resource template list."""
    pytest.importorskip("fastmcp")
    server = mcp_mod.build_server()
    templates = asyncio.run(_list_resource_templates(server))
    uris = {str(t.uriTemplate) for t in templates}
    assert "ws://work/show/{id}" in uris, (
        f"expected ws://work/show/{{id}} in resource template list, got: {uris}"
    )


# ---- payload checks ----------------------------------------------------------


def test_work_show_resource_returns_payload(monkeypatch):
    """ws://work/show/<id> returns the show_payload() dict for a known bead."""
    pytest.importorskip("fastmcp")
    expected = {
        "base": "abc1234",
        "max_commits": 10,
        "commits": [{"sha": FAKE_SHA, "subject": "feat: x", "flags": {}}],
    }
    _patch_resource(monkeypatch, expected)
    server = mcp_mod.build_server()
    contents = asyncio.run(_read(server, f"ws://work/show/{FAKE_BEAD}"))
    assert contents, "expected at least one content block"
    data = json.loads(contents[0].text)
    assert isinstance(data, dict), f"expected a dict, got {type(data)}"
    assert data["base"] == "abc1234"
    assert data["max_commits"] == 10
    assert len(data["commits"]) == 1


def test_work_show_resource_returns_empty_commits_when_no_base(monkeypatch):
    """ws://work/show/<id> returns empty commits when base cannot be resolved."""
    pytest.importorskip("fastmcp")
    _patch_resource(monkeypatch, {"base": "", "max_commits": 10, "commits": []})
    server = mcp_mod.build_server()
    contents = asyncio.run(_read(server, f"ws://work/show/{FAKE_BEAD}"))
    assert contents, "expected at least one content block"
    data = json.loads(contents[0].text)
    assert data["base"] == ""
    assert data["commits"] == []


# ---- annotation / mime checks ------------------------------------------------


def test_work_show_resource_has_json_mime_and_readonly_idempotent_annotations():
    """ws://work/show/{id} defaults: application/json + readOnlyHint=True + idempotentHint=True."""
    pytest.importorskip("fastmcp")
    server = mcp_mod.build_server()
    templates = asyncio.run(_list_resource_templates(server))
    tmpl = next((t for t in templates if str(t.uriTemplate) == "ws://work/show/{id}"), None)
    assert tmpl is not None, "ws://work/show/{id} not found in resource template list"
    assert tmpl.mimeType == "application/json"
    assert tmpl.annotations is not None
    assert tmpl.annotations.readOnlyHint is True
    assert tmpl.annotations.idempotentHint is True
