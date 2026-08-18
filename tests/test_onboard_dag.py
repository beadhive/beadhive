"""Concrete onboard DAG (bead) — steps + per-step preflight checks.

Drives ``onboard.build_steps`` + ``onboard.run_onboard`` against real temp git repos under
$GIT_WORKSPACE (the ``world`` harness), asserting the step ordering and — the point of the
gate — that the dirty-tree / on-default-branch checks fire during Phase A, before bd-init.
Hermetic: ``registry.classify`` is stubbed, ``hub.sync`` is recorded, and ``.beads/`` is
pre-created so bd-init skips the real ``bd`` binary.
"""

from __future__ import annotations

import pytest
import typer

from beadhive import config, hub, onboard, registry
from harness.world import git


@pytest.fixture
def synced(monkeypatch):
    calls = []
    monkeypatch.setattr(hub, "sync", lambda: calls.append(True))
    return calls


def _make_repo(world, *, org="acme", repo="widget", branch="main", with_beads=True):
    target = world.ws_root / "github" / org / repo
    target.mkdir(parents=True)
    git("init", "-q", "-b", branch, cwd=target)
    git("config", "user.email", "t@ws.dev", cwd=target)
    git("config", "user.name", "T", cwd=target)
    (target / "README.md").write_text("hi")
    git("add", ".", cwd=target)
    git("commit", "-q", "-m", "init", cwd=target)
    if with_beads:
        (target / ".beads").mkdir()
    return target


def _ctx(world, target, *, org="acme", repo="widget", hub_sync=True, **kw):
    ctx = onboard.Ctx(
        hive=f"github/{org}/{repo}",
        target=str(target),
        provider="github",
        org=org,
        repo=repo,
        cwd=str(target),
        cfg=config.load(),
        hub_sync=hub_sync,
        **kw,
    )
    ctx.steps = onboard.build_steps(ctx)
    return ctx


# ---------------------------------------------------------------------------
# Step ordering
# ---------------------------------------------------------------------------


def test_existing_clean_folder_runs_full_dag_in_order(world, synced, monkeypatch):
    target = _make_repo(world)
    monkeypatch.setattr(registry, "classify", lambda *a, **k: "personal-or-prototype")
    ctx = _ctx(world, target)

    plan = onboard.run_onboard(ctx)

    # No clone (folder exists); every non-clone step runs in a valid topological order.
    assert "clone" not in plan.steps_run
    assert set(plan.steps_run) == {
        "resolve",
        "identity",
        "classify",
        "prefix",
        "worktree-clean",
        "bd-init",
        "register",
        "node-id",
        "beads-role",
        "hq-parent",
        "hub-sync",
        "footprint",
    }
    order = plan.steps_run.index
    # The DAG edges: resolve first; bd-init after both prefix and worktree-clean; register
    # after bd-init; node-id/beads-role after register (bh-y85rj/bh-f3blt); hub-sync after
    # register; footprint last (captures hub-sync's jsonl export). No prepush-hook step since
    # bh-smcj — see the zero-footprint test below.
    assert order("resolve") == 0
    assert order("bd-init") > order("prefix")
    assert order("bd-init") > order("worktree-clean")
    assert order("register") > order("bd-init")
    assert order("node-id") > order("register")
    assert order("beads-role") > order("register")
    assert order("hub-sync") > order("register")
    assert plan.steps_run[-1] == "footprint"
    assert plan.registered is True
    assert plan.hub_synced is True
    assert synced == [True]
    assert registry.find_entry(config.load(), "github", "acme", "widget") is not None


def test_onboard_warns_fenced_when_no_hq_registered(world, synced, monkeypatch, capsys):
    """No kind=hq hive → the hq-parent step records a fenced warning pointing at `bh hq init`
    (non-fatal: onboarding completes, exit stays 0, nothing is auto-created) (bh-ufne)."""
    target = _make_repo(world)
    monkeypatch.setattr(registry, "classify", lambda *a, **k: "personal-or-prototype")
    ctx = _ctx(world, target)

    plan = onboard.run_onboard(ctx)  # no typer.Exit — the warning never fails the onboard

    assert "hq-parent" in plan.steps_run
    assert any("hq init" in w for w in plan.warnings)
    assert plan.registered is True and plan.hub_synced is True  # everything else still ran
    # No HQ was auto-created during onboard.
    assert registry.hive_of_kind(config.load(), registry.HQ_KIND) is None
    out = capsys.readouterr()
    assert "hq init" in out.err  # step-time fence
    assert "⚠" in out.out  # summarized in the rendered plan


def test_onboard_no_hq_warning_when_hq_registered(world, synced, monkeypatch):
    """With a registered HQ the hq-parent step is silent — no warning recorded."""
    cfg = config.load()
    cfg.setdefault("managed_repos", []).append(
        {
            "provider": "local",
            "org": "factory",
            "repo": "hq",
            "prefix": "hq",
            "kind": "hq",
        }
    )
    config.save(cfg)
    target = _make_repo(world)
    monkeypatch.setattr(registry, "classify", lambda *a, **k: "personal-or-prototype")
    ctx = _ctx(world, target)

    plan = onboard.run_onboard(ctx)

    assert "hq-parent" in plan.steps_run
    assert plan.warnings == []


def test_hub_sync_skipped_for_plain_init(world, synced, monkeypatch):
    target = _make_repo(world)
    monkeypatch.setattr(registry, "classify", lambda *a, **k: "personal-or-prototype")
    ctx = _ctx(world, target, hub_sync=False)

    plan = onboard.run_onboard(ctx)

    assert "hub-sync" not in plan.steps_run
    assert plan.hub_synced is False
    assert synced == []


def test_hub_sync_defaults_to_deferred_single_hive_sync(world, monkeypatch):
    """bh-d5jhc.1: unset ``hub_sync`` (the CLI default, no ``--hub-sync``/``--no-hub-sync``)
    keeps the TRIGGERING hive's own export/add synchronous (``hub.sync_one``) but moves the
    fleet-wide aggregation walk off the interactive path (``hub.sync_background``) instead of
    blocking on the full ``hub.sync()`` — the fix this bead exists for."""
    calls = []
    monkeypatch.setattr(
        hub, "sync_one", lambda prefix, src: (calls.append(("one", prefix, str(src))), True)[-1]
    )
    monkeypatch.setattr(hub, "sync_background", lambda cfg=None: calls.append(("background",)))
    monkeypatch.setattr(
        hub, "sync", lambda: (_ for _ in ()).throw(AssertionError("full sync must not block"))
    )
    target = _make_repo(world)
    monkeypatch.setattr(registry, "classify", lambda *a, **k: "personal-or-prototype")
    ctx = _ctx(world, target, hub_sync=None)

    plan = onboard.run_onboard(ctx)

    assert "hub-sync" in plan.steps_run
    assert plan.hub_synced is True
    assert len(calls) == 2
    assert calls[0][0] == "one" and calls[0][2] == str(target)
    assert calls[1] == ("background",)


def test_hub_sync_explicit_flag_waits_for_full_sync(world, synced, monkeypatch):
    """Explicit ``--hub-sync`` (``ctx.hub_sync is True``) opts back into the pre-bh-d5jhc.1
    behavior: wait for the full fleet-wide ``hub.sync()`` synchronously."""
    target = _make_repo(world)
    monkeypatch.setattr(registry, "classify", lambda *a, **k: "personal-or-prototype")
    ctx = _ctx(world, target, hub_sync=True)

    plan = onboard.run_onboard(ctx)

    assert "hub-sync" in plan.steps_run
    assert plan.hub_synced is True
    assert synced == [True]


def test_installers_gated_by_flags_and_recorded(world, synced, monkeypatch):
    target = _make_repo(world)
    monkeypatch.setattr(registry, "classify", lambda *a, **k: "personal-or-prototype")
    monkeypatch.setattr(registry, "has_push_access", lambda *a, **k: True)
    ctx = _ctx(world, target, agents=True)

    plan = onboard.run_onboard(ctx)

    assert (target / "AGENTS.md").exists()
    assert plan.installers_run == ["agents"]
    # Un-flagged installers never run.
    assert "claude" not in plan.steps_run
    assert "skills" not in plan.steps_run
    assert "codex" not in plan.steps_run


def test_codex_installer_gated_by_flag_and_recorded(world, synced, monkeypatch):
    """bh-odulu: `--codex` writes only the git-excluded sandbox grant — no tracked furniture,
    unlike --claude/--agents/--skills/--opencode — so it must not imply --furnish."""
    target = _make_repo(world)
    monkeypatch.setattr(registry, "classify", lambda *a, **k: "personal-or-prototype")
    monkeypatch.setattr(registry, "has_push_access", lambda *a, **k: True)
    ctx = _ctx(world, target, codex=True)
    ctx.cfg["worktrees"] = {"ephemeral": False, "path": str(world.tmp / "wts")}

    plan = onboard.run_onboard(ctx)

    assert plan.installers_run == ["codex"]
    assert (target / ".codex" / "config.toml").exists()
    assert not ctx.furnish  # codex alone stays zero-footprint


# ---------------------------------------------------------------------------
# The preflight gate — dirty-tree / on-default-branch fire before bd-init
# ---------------------------------------------------------------------------


def test_dirty_tree_gate_fires_before_bd_init(world, synced, monkeypatch):
    target = _make_repo(world)
    (target / "uncommitted.txt").write_text("wip")  # make the tree dirty
    monkeypatch.setattr(registry, "classify", lambda *a, **k: "personal-or-prototype")
    ctx = _ctx(world, target)

    with pytest.raises(typer.Exit):
        onboard.run_onboard(ctx)

    # Gate fired in Phase A: nothing mutated, hub never synced, not registered.
    assert synced == []
    assert registry.find_entry(config.load(), "github", "acme", "widget") is None
    dirty = next(c for c in ctx.plan.checks if c.id == "dirty-tree")
    assert dirty.ok is False


def test_non_default_branch_gate_fires_before_bd_init(world, synced, monkeypatch):
    target = _make_repo(world, branch="main")
    git("checkout", "-q", "-b", "feature", cwd=target)  # off the default branch
    monkeypatch.setattr(registry, "classify", lambda *a, **k: "personal-or-prototype")
    ctx = _ctx(world, target)

    with pytest.raises(typer.Exit):
        onboard.run_onboard(ctx)

    branch = next(c for c in ctx.plan.checks if c.id == "on-default-branch")
    assert branch.ok is False
    assert synced == []


def test_skip_check_downgrades_dirty_and_branch_and_proceeds(world, synced, monkeypatch):
    target = _make_repo(world, branch="main")
    git("checkout", "-q", "-b", "feature", cwd=target)
    (target / "wip.txt").write_text("wip")
    monkeypatch.setattr(registry, "classify", lambda *a, **k: "personal-or-prototype")
    ctx = _ctx(world, target)

    plan = onboard.run_onboard(ctx, skip_checks=["dirty-tree", "on-default-branch"])

    # Downgraded to warnings → onboarding proceeds through bd-init/register/hub-sync.
    assert set(plan.skipped_checks) == {"dirty-tree", "on-default-branch"}
    assert plan.registered is True
    assert synced == [True]


def test_dry_run_lists_checks_and_mutates_nothing(world, synced, monkeypatch):
    target = _make_repo(world)
    monkeypatch.setattr(registry, "classify", lambda *a, **k: "personal-or-prototype")
    monkeypatch.setattr(registry, "has_push_access", lambda *a, **k: True)
    ctx = _ctx(world, target, agents=True)

    plan = onboard.run_onboard(ctx, dry_run=True)

    # Every applicable check id is recorded (discoverable), and nothing mutated.
    ids = {c.id for c in plan.checks}
    assert {"valid-triplet", "prefix-policy", "dirty-tree", "on-default-branch"} <= ids
    assert plan.registered is False
    assert plan.hub_synced is False
    assert not (target / "AGENTS.md").exists()
    assert synced == []
    assert registry.find_entry(config.load(), "github", "acme", "widget") is None


# ---------------------------------------------------------------------------
# Fresh clone marks the dirty/branch checks N/A
# ---------------------------------------------------------------------------


def test_fresh_clone_marks_worktree_checks_na(world, synced, monkeypatch):
    import types

    target = world.ws_root / "github" / "acme" / "gadget"
    assert not target.exists()
    monkeypatch.setattr(registry, "classify", lambda *a, **k: "personal-or-prototype")

    from beadhive import hive
    from beadhive.run import run as real_run

    def fake_run(cmd, **kw):
        if cmd[:2] == ["git", "clone"]:
            dest = cmd[3]
            target.mkdir(parents=True, exist_ok=True)
            git("init", "-q", "-b", "main", cwd=dest)
            (target / ".beads").mkdir()
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        return real_run(cmd, **kw)  # scaffold-step git calls run for real

    monkeypatch.setattr(hive, "run", fake_run)

    ctx = _ctx(
        world, target, org="acme", repo="gadget", clone_url="git@example.com:acme/gadget.git"
    )
    plan = onboard.run_onboard(ctx)

    assert plan.cloned is True
    # dirty-tree / on-default-branch never evaluated (applies=False post-clone).
    ids = {c.id for c in plan.checks}
    assert "dirty-tree" not in ids
    assert "on-default-branch" not in ids
    assert plan.registered is True
    assert synced == [True]


# ---------------------------------------------------------------------------
# The footprint step — declared footprint (zero by default, furnished on opt-in)
# ---------------------------------------------------------------------------


_STEALTH_BLOCK = "\n# Beads stealth mode (added by bd init --stealth)\n.beads/\n"


def _stealth_diverge(target):
    """Reproduce the post-onboard divergence: stealth-excluded .beads/ + untracked artifacts."""
    exclude = target / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    with exclude.open("a") as fh:
        fh.write(_STEALTH_BLOCK)
    (target / ".beads" / "config.yaml").write_text("prefix: widget\n")
    (target / ".claude").mkdir()
    (target / ".claude" / "settings.json").write_text("{}\n")
    (target / "CLAUDE.md").write_text("# hints\n")


def test_default_onboard_is_zero_footprint(world, synced, monkeypatch):
    """The default (no declaration): nothing tracked, nothing committed, .beads/ excluded,
    registry records furnish: none."""
    target = _make_repo(world)
    monkeypatch.setattr(registry, "classify", lambda *a, **k: "personal-or-prototype")

    plan = onboard.run_onboard(_ctx(world, target))

    assert git("rev-list", "--count", "HEAD", cwd=target).stdout.strip() == "1"
    assert git("log", "-1", "--format=%s", cwd=target).stdout.strip() == "init"
    assert git("status", "--porcelain", cwd=target).stdout.strip() == ""
    assert ".beads/" in (target / ".git" / "info" / "exclude").read_text()
    entry = registry.find_entry(config.load(), "github", "acme", "widget")
    assert registry.furnish_of(entry) == "none"
    assert plan.steps_run[-1] == "footprint"


def test_onboard_installs_no_git_hook_at_all(world, synced, monkeypatch):
    """Inverts bh-ytbb.12's original invariant, per bh-smcj. Onboard used to furnish the
    pre-push fence for every hive; it no longer installs ANY hook file, because doing so as a
    side effect is what docs/design/hooks-as-functionality-adr.md forbids — it fights whatever
    dispatcher the repo actually uses and loses SILENTLY (a foreign pre-push makes
    `_write_hook` return "skipped (custom hook present)", which nobody reads).

    Safe because the fence was never the enforcement: the atomic --force-with-lease epoch
    fence (host_fence.py) rejects a stale-epoch push regardless of hooks and regardless of
    --no-verify. Operators who want the early refusal run `bh hive hook install`."""
    target = _make_repo(world)
    monkeypatch.setattr(registry, "classify", lambda *a, **k: "personal-or-prototype")
    ctx = _ctx(world, target, furnish=False)

    plan = onboard.run_onboard(ctx)

    entry = registry.find_entry(config.load(), "github", "acme", "widget")
    assert registry.furnish_of(entry) == "none"  # confirms this really is the zero-footprint path
    assert not (target / ".git" / "hooks" / "pre-push").exists()
    assert not [s for s in plan.steps_run if "prepush" in s]
    # And onboard still leaves the worktree clean, same as before.
    assert git("status", "--porcelain", cwd=target).stdout.strip() == ""


def test_furnish_unstealths_and_commits_leaving_clean_tree(world, synced, monkeypatch):
    target = _make_repo(world)
    _stealth_diverge(target)
    monkeypatch.setattr(registry, "classify", lambda *a, **k: "personal-or-prototype")
    monkeypatch.setattr(registry, "has_push_access", lambda *a, **k: True)
    ctx = _ctx(world, target, furnish=True)

    plan = onboard.run_onboard(ctx)

    # The stealth exclusion is gone (other exclude lines untouched) …
    assert ".beads/" not in (target / ".git" / "info" / "exclude").read_text()
    # … the scaffolding is committed with the conventional subject …
    subject = git("log", "-1", "--format=%s", cwd=target).stdout.strip()
    assert subject == "chore(agf): hive scaffolding (beads + agent config)"
    tracked = git("ls-files", cwd=target).stdout
    assert ".beads/config.yaml" in tracked
    assert ".claude/settings.json" in tracked
    assert "CLAUDE.md" in tracked
    # … and a green onboard ends with a CLEAN working tree (the survey-row acceptance).
    assert git("status", "--porcelain", cwd=target).stdout.strip() == ""
    assert plan.steps_run[-1] == "footprint"
    entry = registry.find_entry(config.load(), "github", "acme", "widget")
    assert registry.furnish_of(entry) == "full"


def test_furnish_is_sticky_and_rerun_does_not_duplicate_commits(world, synced, monkeypatch):
    """Re-onboard of a furnished hive keeps the declaration (registry-sticky) and a no-change
    re-run creates no new commit."""
    target = _make_repo(world)
    _stealth_diverge(target)
    monkeypatch.setattr(registry, "classify", lambda *a, **k: "personal-or-prototype")
    monkeypatch.setattr(registry, "has_push_access", lambda *a, **k: True)

    onboard.run_onboard(_ctx(world, target, furnish=True))
    count_after_furnish = git("rev-list", "--count", "HEAD", cwd=target).stdout.strip()
    onboard.run_onboard(_ctx(world, target))  # no flags: sticky from registry

    assert git("rev-list", "--count", "HEAD", cwd=target).stdout.strip() == count_after_furnish
    entry = registry.find_entry(config.load(), "github", "acme", "widget")
    assert registry.furnish_of(entry) == "full"


def test_furnish_rerun_amends_unpushed_scaffold_commit(world, synced, monkeypatch):
    """New scaffolding after an unpushed scaffold commit amends it — no duplicate
    identically-titled commits (the fleet-onboarding bug)."""
    target = _make_repo(world)
    _stealth_diverge(target)
    monkeypatch.setattr(registry, "classify", lambda *a, **k: "personal-or-prototype")
    monkeypatch.setattr(registry, "has_push_access", lambda *a, **k: True)

    onboard.run_onboard(_ctx(world, target, furnish=True))
    count = git("rev-list", "--count", "HEAD", cwd=target).stdout.strip()
    (target / "AGENTS.md").write_text("# late furniture\n")  # new scaffolding, HEAD unpushed
    onboard.run_onboard(_ctx(world, target, furnish=True))

    assert git("rev-list", "--count", "HEAD", cwd=target).stdout.strip() == count  # amended
    assert "AGENTS.md" in git("ls-files", cwd=target).stdout
    subject = git("log", "-1", "--format=%s", cwd=target).stdout.strip()
    assert subject == "chore(agf): hive scaffolding (beads + agent config)"


def test_furnish_rerun_after_push_uses_repair_subject(world, synced, monkeypatch):
    """Once the scaffold commit is on a remote, a repair pass commits under the distinct
    repair subject instead of duplicating the original message."""
    target = _make_repo(world)
    _stealth_diverge(target)
    monkeypatch.setattr(registry, "classify", lambda *a, **k: "personal-or-prototype")
    monkeypatch.setattr(registry, "has_push_access", lambda *a, **k: True)

    onboard.run_onboard(_ctx(world, target, furnish=True))
    remote = world.ws_root / "remote.git"
    git("init", "-q", "--bare", str(remote), cwd=world.ws_root)
    git("remote", "add", "origin", str(remote), cwd=target)
    git("push", "-q", "-u", "origin", "main", cwd=target)
    (target / "AGENTS.md").write_text("# late furniture\n")
    onboard.run_onboard(_ctx(world, target, furnish=True))

    subject = git("log", "-1", "--format=%s", cwd=target).stdout.strip()
    assert subject == "chore(agf): hive scaffolding repair"


def test_explicit_furnish_refused_without_push_access(world, synced, monkeypatch):
    target = _make_repo(world)
    monkeypatch.setattr(registry, "classify", lambda *a, **k: "personal-or-prototype")
    monkeypatch.setattr(registry, "has_push_access", lambda *a, **k: False)
    ctx = _ctx(world, target, furnish=True)

    with pytest.raises(typer.Exit):
        onboard.run_onboard(ctx)

    failed = next(c for c in ctx.plan.checks if c.id == "furnish-needs-ownership")
    assert failed.ok is False
    assert registry.find_entry(config.load(), "github", "acme", "widget") is None


def test_scaffold_preserves_host_local_excludes(world, synced, monkeypatch):
    target = _make_repo(world)
    _stealth_diverge(target)
    exclude = target / ".git" / "info" / "exclude"
    with exclude.open("a") as fh:
        fh.write(".claude/settings.local.json\n.ws/\n")
    monkeypatch.setattr(registry, "classify", lambda *a, **k: "personal-or-prototype")
    monkeypatch.setattr(registry, "has_push_access", lambda *a, **k: True)

    onboard.run_onboard(_ctx(world, target, furnish=True))

    text = exclude.read_text()
    assert ".claude/settings.local.json" in text  # host-local entries survive
    assert ".ws/" in text
    assert ".beads/" not in text


def test_scaffold_skips_forks_keeping_stealth(world, synced, monkeypatch):
    target = _make_repo(world)
    _stealth_diverge(target)
    monkeypatch.setattr(registry, "classify", lambda *a, **k: "fork upstream=github/up/widget")
    ctx = _ctx(world, target, yes=True)  # forks need --yes to onboard at all

    onboard.run_onboard(ctx)

    # Fork convention: .beads/ stays stealth-excluded, nothing hive-side is committed.
    assert ".beads/" in (target / ".git" / "info" / "exclude").read_text()
    subject = git("log", "-1", "--format=%s", cwd=target).stdout.strip()
    assert subject == "init"


def test_explicit_furnish_refused_on_fork(world, synced, monkeypatch):
    """External hives are never furnished — an explicit --furnish is refused outright
    (non-overridable), not silently downgraded."""
    target = _make_repo(world)
    monkeypatch.setattr(registry, "classify", lambda *a, **k: "fork upstream=github/up/widget")
    monkeypatch.setattr(registry, "has_push_access", lambda *a, **k: True)
    ctx = _ctx(world, target, yes=True, furnish=True)

    with pytest.raises(typer.Exit):
        onboard.run_onboard(ctx)

    failed = next(c for c in ctx.plan.checks if c.id == "external-no-furnish")
    assert failed.ok is False
    assert git("rev-list", "--count", "HEAD", cwd=target).stdout.strip() == "1"


def _add_fork_remotes(target, *, upstream="stablyai/widget", origin="acme/widget"):
    """Give `target` a fork's remote shape: origin + a distinct upstream (bh-4k3w/bh-djx2)."""
    git("remote", "add", "origin", f"git@github.com:{origin}.git", cwd=target)
    git("remote", "add", "upstream", f"git@github.com:{upstream}.git", cwd=target)


def test_fork_needs_yes_fires_on_distinct_upstream_remote(world, synced, monkeypatch):
    """The guard must fire on a real fork even when classify misses it — an `upstream` remote that
    differs from `origin` is an independent, gh-free fork signal (bh-4k3w)."""
    target = _make_repo(world)
    _add_fork_remotes(target)
    monkeypatch.setattr(registry, "classify", lambda *a, **k: "personal-or-prototype")
    ctx = _ctx(world, target)  # no --yes

    ok, msg = onboard._chk_fork_needs_yes(ctx)

    assert ok is False
    assert "fork" in msg and "stablyai/widget" in msg


def test_scaffold_skips_repo_with_distinct_upstream_remote(world, synced, monkeypatch):
    """A repo with an external upstream is a fork regardless of classified kind — footprint
    must leave .beads/ stealth-excluded and land no commit on its default branch (bh-djx2)."""
    target = _make_repo(world)
    _add_fork_remotes(target)
    _stealth_diverge(target)  # residue a furnished repair would otherwise un-stealth + commit
    monkeypatch.setattr(registry, "classify", lambda *a, **k: "personal-or-prototype")
    ctx = _ctx(world, target, yes=True)

    onboard.run_onboard(ctx)

    assert ".beads/" in (target / ".git" / "info" / "exclude").read_text()
    # No scaffold commit ahead of the fork's default branch tip.
    assert git("rev-list", "--count", "HEAD", cwd=target).stdout.strip() == "1"
    assert git("log", "-1", "--format=%s", cwd=target).stdout.strip() == "init"


def test_dirty_tree_discounts_hive_state_residue(world, synced, monkeypatch):
    # A prior diverged onboard's residue (untracked .claude/settings.json + CLAUDE.md) must not
    # block a repair re-run — dirty-tree fires only on genuine (non-hive-state) dirt.
    target = _make_repo(world)
    _stealth_diverge(target)
    monkeypatch.setattr(registry, "classify", lambda *a, **k: "personal-or-prototype")
    monkeypatch.setattr(registry, "has_push_access", lambda *a, **k: True)

    plan = onboard.run_onboard(_ctx(world, target, furnish=True))  # no --skip-check needed

    dirty = next(c for c in plan.checks if c.id == "dirty-tree")
    assert dirty.ok is True
    assert git("status", "--porcelain", cwd=target).stdout.strip() == ""


# ---------------------------------------------------------------------------
# bh-2w8d — un-stealth must strip bd's fork-protection block across bd versions
# ---------------------------------------------------------------------------

# The exact block bd ≥1.1.0 writes into .git/info/exclude on a fork-shaped repo (verified against
# bd 1.1.0): renamed marker comment + .beads/ + the RECOVERY/SESSION patterns bd ≤1.0.5 did not add.
_BD_1_1_FORK_BLOCK = (
    "\n# Beads fork protection (bd init)\n.beads/\n**/RECOVERY*.md\n**/SESSION*.md\n"
)


def _exclude(target):
    ex = target / ".git" / "info" / "exclude"
    ex.parent.mkdir(parents=True, exist_ok=True)
    return ex


def test_remove_stealth_strips_whole_bd_1_1_fork_block(world):
    """bd ≥1.1.0's `# Beads fork protection` block — marker comment AND every pattern
    (.beads/, **/RECOVERY*.md, **/SESSION*.md) — is fully removed, host-local lines kept."""
    from beadhive import hive

    target = _make_repo(world)
    ex = _exclude(target)
    ex.write_text(".ws/\n.claude/settings.local.json\n" + _BD_1_1_FORK_BLOCK)

    changed = hive._remove_stealth_exclude(target)

    text = ex.read_text()
    assert changed is True
    assert "Beads fork protection" not in text  # stray marker comment gone
    assert ".beads/" not in text
    assert "**/RECOVERY*.md" not in text
    assert "**/SESSION*.md" not in text
    assert ".ws/" in text  # host-local entries survive
    assert ".claude/settings.local.json" in text


def test_remove_stealth_still_strips_legacy_bd_1_0_5_block(world):
    """The bd ≤1.0.5 `# Beads stealth mode` + `.beads/` shape is still removed (no regression)."""
    from beadhive import hive

    target = _make_repo(world)
    ex = _exclude(target)
    ex.write_text(".ws/\n# Beads stealth mode (added by bd init --stealth)\n.beads/\n")

    changed = hive._remove_stealth_exclude(target)

    text = ex.read_text()
    assert changed is True
    assert "Beads stealth mode" not in text
    assert ".beads/" not in text
    assert ".ws/" in text


# ---------------------------------------------------------------------------
# _act_node_id / _act_beads_role (bh-y85rj / bh-f3blt)
# ---------------------------------------------------------------------------


class _FakeKV:
    """Fakes `bd config get/set <key>` for one in-memory key — same shape the hive_repair
    tests use for the identical two-verb contract."""

    def __init__(self, key, initial=""):
        self.key = key
        self.value = initial
        self.set_calls = 0

    def fake_json(self, args, cwd, **kw):
        assert args == ["config", "get", self.key]
        return {"key": self.key, "value": self.value}

    def fake_run(self, args, cwd, actor="", capture=False, text_input=None, **kw):
        assert args[:2] == ["config", "set"] and args[2] == self.key
        self.value = args[3]
        self.set_calls += 1
        from types import SimpleNamespace

        return SimpleNamespace(returncode=0, stdout="", stderr="")


def test_act_node_id_sets_from_host_id_when_absent(world, monkeypatch):
    from beadhive import bd as bd_mod
    from beadhive import host

    fake = _FakeKV("node_id", "")
    monkeypatch.setattr(bd_mod, "json", fake.fake_json)
    monkeypatch.setattr(bd_mod, "run", fake.fake_run)
    monkeypatch.setattr(host, "host_id", lambda: "host-xyz")
    ctx = onboard.Ctx(hive="github/acme/widget", target="", cwd=str(world.ws_root))

    onboard._act_node_id(ctx)

    assert fake.value == "host-xyz"
    assert fake.set_calls == 1


def test_act_node_id_never_overwrites_an_existing_value(world, monkeypatch):
    from beadhive import bd as bd_mod
    from beadhive import host

    fake = _FakeKV("node_id", "already-set")
    monkeypatch.setattr(bd_mod, "json", fake.fake_json)
    monkeypatch.setattr(bd_mod, "run", fake.fake_run)
    monkeypatch.setattr(host, "host_id", lambda: "host-xyz")
    ctx = onboard.Ctx(hive="github/acme/widget", target="", cwd=str(world.ws_root))

    onboard._act_node_id(ctx)

    assert fake.value == "already-set"
    assert fake.set_calls == 0


def test_act_node_id_skips_gracefully_when_host_identity_unminted(world, monkeypatch):
    """`host.host_id()` raises `FileNotFoundError` when `bh config init` never ran (host.yaml
    absent) — must never fail onboarding itself over this."""
    from beadhive import bd as bd_mod
    from beadhive import host

    fake = _FakeKV("node_id", "")
    monkeypatch.setattr(bd_mod, "json", fake.fake_json)
    monkeypatch.setattr(bd_mod, "run", fake.fake_run)

    def _raise():
        raise FileNotFoundError("host identity not found")

    monkeypatch.setattr(host, "host_id", _raise)
    ctx = onboard.Ctx(hive="github/acme/widget", target="", cwd=str(world.ws_root))

    onboard._act_node_id(ctx)  # must not raise

    assert fake.set_calls == 0


def test_act_beads_role_sets_from_kind_when_absent(world, monkeypatch):
    from beadhive import bd as bd_mod

    fake = _FakeKV("beads.role", "")
    monkeypatch.setattr(bd_mod, "json", fake.fake_json)
    monkeypatch.setattr(bd_mod, "run", fake.fake_run)
    ctx = onboard.Ctx(hive="github/acme/widget", target="", cwd=str(world.ws_root), kind="fork")

    onboard._act_beads_role(ctx)

    assert fake.value == "contributor"
    assert fake.set_calls == 1


def test_act_beads_role_maps_org_native_to_maintainer(world, monkeypatch):
    from beadhive import bd as bd_mod

    fake = _FakeKV("beads.role", "")
    monkeypatch.setattr(bd_mod, "json", fake.fake_json)
    monkeypatch.setattr(bd_mod, "run", fake.fake_run)
    ctx = onboard.Ctx(
        hive="github/acme/widget", target="", cwd=str(world.ws_root), kind="org-native"
    )

    onboard._act_beads_role(ctx)

    assert fake.value == "maintainer"


def test_act_beads_role_reports_mismatch_without_overwriting(world, monkeypatch, capsys):
    from beadhive import bd as bd_mod

    fake = _FakeKV("beads.role", "maintainer")
    monkeypatch.setattr(bd_mod, "json", fake.fake_json)
    monkeypatch.setattr(bd_mod, "run", fake.fake_run)
    ctx = onboard.Ctx(hive="github/acme/widget", target="", cwd=str(world.ws_root), kind="fork")

    onboard._act_beads_role(ctx)

    assert fake.value == "maintainer"  # never silently overwritten
    assert fake.set_calls == 0
    out = capsys.readouterr().out
    assert "disagrees with kind=fork" in out
    assert "hive repair --hive github/acme/widget --role --yes" in out


def test_act_beads_role_noop_when_already_correct(world, monkeypatch):
    from beadhive import bd as bd_mod

    fake = _FakeKV("beads.role", "contributor")
    monkeypatch.setattr(bd_mod, "json", fake.fake_json)
    monkeypatch.setattr(bd_mod, "run", fake.fake_run)
    ctx = onboard.Ctx(hive="github/acme/widget", target="", cwd=str(world.ws_root), kind="fork")

    onboard._act_beads_role(ctx)

    assert fake.value == "contributor"
    assert fake.set_calls == 0


def test_act_beads_role_scopes_to_ctx_base_not_process_cwd(world, monkeypatch):
    """bh-s08me: onboard's own `_act_beads_role` is one of `beads.role`'s call sites too —
    it must pin the child's real cwd to `ctx.base` (the target hive `hive.onboard` threaded
    in, per `hive.py`'s "Threading cwd=target" contract), not let bd fall back to onboard's
    own process cwd. Reuses `RealCwdGitConfig`, which reproduces bd's real `-C`-vs-process-cwd
    split, rather than mocking the return value directly — the same fake `test_hive_repair.py`
    uses for the n>1 regression."""
    from pathlib import Path

    from beadhive import bd as bd_mod
    from test_hive_repair import RealCwdGitConfig

    target = world.ws_root / "github" / "acme" / "widget"
    target.mkdir(parents=True)

    fake = RealCwdGitConfig()
    # The process happens to be sitting somewhere else entirely (pytest's own cwd), with a
    # DIFFERENT value — exactly what an unpinned call would silently borrow instead of
    # ctx.base's own value.
    fake.store[str(Path.cwd())] = "maintainer"
    monkeypatch.setattr(bd_mod, "_run", fake)

    ctx = onboard.Ctx(hive="github/acme/widget", target="", cwd=str(target), kind="fork")
    onboard._act_beads_role(ctx)

    assert fake.store[str(target)] == "contributor"
    assert fake.store[str(Path.cwd())] == "maintainer"  # runner's own cwd untouched
