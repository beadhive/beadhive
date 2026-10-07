"""Spreading hive primaries across executors (bh-zncqo, M8b): the pure successor rule, the
doctor's lopsided warning, the loop's use of both, and the configurable threshold."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from beadhive import doctor
from beadhive import failover_policy as fp
from beadhive.director_failover import SqlFailoverPorts
from beadhive.hq_sql_placement import Survey
from beadhive.kernel.daemon.contracts.config import DaemonFailoverConfig
from beadhive.placement_spread import (
    DEFAULT_MAX_SPREAD,
    pick_successor,
    primary_holdings,
    spread_report,
    validate_max_spread,
)

_BOUNDS = fp.Bounds(session_ttl_s=300)
FRAMES = ("frame-a", "frame-b", "frame-c", "frame-d")


def _rec(frame, revision="r"):
    return SimpleNamespace(frame_id=frame, revision=revision)


def _state(*frames, cordoned=()):
    return {
        "frames": {
            f: {
                "active": {
                    "authority": {"config_revision": "desired-1"},
                    "state": "active",
                    "cordoned": f in cordoned,
                }
            }
            for f in frames
        }
    }


def test_pick_successor_prefers_fewest_primaries_then_freshest_then_id():
    load = {"a": 3, "b": 1, "c": 1, "d": 0}
    assert pick_successor([("a", 1.0), ("b", 9.0), ("c", 2.0)], load) == "c"  # 1 vs 1: fresher
    assert pick_successor([("a", 1.0), ("b", 9.0), ("c", 2.0), ("d", 99.0)], load) == "d"
    assert pick_successor([("y", 5.0), ("x", 5.0)], {}) == "x"  # all tied: lowest frame id
    assert pick_successor([], load) is None


def test_spreading_four_executors_one_hive_at_a_time():
    """Hives that must move land on distinct executors until each holds one, never piling on."""
    placements = {"h0": _rec("frame-a"), "h1": _rec("frame-a"), "h2": _rec("frame-a")}
    load = {f: len(h) for f, h in primary_holdings(placements, FRAMES).items()}
    assert load == {"frame-a": 3, "frame-b": 0, "frame-c": 0, "frame-d": 0}
    chosen = []
    for _ in range(3):
        target = pick_successor([(f, 1.0) for f in FRAMES[1:]], load)
        chosen.append(target)
        load[target] += 1
    assert sorted(chosen) == ["frame-b", "frame-c", "frame-d"]


class _Director:
    def __init__(self, state, observed, placements):
        self.state, self.observed, self.placements = state, observed, placements
        self.placed = []

    def survey(self, observe=None):
        return Survey(
            self.state,
            {p: {"config_revision": "desired-1"} for p in ("ah", "bh", "ch", "dh", "eh")},
            self.placements,
            {},
            dict(self.observed),
        )

    @staticmethod
    def placeable(state, policies, prefix, frame):
        from beadhive.hq_sql_placement import SqlPlacementDirector

        return SqlPlacementDirector.placeable(state, policies, prefix, frame)

    def place(self, prefix, *, frame_id, expected_revision, cause):
        self.placed.append((prefix, frame_id))
        return SimpleNamespace(prefix=prefix, frame_id=frame_id)


def test_failover_successor_is_the_least_loaded_and_counts_this_ticks_moves():
    placements = {
        "ah": _rec("frame-a"),
        "bh": _rec("frame-a"),
        "ch": _rec("frame-b"),
        "dh": _rec("frame-b"),
        "eh": _rec("frame-c"),
    }
    observed = {"frame-a": 9000.0, "frame-b": 1.0, "frame-c": 50.0, "frame-d": 5.0}
    ports = SqlFailoverPorts(_Director(_state(*FRAMES), observed, placements))
    ports.staleness()
    # frame-d holds nothing: it wins over fresher, busier frame-b.
    assert ports.successor("ah", "frame-a") == "frame-d"
    ports.place("ah", "frame-d", "r")
    # frame-d now holds 1, frame-c holds 1: ties break on freshness (frame-d at 5 s).
    assert ports.successor("bh", "frame-a") == "frame-d"
    ports.place("bh", "frame-d", "r")
    # frame-d holds 2, frame-b 2, frame-c 1: the next hive goes to frame-c.
    assert ports.successor("bh", "frame-a") == "frame-c"
    ports.staleness()  # a fresh survey resets the in-tick overlay
    assert ports.moved == {}


def test_doctor_names_hives_and_executors_when_lopsided():
    placements = {f"h{i}": _rec("frame-a") for i in range(4)}
    placements["h4"] = _rec("frame-b")
    holdings = primary_holdings(placements, FRAMES)
    report = spread_report(holdings, DEFAULT_MAX_SPREAD)
    assert report.lopsided and report.spread == 4
    text = report.describe()
    assert "frame-a holds 4 (h0, h1, h2, h3)" in text and "frame-c holds 1" not in text
    assert "frame-d holds 0 (none)" in text and "max_primary_spread" in text
    assert not spread_report(holdings, 4).lopsided  # at the threshold: quiet
    assert spread_report(holdings, 4).describe() == ""
    assert not spread_report({}, 1).lopsided


@pytest.mark.parametrize("bad", [0, -1, 1.5, "2", True, None])
def test_threshold_is_refused_never_clamped(bad):
    with pytest.raises(ValueError, match="max_primary_spread"):
        validate_max_spread(bad)
    with pytest.raises(ValueError):
        spread_report({}, bad)


def test_config_default_and_validation():
    assert DaemonFailoverConfig().max_primary_spread == DEFAULT_MAX_SPREAD == 2
    assert DaemonFailoverConfig(max_primary_spread=5).max_primary_spread == 5
    for bad in (0, -3, "x"):
        with pytest.raises(ValidationError):
            DaemonFailoverConfig(max_primary_spread=bad)


def _doctor_data(monkeypatch, placements, **failover):
    from beadhive import hq_operator_settings

    director = SimpleNamespace(
        failover_policy=lambda: fp.load_policy([], _BOUNDS),
        survey=lambda: Survey(_state(*FRAMES, cordoned=("frame-d",)), {}, placements, {}),
    )
    monkeypatch.setattr(hq_operator_settings, "placement_director", lambda path=None, **_: director)
    cfg = {"host": {"daemon": {"failover": {"enabled": True, **failover}}}}
    return doctor._data_failover_policy(cfg)


def test_doctor_warns_when_lopsided_and_ignores_cordoned_executors(monkeypatch, capsys):
    placements = {f"h{i}": _rec("frame-a") for i in range(4)}
    data = _doctor_data(monkeypatch, placements)
    spread = data["spread"]
    assert spread["lopsided"] and set(spread["counts"]) == {"frame-a", "frame-b", "frame-c"}
    doctor._render_failover_policy(data)
    out = capsys.readouterr().out
    assert "! hive primaries are lopsided: spread 4 exceeds 2" in out
    assert "frame-a holds 4 (h0, h1, h2, h3)" in out and "frame-b holds 0 (none)" in out

    data = _doctor_data(monkeypatch, placements, max_primary_spread=4)
    doctor._render_failover_policy(data)
    assert "placement spread: frame-a 4, frame-b 0, frame-c 0 (spread 4 <= 4)" in (
        capsys.readouterr().out
    )

    data = _doctor_data(monkeypatch, placements, max_primary_spread=0)
    assert data["spread"]["state"] == "unavailable"
    assert "max_primary_spread must be an integer >= 1" in data["spread"]["detail"]
