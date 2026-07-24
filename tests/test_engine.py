"""The Engine seam (bh-dw3e.5) — `beads:` config + the `bd` adapter.

`BdEngine` is a pure extraction: every method must build the EXACT `bd` command its call site
used to build inline. These tests patch `bd._run` (the same seam `bd.run`/`bd.json` tests use)
so no real `bd` binary is needed, and assert on the recorded command/kwargs.
"""

from __future__ import annotations

import json
import subprocess
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


# ---- BdEngine.federation_status — parses `bd federation status --json` defensively --------
# JSON shapes below verified against a live bd (2026-07): a real file:// peer, an unreachable
# peer, and the no-peer project.


def _status_json(peers, pending=0):
    return json.dumps({"peers": peers, "pendingChanges": pending, "schema_version": 1})


REACHABLE_PEER = {
    "ReachError": "",
    "Reachable": True,
    "Status": {
        "HasConflicts": False,
        "LastSync": "0001-01-01T00:00:00Z",
        "LocalAhead": 2,
        "LocalBehind": 1,
        "Peer": "hub",
    },
    "URL": "file:///towns/hub",
}

UNREACHABLE_PEER = {
    "ReachError": "fetch from ghost: no such host",
    "Reachable": False,
    "Status": {
        "HasConflicts": False,
        "LastSync": "0001-01-01T00:00:00Z",
        "LocalAhead": -1,
        "LocalBehind": -1,
        "Peer": "ghost",
    },
    "URL": "https://nonexistent.invalid:3306/beads",
}


def test_federation_status_builds_command_and_parses_verified_shape(monkeypatch):
    calls = []
    payload = _status_json([REACHABLE_PEER], 3)
    monkeypatch.setattr(
        bd, "_run", lambda cmd, **k: calls.append((cmd, k)) or Completed(0, payload, "")
    )

    got = engine.BdEngine().federation_status("/hive")

    cmd, kwargs = calls[0]
    assert cmd == ["bd", "-C", "/hive", "federation", "status", "--json"]
    assert kwargs == {"check": False, "capture": True, "timeout": engine.FEDERATION_TIMEOUT}
    assert got == engine.FederationStatus(
        ok=True,
        pending_changes=3,
        peers=(
            engine.FederationPeer(
                peer="hub", url="file:///towns/hub", reachable=True, ahead=2, behind=1
            ),
        ),
    )


def test_federation_status_timeout_is_not_ok(monkeypatch):
    def raise_timeout(cmd, **k):
        raise subprocess.TimeoutExpired(cmd, k.get("timeout"))

    monkeypatch.setattr(bd, "_run", raise_timeout)

    got = engine.BdEngine().federation_status("/hive", timeout=0.1)

    assert got == engine.FederationStatus(ok=False, error="timeout")


def test_federation_status_unreachable_peer_keeps_reach_error_and_unknown_counts(monkeypatch):
    """An unreachable peer must never read as in-sync: reachable stays False, ReachError is
    surfaced, and bd's -1/unknown counts are preserved (not coerced to 0/0). A peer entry
    with no Status at all parses to safe defaults instead of crashing."""
    payload = _status_json([UNREACHABLE_PEER, {"Reachable": False}])
    monkeypatch.setattr(bd, "_run", lambda cmd, **k: Completed(0, payload, ""))

    got = engine.BdEngine().federation_status("/hive")

    assert got.ok
    ghost, bare = got.peers
    assert not ghost.reachable
    assert ghost.reach_error == "fetch from ghost: no such host"
    assert (ghost.ahead, ghost.behind) == (-1, -1)
    assert bare == engine.FederationPeer(peer="", reachable=False)


def test_federation_status_malformed_json_is_not_ok(monkeypatch):
    monkeypatch.setattr(bd, "_run", lambda cmd, **k: Completed(0, "not json {", ""))

    got = engine.BdEngine().federation_status("/hive")

    assert got == engine.FederationStatus(ok=False, error="parse-error")


def test_federation_status_nonzero_exit_is_not_ok(monkeypatch):
    monkeypatch.setattr(
        bd, "_run", lambda cmd, **k: Completed(1, "", "Error: no beads project found\n")
    )

    got = engine.BdEngine().federation_status("/hive")

    assert not got.ok
    assert "no beads project" in got.error


# ---- BdEngine.sync_state — parses `bd federation sync --json` defensively -----------------


def _sync_result(**overrides):
    result = {
        "Conflicts": None,
        "ConflictsResolved": False,
        "Fetched": True,
        "Merged": False,
        "Peer": "hub",
        "PulledCommits": 0,
        "PushError": None,
        "Pushed": False,
        "PushedCommits": 0,
    }
    result.update(overrides)
    return result


def test_sync_state_builds_command_with_peer_and_strategy(monkeypatch):
    calls = []
    payload = json.dumps({"peers": ["hub"], "results": [_sync_result()], "schema_version": 1})
    monkeypatch.setattr(
        bd, "_run", lambda cmd, **k: calls.append((cmd, k)) or Completed(0, payload, "")
    )

    got = engine.BdEngine().sync_state("/hive", peer="hub", strategy="theirs")

    cmd, kwargs = calls[0]
    assert cmd == ["bd", "-C", "/hive", "federation", "sync"] + [
        "--peer", "hub", "--strategy", "theirs", "--json",
    ]
    assert kwargs == {"check": False, "capture": True, "timeout": engine.FEDERATION_TIMEOUT * 2}
    assert got == engine.SyncOutcome(ok=True)


def test_sync_state_clean_sync_is_ok_and_not_paused(monkeypatch):
    payload = json.dumps({"peers": ["hub"], "results": [_sync_result()], "schema_version": 1})
    monkeypatch.setattr(bd, "_run", lambda cmd, **k: Completed(0, payload, ""))

    assert engine.BdEngine().sync_state("/hive") == engine.SyncOutcome(ok=True)


def test_sync_state_conflicts_without_strategy_pause_with_tables(monkeypatch):
    payload = json.dumps(
        {
            "peers": ["hub"],
            "results": [_sync_result(Conflicts=["issues", "dependencies"], Merged=False)],
            "schema_version": 1,
        }
    )
    monkeypatch.setattr(bd, "_run", lambda cmd, **k: Completed(1, payload, ""))

    got = engine.BdEngine().sync_state("/hive")

    assert got == engine.SyncOutcome(
        ok=False, error="conflicts", paused=True, conflicts=("issues", "dependencies")
    )


def test_sync_state_conflicts_with_strategy_do_not_pause(monkeypatch):
    payload = json.dumps(
        {
            "peers": ["hub"],
            "results": [_sync_result(Conflicts=["issues"], ConflictsResolved=True, Merged=True)],
            "schema_version": 1,
        }
    )
    monkeypatch.setattr(bd, "_run", lambda cmd, **k: Completed(0, payload, ""))

    got = engine.BdEngine().sync_state("/hive", strategy="ours")

    assert got == engine.SyncOutcome(ok=True, conflicts=("issues",))


def test_sync_state_timeout_is_not_ok(monkeypatch):
    def raise_timeout(cmd, **k):
        raise subprocess.TimeoutExpired(cmd, k.get("timeout"))

    monkeypatch.setattr(bd, "_run", raise_timeout)

    assert engine.BdEngine().sync_state("/hive") == engine.SyncOutcome(ok=False, error="timeout")


def test_sync_state_error_json_surfaces_bd_error(monkeypatch):
    payload = json.dumps({"error": "no federation peers configured", "schema_version": 1})
    monkeypatch.setattr(bd, "_run", lambda cmd, **k: Completed(1, payload, ""))

    got = engine.BdEngine().sync_state("/hive")

    assert got == engine.SyncOutcome(ok=False, error="no federation peers configured")


def test_sync_state_malformed_output_falls_back_to_stderr_tail(monkeypatch):
    monkeypatch.setattr(
        bd, "_run", lambda cmd, **k: Completed(1, "garbage", "panic: dolt exploded\n")
    )

    got = engine.BdEngine().sync_state("/hive")

    assert got == engine.SyncOutcome(ok=False, error="panic: dolt exploded")


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
