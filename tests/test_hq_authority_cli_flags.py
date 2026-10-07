"""Authority CLI flags in wire release 2.5.0 (bh-16347.2): fakes only, never a live HQ."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from beadhive import hq_authority_cli, hq_operator_settings
from beadhive import hq_authority_expiry as expiry
from beadhive.cli import app

DAY = 86400


class FakePlane:
    authority_max_duration_s = None

    def __init__(self):
        self.renewed = []

    def renew(self, *, expected, operator_key, duration, ceiling):
        self.renewed.append((duration, ceiling))
        from beadhive import hq_authority_ceiling as c

        c.signed_expiry(0, duration, ceiling)
        return "rev1"


@pytest.fixture
def plane(monkeypatch):
    fake = FakePlane()
    seen = []

    def select(operator_settings=None):
        seen.append(operator_settings)
        return fake

    monkeypatch.setattr(hq_operator_settings, "select_plane", select)
    monkeypatch.delenv("BH_HQ_AUTHORITY_MAX_DURATION", raising=False)
    monkeypatch.delenv("BH_HQ_OPERATOR_SETTINGS", raising=False)
    fake.seen = seen
    return fake


def _renew(*extra):
    return CliRunner().invoke(
        app,
        [
            "hq",
            "authority",
            "renew",
            "--confirm",
            "--operator-key",
            "k",
            "--duration",
            "10d",
            *extra,
        ],
    )


def test_operator_settings_flag_reaches_select_plane(plane):
    _renew("--operator-settings", "s.json", "--max-duration", "10d")
    assert plane.seen == ["s.json"]


def test_renew_without_duration_signs_no_expiry(plane):
    result = CliRunner().invoke(
        app, ["hq", "authority", "renew", "--confirm", "--operator-key", "k"]
    )
    assert result.exit_code == 0, result.output
    assert plane.renewed[-1][0] is None
    assert plane.renewed[-1][1].source.startswith("built-in default")


def test_max_duration_flag_raises_ceiling(plane, monkeypatch):
    assert _renew().exit_code == 0  # default ceiling: unlimited
    assert _renew("--max-duration", "7d").exit_code == 1  # 10d > explicit 7d
    result = _renew("--max-duration", "30d")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"revision": "rev1"}
    assert plane.renewed[-1][1].source == "--max-duration"


def test_max_duration_flag_beats_env(plane, monkeypatch):
    monkeypatch.setenv("BH_HQ_AUTHORITY_MAX_DURATION", "1d")
    assert _renew("--max-duration", "30d").exit_code == 0
    assert _renew().exit_code == 1  # env still honoured


@pytest.mark.parametrize("bad", ["0", "-5", "abc"])
def test_invalid_max_duration_refused_not_clamped(plane, bad):
    result = _renew("--max-duration", bad)
    assert result.exit_code == 1
    assert "authority refused" in result.output
    assert not plane.renewed


def test_install_refuses_operator_settings_flag(plane):
    result = CliRunner().invoke(
        app, ["hq", "authority", "install", "--operator-settings", "s.json", "--confirm"]
    )
    assert result.exit_code == 1


def test_check_min_remaining_flag_overrides_env(plane, monkeypatch):
    seen = []

    def fake_check(_plane, *, min_remaining=0.0, now=None):
        seen.append(min_remaining)
        return True, {"ok": True}, "ok"

    monkeypatch.setattr(expiry, "check", fake_check)
    monkeypatch.setenv(expiry.MIN_REMAINING_ENV, "1h")
    run = lambda *a: CliRunner().invoke(app, ["hq", "authority", "check", *a])  # noqa: E731
    assert run().exit_code == 0
    assert run("--min-remaining", "6h").exit_code == 0
    assert seen == [3600.0, 6 * 3600.0]
    assert run("--min-remaining", "bogus").exit_code == 1


def test_flags_are_in_the_published_catalog():
    from beadhive.operation_catalog import document

    ops = {o["name"]: o for o in document()["operations"]}
    auth = [p["name"] for p in ops["hq.authority"]["parameters"]]
    assert {"operator_settings", "max_duration", "min_remaining"} <= set(auth)
    rel = [p["name"] for p in ops["host.release-upgrade"]["parameters"]]
    assert "operator_settings" in rel
    assert hq_authority_cli.authority_cmd
