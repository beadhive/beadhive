"""bh-gaytj: credentials for a service-managed shared Dolt server (fake fnox, no real secrets)."""

from __future__ import annotations

import stat

from typer.testing import CliRunner

from beadhive import dolt_health
from beadhive import dolt_server_credentials as dsc

KEY_ID = "AKIA-FAKE-KEY-ID"
SECRET = 'fake"secret\\value'


class _Broker:
    def get(self, reference, *, deadline):
        return {"k": KEY_ID, "s": SECRET}[reference["key"]]


def _cfg(label="host", token=False):
    ref = {"config_path": "/etc/fnox.toml", "profile": "default"}
    profile = {
        "source": "fnox",
        "access_key_id": {**ref, "key": "k"},
        "secret_access_key": {**ref, "key": "s"},
    }
    if token:
        profile["session_token"] = {**ref, "key": "k"}
    return {"dolt": {"server_credentials": label, "remote_credentials": {"host": profile}}}


def _server(pid=10, role="shared"):
    return dolt_health.RunningServer(pid, "/d", True, "", role)


def test_render_is_off_by_default_and_writes_nothing(tmp_path):
    out = tmp_path / "env"
    res = dsc.render({}, out)
    assert not res.ok and not out.exists()


def test_render_writes_0600_and_never_returns_values(tmp_path):
    out = tmp_path / "run" / "env"
    res = dsc.render(_cfg(), out, broker=_Broker())
    assert res.ok and res.names == ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert KEY_ID not in repr(res) and SECRET not in repr(res)
    assert dsc._parse_env_file(out)["AWS_SECRET_ACCESS_KEY"] == SECRET
    assert 'AWS_ACCESS_KEY_ID="AKIA-FAKE-KEY-ID"' in out.read_text()


def test_render_shell_format_round_trips(tmp_path):
    out = tmp_path / "env.sh"
    assert dsc.render(_cfg(), out, fmt="shell", broker=_Broker()).ok
    assert out.read_text().startswith("export ")
    assert dsc._parse_env_file(out)["AWS_SECRET_ACCESS_KEY"] == SECRET


def test_render_fails_closed_without_writing(tmp_path):
    out = tmp_path / "env"
    res = dsc.render(_cfg("absent"), out, broker=_Broker())
    assert not res.ok and "absent" in res.detail and not out.exists()
    assert not dsc.render(_cfg(), out, fmt="bogus").ok


def test_inspect_unconfigured_is_silent():
    assert not dsc.inspect({}).configured


def test_inspect_flags_missing_and_names_only():
    res = dsc.inspect(_cfg(), servers=[_server()], environ_reader=lambda pid: {"PATH": "/x"})
    assert res.servers[0].state == "missing"
    lines = " ".join(dsc.render_inspection(res))
    assert "AWS_SECRET_ACCESS_KEY" in lines and "restart" in lines


def test_inspect_present_and_stale_detection(tmp_path):
    out = tmp_path / "env"
    dsc.render(_cfg(), out, broker=_Broker())
    env = {"AWS_ACCESS_KEY_ID": KEY_ID, "AWS_SECRET_ACCESS_KEY": SECRET}
    ok = dsc.inspect(_cfg(), servers=[_server()], environ_reader=lambda p: env, env_file=out)
    assert ok.servers[0].state == "has-credentials" and not ok.servers[0].stale
    rotated = dict(env, AWS_SECRET_ACCESS_KEY="old")
    old = dsc.inspect(_cfg(), servers=[_server()], environ_reader=lambda p: rotated, env_file=out)
    assert old.servers[0].stale
    text = " ".join(dsc.render_inspection(old))
    assert "old" not in text.replace("rotated", "") and SECRET not in text


def test_inspect_unreadable_and_cache_servers():
    res = dsc.inspect(
        _cfg(), servers=[_server(), _server(11, "cache")], environ_reader=lambda p: None
    )
    assert [s.pid for s in res.servers] == [10] and res.servers[0].state == "unknown"
    none = dsc.inspect(_cfg(), servers=[])
    assert not none.servers and "no shared" in none.detail


def test_session_token_expected_only_when_configured():
    assert "AWS_SESSION_TOKEN" not in dsc.expected_names(_cfg())
    assert "AWS_SESSION_TOKEN" in dsc.expected_names(_cfg(token=True))


def test_read_proc_environ(tmp_path):
    (tmp_path / "7").mkdir()
    (tmp_path / "7" / "environ").write_bytes(b"A=1\0B=x=y\0")
    assert dsc.read_proc_environ(7, tmp_path) == {"A": "1", "B": "x=y"}
    assert dsc.read_proc_environ(8, tmp_path) is None


def test_cli_render_unconfigured_exits_nonzero(tmp_path, monkeypatch):
    from beadhive.dolt_server_credentials import app

    monkeypatch.setattr(dsc, "_load_config", lambda: {})
    res = CliRunner().invoke(app, ["render", "--path", str(tmp_path / "e")])
    assert res.exit_code == 1 and not (tmp_path / "e").exists()


_CHECKSUM_VARS = ("AWS_REQUEST_CHECKSUM_CALCULATION", "AWS_RESPONSE_CHECKSUM_VALIDATION")


def _opt_in(cfg, mode="when_required"):
    cfg["dolt"]["remote_credentials"]["host"]["s3_checksums"] = mode
    return cfg


def test_checksum_env_rendered_only_when_profile_opts_in(tmp_path):
    off, on = tmp_path / "off", tmp_path / "on"
    assert dsc.render(_cfg(), off, broker=_Broker()).ok
    assert not set(_CHECKSUM_VARS) & set(dsc._parse_env_file(off))
    res = dsc.render(_opt_in(_cfg()), on, broker=_Broker())
    assert res.ok and set(_CHECKSUM_VARS) <= set(res.names)
    parsed = dsc._parse_env_file(on)
    assert all(parsed[name] == "when_required" for name in _CHECKSUM_VARS)


def test_checksum_env_reaches_client_resolution_only_when_opted_in():
    from beadhive import dolt_remote_credentials as drc

    kwargs = {"environ": {}, "home": None, "broker": _Broker()}
    plain = drc.resolve_label("host", _cfg(), **kwargs)
    assert not set(_CHECKSUM_VARS) & set(plain.env)
    opted = drc.resolve_label("host", _opt_in(_cfg()), **kwargs)
    assert opted.env["AWS_RESPONSE_CHECKSUM_VALIDATION"] == "when_required"
    assert opted.env["AWS_REQUEST_CHECKSUM_CALCULATION"] == "when_required"
    # sourceless profile (instance-role) also carries it
    cfg = {
        "dolt": {
            "remote_credentials": {
                "r": {"source": "instance-role", "s3_checksums": "when_required"}
            }
        }
    }
    assert drc.resolve_label("r", cfg).env == dict.fromkeys(_CHECKSUM_VARS, "when_required")


def test_status_reports_checksum_requirement_and_missing_on_server():
    cfg = _opt_in(_cfg())
    env = {"AWS_ACCESS_KEY_ID": KEY_ID, "AWS_SECRET_ACCESS_KEY": SECRET}
    res = dsc.inspect(cfg, servers=[_server()], environ_reader=lambda p: env)
    assert res.s3_checksums and res.servers[0].state == "missing"
    assert set(res.servers[0].missing) == set(_CHECKSUM_VARS)
    text = " ".join(dsc.render_inspection(res))
    assert "s3_checksums" in text and SECRET not in text
    assert (
        dsc.inspect(_cfg(), servers=[_server()], environ_reader=lambda p: env).s3_checksums is False
    )
