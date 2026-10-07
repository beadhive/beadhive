"""Authority expiry visibility and the operator-settings binding (bh-qfvxz, bh-qtnn4)."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from beadhive import hq_authority_expiry as expiry
from beadhive import hq_operator_settings
from beadhive.cli import app
from beadhive.hq_control_plane import ControlPlaneError, GitControlPlane, SqlControlPlane
from harness import config_tolerance as tolerance

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
            def authority_binding(inner, crossref, policies, *, expires_at):
                head = "cfg" if self._bound else "newer"
                return self._bound, head, None

        return Store()


class BindingSql(FakeSql):
    """Real store tolerance check (bh-3h6al): the latest config head is `current`."""

    def __init__(self, monkeypatch, expires_at, current, *, ancestor=1, runtime=True):
        super().__init__(expires_at, runtime=runtime)
        # The operator signed the projection with the authority's own expiry.
        self._policies = tolerance.signed_policies(
            expires_at=expires_at, now=min(NOW, expires_at - 1)
        )
        self._store = tolerance.binding_store(monkeypatch, current, now=NOW, ancestor=ancestor)
        self._state = {"revision": 3, "expires_at": expires_at}

    def load_state(self, allow_expired=False):
        assert allow_expired
        return "head1", self._state, tolerance.CROSSREF, self._policies

    def config_store(self):
        return self._store


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
    assert line and "2h" in line and "bh hq authority rebind" in line
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
    assert not ok and "bh hq authority rebind" in msg
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
    monkeypatch.setenv("BH_HQ_OPERATOR_SETTINGS", "s.json")
    monkeypatch.setenv("BH_HQ_AUTHORITY_MIN_REMAINING", "6h")
    result = runner.invoke(app, ["hq", "authority", "status"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["expires_in_s"] == 100 and seen
    bad = runner.invoke(app, ["hq", "authority", "check"])
    assert bad.exit_code == 1
    refused = runner.invoke(app, ["hq", "authority", "install"])
    assert refused.exit_code == 1

    monkeypatch.setattr(
        hq_operator_settings, "operator_plane", lambda p: hq_operator_settings.load_settings(p)
    )
    bad_file = _write(tmp_path, {"hq": {"sql": {"authority_writer": BINDING, "runtime": BINDING}}})
    monkeypatch.setenv("BH_HQ_OPERATOR_SETTINGS", str(bad_file))
    result = runner.invoke(app, ["hq", "authority", "renew", "--confirm"])
    assert result.exit_code == 1


def test_release_upgrade_accepts_operator_settings(tmp_path, monkeypatch):
    plane = FakeSql(NOW + 100, runtime=False)
    plane.release_upgrade = lambda *a, **k: {"ok": True}
    monkeypatch.setenv("BH_HQ_OPERATOR_SETTINGS", "s.json")
    monkeypatch.setattr(hq_operator_settings, "operator_plane", lambda p: plane)
    result = CliRunner().invoke(app, ["host", "release-upgrade", "plan", "frame-1"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"ok": True}


def test_operator_settings_ceiling_key_is_carried_not_rejected(tmp_path):
    path = _write(
        tmp_path,
        {
            "hq": {
                "sql": {
                    "reader": READER,
                    "authority_writer": BINDING,
                    "authority_max_duration_s": 7200,
                }
            }
        },
    )
    assert hq_operator_settings.operator_plane(path).authority_max_duration_s == 7200
    bad = _write(
        tmp_path, {"hq": {"sql": {"authority_writer": BINDING, "authority_max_duration_s": -1}}}
    )
    with pytest.raises(ControlPlaneError, match="authority_max_duration_s"):
        hq_operator_settings.load_settings(bad)


def test_renew_resolves_ceiling_from_operator_settings(monkeypatch):
    seen = {}
    plane = FakeSql(NOW + 100, runtime=False)
    plane.authority_max_duration_s = "2h"

    def renew(**kwargs):
        seen.update(kwargs)
        return "newhead"

    plane.renew = renew
    monkeypatch.setenv("BH_HQ_OPERATOR_SETTINGS", "s.json")
    monkeypatch.setattr(hq_operator_settings, "operator_plane", lambda p: plane)
    result = CliRunner().invoke(
        app,
        [
            "hq",
            "authority",
            "renew",
            "--expected-revision",
            "r",
            "--operator-key",
            "k",
            "--duration",
            "1h",
            "--confirm",
        ],
    )
    assert result.exit_code == 0, result.output
    assert seen["ceiling"].seconds == 7200 and "operator settings" in seen["ceiling"].source


def _hint_args(line):
    import shlex

    tail = line.split("Renew: ", 1)[1]
    return shlex.split(tail)


def test_hint_defaults_to_seven_day_ceiling(monkeypatch):
    monkeypatch.delenv("BH_HQ_AUTHORITY_MAX_DURATION", raising=False)
    line = expiry.warning_line(
        expiry.authority_status(FakeSql(NOW + 3600)), plane=FakeSql(NOW + 3600)
    )
    assert "--duration 604800" in line and "--max-duration" not in line
    assert "--duration 86400" not in line


@pytest.mark.parametrize("source", ["settings", "env"])
def test_hint_honours_30d_ceiling_and_round_trips(monkeypatch, source):
    plane = FakeSql(NOW + 3600, runtime=False)
    monkeypatch.delenv("BH_HQ_AUTHORITY_MAX_DURATION", raising=False)
    if source == "settings":
        plane.authority_max_duration_s = "30d"
    else:
        monkeypatch.setenv("BH_HQ_AUTHORITY_MAX_DURATION", "30d")
    line = expiry.warning_line(expiry.authority_status(plane), plane=plane)
    assert "--duration 2592000" in line and "--max-duration 2592000" in line
    words = _hint_args(line)
    seen = {}
    plane.renew = lambda **kw: seen.update(kw) or "newhead"
    monkeypatch.setenv("BH_HQ_OPERATOR_SETTINGS", "s.json")
    monkeypatch.setattr(hq_operator_settings, "operator_plane", lambda p: plane)
    result = CliRunner().invoke(app, ["hq", "authority", "renew", *_renew_flags(words)])
    assert result.exit_code == 0, result.output
    assert seen["ceiling"].seconds == 2592000


def _renew_flags(words):
    flags = words[words.index("rebind") + 1 :]
    flags[flags.index("--operator-key") + 1] = "k"
    flags[flags.index("--expected-revision") + 1] = "r"
    return flags


@pytest.mark.parametrize("raw", ["90", "90s", "30m", "6h", "2d", "1w", "1.5w", "2W"])
def test_expiry_and_ceiling_parsers_agree_on_every_unit(raw):
    from beadhive import hq_authority_ceiling

    assert expiry.parse_duration(raw) == hq_authority_ceiling.parse_duration(raw)
    assert set(expiry.UNITS) == set("smhdw")


def test_weeks_accepted_by_env_and_check_flag(monkeypatch):
    monkeypatch.setenv(expiry.WARN_ENV, "1w")
    assert expiry.warn_within() == 7 * 86400
    monkeypatch.setenv(expiry.MIN_REMAINING_ENV, "1w")
    assert expiry.min_remaining() == 7 * 86400
    monkeypatch.delenv(expiry.MIN_REMAINING_ENV)
    plane = FakeSql(NOW + 2 * 86400)
    monkeypatch.setattr(hq_operator_settings, "operator_plane", lambda p: plane)
    import beadhive.hq_control_plane as cp

    monkeypatch.setattr(cp, "control_plane", lambda: plane)
    result = CliRunner().invoke(app, ["hq", "authority", "check", "--min-remaining", "1w"])
    assert "invalid" not in result.output.lower() and "must look like" not in result.output
    assert result.exit_code == 1 and "FAIL" in result.output


def test_invalid_duration_still_refused():
    for bad in ("1x", "-1w", "w"):
        with pytest.raises(ValueError):
            expiry.parse_duration(bad)


def _doctor_level(plane):
    import beadhive.hq_authority_expiry as m

    orig = m._frame_plane
    m._frame_plane = lambda: plane
    try:
        return m.doctor_data()["level"]
    finally:
        m._frame_plane = orig


@pytest.mark.parametrize("runtime", [True, False], ids=["frame", "operator"])
@pytest.mark.parametrize(
    "head,fleet",
    [
        (tolerance.H0, tolerance.FLEET),
        (tolerance.H1, tolerance.FLEET),
        (tolerance.H1, tolerance.BYPASS_FLEET),
    ],
    ids=["exact", "identical-republish", "validation_bypass"],
)
def test_tolerated_head_reads_bound_in_status_check_and_doctor(monkeypatch, runtime, head, fleet):
    """bh-3h6al: operator tooling agrees with the frames on a tolerated config head."""
    current = tolerance.snapshot(head, fleet, now=NOW)
    plane = BindingSql(monkeypatch, NOW + 3 * 86400, current, runtime=runtime)
    assert expiry.authority_status(plane)["config_bound"] is True
    ok, _status, message = expiry.check(plane)
    assert ok and message.startswith("OK")
    assert _doctor_level(plane) == "ok"


@pytest.mark.parametrize(
    "fleet,host,ancestor",
    [
        (tolerance.POLICY_FLEET, tolerance.HOST, 1),
        (tolerance.FLEET, tolerance.HOST + "label: x\n", 1),
        (tolerance.BYPASS_FLEET, tolerance.HOST, 0),
    ],
    ids=["frame_policy", "host-manifest", "non-descendant"],
)
def test_enforced_edit_still_reads_unbound_with_existing_messages(
    monkeypatch, fleet, host, ancestor
):
    current = tolerance.snapshot(tolerance.H1, fleet, host, now=NOW)
    plane = BindingSql(monkeypatch, NOW + 3 * 86400, current, ancestor=ancestor)
    status = expiry.authority_status(plane)
    assert status["config_bound"] is False
    ok, _status, message = expiry.check(plane)
    assert not ok and message == "FAIL: authority is not bound to the latest config head"
    assert _doctor_level(plane) == "fail"


def test_expired_authority_binding_is_judged_separately_from_expiry(monkeypatch):
    current = tolerance.snapshot(tolerance.H1, tolerance.BYPASS_FLEET, now=NOW)
    plane = BindingSql(monkeypatch, NOW - 60, current)
    status = expiry.authority_status(plane)
    assert status["config_bound"] is True and status["authority_ready"] is False
    ok, _status, message = expiry.check(plane)
    assert not ok and message.startswith("FAIL: HQ authority expired")


def test_status_result_shape_is_unchanged(monkeypatch):
    current = tolerance.snapshot(tolerance.H1, tolerance.BYPASS_FLEET, now=NOW)
    status = expiry.authority_status(BindingSql(monkeypatch, NOW + 3600, current))
    assert set(status) == {
        "revision",
        "authority_revision",
        "state",
        "authority_ready",
        "expires_at",
        "expires_in_s",
        "expires_in",
        "config_bound",
        "expiring_soon",
        "warn_within_s",
        "expires_never",
        "mode",
    }


def test_unreadable_binding_is_not_bound(monkeypatch):
    plane = FakeSql(NOW + 3 * 86400)

    class Broken:
        def authority_binding(self, *args, **kwargs):
            raise RuntimeError("config reader down")

    monkeypatch.setattr(plane, "config_store", lambda: Broken())
    assert expiry.authority_status(plane)["config_bound"] is False


@pytest.mark.parametrize(
    "fleet,exit_code,bound",
    [(tolerance.BYPASS_FLEET, 0, True), (tolerance.POLICY_FLEET, 1, False)],
    ids=["tolerated", "frame_policy"],
)
def test_cli_status_and_check_agree_with_frames(monkeypatch, fleet, exit_code, bound):
    current = tolerance.snapshot(tolerance.H1, fleet, now=NOW)
    plane = BindingSql(monkeypatch, NOW + 3 * 86400, current, runtime=False)
    monkeypatch.setattr(hq_operator_settings, "operator_plane", lambda path: plane)
    monkeypatch.setenv("BH_HQ_OPERATOR_SETTINGS", "s.json")
    monkeypatch.delenv(expiry.MIN_REMAINING_ENV, raising=False)
    runner = CliRunner()
    status = runner.invoke(app, ["hq", "authority", "status"])
    assert status.exit_code == 0, status.output
    assert json.loads(status.stdout)["config_bound"] is bound
    checked = runner.invoke(app, ["hq", "authority", "check"])
    assert checked.exit_code == exit_code, checked.output


# ---- non-expiring authority and mode reporting (bh-oguxa) ----------------------------------

NEVER = 4102444800


def _doctor(plane):
    orig = expiry._frame_plane
    expiry._frame_plane = lambda: plane
    try:
        return expiry.doctor_data(now=NOW)
    finally:
        expiry._frame_plane = orig


def test_sentinel_authority_is_quiet_on_every_surface(capsys):
    plane = FakeSql(NEVER)
    status = expiry.authority_status(plane)
    assert status["expires_never"] is True and status["expires_in"] == "never"
    assert status["expiring_soon"] is False and status["authority_ready"] is True
    assert expiry.warn_if_expiring(plane) is None
    assert capsys.readouterr().err == ""
    for floor in (0, 6 * 3600, 10**12):
        ok, _, msg = expiry.check(plane, min_remaining=floor)
        assert ok and "WARN" not in msg and "FAIL" not in msg and "rebind" not in msg
    # a huge lead time must not make it "expiring"
    assert expiry.authority_status(plane, lead=10**12)["expiring_soon"] is False
    doc = _doctor(plane)
    assert doc["level"] == "ok" and "never" in doc["detail"]
    assert "rebind" not in doc["detail"]


def test_sentinel_authority_still_requires_config_binding():
    assert not expiry.check(FakeSql(NEVER, bound=False))[0]


def test_expiring_authority_still_warns_with_rebind_hint():
    plane = FakeSql(NOW + 3600)
    status = expiry.authority_status(plane)
    assert status["expires_never"] is False
    assert "bh hq authority rebind" in expiry.warning_line(status, plane=plane)
    assert _doctor(plane)["level"] == "warn"


def test_trusted_mode_expiry_is_not_enforced(monkeypatch, capsys):
    monkeypatch.setenv("BH_HQ_AUTHORITY_MODE", "trusted")
    plane = FakeSql(NOW + 3600)
    assert expiry.authority_status(plane)["mode"] == "trusted"
    assert expiry.warn_if_expiring(plane) is None
    doc = _doctor(plane)
    assert doc["level"] == "ok" and "not enforced (trusted)" in doc["detail"]
    assert doc["mode"] == "trusted"
    monkeypatch.setenv("BH_HQ_AUTHORITY_MODE", "signed")
    assert expiry.authority_status(plane)["mode"] == "signed"
