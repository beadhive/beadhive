"""bh-buaij: onboard publishes the bead database BEFORE registering the hive fleet-wide.

A zero-footprint onboard used to register the hive in ``managed_repos`` while its remote had
no ``refs/dolt/data``, so no other host could create a replica and hives-ready fenced every
other executor. The ``publish`` step now pushes through the managed remote sync's push leg
(``sync_remote._push_dolt_state``) before ``register``; a failed publish means no fleet
registration.

Hermetic: real temp git repos under $GIT_WORKSPACE (the ``world`` harness), ``.beads/``
pre-created so bd-init skips the real ``bd``, the origin probe and the push stubbed.
"""

from __future__ import annotations

import pytest
import typer

from beadhive import config, hub, onboard, registry, sync_remote
from harness.world import git

_REMOTE = "git+ssh://git@github.com/acme/widget.git"


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    monkeypatch.setattr(hub, "sync", lambda: None)
    monkeypatch.setattr(registry, "classify", lambda *a, **k: "personal-or-prototype")
    monkeypatch.setattr(registry, "has_push_access", lambda *a, **k: True)
    monkeypatch.setattr(onboard, "_origin_has_dolt_data", lambda ctx: False)


def _make_repo(world, *, sync_remote_url: str = _REMOTE):
    target = world.ws_root / "github" / "acme" / "widget"
    target.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=target)
    git("config", "user.email", "t@ws.dev", cwd=target)
    git("config", "user.name", "T", cwd=target)
    (target / "README.md").write_text("hi")
    git("add", ".", cwd=target)
    git("commit", "-q", "-m", "init", cwd=target)
    beads = target / ".beads"
    beads.mkdir()
    if sync_remote_url:
        (beads / "config.yaml").write_text(f'sync.remote: "{sync_remote_url}"\n')
    return target


def _ctx(target, **kw):
    ctx = onboard.Ctx(
        hive="github/acme/widget",
        target=str(target),
        provider="github",
        org="acme",
        repo="widget",
        cwd=str(target),
        cfg=config.load(),
        hub_sync=False,
        **kw,
    )
    ctx.steps = onboard.build_steps(ctx)
    return ctx


def _registered() -> bool:
    return registry.find_entry(config.load(), "github", "acme", "widget") is not None


def _record_push(monkeypatch, *, ok: bool = True, reason: str = ""):
    calls: list[dict] = []

    def push(cfg, clone_path, **kw):
        # The order under test: at publish time the hive is NOT yet registered fleet-wide.
        calls.append({"path": str(clone_path), "registered": _registered(), **kw})
        return ok, reason

    monkeypatch.setattr(sync_remote, "_push_dolt_state", push)
    return calls


def test_publish_runs_before_fleet_registration(world, monkeypatch):
    target = _make_repo(world)
    calls = _record_push(monkeypatch)

    plan = onboard.run_onboard(_ctx(target))

    order = plan.steps_run.index
    assert order("bd-init") < order("publish") < order("register")
    assert calls == [{"path": str(target), "registered": False}]
    assert plan.published is True
    assert plan.registered is True
    assert _registered()


def test_failed_publish_means_no_fleet_registration(world, monkeypatch, capsys):
    target = _make_repo(world)
    calls = _record_push(monkeypatch, ok=False, reason="remote: permission denied")

    with pytest.raises(typer.Exit) as exc:
        onboard.run_onboard(_ctx(target))

    assert exc.value.exit_code == 1
    assert len(calls) == 1
    assert not _registered()
    err = capsys.readouterr().err
    assert "permission denied" in err
    assert "NOT registered" in err
    # The local store is kept, so a re-run retries the publish via the initialized path.
    assert (target / ".beads").is_dir()


def test_retry_after_failed_publish_publishes_then_registers(world, monkeypatch):
    target = _make_repo(world)
    _record_push(monkeypatch, ok=False, reason="network down")
    with pytest.raises(typer.Exit):
        onboard.run_onboard(_ctx(target))

    calls = _record_push(monkeypatch)
    plan = onboard.run_onboard(_ctx(target))

    assert calls == [{"path": str(target), "registered": False}]
    assert plan.published is True and _registered()


def test_no_beads_remote_stays_local_and_registers(world, monkeypatch):
    """No sync.remote (e.g. no confirmed push access, bh-dhl6): beads stay local by design."""
    target = _make_repo(world, sync_remote_url="")
    calls = _record_push(monkeypatch)

    plan = onboard.run_onboard(_ctx(target))

    assert calls == []
    assert plan.published is False
    assert plan.registered is True


def test_remote_already_carrying_dolt_data_is_not_republished(world, monkeypatch):
    target = _make_repo(world)
    monkeypatch.setattr(onboard, "_origin_has_dolt_data", lambda ctx: True)
    calls = _record_push(monkeypatch)

    plan = onboard.run_onboard(_ctx(target))

    assert calls == []
    assert plan.registered is True


def test_external_hive_is_never_published(world, monkeypatch):
    target = _make_repo(world)
    calls = _record_push(monkeypatch)
    ctx = _ctx(target, kind="external")
    ctx.upstream = "github/upstream/widget"

    onboard._act_publish(ctx)

    assert calls == []


def test_dry_run_never_publishes(world, monkeypatch):
    target = _make_repo(world)
    calls = _record_push(monkeypatch)

    plan = onboard.run_onboard(_ctx(target), dry_run=True)

    assert "publish" in plan.steps_run
    assert calls == []
    assert not _registered()


def test_failed_publish_on_registered_hive_warns(world, monkeypatch, capsys):
    """Already registered (no re-registration this run): there is no registration to hold
    back, so a failed publish is a warning naming the remedy, not a refusal."""
    target = _make_repo(world)
    _record_push(monkeypatch, ok=True)
    onboard.run_onboard(_ctx(target))
    assert _registered()

    calls = _record_push(monkeypatch, ok=False, reason="network down")
    plan = onboard.run_onboard(_ctx(target))

    assert len(calls) == 1
    assert any("sync remotes --push" in w for w in plan.warnings)
    assert "network down" in capsys.readouterr().err
