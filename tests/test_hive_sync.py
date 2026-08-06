"""Tests for beadhive.hive_sync — `bh hive sync`: bidirectional federation sync with
conflicts-as-data (bh-wty3.5).

The engine is stubbed throughout (`hive_sync.engine.get_engine` → a fake recording calls),
so these tests exercise targeting (HQ skip, HIVE_ID vs --all), the read-only guarantee of
--dry-run, the unknown-is-loud status table, and the paused/failed offending-exit contract —
never a real `bd federation` subprocess.
"""

from __future__ import annotations

from pathlib import Path

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
        self.sync_calls: list[tuple[Path, str | None]] = []

    def federation_status(self, cwd, *, timeout=None):
        self.status_calls.append(Path(cwd))
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

    res = runner.invoke(app, ["hive", "sync", "--all"])

    assert res.exit_code == 1
    assert f"✗ {hive_id}: sync paused" in res.output
    assert "issues" in res.output
    assert "deps" in res.output
    assert "--strategy ours|theirs" in res.output


def test_failed_sync_exits_1_with_error(world, monkeypatch):
    hive_id = _register()
    stub = _StubEngine(outcome=SyncOutcome(ok=False, error="timeout"))
    _install(monkeypatch, stub)

    res = runner.invoke(app, ["hive", "sync", "--all"])

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

    res = runner.invoke(app, ["hive", "sync", "--all", "--dry-run"])

    assert res.exit_code == 1
    assert "unknown (dial tcp: refused)" in res.output
    # Never a fabricated 0/0 for a peer that couldn't be checked.
    assert "0" not in [c.strip() for c in res.output.splitlines()[1].split("  ")]
    assert f"- {hive_id}" in res.output
    assert stub.sync_calls == []


def test_dry_run_status_failure_reports_unknown_exit_1(world, monkeypatch):
    hive_id = _register()
    _install(monkeypatch, _StubEngine(status=FederationStatus(ok=False, error="timeout")))

    res = runner.invoke(app, ["hive", "sync", "--all", "--dry-run"])

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

    live = runner.invoke(app, ["hive", "sync", "--all"])
    dry = runner.invoke(app, ["hive", "sync", "--all", "--dry-run"])

    assert live.exit_code == 0 and dry.exit_code == 0
    assert hq_id not in live.output and hq_id not in dry.output
    assert [p.name for p, _ in stub.sync_calls] == ["normal"]
    assert [p.name for p in stub.status_calls] == ["normal"]


def test_targeting_hq_directly_is_refused(world, monkeypatch):
    _register(repo="hq", prefix="bh", kind="hq")
    stub = _StubEngine()
    _install(monkeypatch, stub)

    res = runner.invoke(app, ["hive", "sync", "hq"])

    assert res.exit_code == 1
    assert "local-only" in res.output
    assert stub.sync_calls == [] and stub.status_calls == []


def test_requires_exactly_one_of_hive_id_or_all(world):
    neither = runner.invoke(app, ["hive", "sync"])
    both = runner.invoke(app, ["hive", "sync", "myrepo", "--all"])

    assert neither.exit_code == 1
    assert both.exit_code == 1
    assert "exactly one of HIVE_ID or --all" in neither.output


def test_bogus_strategy_is_refused(world):
    res = runner.invoke(app, ["hive", "sync", "--all", "--strategy", "mine"])

    assert res.exit_code == 1
    assert "ours|theirs" in res.output


def test_help_distinguishes_from_hub_sync(world):
    res = runner.invoke(app, ["hive", "sync", "--help"])

    assert res.exit_code == 0
    assert "bh sync" in res.output  # the hub-hydration verb is named explicitly
