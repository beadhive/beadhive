"""The director's failover observer (bh-a94qw; ADR §4, condition 8).

``min(server staleness, observed window) > failover_after`` on the director's own monotonic
clock, with the window reset when HQ is unreachable and on any observation gap longer than
``failover_after / 2``. Clocks are fakes: the shape, not wall time, is under test.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from beadhive.failover_observer import (
    ROLE_FAILOVER_AFTER_S,
    FailoverDirector,
    FailoverMonitor,
    FailoverObserver,
    UnattendedFailoverUnsupported,
    failover_after_for,
    git_beat_staleness,
    sql_session_staleness,
    unattended_failover_supported,
)
from beadhive.hq_sql_placement import PlacementLost

AFTER = 3600.0  # executor default


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Frame:
    """A frame's session row, stamped by the HQ server's clock (server time == fake time)."""

    def __init__(self, clock):
        self.clock, self.renewed = clock, clock()

    def renew(self):
        self.renewed = self.clock()

    def staleness(self):
        return self.clock() - self.renewed


def _poll(observer, clock, frame, *, every, total, hq_up=True):
    decisions = []
    end = clock() + total
    while clock() < end:
        clock.advance(every)
        decisions.append(observer.observe(frame.staleness() if hq_up else None))
    return decisions


def test_code_defaults_and_the_m8c_override_hook():
    assert failover_after_for("executor") == 3600.0
    assert failover_after_for("transient") == 1800.0
    assert failover_after_for("viewer") is None
    assert failover_after_for("something-new") == ROLE_FAILOVER_AFTER_S["executor"]
    assert failover_after_for("transient", override=2700) == 2700.0
    for bad in (0, -1, float("inf"), "60"):
        with pytest.raises(ValueError):
            failover_after_for("executor", override=bad)
    with pytest.raises(ValueError):
        FailoverObserver(0)


def test_a_silent_frame_fails_over_only_after_failover_after_observed():
    clock = Clock()
    frame, observer = Frame(clock), FailoverObserver(AFTER, clock=clock)
    started = clock()
    decision = observer.observe(frame.staleness())  # the window starts at the first look
    while not decision.due:
        assert clock() - started <= AFTER + 60, "never failed over"
        clock.advance(60)
        decision = observer.observe(frame.staleness())
    assert decision.reason == "due" and clock() - started > AFTER
    assert decision.window > AFTER and decision.staleness > AFTER


def test_a_renewing_frame_never_fails_over():
    clock = Clock()
    frame, observer = Frame(clock), FailoverObserver(AFTER, clock=clock)
    for _ in range(500):
        clock.advance(60)
        frame.renew()
        assert not observer.observe(frame.staleness()).due


def test_director_missed_an_outage_and_does_not_fail_over_when_hq_returns():
    """The ADR's motivating case. The director observes a healthy frame, then is cut off from
    HQ (here: it is simply not polling — a director restart, a laptop asleep) for longer
    than failover_after. The frame cannot renew while HQ is down, so on HQ's return its
    server-stamped staleness spans the outage. Without the gap reset the remembered window
    would also span it, and a healthy primary would be failed over on the first poll."""
    clock = Clock()
    frame, observer = Frame(clock), FailoverObserver(AFTER, clock=clock)
    for _ in range(10):
        clock.advance(60)
        frame.renew()
        assert not observer.observe(frame.staleness()).due

    clock.advance(AFTER * 2)  # the outage: no renewals, no observations

    returned = observer.observe(frame.staleness())
    assert returned.staleness > AFTER  # the raw server staleness alone would fail over
    assert not returned.due and returned.reason == "gap-reset" and returned.window == 0
    frame.renew()  # the frame renews on its normal cadence once HQ is back
    clock.advance(60)
    assert not observer.observe(frame.staleness()).due
    # ...and a frame that stays silent after HQ returns is failed over only after a full
    # failover_after of OBSERVED time.
    silent = _poll(observer, clock, frame, every=60, total=AFTER - 120)
    assert not any(d.due for d in silent)
    clock.advance(120)
    assert observer.observe(frame.staleness()).due


def test_hq_unreachable_resets_the_window():
    clock = Clock()
    frame, observer = Frame(clock), FailoverObserver(AFTER, clock=clock)
    _poll(observer, clock, frame, every=60, total=AFTER - 60)  # nearly due
    unreachable = _poll(observer, clock, frame, every=60, total=600, hq_up=False)
    assert all(not d.due and d.reason == "hq-unreachable" for d in unreachable)
    back = observer.observe(frame.staleness())
    assert not back.due and back.reason == "window-start" and back.window == 0


def test_a_gap_just_under_half_keeps_the_window_and_just_over_resets_it():
    clock = Clock()
    frame, observer = Frame(clock), FailoverObserver(AFTER, clock=clock)
    observer.observe(frame.staleness())
    clock.advance(AFTER / 2)
    kept = observer.observe(frame.staleness())
    assert kept.reason == "observing" and kept.window == AFTER / 2
    clock.advance(AFTER / 2 + 1)
    assert observer.observe(frame.staleness()).reason == "gap-reset"


def test_min_rule_a_fresh_session_is_never_due_however_long_observed():
    clock = Clock()
    observer = FailoverObserver(AFTER, clock=clock)
    for _ in range(200):
        clock.advance(60)
        assert not observer.observe(5.0).due  # server says fresh: the window cannot override


def test_monitor_tracks_each_frame_and_resets_all_on_unreachable():
    clock = Clock()
    roles = {"exec": "executor", "laptop": "transient", "view": "viewer"}
    monitor = FailoverMonitor(lambda f: failover_after_for(roles[f]), clock=clock)
    for _ in range(32):
        clock.advance(60)
        decisions = monitor.observe({"exec": clock() - 1000, "laptop": clock() - 1000, "view": 1e9})
    assert decisions["laptop"].due and not decisions["exec"].due
    assert decisions["view"].reason == "never-fails-over"
    assert monitor.observe(None) == {}
    clock.advance(60)
    assert not monitor.observe({"laptop": 1e9})["laptop"].due  # window restarted


def test_staleness_sources():
    class Cursor:
        def __init__(self, row):
            self.row, self.sql = row, None

        def execute(self, sql, params=None):
            self.sql = sql

        def fetchone(self):
            return self.row

    cursor = Cursor((90_500_000,))
    assert sql_session_staleness(cursor, "frame_a_1_session") == 90.5
    assert "UTC_TIMESTAMP(6)" in cursor.sql and "frame_a_1_session" in cursor.sql
    assert sql_session_staleness(Cursor(None), "frame_a_1_session") is None
    with pytest.raises(ValueError):
        sql_session_staleness(cursor, "x; DROP TABLE hq_authority")
    beat = SimpleNamespace(verified=True, age_seconds=42.0)
    assert git_beat_staleness(beat) == 42.0
    assert git_beat_staleness(SimpleNamespace(verified=False, age_seconds=1e9)) is None
    assert git_beat_staleness(SimpleNamespace(status="absent")) is None


def test_unattended_failover_is_dolt_server_only():
    assert unattended_failover_supported("dolt-server")
    assert not unattended_failover_supported("git")
    with pytest.raises(ValueError):
        unattended_failover_supported("sqlite")
    monitor = FailoverMonitor(lambda f: AFTER)
    with pytest.raises(UnattendedFailoverUnsupported, match="refs/bh/lease"):
        FailoverDirector(
            hq_mode="git",
            monitor=monitor,
            staleness=dict,
            placements=dict,
            successor=lambda *a: None,
            place=lambda *a: None,
        )


def _row(frame, revision):
    return SimpleNamespace(frame_id=frame, revision=revision)


def test_director_tick_places_due_hives_once_and_records_losses():
    clock = Clock()
    stale = {"a": 0.0, "b": 0.0}
    placed, outcomes = [], {}

    def place(prefix, frame, expected):
        placed.append((prefix, frame, expected))
        if outcomes.get(prefix) == "lost":
            raise PlacementLost(prefix, expected, "rowcount 0")

    director = FailoverDirector(
        hq_mode="dolt-server",
        monitor=FailoverMonitor(lambda f: AFTER, clock=clock),
        staleness=lambda: dict(stale),
        placements=lambda: {"ah": _row("a", "r1"), "bh": _row("a", "r2"), "ch": _row("b", "r3")},
        successor=lambda prefix, dead: None if prefix == "ch" else "b",
        place=place,
    )
    assert director.tick() == []
    outcomes["bh"] = "lost"
    for _ in range(61):
        clock.advance(60)
        stale["a"] += 60
        results = director.tick()
    assert [(r.prefix, r.outcome) for r in results] == [("ah", "placed"), ("bh", "lost")]
    assert placed == [("ah", "b", "r1"), ("bh", "b", "r2")]  # each CAS issued exactly once


def test_director_tick_treats_an_unreadable_hq_as_unreachable():
    clock = Clock()
    calls = {"n": 0}

    def staleness():
        calls["n"] += 1
        if calls["n"] % 2 == 0:
            raise ConnectionError("HQ down")
        return {"a": 1e9}

    director = FailoverDirector(
        hq_mode="dolt-server",
        monitor=FailoverMonitor(lambda f: AFTER, clock=clock),
        staleness=staleness,
        placements=lambda: {"ah": _row("a", "r1")},
        successor=lambda *a: "b",
        place=lambda *a: pytest.fail("failed over across HQ outages"),
    )
    for _ in range(200):
        clock.advance(600)
        assert director.tick() == []
