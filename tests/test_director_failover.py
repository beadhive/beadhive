"""The director failover loop wired into the host daemon (bh-16347.5).

Ports over the placement library, the successor rule, the loop's resilience, and the daemon
wiring: off by default, opt-in with a configurable interval, refusing git HQ and a missing
director credential before any socket is bound. SQL is faked here;
``test_director_failover_int.py`` runs a whole failover against a throwaway Dolt server.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from beadhive import host_daemon
from beadhive.director_failover import (
    FailoverLoop,
    FailoverRefused,
    SqlFailoverPorts,
    build_loop,
    host_hq_mode,
    observe_sessions,
)
from beadhive.failover_observer import FailoverResult, session_table
from beadhive.hq_sql_placement import PlacementError, Survey
from beadhive.kernel.daemon.contracts.config import DaemonFailoverConfig, HostDaemonConfig


def _authority(frame, epoch=1):
    return {"holder_identity": f"host-{frame}", "epoch": epoch, "config_revision": "desired-1"}


def _state(*frames, cordoned=()):
    return {
        "frames": {
            frame: {
                "active": {
                    "authority": _authority(frame),
                    "state": "active",
                    "cordoned": frame in cordoned,
                }
            }
            for frame in frames
        }
    }


class SessionCursor:
    def __init__(self, registry, ages):
        self.registry, self.ages, self.result = registry, ages, []
        self.statements = []

    def execute(self, sql, params=None):
        self.statements.append((sql, params))
        if "information_schema.tables" in sql:
            self.result = [
                (session_table(p, e),) for p, _f, _h, e in self.registry if p in self.ages
            ]
        elif sql.startswith("SELECT principal,frame_id,holder_identity,epoch"):
            assert params == ("head",)
            self.result = list(self.registry)
        elif sql.startswith("SELECT TIMESTAMPDIFF"):
            principal = next(p for p, *_ in self.registry if session_table(p, 1) in sql)
            age = self.ages[principal]
            self.result = [] if age is None else [(int(age * 1e6),)]
        else:
            raise AssertionError(sql)

    def fetchone(self):
        return self.result[0] if self.result else None

    def fetchall(self):
        return list(self.result)


REGISTRY = [
    ("fa", "frame-a", "host-frame-a", 1),
    ("fb", "frame-b", "host-frame-b", 1),
    ("fc", "frame-c", "host-frame-c", 1),
    ("old", "frame-a", "host-frame-a", 0),  # a retired incarnation is not observed
]


def test_session_table_is_the_adr_name_and_refuses_unsafe_identifiers():
    assert session_table("frame_ab12", 3) == "frame_frame_ab12_3_session"
    for principal, epoch in (("x; DROP", 1), ("A", 1), ("ok", -1), ("ok", True)):
        with pytest.raises(ValueError):
            session_table(principal, epoch)


def test_observe_sessions_reads_only_active_incarnations_with_a_session_table():
    cursor = SessionCursor(REGISTRY, {"fa": 7200.0, "fb": 3.5, "old": 1.0})
    observed = observe_sessions(cursor, "head", _state("frame-a", "frame-b", "frame-c"))
    # frame-c has no session table (M9 not provisioned for it): unobserved, never failed over.
    assert observed == {"frame-a": 7200.0, "frame-b": 3.5, "frame-c": None}
    assert not any("frame_old" in sql for sql, _ in cursor.statements)
    assert observe_sessions(SessionCursor(REGISTRY, {}), "head", {"frames": {}}) == {}


class StubDirector:
    def __init__(self, state, observed, placements):
        self.state, self.observed, self.placements = state, observed, placements
        self.placed = []

    def survey(self, observe=None):
        if isinstance(self.observed, Exception):
            raise self.observed
        return Survey(
            self.state,
            {"ah": {"config_revision": "desired-1"}},
            self.placements,
            {},
            dict(self.observed),
        )

    @staticmethod
    def placeable(state, policies, prefix, frame):
        from beadhive.hq_sql_placement import SqlPlacementDirector

        return SqlPlacementDirector.placeable(state, policies, prefix, frame)

    def place(self, prefix, *, frame_id, expected_revision):
        self.placed.append((prefix, frame_id, expected_revision))
        return SimpleNamespace(prefix=prefix, frame_id=frame_id)


def test_successor_is_the_freshest_placeable_live_frame():
    state = _state("frame-a", "frame-b", "frame-c", "frame-d", cordoned=("frame-d",))
    observed = {"frame-a": 9000.0, "frame-b": 40.0, "frame-c": 5.0, "frame-d": 1.0, "frame-e": 2.0}
    ports = SqlFailoverPorts(StubDirector(state, observed, {}))
    ports.staleness()
    # frame-d is cordoned and frame-e holds no grant: neither is placeable.
    assert ports.successor("ah", "frame-a") == "frame-c"
    assert ports.successor("ah", "frame-c") == "frame-b"
    ports.failover_after = lambda frame: 30.0
    assert ports.successor("ah", "frame-c") is None  # frame-b is itself stale past 30 s
    assert ports.successor("zz", "frame-a") is None  # no signed policy for the hive


def test_a_failed_survey_leaves_nothing_to_place_from():
    ports = SqlFailoverPorts(StubDirector({}, PlacementError("down"), {}))
    ports.survey = Survey({}, {}, {"ah": object()}, {})
    with pytest.raises(PlacementError):
        ports.staleness()
    assert ports.placements() == {} and ports.successor("ah", "frame-a") is None


def test_the_loop_fails_a_dead_frame_over_through_the_director():
    clock = SimpleNamespace(t=0.0)
    placements = {
        "ah": SimpleNamespace(frame_id="frame-a", revision="r1"),
        "bh": SimpleNamespace(frame_id="frame-b", revision="r2"),
    }
    director = StubDirector(
        _state("frame-a", "frame-b"), {"frame-a": 9000.0, "frame-b": 1.0}, placements
    )
    loop = build_loop(
        DaemonFailoverConfig(enabled=True, interval_seconds=25),
        hq_mode="dolt-server",
        director=director,
        clock=lambda: clock.t,
    )
    assert loop.interval == 25.0
    results = []
    for _ in range(200):
        clock.t += 25
        results += loop.tick()
        if results:
            break
    assert [(r.prefix, r.from_frame, r.to_frame, r.outcome) for r in results] == [
        ("ah", "frame-a", "frame-b", "placed")
    ]
    assert director.placed == [("ah", "frame-b", "r1")]
    assert clock.t > 3600  # never before the executor's failover_after


def test_build_loop_is_off_by_default_and_refuses_git_hq_and_missing_settings(monkeypatch):
    monkeypatch.delenv("BH_HQ_OPERATOR_SETTINGS", raising=False)
    assert build_loop(DaemonFailoverConfig()) is None
    enabled = DaemonFailoverConfig(enabled=True)
    with pytest.raises(FailoverRefused, match="dolt-server"):
        build_loop(enabled, hq_mode="git", director=StubDirector({}, {}, {}))
    with pytest.raises(FailoverRefused, match="operator_settings"):
        build_loop(enabled, hq_mode="dolt-server")
    with pytest.raises(FailoverRefused, match="settings refused"):
        build_loop(
            DaemonFailoverConfig(enabled=True, operator_settings="/nonexistent/director.yaml"),
            hq_mode="dolt-server",
        )


def test_failover_config_defaults_off_and_refuses_invalid_values():
    config = HostDaemonConfig()
    assert config.failover.enabled is False and config.failover.interval_seconds == 60.0
    for bad in ({"interval_seconds": 0}, {"interval_seconds": 3601}, {"operator_settings": "x"}):
        with pytest.raises(ValidationError):
            DaemonFailoverConfig(**bad)
    with pytest.raises(ValidationError):
        DaemonFailoverConfig(enabled=True, extra_key=1)


def test_host_hq_mode_follows_the_sql_binding():
    assert host_hq_mode({}) == "git"
    assert host_hq_mode({"hq": {"mode": "git"}}) == "git"
    assert host_hq_mode({"hq": {"mode": "dolt-server"}}) == "dolt-server"
    assert host_hq_mode({"hq": {"sql": {"enabled": True}}}) == "dolt-server"


def test_a_failing_tick_is_counted_and_the_loop_keeps_running():
    calls = {"n": 0}

    def tick():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return [FailoverResult("ah", "frame-a", "frame-b", "placed")]

    loop = FailoverLoop(SimpleNamespace(tick=tick), interval=0.01)
    assert loop.tick() == [] and loop.errors == 1
    assert loop.tick()[0].outcome == "placed" and loop.ticks == 1

    async def run():
        component = loop.component()
        assert component.name == "director-failover"
        async with component.lifespan(None):
            await asyncio.sleep(0.05)
        assert loop._closed

    asyncio.run(run())
    assert calls["n"] > 2


def test_daemon_startup_refuses_a_misconfigured_loop_and_skips_a_disabled_one():
    assert host_daemon._failover_loop(HostDaemonConfig()) is None
    settings = HostDaemonConfig(failover={"enabled": True, "operator_settings": "/x/d.yaml"})
    with pytest.raises(host_daemon.DaemonError, match="HQ is git"):
        host_daemon._failover_loop(settings, {"hq": {"mode": "git"}})
    with pytest.raises(host_daemon.DaemonError, match="settings refused"):
        host_daemon._failover_loop(settings, {"hq": {"mode": "dolt-server"}})
    with pytest.raises(host_daemon.DaemonError, match="HQ is git"):
        host_daemon._failover_loop(settings, {})  # no HQ section: git, never assumed SQL
