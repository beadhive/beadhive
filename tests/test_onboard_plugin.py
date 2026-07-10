"""Generic plugin onboard step (bead .6) — onboard loops plugins.registry() generically.

A plugin's on_onboard hook runs as a ``plugin-<name>`` step when the plugin is flagged
(``ctx.plugins``) OR its ``enabled(cfg, entry)`` predicate is true; a raising hook is fenced
(warn-and-continue) and never aborts onboarding; and NO plugin step is built when the registry
is empty. plugins.registry is monkeypatched to control the set (no dependence on orca).
"""

from __future__ import annotations

import typer

from beadhive import config, hub, onboard, plugins, registry
from harness.world import git


def _mk_plugin(name="orca", *, enabled=False, hook=None):
    return plugins.Plugin(
        name=name, cli=typer.Typer(), enabled=lambda cfg, entry: enabled, on_onboard=hook,
    )


def _make_repo(world, *, org="acme", repo="widget"):
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


def _ctx(world, target, **kw):
    ctx = onboard.Ctx(
        rig="github/acme/widget", target=str(target), provider="github", org="acme",
        repo="widget", cwd=str(target), cfg=config.load(), do_hub_sync=True, **kw,
    )
    ctx.steps = onboard.build_steps(ctx)
    return ctx


def _stub(monkeypatch, plugin_list):
    monkeypatch.setattr(onboard._plugins, "registry", lambda: plugin_list)
    monkeypatch.setattr(registry, "classify", lambda *a, **k: "personal-or-prototype")
    monkeypatch.setattr(hub, "sync", lambda: None)


# ---- build_steps step construction ------------------------------------------


def test_no_plugin_step_when_registry_empty(world, monkeypatch):
    _stub(monkeypatch, [])
    ctx = _ctx(world, _make_repo(world))
    assert not any(s.id.startswith("plugin-") for s in ctx.steps)


def test_plugin_step_present_and_flag_enables_it(world, monkeypatch):
    _stub(monkeypatch, [_mk_plugin(enabled=False, hook=lambda c: None)])
    ctx = _ctx(world, _make_repo(world), plugins=["orca"])
    step = next(s for s in ctx.steps if s.id == "plugin-orca")
    assert step.enabled(ctx) is True  # forced on via ctx.plugins


def test_plugin_step_config_enabled_without_flag(world, monkeypatch):
    _stub(monkeypatch, [_mk_plugin(enabled=True, hook=lambda c: None)])
    ctx = _ctx(world, _make_repo(world))  # no --plugin flag
    step = next(s for s in ctx.steps if s.id == "plugin-orca")
    assert step.enabled(ctx) is True  # enabled by the plugin's own predicate


def test_plugin_step_disabled_and_unflagged(world, monkeypatch):
    _stub(monkeypatch, [_mk_plugin(enabled=False, hook=lambda c: None)])
    ctx = _ctx(world, _make_repo(world))
    step = next(s for s in ctx.steps if s.id == "plugin-orca")
    assert step.enabled(ctx) is False


# ---- run_onboard fence behavior ---------------------------------------------


def test_flagged_plugin_hook_fires_and_records(world, monkeypatch):
    calls: list[object] = []
    _stub(monkeypatch, [_mk_plugin(hook=lambda c: calls.append(c))])
    ctx = _ctx(world, _make_repo(world), plugins=["orca"])

    plan = onboard.run_onboard(ctx)

    assert len(calls) == 1
    assert "plugin-orca" in plan.installers_run
    assert "plugin-orca" in plan.steps_run


def test_raising_hook_does_not_abort_onboarding(world, monkeypatch):
    def boom(ctx):
        raise RuntimeError("plugin exploded")

    synced: list[bool] = []
    _stub(monkeypatch, [_mk_plugin(hook=boom)])
    monkeypatch.setattr(hub, "sync", lambda: synced.append(True))
    ctx = _ctx(world, _make_repo(world), plugins=["orca"])

    plan = onboard.run_onboard(ctx)

    # Onboarding completed despite the raising hook.
    assert plan.registered is True
    assert synced == [True]
    assert "plugin-orca" not in plan.installers_run  # failure is not recorded as success


def test_disabled_plugin_hook_never_runs(world, monkeypatch):
    calls: list[object] = []
    _stub(monkeypatch, [_mk_plugin(enabled=False, hook=lambda c: calls.append(c))])
    ctx = _ctx(world, _make_repo(world))  # not flagged, not enabled

    plan = onboard.run_onboard(ctx)

    assert calls == []
    assert "plugin-orca" not in plan.steps_run
