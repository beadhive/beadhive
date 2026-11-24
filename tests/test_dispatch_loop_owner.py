"""`work.dispatch.loop: bh | gc` (bh-njarn, gas-city-runtime-seam-adr.md Decision 7).

One stand-down predicate — hive `loop: gc` OR a member labeled `dispatch-owner:gc` — read by the
dispatch pass, lease acquisition, service install/start and reclaim. Unset/`bh` is unchanged.
"""

from __future__ import annotations

import asyncio
import json

from typer.testing import CliRunner

from beadhive import (
    bd as bd_mod,
)
from beadhive import (
    dispatch_hive_run,
    dispatch_loop_owner,
    guard,
    host_cli,
    registry,
)
from beadhive import dispatch_status as dstat
from beadhive import dispatch_supervisor as ds
from beadhive.cli import app

runner = CliRunner()

GC_CFG = {"work": {"dispatch": {"loop": "gc"}}}
ENTRY = {"provider": "github", "org": "acme", "repo": "widgets", "prefix": "acme"}


# ---- the predicate -------------------------------------------------------------------------


def test_unset_and_bh_and_garbage_all_mean_bh():
    for cfg in ({}, {"work": {"dispatch": {"loop": "bh"}}}, {"work": {"dispatch": {"loop": "x"}}}):
        assert dispatch_loop_owner.declared_loop(cfg, {}) == "bh"
        assert not dispatch_loop_owner.stands_down(cfg, {})


def test_hive_loop_gc_stands_down_globally_and_per_hive():
    assert dispatch_loop_owner.stands_down(GC_CFG, {})
    per_hive = {"work": {"dispatch": {"loop": "gc"}}}
    assert dispatch_loop_owner.stands_down({}, per_hive)
    # a per-hive `bh` overrides a global `gc`
    assert not dispatch_loop_owner.stands_down(GC_CFG, {"work": {"dispatch": {"loop": "bh"}}})


def test_member_label_stands_down_without_the_hive_key():
    assert dispatch_loop_owner.stands_down({}, {}, ["x", "dispatch-owner:gc"])
    assert not dispatch_loop_owner.stands_down({}, {}, ["dispatch-owner:bh"])


def test_refusal_names_gc_as_owner():
    msg = dispatch_loop_owner.refusal("host dispatch enable", "acme/widgets")
    assert "owned by gc" in msg and "acme/widgets" in msg


# ---- status --------------------------------------------------------------------------------


class _Backend:
    name = "fixed"

    def __init__(self, state):
        self._state = state

    def status(self, hive_slug):  # noqa: ARG002
        return self._state


def _patch_hive(monkeypatch, tmp_path):
    monkeypatch.setattr(registry, "hive_dir_for", lambda cfg, hive: tmp_path)
    monkeypatch.setattr(registry, "entry_for_dir", lambda cfg, cwd: ENTRY)
    monkeypatch.setattr(guard, "primary_state", lambda *a, **k: None)


def test_status_is_delegated_under_loop_gc_even_when_a_unit_is_running(monkeypatch, tmp_path):
    _patch_hive(monkeypatch, tmp_path)
    for installed, running in ((False, False), (True, True)):
        backend = _Backend(ds.SupervisorState(installed=installed, running=running))
        status = dstat.compute_status("acme/widgets", cfg=GC_CFG, backend=backend)
        assert status.state == dstat.STATE_DELEGATED
        assert status.detail == dispatch_loop_owner.DELEGATED_DETAIL
        assert "bh not managing dispatch, lease, or reclaim" in status.detail
    assert dstat.STATE_DELEGATED in dstat.DISPATCH_STATES


def test_status_unchanged_when_loop_unset(monkeypatch, tmp_path):
    _patch_hive(monkeypatch, tmp_path)
    backend = _Backend(ds.SupervisorState(installed=False, running=False))
    status = dstat.compute_status("acme/widgets", cfg={}, backend=backend)
    assert status.state == dstat.STATE_NOT_ENABLED


# ---- refusals: no lease taken, no service installed, no pass run ---------------------------


def _gc_config(monkeypatch):
    monkeypatch.setattr(dispatch_loop_owner, "hive_delegated", lambda cfg, entry: True)
    monkeypatch.setattr(host_cli, "_dispatch_entry", lambda hive, cfg: ({}, "acme/widgets"))


def test_enable_refuses_naming_gc_and_takes_no_lease_and_installs_nothing(monkeypatch):
    _gc_config(monkeypatch)
    calls = []
    monkeypatch.setattr(host_cli, "_ensure_lease_for_enable", lambda *a: calls.append("lease"))
    monkeypatch.setattr(
        host_cli.dispatch_supervisor,
        "get_supervisor_backend",
        lambda cfg: calls.append("backend"),
    )

    result = runner.invoke(app, ["host", "dispatch", "enable", "--hive", "acme/widgets"])

    assert result.exit_code == 1
    assert "owned by gc" in result.output
    assert calls == []


def test_run_refuses_naming_gc_and_builds_no_driver(monkeypatch, tmp_path):
    monkeypatch.setattr(dispatch_loop_owner, "hive_delegated", lambda cfg, entry: True)
    monkeypatch.setattr(registry, "hive_dir_for", lambda cfg, hive: tmp_path)
    monkeypatch.setattr(registry, "entry_for_dir", lambda cfg, cwd: ENTRY)

    result = runner.invoke(app, ["host", "dispatch", "run", "--hive", "acme/widgets"])

    assert result.exit_code == 1
    assert "owned by gc" in result.output


def test_work_next_refuses_under_loop_gc(monkeypatch):
    monkeypatch.setattr(dispatch_loop_owner, "hive_delegated", lambda cfg, entry: True)
    monkeypatch.setattr(guard, "guard_primary", lambda *a, **k: None)
    result = runner.invoke(app, ["work", "next", "--as", "dev/x"])
    assert result.exit_code == 1
    assert "owned by gc" in result.output


# ---- the picker + a pass stand down --------------------------------------------------------


def test_picker_skips_epics_labeled_dispatch_owner_gc(monkeypatch, tmp_path):
    rows = [
        {"id": "bh-a", "issue_type": "epic", "labels": ["kickoff:approved"]},
        {
            "id": "bh-b",
            "issue_type": "epic",
            "labels": ["kickoff:approved", "dispatch-owner:gc"],
        },
    ]
    monkeypatch.setattr(bd_mod, "json", lambda args, cwd: rows)
    assert dispatch_hive_run.kicked_off_ready_epics(tmp_path) == ["bh-a"]


class _CountingLease:
    def __init__(self):
        self.renewals = 0

    def renew(self, *, active):  # noqa: ARG002
        self.renewals += 1
        raise AssertionError("a delegated pass must not touch the lease")


def test_delegated_hive_pass_takes_no_lease_and_spawns_nothing(tmp_path):
    spawned, picked = [], []

    class _Run(dispatch_hive_run.HiveDispatchRun):
        async def _spawn(self, epic):  # pragma: no cover - must not run
            spawned.append(epic)

    run = _Run(
        hive_dir=tmp_path,
        hive="acme/widgets",
        actor="dev/x",
        sink_path=tmp_path / "sink.jsonl",
        lease=_CountingLease(),
        pick=lambda: picked.append(1) or ["bh-x"],
        delegated=lambda: True,
    )
    report = asyncio.run(run.run_pass())
    assert report.idle and report.spawned == ()
    assert spawned == [] and picked == []


# ---- lifecycle verbs are not loop-owning ---------------------------------------------------


def test_lifecycle_verbs_do_not_consult_the_stand_down_predicate():
    """Only the loop-owning modules read the predicate; claim/submit/review/merge/gate-resolve
    code never does, so they work unchanged under `loop: gc`."""
    from pathlib import Path

    import beadhive

    root = Path(beadhive.__file__).parent
    readers = {
        p.name
        for p in root.glob("*.py")
        if "dispatch_loop_owner" in p.read_text() and p.name != "dispatch_loop_owner.py"
    }
    assert readers == {
        "dispatch_hive_run.py",
        "dispatch_status.py",
        "host_cli.py",
        "localloop.py",
        "work_dispatch.py",
    }


def test_schema_default_is_bh_and_rejects_other_values():
    from beadhive.modules.config.contracts import DispatchConfig

    assert DispatchConfig().loop == "bh"
    assert DispatchConfig(loop="gc").loop == "gc"
    import pydantic
    import pytest

    with pytest.raises(pydantic.ValidationError):
        DispatchConfig(loop="cron")
    assert json.dumps(DispatchConfig().model_dump())  # serializable
