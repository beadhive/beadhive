"""End-to-end tests for ``ws.retire.retire_rig`` — the guarded teardown orchestrator.

Each test stands up a real rig under ``workspace_root()`` (``<provider>/<org>/<repo>``) with a
hermetic bare-repo origin, registers it via the on-disk config (so ``registry.resolve_rig`` and
``registry.unregister`` work for real), then drives ``retire_rig`` through the guardrail contract:

  * SAFE rig                 → archives the clone cleanly + unregisters
  * NEEDS_BACKUP, no flags   → REFUSES (typer.Exit); clone present + still registered
  * NEEDS_BACKUP, --backup   → snapshots unpushed work to origin, then archives
  * NEEDS_BACKUP, --confirm  → proceeds, accepting the loss
  * --dry-run                → mutates nothing (clone present, still registered)
  * --purge                  → hard-deletes the clone instead of archiving

The central invariant under test: a repo never loses data without operator consent.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import typer

from beadhive import config, retire
from beadhive.identity import workspace_root
from beadhive.safety import RetireVerdict

# Scrub dir-pointing GIT_* vars so our -C / cwd git calls always win.
_ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


# ---------------------------------------------------------------------------
# Helpers
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


def _sha(clone: Path, rev: str = "HEAD") -> str:
    return _git("rev-parse", rev, cwd=clone).stdout.strip()


def _remote_has_commit(remote: Path, sha: str) -> bool:
    """True iff *sha* is present as a commit object in the bare *remote*."""
    return subprocess.run(
        ["git", "cat-file", "-e", f"{sha}^{{commit}}"],
        cwd=str(remote), capture_output=True, env=_ENV,
    ).returncode == 0


def _make_no_upstream_clone() -> tuple[Path, Path, str]:
    """A clone with a no-upstream branch carrying a unique commit (NEEDS_BACKUP).

    Returns ``(clone, remote, target_sha)`` where ``target_sha`` is the no-upstream commit
    that backup must make reachable on the remote before any destructive step.
    """
    clone, remote = _make_clone()
    _git("switch", "-c", "feature/x", cwd=clone)
    (clone / "nu.txt").write_text("no-upstream work")
    _git("add", ".", cwd=clone)
    _git("commit", "-m", "no-upstream work", cwd=clone)
    return clone, remote, _sha(clone, "feature/x")


def _add_managed_worktree(clone: Path, leaf: str, *, dirty: bool) -> Path:
    """Link a managed worktree at ``<worktrees_root>/github/myorg/myrepo/<leaf>``.

    The path mirrors ``worktree.wt_dir`` so ``worktree.managed`` enumerates it and
    ``worktree.remove(prefix, leaf)`` can find it. A clean worktree has no changes; a dirty
    one carries an untracked file (``git status --porcelain`` → not clean).
    """
    wt_path = Path(config.worktrees_root()) / "github" / "myorg" / "myrepo" / leaf
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    _git("worktree", "add", "-b", f"wt/{leaf}", str(wt_path), "HEAD", cwd=clone)
    if dirty:
        (wt_path / "scratch.txt").write_text("uncommitted work")
    return wt_path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_safe_rig_archives_and_unregisters(world):
    clone, _remote = _make_clone()
    _register()

    plan = retire.retire_rig("mr")

    assert plan.verdict == RetireVerdict.SAFE
    assert plan.unregistered is True
    # Clone soft-archived under workspace_root()/.archived, preserving the triplet subpath.
    dest = Path(workspace_root()) / ".archived" / "github" / "myorg" / "myrepo"
    assert plan.archived_to == str(dest)
    assert dest.exists()
    assert not clone.exists()
    assert _is_registered() is False


def test_needs_backup_refuses_without_flags(world):
    clone, _remote = _make_needs_backup_clone()
    _register()

    with pytest.raises(typer.Exit):
        retire.retire_rig("mr")

    # Nothing mutated: clone present, still registered, not archived.
    assert clone.exists()
    assert _is_registered() is True
    dest = Path(workspace_root()) / ".archived" / "github" / "myorg" / "myrepo"
    assert not dest.exists()


def test_dirty_worktree_refuses_before_removing_clean_worktrees(world):
    """Gate-first regression: a rig with BOTH a clean and a dirty managed worktree, retired
    with NO flags, must REFUSE *before* touching anything — the clean worktree is still on
    disk afterward. Guards against the mutate-then-refuse defect where the real teardown
    removed clean worktrees and only afterward inspected the dirty set to refuse.
    """
    clone, _remote = _make_clone()  # SAFE clone (pushed); dirtiness lives in the worktrees
    _register()
    clean_wt = _add_managed_worktree(clone, "clean", dirty=False)
    dirty_wt = _add_managed_worktree(clone, "dirty", dirty=True)

    with pytest.raises(typer.Exit):
        retire.retire_rig("mr")

    # The dirty gate fired before any teardown: BOTH worktrees are still present (nothing
    # was removed), the clone is still on disk, and the rig is still registered.
    assert clean_wt.exists(), "clean worktree must NOT be removed when retire refuses"
    assert dirty_wt.exists()
    assert clone.exists()
    assert _is_registered() is True
    dest = Path(workspace_root()) / ".archived" / "github" / "myorg" / "myrepo"
    assert not dest.exists()


def test_needs_backup_with_backup_snapshots_then_archives(world):
    clone, remote = _make_needs_backup_clone()
    _register()

    plan = retire.retire_rig("mr", backup=True)

    assert plan.verdict == RetireVerdict.NEEDS_BACKUP
    assert plan.backed_up is True
    # A durable wip/retire-* branch was pushed to the bare origin.
    branches = _git("branch", "--list", "wip/retire-*", cwd=remote).stdout
    assert "wip/retire-" in branches
    # Then the clone is archived + unregistered.
    assert plan.unregistered is True
    dest = Path(workspace_root()) / ".archived" / "github" / "myorg" / "myrepo"
    assert dest.exists()
    assert not clone.exists()


def test_needs_backup_with_confirm_proceeds(world):
    clone, _remote = _make_needs_backup_clone()
    _register()

    plan = retire.retire_rig("mr", confirm=True)

    assert plan.verdict == RetireVerdict.NEEDS_BACKUP
    assert plan.backed_up is False  # --confirm accepts loss, no backup taken
    assert plan.unregistered is True
    dest = Path(workspace_root()) / ".archived" / "github" / "myorg" / "myrepo"
    assert dest.exists()
    assert not clone.exists()
    assert _is_registered() is False


def test_dry_run_mutates_nothing(world):
    clone, _remote = _make_clone()
    _register()

    plan = retire.retire_rig("mr", dry_run=True)

    assert plan.dry_run is True
    assert plan.unregistered is False
    # Clone still present + still registered + nothing archived.
    assert clone.exists()
    assert _is_registered() is True
    dest = Path(workspace_root()) / ".archived" / "github" / "myorg" / "myrepo"
    assert not dest.exists()


def test_purge_hard_deletes_instead_of_archiving(world):
    clone, _remote = _make_clone()
    _register()

    plan = retire.retire_rig("mr", purge=True)

    assert plan.purged is True
    assert plan.archived_to is None
    assert not clone.exists()
    # Purge removes; it does NOT archive.
    dest = Path(workspace_root()) / ".archived" / "github" / "myorg" / "myrepo"
    assert not dest.exists()
    assert _is_registered() is False


def test_missing_clone_path_errors(world):
    # Registered but never cloned on disk → retire must error clearly.
    _register(repo="ghost", prefix="ghost")

    with pytest.raises(typer.Exit):
        retire.retire_rig("ghost")


def test_archive_dir_config_override_is_honored(world):
    clone, _remote = _make_clone()
    _register()
    custom = Path(workspace_root()) / "custom-attic"
    cfg = config.load()
    cfg["archive"] = {"dir": str(custom)}
    config.save(cfg)

    plan = retire.retire_rig("mr")

    dest = custom / "github" / "myorg" / "myrepo"
    assert plan.archived_to == str(dest)
    assert dest.exists()
    assert not clone.exists()


# ---------------------------------------------------------------------------
# Data-loss regression suite — the contract: a repo never loses data without the
# operator either backing it up durably (pushed) or explicitly consenting.
# ---------------------------------------------------------------------------


def test_no_upstream_backup_reaches_remote_before_purge(world):
    """``--backup --purge`` on a no-upstream branch carrying a real commit must push that
    commit durably BEFORE the clone is hard-deleted. The bare remote (which survives the
    purge) is proof the work was not lost."""
    clone, remote, target = _make_no_upstream_clone()
    _register()

    plan = retire.retire_rig("mr", backup=True, purge=True)

    assert plan.backed_up is True
    assert plan.purged is True
    assert not clone.exists()
    # The no-upstream commit is durably on the remote (survived the purge).
    assert _remote_has_commit(remote, target), "no-upstream commit must reach the remote"
    assert _is_registered() is False


def test_dry_run_with_backup_mutates_nothing(world):
    """``--dry-run --backup`` on a NEEDS_BACKUP repo previews the plan but mutates NOTHING:
    no wip branch locally, no ref on the remote, clone present + still registered."""
    clone, remote, _target = _make_no_upstream_clone()
    _register()

    plan = retire.retire_rig("mr", dry_run=True, backup=True)

    assert plan.dry_run is True
    assert plan.unregistered is False
    assert clone.exists()
    assert _is_registered() is True
    # No wip branch created locally, nothing pushed to the remote.
    assert _git("branch", "--list", "wip/retire-*", cwd=clone).stdout.strip() == ""
    assert _git("branch", "--list", "wip/retire-*", cwd=remote).stdout.strip() == ""
    dest = Path(workspace_root()) / ".archived" / "github" / "myorg" / "myrepo"
    assert not dest.exists()


def test_detached_head_backup_reaches_remote_before_purge(world):
    """``--backup --purge`` on a detached HEAD carrying a commit must snapshot + push it
    durably before purge; the commit survives on the bare remote."""
    clone, remote = _make_clone()
    _register()
    _git("checkout", "--detach", "HEAD", cwd=clone)
    (clone / "det.txt").write_text("detached work")
    _git("add", ".", cwd=clone)
    _git("commit", "-m", "detached work", cwd=clone)
    target = _sha(clone, "HEAD")

    plan = retire.retire_rig("mr", backup=True, purge=True)

    assert plan.backed_up is True
    assert plan.purged is True
    assert _remote_has_commit(remote, target), "detached commit must reach the remote"


def test_ready_with_stash_backup_reaches_remote_before_purge(world):
    """``--backup --purge`` on a READY repo carrying a stash must back the stash up to the
    remote (not silently drop it) before purge."""
    clone, remote = _make_clone()
    _register()
    (clone / "file.txt").write_text("stashed change")
    _git("stash", "push", "-m", "wip", cwd=clone)
    stash_sha = _sha(clone, "stash@{0}")

    plan = retire.retire_rig("mr", backup=True, purge=True)

    assert plan.backed_up is True
    assert plan.purged is True
    assert _remote_has_commit(remote, stash_sha), "stash commit must reach the remote"


def test_backup_push_failure_aborts_retire_intact(world):
    """If the backup CANNOT push (bogus/unwritable origin), retire ABORTS with typer.Exit and
    deletes NOTHING — the clone is still present and still registered."""
    clone, _remote, _target = _make_no_upstream_clone()
    _register()
    # Point origin at a non-existent remote so the backup push fails.
    _git("remote", "set-url", "origin", str(Path(workspace_root()) / "nope.git"), cwd=clone)

    with pytest.raises(typer.Exit):
        retire.retire_rig("mr", backup=True, purge=True)

    # Nothing deleted: clone present, still registered, nothing archived.
    assert clone.exists(), "clone must survive a failed backup"
    assert _is_registered() is True
    dest = Path(workspace_root()) / ".archived" / "github" / "myorg" / "myrepo"
    assert not dest.exists()


def test_archive_move_failure_does_not_leave_unregistered_on_disk(world, monkeypatch):
    """If the irreversible archive ``shutil.move`` fails, the rig must NOT end up
    unregistered-but-on-disk: unregister happens only AFTER the move succeeds (H1/M3)."""
    clone, _remote = _make_clone()
    _register()

    def _boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(retire.shutil, "move", _boom)

    with pytest.raises(OSError):
        retire.retire_rig("mr")

    # Move failed → clone still on disk AND still registered (never unregistered).
    assert clone.exists()
    assert _is_registered() is True


def test_clean_worktree_removal_failure_blocks_destructive_steps(world, monkeypatch):
    """A clean worktree that FAILS to remove must gate retire: no archive/purge, clone and
    registration intact (don't delete a clone a live worktree still points at) (C4)."""
    clone, _remote = _make_clone()
    _register()
    clean_wt = _add_managed_worktree(clone, "clean", dirty=False)

    # Force the real removal to fail (git error surfaces as typer.Exit inside worktree.remove).
    def _fail_remove(*_a, **_k):
        raise typer.Exit(1)

    monkeypatch.setattr(retire.worktree, "remove", _fail_remove)

    with pytest.raises(typer.Exit):
        retire.retire_rig("mr")

    # Gated before any destructive step: nothing archived, clone + worktree + registration intact.
    assert clean_wt.exists()
    assert clone.exists()
    assert _is_registered() is True
    dest = Path(workspace_root()) / ".archived" / "github" / "myorg" / "myrepo"
    assert not dest.exists()
