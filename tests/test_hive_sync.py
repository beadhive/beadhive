"""Tests for beadhive.hive_sync — `bh hive sync`: bidirectional federation sync with
conflicts-as-data (bh-wty3.5).

The engine is stubbed throughout (`hive_sync.engine.get_engine` → a fake recording calls),
so these tests exercise targeting (HQ skip, HIVE_ID vs --all), the read-only guarantee of
--dry-run, the unknown-is-loud status table, and the paused/failed offending-exit contract —
never a real `bd federation` subprocess.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from beadhive import config, hive_sync
from beadhive.cli import app
from beadhive.engine import FederationPeer, FederationStatus, SyncOutcome

runner = CliRunner()


def _register(repo="myrepo", prefix=None, kind="personal") -> str:
    cfg = config.load()
    cfg.setdefault("managed_repos", []).append(
        {
            "provider": "github",
            "org": "myorg",
            "repo": repo,
            "prefix": prefix or repo,
            "kind": kind,
        }
    )
    config.save(cfg)
    return f"github/myorg/{repo}"


class _StubEngine:
    """Records every federation call; returns canned per-call results."""

    name = "stub"

    def __init__(self, status=None, outcome=None):
        self._status = status if status is not None else FederationStatus(ok=True)
        self._outcome = outcome if outcome is not None else SyncOutcome(ok=True)
        self.status_calls: list[Path] = []
        self.status_timeouts: list[float | None] = []
        self.sync_calls: list[tuple[Path, str | None]] = []

    def federation_status(self, cwd, *, timeout=None):
        self.status_calls.append(Path(cwd))
        self.status_timeouts.append(timeout)
        return self._status

    def sync_state(self, cwd, *, peer=None, strategy=None, timeout=None):
        self.sync_calls.append((Path(cwd), strategy))
        return self._outcome


def _install(monkeypatch, stub: _StubEngine) -> None:
    monkeypatch.setattr(hive_sync.engine, "get_engine", lambda cfg=None: stub)


_REACHABLE_AHEAD = FederationStatus(
    ok=True,
    peers=(FederationPeer(peer="origin", reachable=True, ahead=4, behind=0),),
)

_UNREACHABLE = FederationStatus(
    ok=True,
    peers=(FederationPeer(peer="origin", reachable=False, reach_error="dial tcp: refused"),),
)


# ---------------------------------------------------------------------------
# live sync
# ---------------------------------------------------------------------------


def test_reachable_ahead_hive_syncs_successfully(world, monkeypatch, capsys):
    hive_id = _register()
    stub = _StubEngine(outcome=SyncOutcome(ok=True))
    _install(monkeypatch, stub)

    offending = hive_sync.hive_sync(hive_id=None)

    assert offending == []
    assert stub.sync_calls == [(Path(world.ws_root) / "github" / "myorg" / "myrepo", None)]
    assert f"✓ {hive_id}: synced" in capsys.readouterr().out


def test_strategy_is_forwarded_to_sync_state(world, monkeypatch):
    _register()
    stub = _StubEngine(outcome=SyncOutcome(ok=True))
    _install(monkeypatch, stub)

    hive_sync.hive_sync(hive_id=None, strategy="theirs")

    assert stub.sync_calls[0][1] == "theirs"


def test_paused_with_conflicts_exits_1_and_prints_tables(world, monkeypatch):
    hive_id = _register()
    stub = _StubEngine(
        outcome=SyncOutcome(ok=False, error="conflicts", paused=True, conflicts=("issues", "deps"))
    )
    _install(monkeypatch, stub)

    res = runner.invoke(app, ["hive", "sync", "peers", "--all"])

    assert res.exit_code == 1
    assert f"✗ {hive_id}: sync paused" in res.output
    assert "issues" in res.output
    assert "deps" in res.output
    assert "--strategy ours|theirs" in res.output


def test_failed_sync_exits_1_with_error(world, monkeypatch):
    hive_id = _register()
    stub = _StubEngine(outcome=SyncOutcome(ok=False, error="timeout"))
    _install(monkeypatch, stub)

    res = runner.invoke(app, ["hive", "sync", "peers", "--all"])

    assert res.exit_code == 1
    assert f"✗ {hive_id}: sync failed — timeout" in res.output


def test_live_sync_is_serial_over_all_hives(world, monkeypatch):
    _register(repo="alpha")
    _register(repo="beta")
    stub = _StubEngine(outcome=SyncOutcome(ok=True))
    _install(monkeypatch, stub)

    offending = hive_sync.hive_sync(hive_id=None)

    assert offending == []
    # Deterministic config order — one sync per hive.
    assert [p.name for p, _ in stub.sync_calls] == ["alpha", "beta"]


def test_single_hive_id_targets_only_that_hive(world, monkeypatch):
    _register(repo="alpha")
    _register(repo="beta")
    stub = _StubEngine(outcome=SyncOutcome(ok=True))
    _install(monkeypatch, stub)

    offending = hive_sync.hive_sync(hive_id="alpha")

    assert offending == []
    assert [p.name for p, _ in stub.sync_calls] == ["alpha"]


# ---------------------------------------------------------------------------
# no peer towns: a STATE, not a fault (bh-libi)
# ---------------------------------------------------------------------------


def test_a_hive_with_no_peer_towns_is_reported_and_not_offending(world, monkeypatch):
    """Federation is hive-to-hive. Every hive in a single-town fleet has nobody to federate
    with, so bd's "no federation peers configured" is the NORMAL answer — counting it as a
    failure is what failed step 7 of `bh host provision` and fail-closed the adopt behind it."""
    hive_id = _register()
    stub = _StubEngine(
        outcome=SyncOutcome(ok=False, error="no federation peers configured", no_peers=True)
    )
    _install(monkeypatch, stub)

    res = runner.invoke(app, ["hive", "sync", "peers", "--all"])

    assert res.exit_code == 0
    assert f"• {hive_id}: no federation peers — nothing to sync" in res.output
    assert "✗" not in res.output
    # The skip is NARROW — only bd's no-peers verdict. Any other failure still offends, which
    # `test_failed_sync_exits_1_with_error` above holds.


# ---------------------------------------------------------------------------
# --dry-run: read-only status table
# ---------------------------------------------------------------------------


def test_dry_run_performs_zero_sync_state_calls(world, monkeypatch):
    _register()
    stub = _StubEngine(status=_REACHABLE_AHEAD)
    stub.sync_state = None  # any call would raise TypeError — read-only guarantee
    _install(monkeypatch, stub)

    offending = hive_sync.hive_sync(hive_id=None, dry_run=True)

    assert offending == []
    assert len(stub.status_calls) == 1
    assert stub.status_timeouts == [hive_sync.engine.FEDERATION_TIMEOUT]


@pytest.mark.parametrize(
    ("peer", "failure", "expected"),
    [
        (FederationPeer(peer="origin", reachable=True), "", "equal"),
        (FederationPeer(peer="origin", reachable=True, ahead=2), "", "ahead"),
        (FederationPeer(peer="origin", reachable=True, behind=3), "", "behind"),
        (
            FederationPeer(peer="origin", reachable=True, ahead=2, behind=3),
            "",
            "diverged",
        ),
        (None, "", "unconfigured"),
        (FederationPeer(peer="origin", reachable=True, ahead=-1, behind=-1), "", "incomparable"),
        (FederationPeer(peer="origin", reachable=False, reach_error="offline"), "", "unavailable"),
        (None, "timeout", "unavailable"),
    ],
)
def test_comparison_models_every_non_stale_state(peer, failure, expected):
    now = datetime(2026, 8, 28, 6, tzinfo=UTC)

    got = hive_sync._comparison("github/myorg/myrepo", peer, observed_at=now, failure=failure)

    assert got["comparisonState"] == expected
    assert got["sourceRevision"].startswith("sha256:")
    assert got["observedAt"] == "2026-08-28T06:00:00Z"
    if expected == "equal":
        assert got["ahead"] == 0 and got["behind"] == 0
        assert got["coverage"]["counts"] == "known"
    if expected in {"unavailable", "incomparable", "unconfigured"}:
        assert got["ahead"] is None and got["behind"] is None
        assert got["coverage"]["counts"] == "unknown"


def test_comparison_marks_dated_remote_knowledge_stale():
    now = datetime(2026, 8, 28, 6, tzinfo=UTC)
    peer = FederationPeer(
        peer="origin",
        reachable=True,
        remote_observed_at="2026-08-28T05:54:59Z",
    )

    got = hive_sync._comparison("github/myorg/myrepo", peer, observed_at=now)

    assert got["comparisonState"] == "stale"
    assert got["ahead"] == 0 and got["behind"] == 0
    assert got["remoteObservedAt"] == "2026-08-28T05:54:59Z"
    assert got["coverage"]["state"] == "partial"


def test_json_requires_dry_run_before_any_engine_call(world, monkeypatch):
    _register()
    stub = _StubEngine()
    _install(monkeypatch, stub)

    res = runner.invoke(app, ["hive", "sync", "peers", "--all", "--json"])

    assert res.exit_code == 1
    assert "--json is read-only and requires --dry-run" in res.output
    assert stub.status_calls == [] and stub.sync_calls == []


def test_json_partial_multi_hive_timeout_is_bounded_and_keeps_true_zero(world, monkeypatch):
    _register(repo="alpha")
    _register(repo="beta")
    stub = _StubEngine()

    def status(cwd, *, timeout=None):
        stub.status_calls.append(Path(cwd))
        stub.status_timeouts.append(timeout)
        if Path(cwd).name == "beta":
            return FederationStatus(ok=False, error="timeout")
        return FederationStatus(
            ok=True,
            peers=(FederationPeer(peer="origin", reachable=True, ahead=0, behind=0),),
        )

    stub.federation_status = status
    stub.sync_state = None
    _install(monkeypatch, stub)

    res = runner.invoke(app, ["hive", "sync", "peers", "--all", "--dry-run", "--json"])

    assert res.exit_code == 1
    payload = json.loads(res.stdout)
    assert payload["schema_version"] == 1
    assert payload["networkPolicy"] == {
        "mode": "bounded-fetch",
        "readOnly": True,
        "timeoutSeconds": hive_sync.engine.FEDERATION_TIMEOUT,
        "maxConcurrency": 4,
    }
    assert payload["coverage"]["state"] == "partial"
    equal, unavailable = payload["comparisons"]
    assert (equal["comparisonState"], equal["ahead"], equal["behind"]) == ("equal", 0, 0)
    assert unavailable["comparisonState"] == "unavailable"
    assert unavailable["ahead"] is None and unavailable["behind"] is None
    assert stub.status_timeouts == [
        hive_sync.engine.FEDERATION_TIMEOUT,
        hive_sync.engine.FEDERATION_TIMEOUT,
    ]


def test_dry_run_renders_two_axis_table(world, monkeypatch, capsys):
    hive_id = _register()
    _install(monkeypatch, _StubEngine(status=_REACHABLE_AHEAD))

    hive_sync.hive_sync(hive_id=None, dry_run=True)

    out = capsys.readouterr().out
    header, row = out.splitlines()[0], out.splitlines()[1]
    for col in ("hive", "peer", "reachable", "ahead", "behind", "conflicts"):
        assert col in header
    assert hive_id in row
    assert "origin" in row
    assert "4" in row


def test_dry_run_unreachable_reports_unknown_not_synced_exit_1(world, monkeypatch):
    hive_id = _register()
    stub = _StubEngine(status=_UNREACHABLE)
    _install(monkeypatch, stub)

    res = runner.invoke(app, ["hive", "sync", "peers", "--all", "--dry-run"])

    assert res.exit_code == 1
    assert "unknown (dial tcp: refused)" in res.output
    # Never a fabricated 0/0 for a peer that couldn't be checked.
    assert "0" not in [c.strip() for c in res.output.splitlines()[1].split("  ")]
    assert f"- {hive_id}" in res.output
    assert stub.sync_calls == []


def test_dry_run_status_failure_reports_unknown_exit_1(world, monkeypatch):
    hive_id = _register()
    _install(monkeypatch, _StubEngine(status=FederationStatus(ok=False, error="timeout")))

    res = runner.invoke(app, ["hive", "sync", "peers", "--all", "--dry-run"])

    assert res.exit_code == 1
    assert "unknown (timeout)" in res.output
    assert f"- {hive_id}" in res.output


# ---------------------------------------------------------------------------
# HQ skip + targeting guards
# ---------------------------------------------------------------------------


def test_hq_hive_is_skipped_everywhere(world, monkeypatch):
    _register(repo="normal")
    hq_id = _register(repo="hq", prefix="bh", kind="hq")
    stub = _StubEngine(status=_REACHABLE_AHEAD, outcome=SyncOutcome(ok=True))
    _install(monkeypatch, stub)

    live = runner.invoke(app, ["hive", "sync", "peers", "--all"])
    dry = runner.invoke(app, ["hive", "sync", "peers", "--all", "--dry-run"])

    assert live.exit_code == 0 and dry.exit_code == 0
    assert hq_id not in live.output and hq_id not in dry.output
    assert [p.name for p, _ in stub.sync_calls] == ["normal"]
    assert [p.name for p in stub.status_calls] == ["normal"]


def test_targeting_hq_directly_is_refused(world, monkeypatch):
    _register(repo="hq", prefix="bh", kind="hq")
    stub = _StubEngine()
    _install(monkeypatch, stub)

    res = runner.invoke(app, ["hive", "sync", "peers", "hq"])

    assert res.exit_code == 1
    assert "local-only" in res.output
    assert stub.sync_calls == [] and stub.status_calls == []


def test_requires_exactly_one_of_hive_id_or_all(world):
    neither = runner.invoke(app, ["hive", "sync", "peers"])
    both = runner.invoke(app, ["hive", "sync", "peers", "myrepo", "--all"])

    assert neither.exit_code == 1
    assert both.exit_code == 1
    assert "pass one or more HIVE, or --all" in neither.output


def test_bogus_strategy_is_refused(world):
    res = runner.invoke(app, ["hive", "sync", "peers", "--all", "--strategy", "mine"])

    assert res.exit_code == 1
    assert "ours|theirs" in res.output


def test_help_distinguishes_from_hub_sync(world):
    res = runner.invoke(app, ["hive", "sync", "peers", "--help"])

    assert res.exit_code == 0
    # names the underlying bd verb, not `bh sync` (the hub-hydration verb) — "bd federation" and
    # "sync" can land on either side of a rich-help wrap, so check both rather than one phrase.
    assert "bd federation" in res.output
    assert "federation peer" in res.output
