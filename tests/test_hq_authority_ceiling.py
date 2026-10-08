"""Configurable authority duration ceiling (bh-od8ve); unlimited by default since bh-y929l."""

from __future__ import annotations

import json
import math
import re
from pathlib import Path

import pytest
from tests.test_hq_authority_backend import _prepared_hqs, backend  # noqa: F401

from beadhive import hq_authority_ceiling as c
from beadhive import hq_authority_guard as guard
from beadhive.hq_control_plane import ControlPlaneError, SqlControlPlane

DAY = 86400


def test_default_ceiling_is_unlimited_and_named():
    got = c.resolve_ceiling(env={})
    assert got.seconds == math.inf
    assert "default" in got.source and "unlimited" in got.source
    c.check_duration(3650 * DAY, got)


@pytest.mark.parametrize(
    ("raw", "seconds"),
    [("7d", 7 * DAY), ("36h", 36 * 3600), ("90m", 5400), ("3600", 3600), (45, 45), ("1w", 7 * DAY)],
)
def test_human_duration_parsing(raw, seconds):
    assert c.parse_duration(raw) == seconds


@pytest.mark.parametrize("raw", ["0", "-5", "abc", "nan", "inf", "", True, 0, "7 days"])
def test_bad_durations_refused(raw):
    with pytest.raises(ValueError):
        c.parse_duration(raw)


def test_resolution_order_cli_settings_env_default():
    env = {c.ENV_VAR: "10d"}
    assert c.resolve_ceiling(cli="30d", settings=20 * DAY, env=env).source == "--max-duration"
    assert c.resolve_ceiling(settings=20 * DAY, env=env).seconds == 20 * DAY
    assert "authority_max_duration_s" in c.resolve_ceiling(settings=20 * DAY, env=env).source
    got = c.resolve_ceiling(env=env)
    assert (got.seconds, got.source) == (10 * DAY, f"${c.ENV_VAR}")


def test_invalid_configured_ceiling_refused_with_source():
    with pytest.raises(ValueError, match="BH_HQ_AUTHORITY_MAX_DURATION"):
        c.resolve_ceiling(env={c.ENV_VAR: "-1"})


def test_over_ceiling_error_names_ceiling_and_source():
    with pytest.raises(ValueError, match=rf"exceeds ceiling 604800s .*\${c.ENV_VAR}"):
        c.check_duration(8 * DAY, c.resolve_ceiling(env={c.ENV_VAR: "7d"}))


def test_each_source_admits_a_higher_duration(monkeypatch):
    monkeypatch.setenv(c.ENV_VAR, "7d")
    with pytest.raises(ValueError):
        c.check_duration(30 * DAY)
    c.check_duration(30 * DAY, c.resolve_ceiling(cli="30d"))
    c.check_duration(30 * DAY, c.resolve_ceiling(settings=30 * DAY))
    monkeypatch.setenv(c.ENV_VAR, "30d")
    c.check_duration(30 * DAY)  # no hard maximum
    c.check_duration(3650 * DAY, c.resolve_ceiling(cli="3650d"))


def _sql_plane(published):
    plane = object.__new__(SqlControlPlane)
    plane.settings = {"authority_writer": {"operation_timeout": 5}}
    plane.clock = lambda: 1000.0

    class Operator:
        def load(self, *, deadline):
            return "head", {"revision": 1, "issued_at": 1, "expires_at": 2}, (), {}

        def publish(self, state, **_kw):
            published.append(state)
            return "new"

    plane._operator = lambda: Operator()
    return plane


def test_sql_renew_explicit_duration_and_refusals(monkeypatch):
    monkeypatch.delenv(c.ENV_VAR, raising=False)
    out = []
    plane = _sql_plane(out)
    assert plane.renew(expected="head", operator_key="k", duration=7 * DAY) == "new"
    assert out[-1]["expires_at"] == 1000.0 + 7 * DAY
    plane.renew(expected="head", operator_key="k", duration=30 * DAY)  # default: unlimited
    assert out[-1]["expires_at"] == 1000.0 + 30 * DAY
    with pytest.raises(ControlPlaneError, match=r"exceeds ceiling 604800s.*--max-duration"):
        plane.renew(
            expected="head",
            operator_key="k",
            duration=30 * DAY,
            ceiling=c.resolve_ceiling(cli="7d"),
        )
    ceiling = c.resolve_ceiling(cli="30d")
    plane.renew(expected="head", operator_key="k", duration=30 * DAY, ceiling=ceiling)
    monkeypatch.setenv(c.ENV_VAR, str(30 * DAY))
    plane.renew(expected="head", operator_key="k", duration=30 * DAY)
    with pytest.raises(ControlPlaneError):
        plane.renew(expected="head", operator_key="k", duration=0)


def test_git_renew_explicit_ceiling_and_expiry_fence(backend, monkeypatch):  # noqa: F811
    monkeypatch.setenv(c.ENV_VAR, "7d")
    b = backend
    plane, key = b["plane"], str(b["operator"])
    head = plane._read()[0]
    with pytest.raises(ControlPlaneError, match="exceeds ceiling 604800s"):
        plane.renew(expected=head, operator_key=key, duration=30 * DAY)
    head = plane.renew(expected=head, operator_key=key, duration=7 * DAY)
    _, state, _ = plane._read()
    assert state["expires_at"] - state["issued_at"] == 7 * DAY
    head = plane.renew(
        expected=head, operator_key=key, duration=30 * DAY, ceiling=c.resolve_ceiling(cli="30d")
    )
    _, state, _ = plane._read()
    assert state["expires_at"] - state["issued_at"] == 30 * DAY
    # unchanged verifier: valid inside the 30 d window, fenced after it (simulated clock)
    guard.validate_state(state)
    real = plane.clock
    plane.clock = lambda: state["expires_at"] + 1
    try:
        with pytest.raises(ControlPlaneError, match="expired"):
            plane._read()
    finally:
        plane.clock = real


@pytest.mark.parametrize("days", [7, 30])
def test_unchanged_state_validator_accepts_long_authority(days):
    state = {
        "domain": guard.DOMAIN,
        "generation": "g",
        "revision": 1,
        "issued_at": 1000,
        "expires_at": 1000 + days * DAY,
        "frames": {},
    }
    guard.validate_state(state)


def test_no_hardcoded_86400_authority_cap():
    src = Path(__file__).resolve().parents[1] / "src" / "beadhive"
    for name in ("hq_control_plane.py", "hq_fleet_config.py", "hq_authority_cli.py"):
        assert not re.search(r"\b86400\b", (src / name).read_text()), name
    assert json.dumps(c.AUTHORITY_MAX_DURATION_DEFAULT_S) == "604800"
