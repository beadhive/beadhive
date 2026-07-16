"""`ws hive ls --available` + the `hives_available` MCP tool (, Phase 1).

Contract:
  * `hive.available()` diffs git-workspace's tracked repos (read from a synthetic
    `workspace-lock.toml` — zero API calls) against the registered `managed_repos`,
    returning a structured `{candidates, registered}` of `provider/org/repo` triplets;
  * candidates are tracked-but-unregistered repos; already-registered repos are excluded;
  * the CLI printer (`hive.ls`) renders candidates under `--available`, registered by default;
  * the same structured core feeds the `hives_available` MCP tool.

Pure reuse — no real `gh`, no live API, no repo on disk; the lock file is synthetic.
"""

from __future__ import annotations

import asyncio

import pytest

from beadhive import config, hive


def _write_lock(world, *paths: str) -> None:
    """Write a synthetic workspace-lock.toml with one `[[repo]]` per `provider/org/repo`."""
    blocks = "\n".join(
        f'[[repo]]\npath = "{p}"\nurl = "git@github.com:{p}.git"\n' for p in paths
    )
    (world.ws_root / "workspace-lock.toml").write_text(blocks)


def _register(*, provider="github", org="acme", repo="registered", prefix="reg", kind="personal"):
    cfg = config.load()
    cfg.setdefault("managed_repos", []).append(
        {"provider": provider, "org": org, "repo": repo, "prefix": prefix, "kind": kind}
    )
    config.save(cfg)


def test_available_lists_unregistered_and_excludes_registered(world):
    _write_lock(world, "github/acme/registered", "github/acme/widget", "github/other/gizmo")
    _register(org="acme", repo="registered", prefix="reg")

    result = hive.available()

    # Structured shape (not a CLI string).
    assert set(result) == {"candidates", "registered"}
    # Tracked-but-unregistered repos are candidates; the registered one is excluded.
    assert result["candidates"] == ["github/acme/widget", "github/other/gizmo"]
    assert result["registered"] == ["github/acme/registered"]


def test_available_empty_when_every_tracked_repo_is_registered(world):
    _write_lock(world, "github/acme/widget")
    _register(org="acme", repo="widget", prefix="wid")

    assert hive.available()["candidates"] == []


def test_available_no_lock_file_yields_no_candidates(world):
    # No workspace-lock.toml at all → nothing tracked → nothing to register.
    result = hive.available()
    assert result == {"candidates": [], "registered": []}


def test_ls_available_prints_candidates(world, capsys):
    _write_lock(world, "github/acme/registered", "github/acme/widget")
    _register(org="acme", repo="registered", prefix="reg")

    hive.ls(show_available=True)

    out = capsys.readouterr().out
    assert "github/acme/widget" in out
    # The already-registered repo is NOT offered as a candidate.
    assert "github/acme/registered" not in out


def test_ls_default_prints_registered(world, capsys):
    _write_lock(world, "github/acme/widget")
    _register(org="acme", repo="registered", prefix="reg")

    hive.ls()

    out = capsys.readouterr().out
    assert "github/acme/registered" in out
    # The default view is the registry, not the candidate diff.
    assert "github/acme/widget" not in out


def test_hives_available_mcp_tool_returns_same_structured(world):
    pytest.importorskip("fastmcp")
    from fastmcp import Client

    from beadhive import mcp as mcp_mod

    _write_lock(world, "github/acme/registered", "github/acme/widget")
    _register(org="acme", repo="registered", prefix="reg")

    server = mcp_mod.build_server()

    async def call():
        async with Client(server) as client:
            return await client.call_tool("hives_available", {})

    result = asyncio.run(call())
    assert result.data == {
        "candidates": ["github/acme/widget"],
        "registered": ["github/acme/registered"],
    }
