"""Foreground process dispatch: fencing and actual POSIX signal delivery."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from beadhive import (
    config,
    config_store,
    dispatch_log,
    dispatch_supervisor,
    frame_eligibility,
    guard,
    host,
    hosts,
    hq_control_plane,
    identity,
    localloop,
    registry,
)
from beadhive import dispatch_hive_run as dhr
from beadhive.modules.config.domain.ports import FleetConfigDocument


@pytest.fixture
def sql_config(monkeypatch, tmp_path):
    """Exercise the real HOST selector and facade without a live SQL server."""
    monkeypatch.setenv("BH_HOME", str(tmp_path))
    binding = {
        "enabled": True,
        "reader": {
            "host": "sql.example.test",
            "database": "beadhive_hq_config",
            "user": "reader",
            "server_name": "sql.example.test",
            "ca_file": str(tmp_path / "ca.pem"),
            "credential": {
                "config_path": str(tmp_path / "fnox.toml"),
                "profile": "test",
                "key": "SQL_READER",
            },
        },
        "floor_path": str(tmp_path / "floor.json"),
        "backend_identity": "fixture",
        "generation": "fixture-generation",
        "initial_revision": "a" * 32,
    }
    config.config_path().write_text(json.dumps({"hq": {"sql": binding}}))
    config_store.clear_load_cache()
    state = {"revision": "r1", "available": True, "reads": 0}

    def snapshot(*, bootstrap):
        assert bootstrap["sql"]["enabled"] is True
        state["reads"] += 1
        if not state["available"]:
            raise hq_control_plane.ControlPlaneError("SQL credential=secret unavailable")
        return None, SimpleNamespace(
            documents=(
                FleetConfigDocument(
                    "fleet.yaml",
                    f"hq:\n  mode: dolt-server\nwork:\n  validate_cmd: {state['revision']}\n",
                ),
            )
        )

    monkeypatch.setattr(hq_control_plane, "attach_fleet_config", snapshot)
    return state


def test_fence_is_rechecked_before_each_spawn(tmp_path):
    checks = iter([True, True, False])
    spawned = []
    driver = dhr.HiveDispatchRun(
        hive_dir=tmp_path,
        hive="fixture/hive",
        actor="dev/test",
        sink_path=tmp_path / "dispatch.jsonl",
        pick=lambda: ["one", "two"],
        eligible=lambda: next(checks),
    )

    async def spawn(epic):
        spawned.append(epic)
        return dhr._Child(epic, type("Proc", (), {"returncode": None})())

    driver._spawn = spawn
    asyncio.run(driver.run_pass())
    assert spawned == ["one"]


def test_stopping_never_picks_new_work(tmp_path):
    def pick():
        raise AssertionError("draining dispatcher queried new work")

    driver = dhr.HiveDispatchRun(
        hive_dir=tmp_path,
        hive="fixture/hive",
        actor="dev/test",
        sink_path=tmp_path / "dispatch.jsonl",
        pick=pick,
    )
    driver._on_sigterm()
    assert asyncio.run(driver.run_pass()).idle


@pytest.mark.parametrize(
    ("candidate", "holder", "expected"),
    [(True, True, True), (False, True, False), (True, False, False)],
)
def test_process_adapter_requires_frame_admission_and_current_holder(
    monkeypatch, tmp_path, candidate, holder, expected
):
    current = {"revision": "current"}
    monkeypatch.setattr(config, "load", lambda: current)
    monkeypatch.setattr(
        frame_eligibility,
        "require_local",
        lambda hive, *, cfg, hive_dir: frame_eligibility.EligibilityDecision(
            (("admitted_active", candidate and cfg is current and hive_dir == tmp_path),)
        ),
    )
    lease = SimpleNamespace(held_by=lambda identity: holder and identity == "frame-id")
    monkeypatch.setattr(
        frame_eligibility,
        "authoritative_primary",
        lambda hive, *, cfg, hive_dir: ("fixture", "frame-id", lease),
    )
    monkeypatch.setattr(
        guard,
        "primary_state",
        lambda *a, **kw: pytest.fail("frame used legacy cached lease"),
    )
    assert dhr.process_eligible("fixture/hive", hive_dir=tmp_path) is expected


@pytest.mark.parametrize(
    ("lease_present", "holder", "expected"),
    [(False, False, True), (True, True, True), (True, False, False)],
)
def test_process_adapter_preserves_legacy_primary_policy(
    monkeypatch, tmp_path, lease_present, holder, expected
):
    monkeypatch.setattr(config, "load", lambda: {})
    monkeypatch.setattr(frame_eligibility, "require_local", lambda *a, **kw: None)
    lease = SimpleNamespace(held_by=lambda identity: holder and identity == "legacy-id")
    calls = []

    def primary(hive, *, cfg, hive_dir):
        calls.append((hive, cfg, hive_dir))
        return ("fixture", "legacy-id", lease) if lease_present else None

    monkeypatch.setattr(guard, "primary_state", primary)
    assert dhr.process_eligible("fixture/hive", hive_dir=tmp_path) is expected
    assert calls == [("fixture/hive", {}, tmp_path)]


@pytest.mark.parametrize(
    "failure", [OSError("config unavailable"), RuntimeError("authority unavailable")]
)
def test_process_adapter_denies_config_or_authority_outage(monkeypatch, tmp_path, failure):
    if isinstance(failure, OSError):
        monkeypatch.setattr(config, "load", lambda: (_ for _ in ()).throw(failure))
    else:
        monkeypatch.setattr(config, "load", lambda: {})
        monkeypatch.setattr(
            frame_eligibility,
            "local_intake_decision",
            lambda *a, **kw: (_ for _ in ()).throw(failure),
        )
    assert not dhr.process_eligible("fixture/hive", hive_dir=tmp_path)


def test_process_adapter_rereads_config_before_each_spawn(monkeypatch, tmp_path):
    revisions = iter(["valid", "valid", "expired"])
    monkeypatch.setattr(config, "load", lambda: {"revision": next(revisions)})
    monkeypatch.setattr(
        frame_eligibility,
        "local_intake_decision",
        lambda hive, *, cfg, hive_dir, legacy_primary: frame_eligibility.EligibilityDecision(
            (("config_valid", cfg["revision"] == "valid" and hive_dir == tmp_path),)
        ),
    )
    spawned = []
    driver = dhr.HiveDispatchRun(
        hive_dir=tmp_path,
        hive="fixture/hive",
        actor="dev/test",
        sink_path=tmp_path / "dispatch.jsonl",
        pick=lambda: ["one", "two"],
        eligible=lambda: dhr.process_eligible("fixture/hive", hive_dir=tmp_path),
    )

    async def spawn(epic):
        spawned.append(epic)
        return dhr._Child(epic, type("Proc", (), {"returncode": None})())

    driver._spawn = spawn
    asyncio.run(driver.run_pass())
    assert spawned == ["one"]


def test_process_lease_renew_denies_expired_config(monkeypatch, tmp_path):
    revisions = iter([{"current": True}, {"current": False}])
    monkeypatch.setattr(config, "load", lambda: next(revisions))

    def require_intake(hive, *, cfg, hive_dir):
        assert (hive, hive_dir) == ("fixture/hive", tmp_path)
        if not cfg["current"]:
            raise frame_eligibility.EligibilityError("central config expired")

    monkeypatch.setattr(frame_eligibility, "require_intake", require_intake)
    keeper = localloop.EligibilityLeaseKeeper(
        localloop.NullLeaseKeeper(),
        "fixture/hive",
        {"current": True},
        tmp_path,
        fresh_config=True,
    )
    assert keeper.renew(active=False).held
    denied = keeper.renew(active=False)
    assert not denied.held
    assert "central config expired" in denied.detail


def test_process_lease_renew_denies_config_outage(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "load", lambda: (_ for _ in ()).throw(OSError("config down")))

    class ForbiddenRenew:
        def renew(self, *, active):
            pytest.fail("lease renewed after qualified config read failed")

    keeper = localloop.EligibilityLeaseKeeper(
        ForbiddenRenew(), "fixture/hive", {}, tmp_path, fresh_config=True
    )
    denied = keeper.renew(active=False)
    assert not denied.held
    assert denied.detail == "configuration unavailable"


def test_shared_keeper_factory_refreshes_config_for_systemd_too(monkeypatch, tmp_path):
    monkeypatch.setattr(frame_eligibility, "require_local", lambda *a, **kw: None)
    monkeypatch.setattr(guard, "primary_state", lambda *a, **kw: None)
    monkeypatch.setattr(config, "load", lambda: {"current": False})

    def require_intake(hive, *, cfg, hive_dir):
        assert hive_dir == tmp_path
        if not cfg["current"]:
            raise frame_eligibility.EligibilityError("central config expired")

    monkeypatch.setattr(frame_eligibility, "require_intake", require_intake)
    keeper = localloop.lease_keeper_for("fixture/hive", cfg={"current": True}, hive_dir=tmp_path)
    denied = keeper.renew(active=False)
    assert not denied.held
    assert denied.detail == "central config expired"


@pytest.mark.parametrize("change", ["revision", "outage"])
def test_sql_selected_picker_requalifies_before_each_spawn(
    monkeypatch, tmp_path, sql_config, change
):
    monkeypatch.setattr(
        frame_eligibility,
        "require_local",
        lambda hive, *, cfg, hive_dir: frame_eligibility.EligibilityDecision(
            (("current_sql_revision", cfg["work"]["validate_cmd"] == "r1"),)
        ),
    )
    monkeypatch.setattr(
        frame_eligibility,
        "authoritative_primary",
        lambda *a, **kw: ("fixture", "frame-id", SimpleNamespace(held_by=lambda _: True)),
    )
    monkeypatch.setattr(
        guard, "primary_state", lambda *a, **kw: pytest.fail("frame used legacy lease")
    )
    spawned = []
    driver = dhr.HiveDispatchRun(
        hive_dir=tmp_path,
        hive="fixture/hive",
        actor="dev/test",
        sink_path=tmp_path / "dispatch.jsonl",
        pick=lambda: ["one", "two"],
        eligible=lambda: dhr.process_eligible("fixture/hive", hive_dir=tmp_path),
    )

    async def spawn(epic):
        spawned.append(epic)
        if change == "revision":
            sql_config["revision"] = "r2"
        else:
            sql_config["available"] = False
        return dhr._Child(epic, SimpleNamespace(returncode=None))

    driver._spawn = spawn
    asyncio.run(driver.run_pass())
    assert spawned == ["one"]
    assert sql_config["reads"] == 3  # before pick, first spawn, second spawn


@pytest.mark.parametrize("backend", ["process", "systemd"])
@pytest.mark.parametrize("change", ["revision", "outage"])
def test_sql_selected_keeper_requalifies_for_both_dispatch_backends(
    monkeypatch, tmp_path, sql_config, backend, change
):
    monkeypatch.setattr(
        dispatch_supervisor,
        "get_supervisor_backend",
        lambda cfg: SimpleNamespace(name=backend),
    )
    monkeypatch.setattr(registry, "hive_dir_for", lambda cfg, hive: tmp_path)
    monkeypatch.setattr(registry, "entry_for_dir", lambda cfg, main: {})
    monkeypatch.setattr(dispatch_log, "ensure_sink_dir", lambda: None)
    monkeypatch.setattr(dispatch_log, "sink_path", lambda cfg, entry: tmp_path / "sink")
    monkeypatch.setattr(config, "work_identity", lambda cfg, entry: {"name": "dev/test"})
    monkeypatch.setattr(identity, "resolve_actor", lambda actor, fallback: actor or fallback)
    monkeypatch.setattr(frame_eligibility, "require_local", lambda *a, **kw: None)
    monkeypatch.setattr(guard, "primary_state", lambda *a, **kw: None)

    def require_intake(hive, *, cfg, hive_dir):
        assert hive_dir == tmp_path
        if cfg["work"]["validate_cmd"] != "r1":
            raise frame_eligibility.EligibilityError("central revision revoked")

    monkeypatch.setattr(frame_eligibility, "require_intake", require_intake)
    driver = dhr.build_run("fixture/hive", cfg=config.load())
    assert driver.lease.renew(active=False).held
    if change == "revision":
        sql_config["revision"] = "r2"
    else:
        sql_config["available"] = False
    denied = driver.lease.renew(active=False)
    assert not denied.held
    assert denied.detail == (
        "central revision revoked" if change == "revision" else "configuration unavailable"
    )
    assert "secret" not in denied.detail
    assert sql_config["reads"] == 3  # build, first renew, second renew


@pytest.mark.parametrize("claim", ["claim_issue", "claim_next"])
@pytest.mark.parametrize("change", ["revision", "outage"])
def test_sql_selected_claim_rechecks_current_intake(
    monkeypatch, tmp_path, sql_config, claim, change
):
    calls = []
    session = SimpleNamespace(
        claim_issue=lambda *a, **kw: calls.append("issue"),
        claim_next=lambda *a, **kw: calls.append("next"),
    )
    monkeypatch.setattr(host, "host_id", lambda: "frame-id")
    monkeypatch.setattr(registry, "entry_for_dir", lambda cfg, main: {"prefix": "fixture"})

    def require_eligible(host_id, hive, *, cfg):
        if cfg["work"]["validate_cmd"] != "r1":
            raise frame_eligibility.EligibilityError("current_sql_revision")
        return frame_eligibility.EligibilityDecision((("current_sql_revision", True),))

    monkeypatch.setattr(frame_eligibility, "require_eligible", require_eligible)
    monkeypatch.setattr(
        frame_eligibility,
        "authoritative_primary",
        lambda *a, **kw: ("fixture", "frame-id", SimpleNamespace(held_by=lambda _: True)),
    )
    guarded = frame_eligibility.GuardedClaimSession(session, tmp_path)
    getattr(guarded, claim)()
    if change == "revision":
        sql_config["revision"] = "r2"
        with pytest.raises(frame_eligibility.EligibilityError, match="current_sql_revision"):
            getattr(guarded, claim)()
    else:
        sql_config["available"] = False
        with pytest.raises(hq_control_plane.ControlPlaneError, match="unavailable"):
            getattr(guarded, claim)()
    assert calls == ["issue" if claim == "claim_issue" else "next"]
    assert sql_config["reads"] == 3  # two checks at allowed claim, one at denied claim


@pytest.mark.parametrize(
    ("primary", "expected"), [(None, True), ("held", True), ("foreign", False)]
)
def test_sql_config_only_legacy_host_keeps_legacy_lease_policy(
    monkeypatch, tmp_path, sql_config, primary, expected
):
    monkeypatch.setattr(host, "host_id", lambda: "legacy-id")
    monkeypatch.setattr(hosts, "load", lambda *a, **kw: (_ for _ in ()).throw(FileNotFoundError()))
    monkeypatch.setattr(
        hq_control_plane,
        "control_plane",
        lambda *a, **kw: pytest.fail("config-only legacy host enrolled into SQL runtime"),
    )
    monkeypatch.setattr(
        guard,
        "primary_state",
        lambda *a, **kw: (
            None
            if primary is None
            else (
                "fixture",
                "legacy-id",
                SimpleNamespace(
                    held_by=lambda identity: primary == "held" and identity == "legacy-id"
                ),
            )
        ),
    )
    assert dhr.process_eligible("fixture/hive", hive_dir=tmp_path) is expected
    assert sql_config["reads"] >= 1


def test_sigterm_checkpoints_child_and_exits_zero(tmp_path):
    """Send real SIGTERM to the picker; its child saves a checkpoint before exit."""
    ready = tmp_path / "ready"
    checkpoint = tmp_path / "checkpoint"
    code = '''
import asyncio
from pathlib import Path
import sys
from beadhive.dispatch_hive_run import HiveDispatchRun, _Child

root = Path(sys.argv[1])
child_code = """
import signal, sys, time
from pathlib import Path
root = Path(sys.argv[1])
def stop(*args):
    (root / 'checkpoint').write_text('saved')
    sys.exit(0)
signal.signal(signal.SIGTERM, stop)
(root / 'ready').write_text('ready')
while True: time.sleep(0.05)
"""
class Driver(HiveDispatchRun):
    async def _spawn(self, epic):
        proc = await asyncio.create_subprocess_exec(
            sys.executable, '-c', child_code, str(root), start_new_session=True)
        return _Child(epic, proc)

driver = Driver(hive_dir=root, hive='fixture/hive', actor='dev/test',
                sink_path=root / 'sink', pick=lambda: ['one'], eligible=lambda: True,
                poll_interval=300, drain_timeout=2)
asyncio.run(driver.run())
'''
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(dhr.__file__).resolve().parents[1])
    proc = subprocess.Popen([sys.executable, "-c", code, str(tmp_path)], env=env)
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            assert proc.poll() is None
            time.sleep(0.02)
        assert ready.exists()
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=5) == 0
        assert checkpoint.read_text() == "saved"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_drain_deadline_fences_unresponsive_child_and_reports_failure(tmp_path):
    import pytest

    async def scenario():
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "print('ready',flush=True); time.sleep(300)",
            stdout=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        await proc.stdout.readline()
        driver = dhr.HiveDispatchRun(
            hive_dir=tmp_path,
            hive="fixture/hive",
            actor="dev/test",
            sink_path=tmp_path / "dispatch.jsonl",
            drain_timeout=0.05,
        )
        driver.children["one"] = dhr._Child("one", proc)
        with pytest.raises(RuntimeError, match="checkpoint completion unverified"):
            await driver.shutdown()
        assert proc.returncode == -signal.SIGKILL

    asyncio.run(scenario())


def test_nonzero_child_drain_refuses_success_after_reaping(tmp_path):
    async def scenario():
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import signal,sys,time; "
            "signal.signal(signal.SIGTERM, lambda *_: sys.exit(1)); "
            "print('ready',flush=True); time.sleep(300)",
            stdout=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        await proc.stdout.readline()
        driver = dhr.HiveDispatchRun(
            hive_dir=tmp_path,
            hive="fixture/hive",
            actor="dev/test",
            sink_path=tmp_path / "dispatch.jsonl",
            drain_timeout=2,
        )
        driver.children["one"] = dhr._Child("one", proc)
        with pytest.raises(RuntimeError, match="checkpoint completion unverified"):
            await driver.shutdown()
        assert proc.returncode == 1
        assert not driver.children

    asyncio.run(scenario())
