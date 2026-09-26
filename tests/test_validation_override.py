from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from beadhive import otel, validation_bypass, validation_records


def _entry():
    return {
        "provider": "github",
        "org": "acme",
        "repo": "app",
        "prefix": "app",
        "kind": "personal",
    }


def _bound(**changes):
    values = {
        "actor": "Alice Operator",
        "bead": "app-1",
        "phase": "submit",
        "reason": "gate is broken during recovery",
        "sha": "a" * 40,
        "tree": "b" * 40,
    }
    values.update(changes)
    return validation_bypass.bind_override(**values)


@pytest.mark.parametrize("actor", ["", "dev/alice", "merge/alice", "ops/alice"])
def test_override_is_supervised_operator_only(actor):
    with pytest.raises(validation_bypass.OverrideRefused, match="operator-only"):
        _bound(actor=actor)


def test_override_requires_reason_exact_candidate_and_supported_phase():
    with pytest.raises(validation_bypass.OverrideRefused, match="non-empty reason"):
        _bound(reason="  ")
    with pytest.raises(validation_bypass.OverrideRefused, match="exact candidate"):
        _bound(sha="")
    with pytest.raises(validation_bypass.OverrideRefused, match="does not support"):
        _bound(phase="push-main")


def test_stale_override_refuses_before_audit_or_bypass(monkeypatch):
    audited = []
    monkeypatch.setattr(
        validation_bypass,
        "record",
        lambda *a, **kw: pytest.fail("stale override wrote a bypass record"),
    )
    with pytest.raises(validation_bypass.OverrideRefused, match="stale validation override"):
        validation_bypass.record_override(
            {},
            _entry(),
            _bound(),
            sha="c" * 40,
            tree="b" * 40,
            command="just check",
            audit=lambda event: audited.append(event) or True,
        )
    assert audited == []


def test_override_writes_bead_audit_and_separate_non_green_record(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    monkeypatch.setattr(validation_bypass.registry, "hive_dir", lambda _entry: repo)
    audited = []

    result = validation_bypass.record_override(
        {},
        _entry(),
        _bound(),
        sha="a" * 40,
        tree="b" * 40,
        command="just check",
        audit=lambda event: audited.append(event) or True,
    )

    assert result.status == "BYPASSED"
    assert result.source == "one-shot-operator-override"
    assert result.reason == "gate is broken during recovery"
    assert audited == [
        {
            "event": "validation_override",
            "status": "BYPASSED",
            "actor": "Alice Operator",
            "reason": "gate is broken during recovery",
            "bead": "app-1",
            "phase": "submit",
            "sha": "a" * 40,
            "tree": "b" * 40,
            "command": "just check",
        }
    ]
    root = validation_records._validation_root(repo)
    [path] = (root / "bypasses").glob("*.json")
    stored = json.loads(path.read_text())
    assert stored["reason"] == "gate is broken during recovery"
    assert stored["actor"] == "Alice Operator"
    assert not (root / "runs").exists()
    assert not (root / "verdicts").exists()
    assert validation_records.is_qualifying_green(stored) is False


def test_override_fails_closed_when_bead_audit_fails(monkeypatch):
    monkeypatch.setattr(
        validation_bypass,
        "record",
        lambda *a, **kw: pytest.fail("missing bead audit still recorded bypass"),
    )
    with pytest.raises(validation_bypass.BypassAuditError, match="bead audit write failed"):
        validation_bypass.record_override(
            {},
            _entry(),
            _bound(),
            sha="a" * 40,
            tree="b" * 40,
            command="just check",
            audit=lambda _event: False,
        )


def test_override_telemetry_event_carries_full_audit(monkeypatch):
    events = []
    span = SimpleNamespace(is_recording=lambda: True, add_event=lambda *args: events.append(args))
    monkeypatch.setattr(otel, "_initialized", True)
    monkeypatch.setattr(otel, "get_current_span", lambda: span)
    payload = {
        "bh.actor": "Alice Operator",
        "bh.validation.bypass.reason": "recovery",
        "bh.validation.sha": "a" * 40,
        "bh.validation.tree": "b" * 40,
        "bh.validation.command": "just check",
        "bh.work.phase": "merge",
    }
    otel.record_validation_bypass_event(payload)
    assert events == [("bh.validation.bypassed", payload)]


def test_bead_event_is_machine_readable_and_attributed():
    calls = []
    bd = SimpleNamespace(
        run=lambda args, main, actor="": calls.append((args, main, actor))
        or SimpleNamespace(returncode=0)
    )
    event = {
        "event": "validation_override",
        "status": "BYPASSED",
        "actor": "Alice Operator",
        "reason": "recovery",
        "bead": "app-1",
        "phase": "molecule",
        "sha": "a" * 40,
        "tree": "b" * 40,
        "command": "just check",
    }
    assert validation_bypass.write_bead_event(bd, "/repo", event) is True
    args, main, actor = calls[0]
    assert args[:3] == ["comments", "add", "app-1"]
    assert json.loads(args[3].removeprefix("bh:validation-override ")) == event
    assert main == "/repo"
    assert actor == "Alice Operator"
