"""Frame lifecycle verbs on an operator host that is also a frame (bh-siluj).

The host's own ``host.yaml`` binds ``hq.sql.runtime`` (it is a frame), which the separate
authority writer refuses. ``bh host admit|cordon|...`` must select the operator plane from
``--operator-settings`` / ``$BH_HQ_OPERATOR_SETTINGS`` exactly as ``bh hq authority`` does.
Fakes only: the authority writer's connection is intercepted before any network I/O.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from beadhive import hq_authority_enforce, hq_control_plane, hq_sql_operator
from beadhive.cli import app
from beadhive.hq_control_plane import SqlControlPlane

BINDING = {
    "host": "127.0.0.1",
    "port": 3306,
    "database": "beadhive_hq",
    "user": "authority_writer",
    "tls_mode": "disabled",
    "credential": {"config_path": "/f/fnox.toml", "profile": "p", "key": "K"},
}
REACHED = "authority writer reached (test stop)"


@pytest.fixture
def frame_host(tmp_path, monkeypatch):
    """This host is a frame: its own plane binds runtime and has no authority writer."""
    frame_plane = SqlControlPlane(
        {
            "reader": {**BINDING, "user": "reader"},
            "runtime": {**BINDING, "user": "frame_xeno"},
            "runtime_backend_identity": "hq",
            "runtime_generation": "g1",
            "runtime_initial_revision": "0" * 32,
            "runtime_floor_path": str(tmp_path / "floor"),
            "runtime_operator_public_key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFixture",
        }
    )
    monkeypatch.setattr(hq_control_plane, "control_plane", lambda *_a, **_k: frame_plane)
    monkeypatch.delenv("BH_HQ_OPERATOR_SETTINGS", raising=False)
    opened = []

    def intercept(self, *, deadline=None):
        opened.append(self.settings)
        # The same gate the real writer applies, then stop before any connection.
        if not self.settings.get("authority_writer") or self.settings.get("runtime"):
            raise hq_sql_operator.SqlOperatorError(
                "separate authority writer capability unavailable"
            )
        raise hq_sql_operator.SqlOperatorError(REACHED)

    monkeypatch.setattr(hq_sql_operator.SqlRuntimeOperator, "_open", intercept)
    settings = tmp_path / "operator.json"
    settings.write_text(
        json.dumps(
            {
                "hq": {
                    "sql": {
                        "reader": {**BINDING, "user": "reader"},
                        "authority_writer": BINDING,
                    }
                }
            }
        )
    )
    hq_authority_enforce.reset_cache()
    yield settings, opened
    hq_authority_enforce.reset_cache()


def _run(*args, env=None):
    return CliRunner().invoke(app, ["host", *args], env=env)


def test_without_operator_settings_the_frame_plane_still_refuses(frame_host):
    _settings, opened = frame_host
    result = _run("admit", "plan", "xeno-mac")
    assert result.exit_code == 1
    assert "separate authority writer capability unavailable" in result.output
    assert opened == []  # refused at the frame plane, before any writer was built


@pytest.mark.parametrize(
    "verb", ["admit", "cordon", "drain", "park", "resume", "quarantine", "frame-retire"]
)
def test_operator_settings_flag_selects_the_authority_writer(frame_host, verb):
    settings, opened = frame_host
    result = _run(verb, "plan", "xeno-mac", "--operator-settings", str(settings))
    assert "separate authority writer capability unavailable" not in result.output
    assert len(opened) == 1
    assert opened[0]["runtime"] is None
    assert opened[0]["authority_writer"]["user"] == "authority_writer"


def test_operator_settings_env_selects_the_authority_writer(frame_host):
    settings, opened = frame_host
    result = _run("admit", "check", "xeno-mac", env={"BH_HQ_OPERATOR_SETTINGS": str(settings)})
    assert "separate authority writer capability unavailable" not in result.output
    assert [item["runtime"] for item in opened] == [None]


def test_emergency_verbs_accept_operator_settings(frame_host):
    settings, opened = frame_host
    for args in (
        ("emergency-admit", "plan", "xeno-mac", "--hive", "bh-app", "--reason", "r"),
        ("emergency-revoke", "plan", "xeno-mac"),
    ):
        result = _run(*args, "--operator-settings", str(settings))
        assert "separate authority writer capability unavailable" not in result.output
    assert len(opened) == 2 and all(item["runtime"] is None for item in opened)


def test_a_settings_file_that_binds_runtime_is_refused(frame_host, tmp_path):
    _settings, opened = frame_host
    bad = tmp_path / "frame-as-operator.json"
    bad.write_text(json.dumps({"hq": {"sql": {"authority_writer": BINDING, "runtime": BINDING}}}))
    result = _run("admit", "plan", "xeno-mac", "--operator-settings", str(bad))
    assert result.exit_code == 1
    assert "hq.sql.runtime must be null" in result.output
    assert opened == []


def test_lifecycle_flags_are_in_the_published_catalog():
    from beadhive.operation_catalog import document

    ops = {o["name"]: o for o in document()["operations"]}
    for verb in (
        "admit",
        "cordon",
        "drain",
        "park",
        "resume",
        "quarantine",
        "frame-retire",
        "emergency-admit",
        "emergency-revoke",
    ):
        names = [p["name"] for p in ops[f"host.{verb}"]["parameters"]]
        assert names[-1] == "operator_settings", verb
