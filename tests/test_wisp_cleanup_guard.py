"""Safety guard for destructive wisp cleanup routed through ``bh bd`` (bh-a74aa.2)."""

from __future__ import annotations

import json
from collections import namedtuple

from beadhive import bd

Completed = namedtuple("Completed", "returncode stdout stderr")


def _wisps(*rows):
    return Completed(0, json.dumps({"count": len(rows), "schema_version": 1, "wisps": rows}), "")


def _allow_host_write(monkeypatch):
    monkeypatch.setattr(bd.guard, "bd_write_refusal", lambda *a, **k: "")


def test_e6_gc_closed_refuses_and_names_every_open_molecule_hive_wide(monkeypatch, capsys):
    """E6 negative: routine GC must not erase completed steps from any in-flight release run."""
    _allow_host_write(monkeypatch)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return _wisps(
            {"id": "rl-wisp-amf", "title": "release 0.12.1", "status": "in_progress"},
            {"id": "pat-wisp-one", "title": "daily patrol", "status": "open"},
            {"id": "old-wisp-done", "title": "finished", "status": "closed"},
        )

    monkeypatch.setattr(bd, "_run", fake_run)

    assert bd._run_one(["mol", "wisp", "gc", "--closed", "--force"], "/hive", {}) == 1

    error = capsys.readouterr().err
    assert "open wisp molecule(s) exist hive-wide" in error
    assert "rl-wisp-amf (release 0.12.1)" in error
    assert "pat-wisp-one (daily patrol)" in error
    assert "old-wisp-done" not in error
    assert [call[0] for call in calls] == [bd._WISP_MOLECULE_QUERY]


def test_e7_squash_refuses_on_an_in_flight_molecule(monkeypatch, capsys):
    """E7 negative: squash must not delete open children and auto-close the release root."""
    _allow_host_write(monkeypatch)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _wisps({"id": "rl-wisp-amf", "title": "release 0.12.1", "status": "open"})

    monkeypatch.setattr(bd, "_run", fake_run)

    assert bd._run_one(["mol", "squash", "rl-wisp-amf"], "/hive", {}) == 1
    assert "rl-wisp-amf" in capsys.readouterr().err
    assert calls == [bd._WISP_MOLECULE_QUERY]


def test_guard_recognizes_leading_bd_global_flags():
    assert bd._is_guarded_wisp_cleanup(
        ["-C", "/hive", "--actor", "ops/a", "mol", "wisp", "gc", "--closed"]
    )
    assert bd._is_guarded_wisp_cleanup(["--actor", "ops/a", "mol", "squash", "x-wisp-1"])
    assert bd._is_guarded_wisp_cleanup(["mol", "wisp", "gc", "--closed=true"])
    assert not bd._is_guarded_wisp_cleanup(["mol", "wisp", "gc", "--closed=false"])


def test_bh_debug_is_an_explicit_operator_override(monkeypatch):
    """The escape hatch bypasses the preflight and forwards the operator's argv byte-for-byte."""
    _allow_host_write(monkeypatch)
    monkeypatch.setenv("BH_DEBUG", "1")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return Completed(23, "", "underlying result")

    monkeypatch.setattr(bd, "_run", fake_run)
    args = ["mol", "squash", "rl-wisp-amf"]

    assert bd._run_one(args, "/hive", {}) == 23
    assert calls == [(["bd", *args], {"check": False, "cwd": "/hive"})]


def test_gc_and_squash_are_unchanged_when_no_wisp_molecule_is_open(monkeypatch):
    """Regression: the common safe case adds only the read preflight, then forwards unchanged."""
    _allow_host_write(monkeypatch)
    commands = []

    def fake_run(cmd, **kwargs):
        commands.append((cmd, kwargs))
        if cmd == bd._WISP_MOLECULE_QUERY:
            return _wisps({"id": "done-wisp", "title": "finished", "status": "closed"})
        return Completed(0, "forwarded", "")

    monkeypatch.setattr(bd, "_run", fake_run)
    gc_args = ["mol", "wisp", "gc", "--closed", "--force"]
    squash_args = ["mol", "squash", "done-wisp"]

    assert bd._run_one(gc_args, "/hive", {}) == 0
    assert bd._run_one(squash_args, "/hive", {}) == 0
    assert commands == [
        (bd._WISP_MOLECULE_QUERY, {"check": False, "capture": True, "cwd": "/hive"}),
        (["bd", *gc_args], {"check": False, "cwd": "/hive"}),
        (bd._WISP_MOLECULE_QUERY, {"check": False, "capture": True, "cwd": "/hive"}),
        (["bd", *squash_args], {"check": False, "cwd": "/hive"}),
    ]


def test_non_closed_gc_modes_are_outside_the_specific_guard(monkeypatch):
    """Age-based GC keeps bd's own live-work protections and is forwarded without this query."""
    _allow_host_write(monkeypatch)
    calls = []
    monkeypatch.setattr(
        bd, "_run", lambda cmd, **kwargs: calls.append((cmd, kwargs)) or Completed(0, "", "")
    )

    args = ["mol", "wisp", "gc", "--age", "24h", "--force"]
    assert bd._run_one(args, "/hive", {}) == 0
    assert calls == [(["bd", *args], {"check": False, "cwd": "/hive"})]


def test_failed_or_malformed_safety_query_fails_closed(monkeypatch, capsys):
    _allow_host_write(monkeypatch)
    results = [
        Completed(2, "", "database unavailable"),
        Completed(0, '{"wisps":"not-a-list"}', ""),
    ]
    monkeypatch.setattr(bd, "_run", lambda *a, **k: results.pop(0))
    args = ["mol", "squash", "rl-wisp-amf"]

    assert bd._run_one(args, "/hive", {}) == 1
    assert "could not verify" in capsys.readouterr().err
    assert bd._run_one(args, "/hive", {}) == 1
    assert "could not parse" in capsys.readouterr().err
