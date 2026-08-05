"""`bh host provision` — the ordered, idempotent, resumable new-host adoption path (bh-twc8.1).

Covers the acceptance bar directly:
  * each step PROBES before acting, so a re-run against any partial state is safe and reports
    done/skipped/would/failed rather than mutating blindly;
  * `host.yaml` is never clobbered/reminted (config.scaffold_home's contract, reused here);
  * `hq.remote` is resolved via the SAME `hq._confirm_remote` prompt `bh hq init`/`clone` use
    (bh-mw97), never a parallel implementation;
  * `--dry-run` never mutates (no writes, no subprocess calls, no prompts);
  * the verifying gate (`status`/`_step_verify`) fails when the host isn't actually usable;
  * the `.beads` 0700 permission fix runs as part of the flow;
  * one step raising never aborts the rest of the pipeline (`provision`'s per-step try/except);
  * the fleet/host config-partition conflict a freshly-templated config.yaml collides into the
    moment a real fleet.yaml lands (`hq clone`) is reconciled automatically, via a rewrite that
    sidesteps a real ruamel round-trip bug (deleting the LAST key of a nested mapping corrupts
    the file) rather than triggering it.

The autouse `_sandbox_bh_home` fixture (tests/conftest.py) isolates `BH_HOME` per test (and
seeds a minimal starter `config.yaml`), so every test below runs against a throwaway home —
never the operator's real `~/.beadhive`. Tests needing `$GIT_WORKSPACE` / real hive dirs use the
`world` fixture, matching test_hive_ready.py's convention.
"""

from __future__ import annotations

import stat

import pytest
import typer
from typer.testing import CliRunner

from beadhive import config, host, host_provision, hosts, registry
from beadhive.cli import app

runner = CliRunner()


class _Res:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# ---- module shape --------------------------------------------------------------


def test_plan_has_one_name_per_step_and_every_glyph_status_is_mapped():
    assert len(host_provision.PLAN) == 10
    assert host_provision.PLAN[-1] == "adopt"
    # bh-1kzc: the gate provision used to require out of band is now its own first step.
    assert host_provision.PLAN[0] == "setup check"
    assert set(host_provision.GLYPH) == {"done", "skipped", "would", "failed"}


# ---- step 1: config init --------------------------------------------------------


def test_config_init_writes_on_a_fresh_home():
    result = host_provision._step_config_init(dry_run=False)

    assert result.status == "done"
    assert host.path().exists()
    assert config.config_path().exists()


def test_config_init_skips_once_already_scaffolded():
    host_provision._step_config_init(dry_run=False)

    result = host_provision._step_config_init(dry_run=False)

    assert result.status == "skipped"


def test_config_init_dry_run_never_writes_host_yaml():
    result = host_provision._step_config_init(dry_run=True)

    assert result.status == "would"
    assert not host.path().exists()  # zero mutation


def test_config_init_never_reminits_an_existing_host_id():
    host_provision._step_config_init(dry_run=False)
    original = host.host_id()

    host_provision._step_config_init(dry_run=False)  # a second, "resumed" run

    assert host.host_id() == original


# ---- step 2: git workspace update -----------------------------------------------


def test_git_workspace_update_skips_when_no_sources_present(world):
    """bh-hsus.4: git-workspace has no `enabled` flag any more (it's a required dep) — the
    only way this step skips (once a config.yaml exists) is no workspace*.toml sources."""
    result = host_provision._step_git_workspace_update(dry_run=False)

    assert result.status == "skipped"
    assert "workspace" in result.detail


def _enable_git_workspace_with_source(world):
    (world.ws_root / "workspace.toml").write_text('[[provider]]\nprovider = "github"\n')


def test_git_workspace_update_dry_run_makes_no_subprocess_call(world, monkeypatch):
    _enable_git_workspace_with_source(world)
    calls = []
    monkeypatch.setattr(host_provision, "run", lambda *a, **k: calls.append(a) or _Res())

    result = host_provision._step_git_workspace_update(dry_run=True)

    assert result.status == "would"
    assert calls == []


def test_git_workspace_update_runs_and_reports_done(world, monkeypatch):
    _enable_git_workspace_with_source(world)

    def fake_run(cmd, **k):
        return _Res(0) if cmd[:2] == ["git", "workspace"] else _Res(1)

    monkeypatch.setattr(host_provision, "run", fake_run)

    result = host_provision._step_git_workspace_update(dry_run=False)

    assert result.status == "done"


def test_git_workspace_update_reports_failed_on_nonzero_exit(world, monkeypatch):
    _enable_git_workspace_with_source(world)
    monkeypatch.setattr(host_provision, "run", lambda *a, **k: _Res(1, "", "boom"))

    result = host_provision._step_git_workspace_update(dry_run=False)

    assert result.status == "failed"
    assert "boom" in result.detail


# ---- step 3: hq.remote -----------------------------------------------------------


def test_hq_remote_skips_when_already_explicitly_set():
    cfg = config.load()
    cfg["hq"] = {"remote": "acme/beadhive-hq"}
    config.save(cfg)

    result = host_provision._step_hq_remote(auto=True, dry_run=False)

    assert result.status == "skipped"
    assert "acme/beadhive-hq" in result.detail


def test_hq_remote_dry_run_never_prompts_or_persists(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not prompt under --dry-run")

    monkeypatch.setattr(host_provision.hq.typer, "prompt", boom)
    monkeypatch.setattr(
        host_provision.config, "hq_remote", lambda cfg=None, cwd=None: "acme/beadhive-hq"
    )

    result = host_provision._step_hq_remote(auto=False, dry_run=True)

    assert result.status == "would"
    assert host_provision.config.hq_cfg(config.load()).get("remote") in (None, "")


def test_hq_remote_auto_resolves_and_persists(monkeypatch):
    monkeypatch.setattr(host_provision.hq, "_confirm_remote", lambda cfg, auto: "acme/beadhive-hq")

    result = host_provision._step_hq_remote(auto=True, dry_run=False)

    assert result.status == "done"
    assert config.hq_cfg(config.load())["remote"] == "acme/beadhive-hq"


def test_hq_remote_failed_when_unresolvable(monkeypatch):
    monkeypatch.setattr(host_provision.hq, "_confirm_remote", lambda cfg, auto: "")

    result = host_provision._step_hq_remote(auto=True, dry_run=False)

    assert result.status == "failed"


# ---- reconciliation: the fleet/host config-partition collision -----------------


def test_delete_leaf_pruning_empty_removes_a_wholly_emptied_section():
    node = {"worktrees": {"a": 1, "b": 2}, "other": {"x": 1}}

    config._delete_leaf_pruning_empty(node, "worktrees.a")
    assert node == {"worktrees": {"b": 2}, "other": {"x": 1}}  # sibling survives, section stays

    config._delete_leaf_pruning_empty(node, "worktrees.b")
    assert node == {"other": {"x": 1}}  # last child gone -> the whole section is pruned


def test_delete_leaf_pruning_empty_prunes_multiple_ancestor_levels():
    node = {"dimensions": {"component": {"description": "x"}, "size": {"values": []}}}

    config._delete_leaf_pruning_empty(node, "dimensions.component.description")

    # "component" had exactly one leaf -> it AND then (were dimensions now empty) dimensions
    # itself would prune; "size" survives so only "component" disappears here.
    assert node == {"dimensions": {"size": {"values": []}}}


def _write_conflicting_host_config():
    """A host config carrying a mix of FLEET-classified keys (some of which fully occupy a
    nested section, so pruning it away is exercised too) and a HOST-classified key that must
    survive reconciliation untouched."""
    config.save(
        {
            "schema_version": 1,
            "providers": ["github"],
            "exclude": {"orgs": [], "repos": []},  # both children FLEET -> the whole section prunes
            "otel": {"enabled": False},  # HOST-classified -> must survive
        }
    )


def test_reconcile_after_clone_drops_fleet_keys_and_config_loads_cleanly():
    _write_conflicting_host_config()
    config.hq_dir().mkdir(parents=True, exist_ok=True)
    config.fleet_path().write_text('schema_version: 1\ndelimiter: ":"\nmanaged_repos: []\n')

    dropped = host_provision._reconcile_host_config_after_clone()

    assert set(dropped) >= {"schema_version", "providers", "exclude.orgs", "exclude.repos"}
    cfg = config.load()  # must not raise ConfigError anymore
    assert cfg["schema_version"] == 1  # now sourced from fleet.yaml, not the stale host copy
    host_cfg = config.load_host()
    assert "exclude" not in host_cfg  # the whole now-empty section was pruned
    assert host_cfg["otel"] == {"enabled": False}  # host-classified — never touched


def test_reconcile_after_clone_is_a_noop_without_a_fleet_yaml():
    _write_conflicting_host_config()  # would conflict IF a fleet.yaml existed — none does yet

    dropped = host_provision._reconcile_host_config_after_clone()

    assert dropped == []
    assert config.load_host()["schema_version"] == 1  # left completely untouched


def test_reconcile_after_clone_is_a_noop_when_config_already_loads_cleanly(monkeypatch):
    """A host config that does NOT conflict with the real fleet.yaml (e.g. one that never set
    any fleet-shaped key at all) must be left byte-for-byte alone — reconciliation is gated on
    `config.load()` actually raising, never a speculative rewrite."""
    config.save({"otel": {"enabled": False}})
    config.hq_dir().mkdir(parents=True, exist_ok=True)
    config.fleet_path().write_text('schema_version: 1\ndelimiter: ":"\nmanaged_repos: []\n')
    before = config.config_path().read_text()

    dropped = host_provision._reconcile_host_config_after_clone()

    assert dropped == []
    assert config.config_path().read_text() == before


def test_reconcile_after_clone_degrades_when_host_config_is_absent():
    config.config_path().unlink()

    assert host_provision._reconcile_host_config_after_clone() == []


# ---- step 4: hq clone -------------------------------------------------------------


def test_hq_clone_skips_and_reconciles_when_hq_already_present(monkeypatch):
    host_provision._step_config_init(dry_run=False)
    config.hq_dir().mkdir(parents=True, exist_ok=True)
    config.fleet_path().write_text('schema_version: 1\ndelimiter: ":"\nmanaged_repos: []\n')
    called = []
    monkeypatch.setattr(host_provision.hq, "clone", lambda **k: called.append(k))

    result = host_provision._step_hq_clone(dry_run=False)

    assert result.status == "skipped"
    assert called == []  # never re-clones an already-present HQ
    config.load()  # reconciled -> loads cleanly


def test_hq_clone_skips_when_remote_unresolved(monkeypatch):
    # Force an unresolvable derivation regardless of the real machine's `gh` login state —
    # otherwise this would silently attempt a REAL clone on any dev box with `gh` authenticated.
    monkeypatch.setattr(host_provision.config, "hq_remote", lambda cfg=None, cwd=None: "")

    result = host_provision._step_hq_clone(dry_run=False)

    assert result.status == "skipped"
    assert "hq.remote" in result.detail


def test_hq_clone_dry_run_makes_no_clone_call(monkeypatch):
    cfg = config.load()
    cfg["hq"] = {"remote": "acme/beadhive-hq"}
    config.save(cfg)
    called = []
    monkeypatch.setattr(host_provision.hq, "clone", lambda **k: called.append(k))

    result = host_provision._step_hq_clone(dry_run=True)

    assert result.status == "would"
    assert called == []


def test_hq_clone_done_reconciles_after_a_successful_clone(monkeypatch):
    host_provision._step_config_init(dry_run=False)
    cfg = config.load()
    cfg["hq"] = {"remote": "acme/beadhive-hq"}
    config.save(cfg)

    def fake_clone(**kwargs):
        assert kwargs.get("auto") is True  # never re-prompts — the remote is already resolved
        config.hq_dir().mkdir(parents=True, exist_ok=True)
        config.fleet_path().write_text('schema_version: 1\ndelimiter: ":"\nmanaged_repos: []\n')

    monkeypatch.setattr(host_provision.hq, "clone", fake_clone)

    result = host_provision._step_hq_clone(dry_run=False)

    assert result.status == "done"
    assert "reconciled" in result.detail
    config.load()  # loads cleanly post-clone


def test_hq_clone_reports_failed_on_hq_exit(monkeypatch):
    cfg = config.load()
    cfg["hq"] = {"remote": "acme/beadhive-hq"}
    config.save(cfg)

    def fake_clone(**kwargs):
        raise typer.Exit(1)

    monkeypatch.setattr(host_provision.hq, "clone", fake_clone)

    result = host_provision._step_hq_clone(dry_run=False)

    assert result.status == "failed"


# ---- step 5: host init -------------------------------------------------------------


def test_host_init_skips_without_a_host_identity():
    result = host_provision._step_host_init(role="worker", force=False, dry_run=False)

    assert result.status == "skipped"
    assert "identity" in result.detail


def test_host_init_skips_without_a_local_hq():
    host_provision._step_config_init(dry_run=False)

    result = host_provision._step_host_init(role="worker", force=False, dry_run=False)

    assert result.status == "skipped"
    assert "HQ" in result.detail or "hq" in result.detail


def _with_host_and_hq():
    host_provision._step_config_init(dry_run=False)
    config.hq_dir().mkdir(parents=True, exist_ok=True)
    (config.hq_dir() / ".beads").mkdir(parents=True, exist_ok=True)


def test_host_init_writes_a_manifest():
    _with_host_and_hq()

    result = host_provision._step_host_init(role="worker", force=False, dry_run=False)

    assert result.status == "done"
    manifest = hosts.load(config.hq_dir(), host.host_id())
    assert manifest.role == "worker"


def test_host_init_skips_an_existing_manifest_without_force():
    _with_host_and_hq()
    host_provision._step_host_init(role="worker", force=False, dry_run=False)

    result = host_provision._step_host_init(role="primary-default", force=False, dry_run=False)

    assert result.status == "skipped"
    manifest = hosts.load(config.hq_dir(), host.host_id())
    assert manifest.role == "worker"  # untouched — never clobbered


def test_host_init_dry_run_writes_nothing():
    _with_host_and_hq()

    result = host_provision._step_host_init(role="worker", force=False, dry_run=True)

    assert result.status == "would"
    assert not hosts.manifest_path(config.hq_dir(), host.host_id()).exists()


# ---- step 6: bead sync -------------------------------------------------------------


def test_bead_sync_skips_without_a_local_hq():
    result = host_provision._step_bead_sync(dry_run=False)

    assert result.status == "skipped"


def test_bead_sync_skips_when_no_hive_clones_present_on_disk(world):
    config.hq_dir().mkdir(parents=True, exist_ok=True)
    cfg = config.load()
    cfg.setdefault("managed_repos", []).append(
        {"provider": "github", "org": "acme", "repo": "app", "prefix": "app", "kind": "personal"}
    )
    config.save(cfg)  # registered, but never cloned to disk

    result = host_provision._step_bead_sync(dry_run=False)

    assert result.status == "skipped"


def _register_present_hive(world, *, prefix="app"):
    config.hq_dir().mkdir(parents=True, exist_ok=True)
    cfg = config.load()
    entry = {
        "provider": "github",
        "org": "acme",
        "repo": "app",
        "prefix": prefix,
        "kind": "personal",
    }
    cfg.setdefault("managed_repos", []).append(entry)
    config.save(cfg)
    hive_dir = registry.hive_dir(entry)
    (hive_dir / ".beads").mkdir(parents=True, exist_ok=True)
    return hive_dir


def test_bead_sync_dry_run_makes_no_sync_call(world, monkeypatch):
    _register_present_hive(world)
    calls = []
    monkeypatch.setattr(host_provision.hive_sync, "hive_sync", lambda **k: calls.append(k) or [])

    result = host_provision._step_bead_sync(dry_run=True)

    assert result.status == "would"
    assert calls == []


def test_bead_sync_done_when_every_hive_syncs_clean(world, monkeypatch):
    _register_present_hive(world)
    monkeypatch.setattr(host_provision.hive_sync, "hive_sync", lambda **k: [])

    result = host_provision._step_bead_sync(dry_run=False)

    assert result.status == "done"


def test_bead_sync_failed_when_a_hive_is_offending(world, monkeypatch):
    _register_present_hive(world, prefix="app")
    monkeypatch.setattr(host_provision.hive_sync, "hive_sync", lambda **k: ["app"])

    result = host_provision._step_bead_sync(dry_run=False)

    assert result.status == "failed"
    assert "app" in result.detail


# ---- step 7: fix permissions --------------------------------------------------------


def test_fix_permissions_skips_with_no_beads_dirs_present():
    result = host_provision._step_fix_permissions(dry_run=False)

    assert result.status == "skipped"


def test_fix_permissions_chmods_a_wrong_mode_dir():
    beads = config.hq_dir() / ".beads"
    beads.mkdir(parents=True)
    beads.chmod(0o750)

    result = host_provision._step_fix_permissions(dry_run=False)

    assert result.status == "done"
    assert stat.S_IMODE(beads.stat().st_mode) == 0o700


def test_fix_permissions_skips_when_already_0700():
    beads = config.hq_dir() / ".beads"
    beads.mkdir(parents=True)
    beads.chmod(0o700)

    result = host_provision._step_fix_permissions(dry_run=False)

    assert result.status == "skipped"


def test_fix_permissions_dry_run_never_chmods():
    beads = config.hq_dir() / ".beads"
    beads.mkdir(parents=True)
    beads.chmod(0o750)

    result = host_provision._step_fix_permissions(dry_run=True)

    assert result.status == "would"
    assert stat.S_IMODE(beads.stat().st_mode) == 0o750  # untouched


# ---- step 8: verify / status --------------------------------------------------------


def test_status_reports_the_right_checks_failing_on_a_bare_home():
    """No `bh host provision` has run at all yet — only the pieces the autouse fixture's own
    starter config.yaml happens to satisfy (config.yaml exists; it loads cleanly with no
    fleet.yaml around; there are no `.beads` dirs yet to have wrong permissions) are green."""
    checks = host_provision.status()
    by_label = {c.label: c for c in checks}

    assert by_label.keys() == {
        "host identity",
        "config.yaml",
        "config loads cleanly",
        "HQ local store",
        "HQ remote wired",
        "registered in HQ roster",
        ".beads permissions",
    }
    assert not by_label["host identity"].ok
    assert by_label["config.yaml"].ok  # the fixture's own starter config.yaml
    assert by_label["config loads cleanly"].ok  # no fleet.yaml yet -> nothing to conflict with
    assert not by_label["HQ local store"].ok
    assert not by_label["HQ remote wired"].ok
    assert not by_label["registered in HQ roster"].ok
    assert by_label[".beads permissions"].ok  # vacuously true — no `.beads` dirs exist yet


def test_status_surfaces_a_config_conflict_by_name():
    host_provision._step_config_init(dry_run=False)
    config.hq_dir().mkdir(parents=True, exist_ok=True)
    config.fleet_path().write_text('schema_version: 1\ndelimiter: ":"\nmanaged_repos: []\n')
    # deliberately do NOT reconcile — this is what an interrupted / dry-run-only host looks like

    checks = host_provision.status()

    conflict = next(c for c in checks if c.label == "config loads cleanly")
    assert not conflict.ok
    assert "fleet-only" in conflict.detail


def test_verify_done_once_fully_provisioned(monkeypatch):
    host_provision._step_config_init(dry_run=False)
    cfg = config.load()
    cfg["hq"] = {"remote": "acme/beadhive-hq"}
    config.save(cfg)

    def fake_clone(**kwargs):
        (config.hq_dir() / ".beads").mkdir(parents=True, exist_ok=True)
        config.fleet_path().write_text('schema_version: 1\ndelimiter: ":"\nmanaged_repos: []\n')

    monkeypatch.setattr(host_provision.hq, "clone", fake_clone)
    monkeypatch.setattr(
        host_provision,
        "run",
        lambda cmd, **k: (
            _Res(0, "git@github.com:acme/beadhive-hq.git\n") if "remote" in cmd else _Res(1)
        ),
    )
    host_provision._step_hq_clone(dry_run=False)
    host_provision._step_host_init(role="worker", force=False, dry_run=False)
    host_provision._step_fix_permissions(dry_run=False)

    result = host_provision._step_verify()

    assert result.status == "done", result.detail


# ---- orchestration: provision() -----------------------------------------------------


_STEP_FUNCS = (
    "_step_setup_check",
    "_step_config_init",
    "_step_git_workspace_update",
    "_step_hq_remote",
    "_step_hq_clone",
    "_step_host_init",
    "_step_bead_sync",
    "_step_fix_permissions",
    "_step_verify",
    "_step_adopt",
)


def test_provision_runs_every_step_in_plan_order(monkeypatch):
    order = []
    for func_name, plan_name in zip(_STEP_FUNCS, host_provision.PLAN, strict=True):

        def fake_step(*_a, _name=plan_name, **_k):
            order.append(_name)
            return host_provision.StepResult(_name, "skipped")

        monkeypatch.setattr(host_provision, func_name, fake_step)

    results = host_provision.provision(role="worker")

    assert order == list(host_provision.PLAN)
    assert [r.name for r in results] == list(host_provision.PLAN)


def test_provision_survives_one_step_raising(monkeypatch):
    def boom(**k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(host_provision, "_step_host_init", boom)

    results = host_provision.provision(role="worker")

    by_name = {r.name: r for r in results}
    assert by_name["host init"].status == "failed"
    assert "kaboom" in by_name["host init"].detail
    assert by_name["verify"].status in ("done", "failed")  # the pipeline kept going regardless


# ---- CLI wiring --------------------------------------------------------------------


def test_cli_rejects_an_unknown_role():
    result = runner.invoke(app, ["host", "provision", "--role", "super-admin"])

    assert result.exit_code == 1
    assert "--role must be one of" in result.output


def test_cli_dry_run_prints_the_ordered_plan_and_exits_zero():
    result = runner.invoke(app, ["host", "provision", "--role", "worker", "--dry-run", "--auto"])

    assert result.exit_code == 0, result.output
    assert "DRY-RUN" in result.output
    for name in host_provision.PLAN:
        assert name in result.output
    assert not host.path().exists()  # zero mutation


def test_cli_exits_nonzero_when_a_step_fails(monkeypatch):
    def fake_provision(**kwargs):
        return [host_provision.StepResult("config init", "failed", "boom")]

    monkeypatch.setattr(host_provision, "provision", fake_provision)

    result = runner.invoke(app, ["host", "provision", "--role", "worker", "--auto"])

    assert result.exit_code == 1
    assert "incomplete" in result.output


def test_cli_exits_zero_when_every_step_succeeds(monkeypatch):
    def fake_provision(**kwargs):
        return [host_provision.StepResult(n, "done") for n in host_provision.PLAN]

    monkeypatch.setattr(host_provision, "provision", fake_provision)

    result = runner.invoke(app, ["host", "provision", "--role", "worker", "--auto"])

    assert result.exit_code == 0, result.output
    assert "fully provisioned" in result.output


# ---- bh-1kzc: the setup gate must not block provision or any zero-mutation preview ---------


def test_setup_gate_exempts_the_verb_that_performs_the_setup_check(monkeypatch):
    """`host provision` runs `bh setup check` as PLAN[0], so gating it behind that check is a
    deadlock: the verb that performs the check could never run on the fresh host that needs it."""
    from beadhive import cli

    assert "provision" in cli._SETUP_GATE_ALLOW_VERBS["host"]
    # Scoped to the verb, not the group — every other `host` verb stays gated.
    assert "host" not in cli._SETUP_GATE_ALLOW


def test_dry_run_is_informational_and_never_gated(monkeypatch):
    """A preview that mutates nothing is the safest thing a new operator can run, and was the
    first thing they were refused (bh-1kzc). Before the fix the only route to it was
    BH_SKIP_SETUP_CHECK=1 — a bypass the error message itself labels debug-only."""
    from beadhive import cli

    class _Ctx:
        resilient_parsing = False

    monkeypatch.setattr(cli.sys, "argv", ["bh", "host", "provision", "--role", "worker"])
    assert cli._is_help_or_completion_invocation(_Ctx()) is False

    monkeypatch.setattr(
        cli.sys, "argv", ["bh", "host", "provision", "--role", "worker", "--dry-run"]
    )
    assert cli._is_help_or_completion_invocation(_Ctx()) is True


def test_setup_check_step_skips_a_passing_cache_without_probing(monkeypatch):
    """The already-provisioned case must not re-probe: this step exists for the fresh host."""
    from beadhive import setup as setup_mod

    probed: list[int] = []
    monkeypatch.setattr(setup_mod, "is_setup_complete", lambda: True)
    monkeypatch.setattr(setup_mod, "run_check", lambda: probed.append(1))
    result = host_provision._step_setup_check(dry_run=False)
    assert result.status == "skipped"
    assert probed == [], "must not probe when the cache already passes"


def test_setup_check_step_reports_failure_rather_than_aborting(monkeypatch):
    """`run_check` exits non-zero on a missing dep. That exit must become a failed STEP, so the
    later steps — especially the verifying gate — still report honestly."""
    from beadhive import setup as setup_mod

    def _exiting_check():
        raise SystemExit(1)

    monkeypatch.setattr(setup_mod, "is_setup_complete", lambda: False)
    monkeypatch.setattr(setup_mod, "run_check", _exiting_check)
    result = host_provision._step_setup_check(dry_run=False)
    assert result.status == "failed"


# ---- bh-q160.2: adopt runs LAST, and only on a clean run -----------------------------------


def test_adopt_is_the_final_step():
    """It is the only fleet-visible, racing step. Everything before it is local and reversible,
    so it goes after the verifying gate — that ordering IS the safety property."""
    assert host_provision.PLAN[-1] == "adopt"
    assert host_provision.PLAN[-2] == "verify"


def test_adopt_does_nothing_when_the_answers_file_asks_for_nothing():
    result = host_provision._step_adopt(adopt=[], dry_run=False, prior=[])
    assert result.status == "skipped"


def test_a_failure_anywhere_earlier_leaves_zero_leases_adopted(monkeypatch):
    """A half-provisioned host that grabbed primary is worse than one that failed cleanly: the
    lease is fleet-visible, and other hosts would defer to a host that does not work."""
    monkeypatch.setattr(
        host_provision.host_cli,
        "adopt_one",
        lambda *a, **k: pytest.fail("must not adopt after an earlier failure"),
    )
    prior = [host_provision.StepResult("verify", "failed", "not usable")]
    result = host_provision._step_adopt(adopt=["bh"], dry_run=False, prior=prior)
    assert result.status == "skipped"
    assert "verify" in result.detail


def test_adopt_dry_run_names_the_hives_without_touching_them(monkeypatch):
    monkeypatch.setattr(
        host_provision.host_cli,
        "adopt_one",
        lambda *a, **k: pytest.fail("dry-run must not adopt"),
    )
    result = host_provision._step_adopt(adopt=["bh", "other"], dry_run=True, prior=[])
    assert result.status == "would"
    assert "bh" in result.detail and "other" in result.detail


def test_a_refused_hive_reports_the_ones_already_adopted(monkeypatch):
    """host_adopt.adopt is itself two-phase per hive, so each adoption either happened
    completely or not at all — but the operator still needs to know where it stopped."""
    done = []

    def _adopt(prefix, **_k):
        if prefix == "second":
            raise RuntimeError("lease held elsewhere")
        done.append(prefix)

    monkeypatch.setattr(host_provision.host_cli, "adopt_one", _adopt)
    result = host_provision._step_adopt(adopt=["first", "second"], dry_run=False, prior=[])
    assert result.status == "failed"
    assert "first" in result.detail and "lease held elsewhere" in result.detail
