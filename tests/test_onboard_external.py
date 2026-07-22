"""`kind=external` — the fork/dual-remote onboarding flow (bh-uxam.1).

Contract:
  * `hive onboard <provider/org/repo> --kind external` forks the triplet (`gh repo fork
    --clone --remote`), landing origin=our fork + upstream=the target repo, and registers a
    managed_repos entry with kind=external + upstream=<o/r> + a contribution marker;
  * `.beads/` stays stealth-excluded (external hives are never furnished — same convention as
    `kind=fork`, generalized);
  * `--dry-run` mutates nothing (no fork/clone, no registry write) but the plan already carries
    the intended kind/upstream/prefix — deterministic from the triplet, no clone required;
  * `worktree.push_branch` refuses a push aimed at the `upstream` remote (see
    test_worktree.py's pull-only rail tests for that half of the contract).
"""

from __future__ import annotations

import types

import pytest
import typer

from beadhive import config, hive, hub, onboard, registry
from harness.world import git


@pytest.fixture
def synced(monkeypatch):
    calls = []
    monkeypatch.setattr(hub, "sync", lambda: calls.append(True))
    return calls


def _entry(org="stablyai", repo="orca"):
    return registry.find_entry(config.load(), "github", org, repo)


def _ext_ctx(world, target, *, org="stablyai", repo="orca", **kw):
    ctx = onboard.Ctx(
        hive=f"github/{org}/{repo}", target=str(target), provider="github", org=org, repo=repo,
        cwd=str(target), cfg=config.load(), kind="external", do_hub_sync=True, **kw,
    )
    ctx.steps = onboard.build_steps(ctx)
    return ctx


# ---------------------------------------------------------------------------
# --dry-run: intended remote + registry plan, zero mutation
# ---------------------------------------------------------------------------


def test_dry_run_scratch_triplet_emits_intended_plan_without_mutation(world, synced, monkeypatch):
    target = world.ws_root / "github" / "stablyai" / "orca"
    assert not target.exists()

    def _no_subprocess(cmd, **kw):  # noqa: ARG001
        raise AssertionError(f"dry-run must not shell out: {cmd}")

    monkeypatch.setattr(hive, "run", _no_subprocess)

    ctx = _ext_ctx(world, target, yes=True)
    plan = onboard.run_onboard(ctx, dry_run=True)

    # The intended plan is fully derived without ever cloning/forking anything.
    assert plan.dry_run is True
    assert plan.cloned is False
    assert "clone" in plan.steps_run  # planned (would run), never executed
    assert ctx.kind == "external"
    assert ctx.upstream == "stablyai/orca"  # the fork target, derived from the triplet itself
    assert ctx.prefix == "fork-orca"

    # Zero mutation: no clone, no registry write, hub never synced.
    assert not target.exists()
    assert _entry() is None
    assert synced == []
    assert plan.registered is False


def test_dry_run_never_requires_clone_url(world, synced, monkeypatch):
    """external onboarding derives what to fork from the triplet — --clone-url is N/A."""
    target = world.ws_root / "github" / "stablyai" / "orca"
    monkeypatch.setattr(hive, "run", lambda *a, **k: pytest.fail("must not shell out"))
    ctx = _ext_ctx(world, target, yes=True)  # no clone_url set

    plan = onboard.run_onboard(ctx, dry_run=True)

    assert all(c.ok for c in plan.checks if c.id == "clone-url-present")


def test_dry_run_fork_needs_yes_still_gates(world, synced, monkeypatch):
    """external hives are forks — the fork-needs-yes gate fires without --yes, same as kind=fork."""
    target = world.ws_root / "github" / "stablyai" / "orca"
    monkeypatch.setattr(hive, "run", lambda *a, **k: pytest.fail("must not shell out"))
    ctx = _ext_ctx(world, target)  # no --yes

    with pytest.raises(typer.Exit):
        onboard.run_onboard(ctx, dry_run=True)

    assert _entry() is None


# ---------------------------------------------------------------------------
# Real run: gh repo fork --clone wiring + registry entry
# ---------------------------------------------------------------------------


def _fake_gh_fork_clone(target, *, fork_owner="acme", org="stablyai", repo="orca"):
    """Fake `hive.run` that materializes what `gh repo fork <org>/<repo> --clone --remote --
    <target>` actually does: clone the fork to `target`, origin=fork, upstream=the target repo,
    `.beads/` pre-created so bd-init is skipped (hermetic, no real `bd`). Every other `hive.run`
    call (the footprint/scaffold step's own git calls) passes through to the real subprocess,
    mirroring test_hive_onboard.py's clone-stub convention."""
    from beadhive.run import run as real_run

    def fake_run(cmd, **kw):
        if cmd[:3] != ["gh", "repo", "fork"]:
            return real_run(cmd, **kw)
        assert cmd[3] == f"{org}/{repo}"
        assert "--clone" in cmd and "--remote" in cmd
        dest = cmd[-1]
        assert dest == str(target)
        target.mkdir(parents=True, exist_ok=True)
        git("init", "-q", "-b", "main", cwd=target)
        git("config", "user.email", "t@ws.dev", cwd=target)
        git("config", "user.name", "T", cwd=target)
        (target / "README.md").write_text("hi")
        git("add", ".", cwd=target)
        git("commit", "-q", "-m", "init", cwd=target)
        git("remote", "add", "origin", f"git@github.com:{fork_owner}/{repo}.git", cwd=target)
        git("remote", "add", "upstream", f"git@github.com:{org}/{repo}.git", cwd=target)
        (target / ".beads").mkdir()
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    return fake_run


def test_onboard_external_forks_clones_and_registers(world, synced, monkeypatch):
    target = world.ws_root / "github" / "stablyai" / "orca"
    monkeypatch.setattr(hive, "run", _fake_gh_fork_clone(target))
    monkeypatch.setattr(registry, "has_push_access", lambda *a, **k: False)

    hive.onboard("github/stablyai/orca", kind="external", yes=True)

    assert target.exists()
    e = _entry()
    assert e is not None
    assert str(e["kind"]) == "external"
    assert str(e["upstream"]) == "stablyai/orca"
    assert str(e["contribution"]) == "pull"
    assert registry.furnish_of(e) == "none"  # never furnished
    assert str(e["prefix"]) == "fork-orca"
    assert synced == [True]

    # Zero-footprint: .beads/ stays stealth-excluded, nothing committed on top of the clone.
    assert ".beads/" in (target / ".git" / "info" / "exclude").read_text()
    assert git("rev-list", "--count", "HEAD", cwd=target).stdout.strip() == "1"


def test_onboard_external_without_yes_refuses_before_register(world, synced, monkeypatch):
    target = world.ws_root / "github" / "stablyai" / "orca"
    monkeypatch.setattr(hive, "run", _fake_gh_fork_clone(target))
    monkeypatch.setattr(registry, "has_push_access", lambda *a, **k: False)

    with pytest.raises(typer.Exit):
        hive.onboard("github/stablyai/orca", kind="external")  # no --yes

    # The clone/fork already ran (it's the preflight acquire step), but registration refused.
    assert target.exists()
    assert _entry() is None
    assert synced == []
