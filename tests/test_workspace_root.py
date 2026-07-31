"""identity.workspace_root() — the ONE root-resolution choke point (bh-cgcg.1).

3-tier precedence: `$GIT_WORKSPACE` > config `git_workspace.mode`/`.root` > internal default
(``<bh home>/ws`` — a sibling of the worktrees shadow tree at ``<bh home>/wt``), with a legacy
guard on the internal-default tier so flipping the default never silently relocates an
existing populated ``~/workspace``. Every test wires its own env/config in isolation via the
``_isolated`` fixture (no `GIT_WORKSPACE`, an isolated `$BH_HOME`, and `identity._legacy_root`
monkeypatched to a tmp stand-in) — nothing here may resolve to, or depend on, the real
``~/workspace`` / ``~/.beadhive`` on the machine running the suite.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from beadhive import config, identity, metadata


@pytest.fixture
def _isolated(tmp_path, monkeypatch):
    """The "fresh install" baseline: no `$GIT_WORKSPACE`, a bh home under `tmp_path`, no
    config.yaml yet, and the legacy `~/workspace` stand-in monkeypatched to an as-yet
    nonexistent tmp dir. Returns the `home` and `legacy` paths for assertions."""
    monkeypatch.delenv("GIT_WORKSPACE", raising=False)
    home = tmp_path / "bh-home"
    monkeypatch.setenv("BH_HOME", str(home))
    monkeypatch.delenv("BH_CONFIG", raising=False)
    monkeypatch.delenv("WS_HOME", raising=False)
    legacy = tmp_path / "home-workspace"  # stand-in for ~/workspace
    monkeypatch.setattr(identity, "_legacy_root", lambda: legacy)
    return {"home": home, "legacy": legacy}


def _write_config(mode=None, root=None, managed_repos=None):
    """Scaffold a minimal host config.yaml (so `config.load()` succeeds) and optionally set
    `git_workspace.mode`/`.root` and/or `managed_repos`."""
    cfg_path = config.config_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text("providers: [github]\nmanaged_repos: []\n")
    cfg = config.load()
    gw = {}
    if mode is not None:
        gw["mode"] = mode
    if root is not None:
        gw["root"] = root
    if gw:
        cfg["git_workspace"] = gw
    if managed_repos is not None:
        cfg["managed_repos"] = managed_repos
    config.save(cfg)


# ---- 1. $GIT_WORKSPACE always wins, over both config and any legacy state ----


def test_env_wins_over_config_and_default(_isolated, tmp_path, monkeypatch):
    _write_config(mode="internal")
    (_isolated["legacy"] / "github" / "acme" / "api" / ".git").mkdir(parents=True)
    env_root = tmp_path / "env-workspace"
    monkeypatch.setenv("GIT_WORKSPACE", str(env_root))

    assert identity.workspace_root() == str(env_root.resolve())


# ---- 2. fresh install: no config, no existing workspace -> internal default --


def test_fresh_install_resolves_to_bh_home_ws(_isolated):
    assert not _isolated["legacy"].exists()  # nothing pre-existing to guard against

    assert identity.workspace_root() == str((_isolated["home"] / "ws").resolve())


def test_internal_default_derives_from_bh_home_not_tilde(tmp_path, monkeypatch):
    """bh-cgcg.1 acceptance: the internal default follows the resolved bh home ($BH_HOME),
    never a hardcoded ``~`` / ``/home/<user>`` assumption — verified with `$BH_HOME` at a
    non-``~`` location, mirroring the operator's own host (``HOME=/opt/orca``)."""
    monkeypatch.delenv("GIT_WORKSPACE", raising=False)
    non_tilde_home = tmp_path / "opt" / "orca" / ".beadhive"
    monkeypatch.setenv("BH_HOME", str(non_tilde_home))
    monkeypatch.delenv("BH_CONFIG", raising=False)
    monkeypatch.delenv("WS_HOME", raising=False)
    legacy = tmp_path / "opt" / "orca" / "workspace"  # ~/workspace under this same non-~ HOME
    monkeypatch.setattr(identity, "_legacy_root", lambda: legacy)
    assert not str(non_tilde_home).startswith(str(Path.home()))

    assert identity.workspace_root() == str((non_tilde_home / "ws").resolve())


# ---- 3. explicit config: mode + optional root override -----------------------


def test_config_mode_internal_explicit(_isolated):
    """An explicit `internal` wins even over a populated legacy workspace — a deliberate
    operator opt-in, not the guarded default path."""
    _write_config(mode="internal")
    (_isolated["legacy"] / "github" / "acme" / "api" / ".git").mkdir(parents=True)

    assert identity.workspace_root() == str((_isolated["home"] / "ws").resolve())


def test_config_mode_external_explicit_reads_legacy_default(_isolated):
    """`mode: external` with no `root` override resolves to the same default location
    git-workspace itself has always used — reading the user's workspace*.toml exactly as
    today."""
    _write_config(mode="external")

    assert identity.workspace_root() == str(_isolated["legacy"].resolve())


def test_config_root_override_wins_regardless_of_mode(_isolated, tmp_path):
    # `_isolated` isn't referenced by name — it's needed for its side effect (an isolated
    # $BH_HOME + no $GIT_WORKSPACE) so `_write_config` resolves config_path() under tmp_path.
    override = tmp_path / "custom-root"
    _write_config(mode="internal", root=str(override))

    assert identity.workspace_root() == str(override.resolve())


# ---- 4. the legacy guard: populated vs. merely-existing-and-empty ------------


def test_existing_populated_workspace_resolves_external_no_root_swap(_isolated):
    """A real clone under the legacy root (no explicit config) resolves EXTERNAL — the
    breaking-change guard bh-cgcg exists for. factory-orca's 16 real clones are the
    acceptance test for this exact branch."""
    (_isolated["legacy"] / "github" / "acme" / "api" / ".git").mkdir(parents=True)

    assert identity.workspace_root() == str(_isolated["legacy"].resolve())


def test_existing_but_empty_workspace_dir_does_not_pin_external(_isolated):
    """An empty leftover `~/workspace` (directory exists, no clones, no registered hives)
    must NOT pin a fresh install to external mode forever — it still resolves the internal
    default."""
    _isolated["legacy"].mkdir(parents=True)

    assert identity.workspace_root() == str((_isolated["home"] / "ws").resolve())


def test_registered_hive_without_an_on_disk_clone_still_counts_as_populated(_isolated):
    """"Existing and populated" also covers a hive already registered in `managed_repos`,
    not just an on-disk clone under the legacy root — registered hives keep resolving."""
    _write_config(
        managed_repos=[{"provider": "github", "org": "acme", "repo": "api", "prefix": "ac-api"}]
    )
    assert not _isolated["legacy"].exists()  # no on-disk clone at all — registration alone counts

    assert identity.workspace_root() == str(_isolated["legacy"].resolve())


def test_no_cache_invalidation_when_populated_workspace_resolves_external(_isolated):
    """metadata.py coarse-invalidates its cache on a `workspace_root` mismatch (root moved).
    The guard keeping an existing populated workspace external must make `workspace_root()`
    stable across calls (nothing actually moved), so a warm cache for a registered hive stays
    warm rather than being dropped."""
    resolved_legacy = str(_isolated["legacy"].resolve())
    (_isolated["legacy"] / "github" / "acme" / "api" / ".git").mkdir(parents=True)

    cache_path = config.cache_dir() / "metadata.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(
            {
                "version": 1,
                "last_updated": None,
                "workspace_root": resolved_legacy,
                "repos": {
                    "github/acme/api": {
                        "git_head": "abc123",
                        "git_mtime": 1000.0,
                        "measured_at": "2026-01-01T00:00:00Z",
                        "category": "READY",
                        "has_origin": True,
                        "stash_count": 0,
                        "disk_bytes": 42,
                        "commit_count": 7,
                        "age_days": 1.5,
                        "last_commit": "2026-01-01",
                        "branches": [],
                        "worktrees": [],
                        "dolt_ref": {},
                    }
                },
            }
        )
    )

    cache = metadata.load()

    assert cache.workspace_root == resolved_legacy
    assert "github/acme/api" in cache.repos  # preserved, not coarse-invalidated


# ---- git_workspace.enabled's meaning is unchanged -----------------------------


def test_git_workspace_enabled_meaning_unchanged():
    """`enabled` still only gates whether bh reads git-workspace's own repo-group config —
    orthogonal to `mode`/`root`, and settable independently of either. A pure function test
    over hand-built dicts — no env/config isolation needed, so no `_isolated` fixture here."""
    from beadhive import gitworkspace

    cfg = {"git_workspace": {"enabled": True, "mode": "internal"}}
    assert gitworkspace.enabled(cfg) is True

    cfg = {"git_workspace": {"enabled": False, "mode": "external"}}
    assert gitworkspace.enabled(cfg) is False
