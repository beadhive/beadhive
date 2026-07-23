"""The Engine seam (bh-dw3e.5) — `beads:` config + the `bd` adapter.

`BdEngine` is a pure extraction: every method must build the EXACT `bd` command its call site
used to build inline. These tests patch `bd._run` (the same seam `bd.run`/`bd.json` tests use)
so no real `bd` binary is needed, and assert on the recorded command/kwargs.
"""

from __future__ import annotations

from collections import namedtuple

import pytest

from beadhive import bd, config, engine

Completed = namedtuple("Completed", "returncode stdout stderr")


# ---- config: beads: section -------------------------------------------------


def test_beads_in_known_sections():
    assert "beads" in config.KNOWN_SECTIONS


def test_beads_cfg_defaults_to_empty():
    assert config.beads_cfg({}) == {}
    assert config.beads_cfg({"beads": None}) == {}


def test_beads_engine_defaults_to_bd():
    assert config.beads_engine({}) == "bd"
    assert config.beads_engine({"beads": {"engine": "bd"}}) == "bd"


def test_beads_engine_reads_configured_value():
    assert config.beads_engine({"beads": {"engine": "br"}}) == "br"


# ---- get_engine() ------------------------------------------------------------


def test_get_engine_defaults_to_bd_engine():
    got = engine.get_engine({})
    assert isinstance(got, engine.BdEngine)
    assert got.name == "bd"


def test_get_engine_falls_back_to_bd_when_config_missing(monkeypatch):
    def raise_not_found():
        raise FileNotFoundError("no config yet")

    monkeypatch.setattr(config, "load", raise_not_found)
    assert isinstance(engine.get_engine(), engine.BdEngine)


def test_get_engine_rejects_unimplemented_engine():
    with pytest.raises(ValueError, match="br"):
        engine.get_engine({"beads": {"engine": "br"}})


# ---- BdEngine: each method is a pure extraction of its call site's original body -----------


def test_passthrough_matches_bd_run_shape(monkeypatch):
    calls = []
    monkeypatch.setattr(bd, "_run", lambda cmd, **k: calls.append((cmd, k)) or Completed(0, "", ""))

    engine.BdEngine().passthrough(["show", "mr-1"], "/hive", actor="dev/a", capture=True)

    cmd, kwargs = calls[0]
    assert cmd == ["bd", "-C", "/hive", "--actor", "dev/a", "show", "mr-1"]
    assert kwargs == {"check": False, "capture": True, "text_input": None}


def test_passthrough_omits_actor_flag_when_unset(monkeypatch):
    calls = []
    monkeypatch.setattr(bd, "_run", lambda cmd, **k: calls.append(cmd) or Completed(0, "", ""))

    engine.BdEngine().passthrough(["list"], "/hive")

    assert calls[0] == ["bd", "-C", "/hive", "list"]


def test_export_jsonl_matches_hub_sync_shape(monkeypatch):
    calls = []
    monkeypatch.setattr(
        bd, "_run", lambda cmd, **k: calls.append((cmd, k)) or Completed(0, "", "")
    )

    engine.BdEngine().export_jsonl("/hive", "/hive/.beads/issues.jsonl", env={"X": "1"})

    cmd, kwargs = calls[0]
    assert cmd == ["bd", "-C", "/hive", "export", "-o", "/hive/.beads/issues.jsonl"]
    assert kwargs == {"env": {"X": "1"}, "check": False, "capture": True}


def test_import_jsonl_matches_import_labeled_shape(monkeypatch):
    calls = []
    monkeypatch.setattr(
        bd, "_run", lambda cmd, **k: calls.append((cmd, k)) or Completed(0, "", "")
    )

    engine.BdEngine().import_jsonl("/hive", ["--dry-run", "/tmp/x.jsonl"])

    cmd, kwargs = calls[0]
    assert cmd == ["bd", "import", "--dry-run", "/tmp/x.jsonl"]
    assert kwargs == {"check": False, "capture": True, "cwd": "/hive"}


def test_bootstrap_matches_hub_fetch_cache_shape(monkeypatch):
    calls = []
    monkeypatch.setattr(
        bd, "_run", lambda cmd, **k: calls.append((cmd, k)) or Completed(0, "", "")
    )

    engine.BdEngine().bootstrap("/cache", env={"BD_NON_INTERACTIVE": "1"})

    cmd, kwargs = calls[0]
    assert cmd == ["bd", "bootstrap", "--non-interactive"]
    assert kwargs == {"cwd": "/cache", "env": {"BD_NON_INTERACTIVE": "1"}, "check": False}


def test_push_state_commits_then_pushes_matching_report_shape(monkeypatch):
    calls = []
    monkeypatch.setattr(
        bd, "_run", lambda cmd, **k: calls.append((cmd, k)) or Completed(0, "", "")
    )

    result = engine.BdEngine().push_state("/hive", actor="dev/a", message="report: title")

    assert [c for c, _ in calls] == [
        ["bd", "-C", "/hive", "--actor", "dev/a", "dolt", "commit", "-m", "report: title"],
        ["bd", "-C", "/hive", "--actor", "dev/a", "dolt", "push"],
    ]
    assert result.returncode == 0  # the push result, per report.py's original contract


def test_push_state_returns_push_failure_even_if_commit_is_a_noop(monkeypatch):
    """Matches report.py's original: commit's result is never checked (an empty commit isn't a
    failure); only push's exit code is."""

    def fake_run(cmd, **k):
        if cmd[-2:] == ["dolt", "push"]:
            return Completed(1, "", "Error: push failed")
        return Completed(1, "", "Error: commit: dolt commit: nothing to commit")

    monkeypatch.setattr(bd, "_run", fake_run)

    result = engine.BdEngine().push_state("/hive", message="report: title")

    assert result.returncode == 1
    assert "push failed" in result.stderr


def test_pull_state_runs_dolt_pull(monkeypatch):
    calls = []
    monkeypatch.setattr(bd, "_run", lambda cmd, **k: calls.append(cmd) or Completed(0, "", ""))

    engine.BdEngine().pull_state("/hive")

    assert calls[0] == ["bd", "-C", "/hive", "dolt", "pull"]


def test_state_channel_is_the_dolt_data_ref():
    assert engine.BdEngine().state_channel("/hive") == "refs/dolt/data"


# ---- bd.run/bd.json route through the seam (transitively covers work/plan/report/triage) ---


def test_bd_run_routes_through_get_engine(monkeypatch):
    calls = []

    class FakeEngine:
        def passthrough(self, args, cwd, actor="", capture=False, text_input=None):
            calls.append((args, cwd, actor, capture, text_input))
            return Completed(0, "faked", "")

    monkeypatch.setattr(engine, "get_engine", lambda: FakeEngine())

    res = bd.run(["list"], "/hive", actor="dev/a", capture=True)

    assert calls == [(["list"], "/hive", "dev/a", True, None)]
    assert res.stdout == "faked"
