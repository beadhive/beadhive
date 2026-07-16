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


def _ctx(world, target, *, org="acme", repo="widget", do_hub_sync=True, **kw):
    ctx = onboard.Ctx(
        hive=f"github/{org}/{repo}",
        target=str(target),
        provider="github",
        org=org,
        repo=repo,
        cwd=str(target),
        cfg=config.load(),
        do_hub_sync=do_hub_sync,
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
        "resolve", "identity", "classify", "prefix", "worktree-clean",
        "bd-init", "register", "hub-sync", "footprint",
    }
    order = plan.steps_run.index
    # The DAG edges: resolve first; bd-init after both prefix and worktree-clean; register
    # after bd-init; hub-sync after register; footprint last (captures hub-sync's jsonl export).
    assert order("resolve") == 0
    assert order("bd-init") > order("prefix")
    assert order("bd-init") > order("worktree-clean")
    assert order("register") > order("bd-init")
    assert order("hub-sync") > order("register")
    assert plan.steps_run[-1] == "footprint"
    assert plan.registered is True
    assert plan.hub_synced is True
    assert synced == [True]
    assert registry.find_entry(config.load(), "github", "acme", "widget") is not None


def test_hub_sync_skipped_for_plain_init(world, synced, monkeypatch):
    target = _make_repo(world)
    monkeypatch.setattr(registry, "classify", lambda *a, **k: "personal-or-prototype")
    ctx = _ctx(world, target, do_hub_sync=False)

    plan = onboard.run_onboard(ctx)

    assert "hub-sync" not in plan.steps_run
    assert plan.hub_synced is False
    assert synced == []


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

    ctx = _ctx(world, target, org="acme", repo="gadget",
               clone_url="git@example.com:acme/gadget.git")
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
    assert "Beads fork protection" not in text     # stray marker comment gone
    assert ".beads/" not in text
    assert "**/RECOVERY*.md" not in text
    assert "**/SESSION*.md" not in text
    assert ".ws/" in text                          # host-local entries survive
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
