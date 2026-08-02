"""End-to-end tests for ``ws.retire.reclaim_hive`` — the HOST-LOCAL guarded teardown.

``managed_repos`` is FLEET-scoped truth (config_partition.py): ``bh hive retire``/``bh hive
rm`` unregister a hive for every host. ``reclaim_hive`` is the host-local counterpart —
identical assess -> (backup|consent) -> worktree teardown -> archive/purge safety contract as
``retire_hive`` (same ``safety.assess_retire`` verdict, unchanged), but the registry step is
never reached: the hive stays registered for the fleet and every other host's clone is
untouched. The mirror of test_hive_retire.py, minus the registry assertions, plus the
central new guarantee: ``managed_repos`` (the whole host config.yaml, byte-for-byte) is
untouched by a reclaim.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import typer

from beadhive import config, registry, retire
from beadhive.identity import workspace_root
from beadhive.safety import RetireVerdict

# Scrub dir-pointing GIT_* vars so our -C / cwd git calls always win.
_ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


# ---------------------------------------------------------------------------
# Helpers (mirrors tests/test_hive_retire.py)
# ---------------------------------------------------------------------------


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True, env=_ENV
    )


def _register(provider="github", org="myorg", repo="myrepo", prefix="mr") -> None:
    cfg = config.load()
    cfg.setdefault("managed_repos", []).append(
        {"provider": provider, "org": org, "repo": repo, "prefix": prefix, "kind": "personal"}
    )
    config.save(cfg)


def _is_registered(provider="github", org="myorg", repo="myrepo") -> bool:
    key = f"{provider}/{org}/{repo}"
    return any(
        f"{e['provider']}/{e['org']}/{e['repo']}" == key
        for e in config.load().get("managed_repos", [])
    )


def _make_clone(provider="github", org="myorg", repo="myrepo") -> tuple[Path, Path]:
    """Create a clone at ``workspace_root()/<provider>/<org>/<repo>`` with a bare origin.

    Returns ``(clone_path, remote_path)``; ``main`` is pushed and tracks ``origin/main``.
    """
    root = Path(workspace_root())
    remote = root / "_remotes" / f"{repo}.git"
    remote.mkdir(parents=True)
    _git("init", "--bare", "-b", "main", cwd=remote)

    clone = root / provider / org / repo
    clone.mkdir(parents=True)
    _git("init", "-b", "main", cwd=clone)
    _git("config", "user.email", "test@ws.dev", cwd=clone)
    _git("config", "user.name", "WS Test", cwd=clone)
    (clone / "file.txt").write_text("hello")
    _git("add", ".", cwd=clone)
    _git("commit", "-m", "init", cwd=clone)
    _git("remote", "add", "origin", str(remote), cwd=clone)
    _git("push", "-u", "origin", "main", cwd=clone)
    return clone, remote


def _make_needs_backup_clone() -> tuple[Path, Path]:
    """A clone that is one commit ahead of origin/main (PUSH_NEEDED → NEEDS_BACKUP)."""
    clone, remote = _make_clone()
    (clone / "extra.txt").write_text("unpushed work")
    _git("add", ".", cwd=clone)
    _git("commit", "-m", "unpushed change", cwd=clone)
    return clone, remote


def _add_managed_worktree(clone: Path, leaf: str, *, dirty: bool) -> Path:
    """Link a managed worktree at ``<worktrees_root>/github/myorg/myrepo/<leaf>``."""
    wt_path = Path(config.worktrees_root()) / "github" / "myorg" / "myrepo" / leaf
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    _git("worktree", "add", "-b", f"wt/{leaf}", str(wt_path), "HEAD", cwd=clone)
    if dirty:
        (wt_path / "scratch.txt").write_text("uncommitted work")
    return wt_path


def _config_bytes() -> bytes:
    return config.config_path().read_bytes()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_safe_hive_reclaims_but_stays_registered(world):
    clone, _remote = _make_clone()
    _register()

    plan = retire.reclaim_hive("mr")

    assert plan.verdict == RetireVerdict.SAFE
    assert plan.unregistered is False
    # Clone soft-archived under workspace_root()/.archived, preserving the triplet subpath —
    # same disposition as retire_hive.
    dest = Path(workspace_root()) / ".archived" / "github" / "myorg" / "myrepo"
    assert plan.archived_to == str(dest)
    assert dest.exists()
    assert not clone.exists()
    # The whole point: still registered for the fleet.
    assert _is_registered() is True


def test_reclaim_leaves_managed_repos_byte_identical(world):
    """The acceptance-critical assertion: a host-local reclaim must not touch managed_repos —
    not just logically (still registered) but literally: the host config.yaml is byte-for-byte
    identical before and after."""
    _make_clone()
    _register()
    before = _config_bytes()

    retire.reclaim_hive("mr")

    after = _config_bytes()
    assert after == before


def test_reclaim_never_calls_registry_unregister(world, monkeypatch):
    """Stronger than an output/state assertion: the registry write path itself must never be
    invoked on the host-local path — not merely skipped in its effect."""
    _make_clone()
    _register()

    def _boom(*_a, **_k):
        raise AssertionError("reclaim_hive must never call registry.unregister")

    monkeypatch.setattr(registry, "unregister", _boom)

    plan = retire.reclaim_hive("mr")

    assert plan.unregistered is False


def test_needs_backup_refuses_without_flags(world):
    clone, _remote = _make_needs_backup_clone()
    _register()
    before = _config_bytes()

    with pytest.raises(typer.Exit):
        retire.reclaim_hive("mr")

    # Nothing mutated: clone present, still registered, not archived, config untouched.
    assert clone.exists()
    assert _is_registered() is True
    assert _config_bytes() == before
    dest = Path(workspace_root()) / ".archived" / "github" / "myorg" / "myrepo"
    assert not dest.exists()


def test_dirty_worktree_refuses_before_removing_clean_worktrees(world):
    """Gate-first regression, mirrored from retire: a hive with BOTH a clean and a dirty
    managed worktree, reclaimed with NO flags, must REFUSE before touching anything."""
    clone, _remote = _make_clone()  # SAFE clone (pushed); dirtiness lives in the worktrees
    _register()
    clean_wt = _add_managed_worktree(clone, "clean", dirty=False)
    dirty_wt = _add_managed_worktree(clone, "dirty", dirty=True)

    with pytest.raises(typer.Exit):
        retire.reclaim_hive("mr")

    assert clean_wt.exists(), "clean worktree must NOT be removed when reclaim refuses"
    assert dirty_wt.exists()
    assert clone.exists()
    assert _is_registered() is True


def test_needs_backup_with_backup_snapshots_then_archives(world):
    clone, remote = _make_needs_backup_clone()
    _register()

    plan = retire.reclaim_hive("mr", backup=True)

    assert plan.verdict == RetireVerdict.NEEDS_BACKUP
    assert plan.backed_up is True
    branches = _git("branch", "--list", "wip/retire-*", cwd=remote).stdout
    assert "wip/retire-" in branches
    assert plan.unregistered is False
    dest = Path(workspace_root()) / ".archived" / "github" / "myorg" / "myrepo"
    assert dest.exists()
    assert not clone.exists()
    assert _is_registered() is True


def test_needs_backup_with_confirm_proceeds(world):
    clone, _remote = _make_needs_backup_clone()
    _register()

    plan = retire.reclaim_hive("mr", confirm=True)

    assert plan.verdict == RetireVerdict.NEEDS_BACKUP
    assert plan.backed_up is False  # --confirm accepts loss, no backup taken
    assert plan.unregistered is False
    dest = Path(workspace_root()) / ".archived" / "github" / "myorg" / "myrepo"
    assert dest.exists()
    assert not clone.exists()
    assert _is_registered() is True


def test_dry_run_mutates_nothing(world):
    clone, _remote = _make_clone()
    _register()
    before = _config_bytes()

    plan = retire.reclaim_hive("mr", dry_run=True)

    assert plan.dry_run is True
    assert plan.unregistered is False
    assert clone.exists()
    assert _is_registered() is True
    assert _config_bytes() == before
    dest = Path(workspace_root()) / ".archived" / "github" / "myorg" / "myrepo"
    assert not dest.exists()


def test_purge_hard_deletes_instead_of_archiving_but_stays_registered(world):
    clone, _remote = _make_clone()
    _register()

    plan = retire.reclaim_hive("mr", purge=True)

    assert plan.purged is True
    assert plan.archived_to is None
    assert not clone.exists()
    dest = Path(workspace_root()) / ".archived" / "github" / "myorg" / "myrepo"
    assert not dest.exists()
    # Purge deletes THIS host's clone but never touches the registry.
    assert _is_registered() is True


def test_missing_clone_path_errors(world):
    # Registered but never cloned on disk → reclaim must error clearly, same as retire.
    _register(repo="ghost", prefix="ghost")

    with pytest.raises(typer.Exit):
        retire.reclaim_hive("ghost")


def test_archive_dir_config_override_is_honored(world):
    clone, _remote = _make_clone()
    _register()
    custom = Path(workspace_root()) / "custom-attic"
    cfg = config.load()
    cfg["archive"] = {"dir": str(custom)}
    config.save(cfg)

    plan = retire.reclaim_hive("mr")

    dest = custom / "github" / "myorg" / "myrepo"
    assert plan.archived_to == str(dest)
    assert dest.exists()
    assert not clone.exists()
    assert _is_registered() is True
