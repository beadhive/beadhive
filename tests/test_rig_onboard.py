"""`ws rig onboard` — end-to-end rig onboarding from a local folder OR a remote clone-down
.

Contract:
  * onboard resolves target = workspace_root()/provider/org/repo;
  * absent target + --clone-url → `git clone` it down first; absent target + no url → abort;
  * already-local folder → no clone, onboards in place;
  * then runs the full `rig init` logic with cwd=target (file installers write under target,
    NOT the process cwd — the cwd-threading contract), and finally `hub.sync()`.

These run without real `bd`/`gh`/network: a `.beads/` dir is pre-created (or created by the fake
clone) so `rig init` skips `bd init`, classification is stubbed on the fresh path, and `hub.sync`
is replaced with a recorder so onboarding stays hermetic.
"""

from __future__ import annotations

import types

import pytest
import typer

from beadhive import config, hub, registry, rig
from harness.world import git


@pytest.fixture
def synced(monkeypatch):
    """Record hub.sync() calls so onboard never touches a real hub DB."""
    calls = []
    monkeypatch.setattr(hub, "sync", lambda: calls.append(True))
    return calls


def _entry(org="acme", repo="widget"):
    return registry.find_entry(config.load(), "github", org, repo)


def _make_local_repo(world, *, org="acme", repo="widget"):
    """A git repo under $GIT_WORKSPACE with `.beads/` present (so init skips `bd init`)."""
    target = world.ws_root / "github" / org / repo
    target.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=target)
    (target / ".beads").mkdir()
    return target


def test_onboard_local_folder_no_clone_runs_init_in_target(world, synced, monkeypatch):
    # Already-local folder: no clone happens, and init's file installers must write under the
    # target (proving cwd is threaded), even though the process cwd is the empty ws root.
    target = _make_local_repo(world)
    world.chdir(world.ws_root)
    # `run` must NOT be asked to clone for a folder that already exists (the scaffold
    # step's own git calls pass through and run for real).
    from beadhive.run import run as real_run

    def no_clone_run(cmd, **kw):
        if cmd[:2] == ["git", "clone"]:
            pytest.fail("must not clone an existing dir")
        return real_run(cmd, **kw)

    monkeypatch.setattr(rig, "run", no_clone_run)
    monkeypatch.setattr(registry, "classify", lambda *a, **k: "personal-or-prototype")

    rig.onboard("github/acme/widget", prime=True)

    assert (target / ".beads" / "PRIME.md").exists()  # init wrote under target, not cwd
    assert not (world.ws_root / ".beads" / "PRIME.md").exists()  # never leaked to process cwd
    assert _entry() is not None  # registered
    assert synced == [True]  # hub.sync() ran


def test_onboard_remote_clone_down(world, synced, monkeypatch):
    # Absent target + --clone-url: onboard clones it down, then inits in the cloned tree.
    target = world.ws_root / "github" / "acme" / "widget"
    assert not target.exists()
    monkeypatch.setattr(registry, "classify", lambda *a, **k: "personal-or-prototype")

    cloned = []

    from beadhive.run import run as real_run

    def fake_run(cmd, **kw):
        # Stub the clone: materialize a real git repo + `.beads/` at the destination so the
        # subsequent identity derivation + init proceed without network or `bd`. Every other
        # git call (the scaffold step's) runs for real.
        if cmd[:2] != ["git", "clone"]:
            return real_run(cmd, **kw)
        url, dest = cmd[2], cmd[3]
        cloned.append((url, dest))
        target.mkdir(parents=True, exist_ok=True)
        git("init", "-q", "-b", "main", cwd=dest)
        (target / ".beads").mkdir()
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(rig, "run", fake_run)

    rig.onboard("github/acme/widget", clone_url="git@example.com:acme/widget.git", prime=True)

    assert cloned == [("git@example.com:acme/widget.git", str(target))]
    assert (target / ".beads" / "PRIME.md").exists()  # init ran in the cloned target
    assert _entry() is not None
    assert synced == [True]


def test_onboard_absent_without_clone_url_aborts(world, synced):
    # Absent target and no --clone-url: nothing to onboard — abort (and never sync the hub).
    with pytest.raises(typer.Exit):
        rig.onboard("github/acme/widget")
    assert synced == []


def test_onboard_rejects_non_triplet(world, synced):
    with pytest.raises(typer.Exit):
        rig.onboard("acme/widget")  # only two parts — not provider/org/repo
    assert synced == []


def _make_committed_repo(world, *, org="acme", repo="widget"):
    """A committed git repo under $GIT_WORKSPACE with `.beads/` (so init skips `bd init`).
    Committed (not unborn) so safety.scan has a checked-out branch to flag dirty."""
    target = world.ws_root / "github" / org / repo
    target.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=target)
    git("config", "user.email", "t@ws.dev", cwd=target)
    git("config", "user.name", "T", cwd=target)
    (target / "README.md").write_text("hi")
    git("add", ".", cwd=target)
    git("commit", "-q", "-m", "init", cwd=target)
    (target / ".beads").mkdir()
    return target


def test_onboard_dry_run_lists_checks_and_mutates_nothing(world, synced, monkeypatch, capsys):
    # --dry-run surfaces the preflight check ids and performs zero mutation.
    _make_committed_repo(world)
    world.chdir(world.ws_root)
    monkeypatch.setattr(registry, "classify", lambda *a, **k: "personal-or-prototype")

    rig.onboard("github/acme/widget", prime=True, dry_run=True)

    out = capsys.readouterr().out
    assert "dirty-tree" in out and "on-default-branch" in out  # ids discoverable
    assert not (world.ws_root / "github" / "acme" / "widget" / ".beads" / "PRIME.md").exists()
    assert _entry() is None  # never registered
    assert synced == []  # hub never synced


def test_onboard_refuses_dirty_folder_before_bd_init(world, synced, monkeypatch):
    # A dirty existing folder fails the preflight gate — before bd-init/register/hub-sync.
    target = _make_committed_repo(world)
    (target / "wip.txt").write_text("uncommitted")  # dirty the tree
    world.chdir(world.ws_root)
    monkeypatch.setattr(registry, "classify", lambda *a, **k: "personal-or-prototype")

    with pytest.raises(typer.Exit):
        rig.onboard("github/acme/widget")

    assert _entry() is None
    assert synced == []


def test_onboard_skip_check_proceeds_past_dirty_and_branch(world, synced, monkeypatch):
    # --skip-check downgrades the dirty-tree / on-default-branch failures to warnings.
    target = _make_committed_repo(world)
    git("checkout", "-q", "-b", "feature", cwd=target)  # off default branch
    (target / "wip.txt").write_text("uncommitted")  # and dirty
    world.chdir(world.ws_root)
    monkeypatch.setattr(registry, "classify", lambda *a, **k: "personal-or-prototype")

    rig.onboard("github/acme/widget", skip_check="dirty-tree,on-default-branch")

    assert _entry() is not None  # onboarding proceeded to registration
    assert synced == [True]


def test_init_accepts_explicit_cwd(world, monkeypatch):
    # The cwd-threading contract in isolation: rig.init(cwd=target) writes under target even
    # when the process cwd is elsewhere.
    target = _make_local_repo(world, repo="other")
    world.chdir(world.ws_root)
    monkeypatch.setattr(registry, "classify", lambda *a, **k: "personal-or-prototype")

    rig.init(prime=True, cwd=str(target))

    assert (target / ".beads" / "PRIME.md").exists()
    assert not (world.ws_root / ".beads" / "PRIME.md").exists()
