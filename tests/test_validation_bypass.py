from __future__ import annotations

import json
import subprocess
from contextlib import contextmanager, nullcontext
from types import SimpleNamespace

import pytest

from beadhive import (
    config,
    doctor,
    otel,
    prepush,
    validation_bypass,
    validation_records,
    work_metrics,
    worktree_verify,
)
from beadhive.modules.config.contracts import BeadhiveConfig


def _entry(enabled: bool = True):
    return {
        "provider": "github",
        "org": "acme",
        "repo": "app",
        "prefix": "app",
        "kind": "personal",
        "work": {"validation_bypass": enabled},
    }


def test_validation_bypass_is_schema_backed_layered_and_strict():
    parsed = BeadhiveConfig.model_validate(
        {"managed_repos": [_entry(True), {**_entry(False), "repo": "other", "prefix": "other"}]}
    )
    assert parsed.managed_repos[0].work.validation_bypass is True
    assert config.validation_bypass_enabled({}, _entry(True)) is True
    assert config.validation_bypass_enabled({}, _entry(False)) is False
    assert config.validation_bypass_enabled(
        {"work": {"validation_bypass": True}}, _entry(False)
    ) is False
    assert config.validation_bypass_enabled(
        {"work": {"validation_bypass": True}}, {"work": {}}
    ) is False
    assert config.validation_bypass_enabled({}, {"work": {"validation_bypass": "true"}}) is False


def test_config_set_requires_a_boolean_and_preserves_commands():
    cfg = {
        "work": {
            "validate_cmd": "just check",
            "validate": {"merge-main": "just exhaustive"},
        }
    }
    changed = config.set_value("work.validation_bypass", "true", cfg=cfg)
    assert changed["ok"] is True
    assert cfg["work"] == {
        "validate_cmd": "just check",
        "validate": {"merge-main": "just exhaustive"},
        "validation_bypass": True,
    }
    refused = config.set_value("work.validation_bypass", "yes", cfg=cfg)
    assert refused["ok"] is False
    assert config.validate_cmd(cfg, {}, "merge", main_gate=True) == "just exhaustive"
    disabled = config.set_value("work.validation_bypass", "false", cfg=cfg)
    assert disabled["ok"] is True
    assert config.validate_cmd(cfg, {}, "merge", main_gate=True) == "just exhaustive"


@pytest.mark.parametrize("phase", ["submit", "merge", "molecule", "union", "postland"])
def test_main_specific_command_is_preserved_in_bypass_record(monkeypatch, phase):
    cfg = {
        "work": {
            "validate_cmd": "default command",
            "validate": {f"{phase}-main": f"{phase} main command"},
        }
    }
    entry = _entry()
    command = config.validate_cmd(cfg, entry, phase, main_gate=True)
    seen = []
    monkeypatch.setattr(
        validation_bypass,
        "record",
        lambda *a, **kw: seen.append(kw) or SimpleNamespace(status="BYPASSED"),
    )

    assert validation_bypass.maybe_record(cfg, entry, phase=phase, command=command)
    assert seen[0]["command"] == f"{phase} main command"


def test_one_config_set_operation_targets_only_one_hive(monkeypatch):
    first = _entry(False)
    second = {**_entry(False), "repo": "other", "prefix": "other"}
    fleet = {"managed_repos": [first, second]}
    saved = []
    monkeypatch.setattr(config, "load_fleet", lambda: fleet)
    monkeypatch.setattr(config, "save_fleet", lambda value: saved.append(value))
    monkeypatch.setattr(config, "_write_transaction", lambda _scope: nullcontext())

    result = config.set_value(
        "hives.app.work.validation_bypass", "true", scope=config.SCOPE_FLEET
    )

    assert result["ok"] is True
    assert first["work"]["validation_bypass"] is True
    assert second["work"]["validation_bypass"] is False
    assert saved == [fleet]


def test_config_show_hive_row_makes_active_bypass_unmistakable(capsys):
    cfg = {"managed_repos": [_entry(True)]}
    rows = doctor._data_hives(cfg)
    doctor._render_hives(rows)
    output = capsys.readouterr().out
    assert "app" in output
    assert "VALIDATION BYPASSED" in output


@pytest.mark.parametrize(
    "phase",
    ["validation", "check", "submit", "merge", "molecule", "union", "postland", "push-main"],
)
def test_every_validation_phase_bypasses_before_receipt_or_execution(monkeypatch, phase):
    calls = []
    monkeypatch.setattr(worktree_verify, "_branch_sha", lambda *_: "a" * 40)
    monkeypatch.setattr(worktree_verify.validation_ledger, "tree_of", lambda *_: "b" * 40)
    monkeypatch.setattr(
        worktree_verify.validation_bypass,
        "maybe_record",
        lambda *a, **kw: calls.append(kw) or SimpleNamespace(status="BYPASSED"),
    )
    monkeypatch.setattr(
        worktree_verify.validation_admission,
        "host_slot",
        lambda *a, **kw: pytest.fail("bypass requested validation admission"),
    )
    monkeypatch.setattr(
        worktree_verify, "_reuse_verdict_hit", lambda *a, **kw: pytest.fail("reused green")
    )
    monkeypatch.setattr(
        worktree_verify,
        "_prepare_verify_worktree",
        lambda *a, **kw: pytest.fail("created validation checkout"),
    )

    assert (
        worktree_verify.impl_clean_checkout(
            _entry(), "branch", "configured command", cfg={}, reuse=True, bead="app-1", phase=phase
        )
        == 0
    )
    assert calls == [
        {
            "phase": phase,
            "command": "configured command",
            "bead": "app-1",
            "sha": "a" * 40,
            "tree": "b" * 40,
            "branch": "branch",
        }
    ]


def test_non_validation_demo_still_executes_normal_path(monkeypatch):
    class ReachedAdmission(Exception):
        pass

    @contextmanager
    def reached_admission(*_args, **_kwargs):
        raise ReachedAdmission
        yield

    monkeypatch.setattr(worktree_verify, "_branch_sha", lambda *_: "a" * 40)
    monkeypatch.setattr(worktree_verify.validation_ledger, "tree_of", lambda *_: "b" * 40)
    monkeypatch.setattr(
        worktree_verify.validation_bypass,
        "maybe_record",
        lambda *a, **kw: None,
    )
    monkeypatch.setattr(
        worktree_verify.validation_admission,
        "host_slot",
        reached_admission,
    )
    with pytest.raises(ReachedAdmission):
        worktree_verify.impl_clean_checkout(_entry(), "branch", "demo", cfg={}, phase="demo")


def test_bypass_refuses_an_unresolvable_candidate_before_audit(monkeypatch, capsys):
    monkeypatch.setattr(worktree_verify, "_branch_sha", lambda *_: "")
    monkeypatch.setattr(worktree_verify.validation_ledger, "tree_of", lambda *_: "")
    monkeypatch.setattr(
        worktree_verify.validation_bypass,
        "maybe_record",
        lambda *a, **kw: pytest.fail("unresolved candidate was audited as bypassed"),
    )

    assert (
        worktree_verify.impl_clean_checkout(
            _entry(), "missing", "configured command", cfg={}, phase="submit"
        )
        == 1
    )
    assert "refusing validation bypass" in capsys.readouterr().err


def test_durable_bypass_record_is_not_a_run_or_green_receipt(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    recorded = validation_records.record_bypass(
        repo,
        {
            "hive": "app",
            "actor": "operator/alice",
            "bead": "app-1",
            "sha": "a" * 40,
            "tree": "b" * 40,
            "phase": "submit",
            "command": "just check",
            "timestamp": "2026-09-26T00:00:00+00:00",
            "source": "work.validation_bypass=true",
        },
    )
    assert recorded["status"] == "BYPASSED"
    assert validation_records.matching_runs(repo, tree="b" * 40, command_hash="irrelevant") == []
    assert validation_records.is_qualifying_green(recorded) is False
    root = validation_records._validation_root(repo)
    [path] = (root / "bypasses").glob("*.json")
    assert json.loads(path.read_text())["command"] == "just check"
    assert not (root / "verdicts").exists()


def test_bypass_fails_closed_when_audit_cannot_be_written(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(validation_bypass.registry, "hive_dir", lambda _entry: repo)
    monkeypatch.setattr(validation_bypass.identity, "resolve_actor", lambda *a, **kw: "dev/test")
    monkeypatch.setattr(
        validation_bypass.validation_records, "record_bypass", lambda *a, **kw: None
    )

    with pytest.raises(validation_bypass.BypassAuditError):
        validation_bypass.record(
            {},
            _entry(),
            phase="submit",
            command="just check",
            sha="a" * 40,
            tree="b" * 40,
        )


def test_bypass_telemetry_is_a_distinct_result(monkeypatch):
    events = []
    monkeypatch.setattr(
        otel,
        "_instrument",
        lambda *a, **kw: SimpleNamespace(add=lambda value, attrs: events.append((value, attrs))),
    )
    otel.count_validation_bypass({"bh.hive": "app", "bh.work.phase": "merge"})
    assert events == [
        (
            1,
            {
                "bh.validation.result": "bypassed",
                "bh.hive": "app",
                "bh.work.phase": "merge",
            },
        )
    ]
    assert validation_bypass.BYPASSED_EXIT == 0
    assert work_metrics.validation_result(validation_bypass.BYPASSED_EXIT) == "bypassed"


def test_push_main_bypass_never_consults_green_receipts(monkeypatch):
    entry = _entry()
    monkeypatch.setattr(prepush.config, "load", lambda: {})
    monkeypatch.setattr(prepush.registry, "resolve_hive", lambda *_: entry)
    monkeypatch.setattr(prepush.config, "validate_cmd", lambda *a, **kw: "just release-gate")
    monkeypatch.setattr(prepush.validation_ledger, "tree_of", lambda *_: "b" * 40)
    monkeypatch.setattr(
        prepush.validation_ledger,
        "green_verdict",
        lambda *a, **kw: pytest.fail("bypass satisfied itself from green"),
    )
    result = validation_bypass.BypassedValidation(
        status="BYPASSED",
        source="work.validation_bypass=true",
        hive="app",
        actor="operator/alice",
        bead=None,
        sha="a" * 40,
        tree="b" * 40,
        phase="push-main",
        command="just release-gate",
        timestamp="2026-09-26T00:00:00+00:00",
    )
    monkeypatch.setattr(prepush.validation_bypass, "record", lambda *a, **kw: result)
    ok, detail = prepush.check_push_main("a" * 40, hive_id="app", gate_cmd="other")
    assert ok is True
    assert "BYPASSED" in detail
    assert "attested green" not in detail
