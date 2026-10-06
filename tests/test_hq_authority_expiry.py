"""Authority expiry visibility and the --operator-settings binding (bh-qfvxz, bh-qtnn4)."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from beadhive import hq_authority_expiry as expiry
from beadhive import hq_operator_settings
from beadhive.cli import app
from beadhive.hq_control_plane import ControlPlaneError, GitControlPlane, SqlControlPlane

NOW = 1_000_000.0
BINDING = {
    "host": "127.0.0.1",
    "port": 3306,
    "database": "beadhive_hq",
    "user": "authority_writer",
    "tls_mode": "disabled",
    "credential": {"config_path": "/f/fnox.toml", "profile": "p", "key": "K"},
}
READER = {**BINDING, "user": "reader"}


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    expiry._reset_for_tests()
    monkeypatch.delenv(expiry.WARN_ENV, raising=False)


class FakeGit(GitControlPlane):
    def __init__(self, expires_at):
        self.clock = lambda: NOW
        self._state = {"revision": 7, "expires_at": expires_at}

    def _read(self, allow_expired=False):
        return "sha1", self._state, {}


class FakeSql(SqlControlPlane):
    def __init__(self, expires_at, *, bound=True, runtime=True):
        self.clock = lambda: NOW
        self.settings = {"runtime": {} if runtime else None, "authority_writer": {}}
        self._state = {"revision": 3, "expires_at": expires_at}
        self._bound = bound

    def _runtime_authority(self):
        return self

    def _operator(self):
        return self

    def load_state(self, allow_expired=False):
        assert allow_expired
        return "head1", self._state, ("b", "g", "cfg"), {}

    def load(self):
        return self.load_state(allow_expired=True)

    def config_store(self):
        class Store:
            def load_snapshot(inner):
                return type("S", (), {"commit_revision": "cfg" if self._bound else "newer"})()

        return Store()


@pytest.mark.parametrize("factory", [FakeGit, FakeSql])
def test_status_top_level_fields_including_expired(factory):
    live = expiry.authority_status(factory(NOW + 3600))
    assert live["expires_in_s"] == 3600 and live["revision"] and live["config_bound"] is True
    assert live["expiring_soon"] is True  # inside the 24h default lead
    gone = expiry.authority_status(factory(NOW - 120))
    assert gone["expires_in_s"] == -120 and gone["authority_ready"] is False
    assert gone["expires_in"].startswith("expired")
    far = expiry.authority_status(factory(NOW + 3 * 86400))
    assert far["expiring_soon"] is False


def test_status_operator_path_and_unbound():
    status = expiry.authority_status(FakeSql(NOW + 100, runtime=False, bound=False))
    assert status["config_bound"] is False and status["expires_in_s"] == 100


def test_warn_prints_exactly_one_line_inside_lead_only(capsys):
    plane = FakeSql(NOW + 2 * 3600)
    line = expiry.warn_if_expiring(plane)
    assert line and "2h" in line and "bh hq authority renew" in line
    assert expiry.warn_if_expiring(plane) is None
    err = capsys.readouterr().err
    assert err.count("WARN") == 1
    expiry._reset_for_tests()
    assert expiry.warn_if_expiring(FakeSql(NOW + 3 * 86400)) is None
    assert capsys.readouterr().err == ""


def test_warn_never_raises():
    class Broken:
        clock = staticmethod(lambda: NOW)

        def __getattr__(self, name):
            raise RuntimeError("boom")

    assert expiry.warn_if_expiring(Broken()) is None


def test_lead_time_env_is_configurable_and_touches_no_head(monkeypatch):
    plane = FakeSql(NOW + 2 * 3600)
    monkeypatch.setenv(expiry.WARN_ENV, "1h")
    assert expiry.authority_status(plane)["expiring_soon"] is False
    monkeypatch.setenv(expiry.WARN_ENV, "3h")
    assert expiry.authority_status(plane)["expiring_soon"] is True
    # Read-only: the carrier head is whatever the plane reports, untouched by the setting.
    assert plane.load()[0] == "head1"


def test_check_min_remaining():
    ok, _, msg = expiry.check(FakeSql(NOW + 8 * 3600), min_remaining=6 * 3600)
    assert ok and msg.startswith("OK")
    ok, _, msg = expiry.check(FakeSql(NOW + 3600), min_remaining=6 * 3600)
    assert not ok and "bh hq authority renew" in msg
    assert not expiry.check(FakeSql(NOW - 1))[0]
    assert not expiry.check(FakeSql(NOW + 99999, bound=False))[0]


def test_doctor_levels():
    def level(plane):
        import beadhive.hq_authority_expiry as m

        orig = m._frame_plane
        m._frame_plane = lambda: plane
        try:
            return m.doctor_data()["level"]
        finally:
            m._frame_plane = orig

    assert level(FakeSql(NOW + 3600)) == "warn"
    assert level(FakeSql(NOW + 3 * 86400)) == "ok"
    assert level(FakeSql(NOW - 5)) == "fail"
    assert level(FakeSql(NOW + 3 * 86400, bound=False)) == "fail"
    assert level(None) == "skip"


def _write(tmp_path, body):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(body))
    return path


def test_operator_settings_loader_accepts_valid(tmp_path):
    path = _write(
        tmp_path,
        {"hq": {"sql": {"reader": READER, "authority_writer": BINDING, "runtime": None}}},
    )
    settings = hq_operator_settings.load_settings(path)
    assert settings["authority_writer"]["user"] == "authority_writer"
    assert settings["runtime"] is None


def test_operator_settings_refuses_runtime_and_missing_writer(tmp_path):
    runtime = _write(tmp_path, {"hq": {"sql": {"authority_writer": BINDING, "runtime": BINDING}}})
    with pytest.raises(ControlPlaneError, match="hq.sql.runtime"):
        hq_operator_settings.load_settings(runtime)
    missing = _write(tmp_path, {"hq": {"sql": {"reader": READER}}})
    with pytest.raises(ControlPlaneError, match="hq.sql.authority_writer"):
        hq_operator_settings.load_settings(missing)
    with pytest.raises(ControlPlaneError, match="hq.sql"):
        hq_operator_settings.load_settings(_write(tmp_path, {"other": 1}))


def test_cli_operator_settings_threads_through_status_and_refusal(tmp_path, monkeypatch):
    seen = []

    def fake_plane(path):
        seen.append(path)
        return FakeSql(NOW + 100, runtime=False)

    monkeypatch.setattr(hq_operator_settings, "operator_plane", fake_plane)
    runner = CliRunner()
    result = runner.invoke(app, ["hq", "authority", "status", "--operator-settings", "s.json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["expires_in_s"] == 100 and seen
    bad = runner.invoke(
        app, ["hq", "authority", "check", "--operator-settings", "s.json", "--min-remaining", "6h"]
    )
    assert bad.exit_code == 1
    refused = runner.invoke(app, ["hq", "authority", "install", "--operator-settings", "s.json"])
    assert refused.exit_code == 1

    monkeypatch.setattr(
        hq_operator_settings, "operator_plane", lambda p: hq_operator_settings.load_settings(p)
    )
    bad_file = _write(tmp_path, {"hq": {"sql": {"authority_writer": BINDING, "runtime": BINDING}}})
    result = runner.invoke(
        app, ["hq", "authority", "renew", "--operator-settings", str(bad_file), "--confirm"]
    )
    assert result.exit_code == 1


def test_release_upgrade_accepts_operator_settings(tmp_path, monkeypatch):
    plane = FakeSql(NOW + 100, runtime=False)
    plane.release_upgrade = lambda *a, **k: {"ok": True}
    monkeypatch.setattr(hq_operator_settings, "operator_plane", lambda p: plane)
    result = CliRunner().invoke(
        app, ["host", "release-upgrade", "plan", "frame-1", "--operator-settings", "s.json"]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"ok": True}
