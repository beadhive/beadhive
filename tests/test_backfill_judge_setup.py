"""``bh backfill judge setup``: extra, secret-store key, probe, opt-in enable (all faked)."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from beadhive import backfill_cli, cli
from beadhive import backfill_judge_setup as setup
from beadhive.hq_sql_transport import SqlTransportError
from beadhive.modules.config.contracts import BackfillJudgeConfig

KEY = "sk-ant-SECRET-VALUE-123"
REF = {"config_path": "/etc/fnox.toml", "profile": "bh", "key": "ANTHROPIC_API_KEY"}
ENV = {"COLUMNS": "120", "BH_SKIP_SETUP_CHECK": "1", "NO_COLOR": "1"}
GOOD = setup.ProbeResult(True, model="claude-haiku-5-5", latency_ms=42)


class Calls:
    def __init__(self) -> None:
        self.installs = 0
        self.persisted: list[object] = []


def _run(calls: Calls, *, present=True, install_ok=True, key=KEY, probe=GOOD, **kw):
    state = {"present": present}

    def installer():
        calls.installs += 1
        state["present"] = install_ok
        return (install_ok, "installed" if install_ok else "installer exited 1")

    def resolve(ref):
        if key is None:
            raise KeyError("fnox credential unavailable")
        return key

    return setup.run_setup(
        kw.pop("section", {"credential": REF}),
        environ=kw.pop("environ", {}),
        installer=installer,
        present=lambda command: state["present"],
        resolve_key=resolve,
        probe=lambda k, s: probe,
        persist=calls.persisted.append,
        **kw,
    )


def test_success_reports_every_step_and_changes_nothing_without_enable() -> None:
    calls = Calls()
    report = _run(calls)
    assert report.ok and not report.enabled and calls.persisted == []
    assert [(s.name, s.status) for s in report.steps] == [
        ("extra", "ok"),
        ("key", "ok"),
        ("probe", "ok"),
        ("enable", "skipped"),
    ]
    assert "claude-haiku-5-5" in report.steps[2].detail and "42 ms" in report.steps[2].detail


def test_enable_persists_only_after_success() -> None:
    calls = Calls()
    report = _run(calls, enable=True)
    assert report.ok and report.enabled and len(calls.persisted) == 1


def test_missing_extra_is_installed() -> None:
    calls = Calls()
    # present flips true after the installer ran
    state = {"n": 0}

    def present(command):
        state["n"] += 1
        return state["n"] > 1

    report = setup.run_setup(
        {"credential": REF},
        environ={},
        installer=lambda: calls.__setattr__("installs", 1) or (True, "installed"),
        present=present,
        resolve_key=lambda r: KEY,
        probe=lambda k, s: GOOD,
        install=True,
    )
    assert report.ok and calls.installs == 1


def test_missing_extra_with_no_install_or_failed_install_stops_unchanged() -> None:
    calls = Calls()
    report = _run(calls, present=False, enable=True)  # default: verify only, never install
    assert not report.ok and [s.name for s in report.steps] == ["extra"] and calls.persisted == []
    assert calls.installs == 0 and "pip install" in report.steps[0].detail
    assert "beadlog-sources[haiku]" in report.steps[0].detail
    report = _run(calls, present=False, install_ok=False, install=True, enable=True)
    assert not report.ok and calls.installs == 1 and calls.persisted == []


def test_missing_key_fails_and_leaves_config_unchanged() -> None:
    calls = Calls()
    report = _run(calls, key=None, enable=True)
    assert not report.ok and report.steps[-1].name == "key" and calls.persisted == []
    # no reference and no env key either
    report = _run(calls, section={}, environ={}, enable=True)
    assert not report.ok and report.steps[-1].name == "key" and calls.persisted == []


def test_environment_key_is_used_without_a_reference() -> None:
    calls = Calls()
    report = _run(calls, section={}, environ={setup.API_KEY_ENV: KEY})
    assert report.ok and "ANTHROPIC_API_KEY" in report.steps[1].detail


def test_failed_probe_is_a_failure_even_offline() -> None:
    calls = Calls()
    bad = setup.ProbeResult(False, detail="fell back (exit)")
    report = _run(calls, probe=bad, enable=True)
    assert not report.ok and report.steps[-1].name == "probe" and calls.persisted == []


def test_dry_run_plans_every_step_and_touches_nothing() -> None:
    calls = Calls()
    report = _run(calls, present=False, dry_run=True, enable=True)
    assert report.ok and {s.status for s in report.steps} == {"planned"}
    assert calls.installs == 0 and calls.persisted == []


def test_probe_detail_never_carries_the_key() -> None:
    calls = Calls()
    leaky = setup.ProbeResult(False, detail=f"boom {KEY}")
    report = _run(calls, probe=leaky)
    assert KEY not in report.steps[-1].detail


def test_fnox_key_maps_broker_failure_to_static_message() -> None:
    class Broker:
        def get(self, reference, *, deadline):
            raise SqlTransportError("fnox credential unavailable")

    with pytest.raises(KeyError, match="unavailable"):
        setup.fnox_key(REF, broker=Broker())

    class Empty:
        def get(self, reference, *, deadline):
            return ""

    with pytest.raises(KeyError):
        setup.fnox_key(REF, broker=Empty())
    assert setup.fnox_key(REF, broker=type("B", (), {"get": lambda s, r, deadline: KEY})()) == KEY


def test_config_keys_default_off() -> None:
    cfg = BackfillJudgeConfig()
    assert cfg.cost_per_call_usd is None and cfg.enabled is False
    assert not setup.fnox_reference(cfg.model_dump())
    assert (
        backfill_cli.judge_settings({"backfill": {"judge": {"credential": REF}}}).enabled is False
    )


def _invoke(monkeypatch, *args, **patches):
    cfg = {"backfill": {"judge": {"cost_per_call_usd": 0.001, "credential": REF}}}
    monkeypatch.setattr(backfill_cli.config, "load", lambda *a, **k: cfg)
    writes: list[tuple[str, str]] = []
    monkeypatch.setattr(
        backfill_cli.config, "set_value", lambda k, v, **kw: writes.append((k, v)) or {"ok": True}
    )
    monkeypatch.setattr(setup, "extra_present", lambda command: True)
    monkeypatch.setattr(setup, "fnox_key", lambda ref, broker=None: KEY)
    monkeypatch.setattr(setup, "probe_judge", lambda k, s: GOOD)
    result = CliRunner().invoke(cli.app, ["backfill", "judge", "setup", *args], env=ENV)
    return result, writes


def test_cli_success_never_prints_the_key_and_enable_writes_config(monkeypatch) -> None:
    result, writes = _invoke(monkeypatch, "--enable")
    assert result.exit_code == 0, result.output
    assert KEY not in result.output and "probe: ok" in result.output and "$0.001" in result.output
    assert writes == [("backfill.judge.enabled", "true")]


def test_cli_json_and_dry_run(monkeypatch) -> None:
    result, writes = _invoke(monkeypatch, "--dry-run", "--enable", "--json")
    payload = json.loads(result.stdout)
    assert result.exit_code == 0 and payload["dry_run"] and writes == []
    assert KEY not in result.output


def test_cli_failed_probe_exits_nonzero_without_writes(monkeypatch) -> None:
    monkeypatch.setattr(setup, "probe_judge", lambda k, s: setup.ProbeResult(False, detail="x"))
    cfg = {"backfill": {"judge": {"credential": REF}}}
    writes: list[str] = []
    monkeypatch.setattr(backfill_cli.config, "load", lambda *a, **k: cfg)
    monkeypatch.setattr(backfill_cli.config, "set_value", lambda k, v, **kw: writes.append(k))
    monkeypatch.setattr(setup, "extra_present", lambda command: True)
    monkeypatch.setattr(setup, "fnox_key", lambda ref, broker=None: KEY)
    result = CliRunner().invoke(cli.app, ["backfill", "judge", "setup", "--enable"], env=ENV)
    assert result.exit_code == 1 and KEY not in result.output and writes == []
