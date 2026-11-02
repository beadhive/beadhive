"""``bh plugin beadlog-xtdb status`` and ``doctor``: offline by default, no secret rendered.

The CLI is driven through its Typer app with the config seam replaced; the password port and the
package's doctor probes are replaced too, so no test reads fnox, opens a socket or needs a node.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from typer.testing import CliRunner

from beadhive import beadlog_xtdb_plugin as cli
from beadhive.plugin_runtime_catalog import PLUGIN_CLI_RUNTIME_CATALOG

SECRET = "pw-" + "Kx7" * 8
REF = {"config_path": "/etc/fnox/fnox.toml", "profile": "xtdb", "key": "XTDB_PASSWORD"}
ON = {"enabled": True, "data_dir": "/data/xtdb", "credential": REF}


def use_config(monkeypatch, cfg: dict) -> None:
    monkeypatch.setattr(cli, "_load_config", lambda: cfg)


def invoke(*args: str):
    return CliRunner().invoke(cli.app, list(args))


@pytest.fixture
def probes(monkeypatch):
    """The package's default probes replaced by a healthy loopback fake node."""
    from beadhive_beadlog_xtdb import doctor

    class Connection:
        def close(self):
            pass

    fake = doctor.Probes(
        filesystem_type=lambda path: "ext4",
        path_exists=lambda path: True,
        http_get_status=lambda url, timeout: 200,
        tcp_open=lambda host, port, timeout: False,
        local_addresses=lambda: (),
        connect=lambda dsn, password: (
            Connection() if password else (_ for _ in ()).throw(PermissionError("auth"))
        ),
        server_version=lambda connection: "2.2.0",
    )
    state = {"probes": fake}
    monkeypatch.setattr(doctor, "Probes", lambda: state["probes"])
    monkeypatch.setattr(cli, "_password_source", lambda values: lambda: SECRET)
    return state


def test_the_cli_is_declared_by_the_manifest_and_mounted_from_the_runtime_catalog() -> None:
    entry = next(row for row in PLUGIN_CLI_RUNTIME_CATALOG if row.plugin_id == "beadlog-xtdb")
    assert (entry.module, entry.object_name) == ("beadhive.beadlog_xtdb_plugin", "app")
    from beadhive.kernel.plugins.builtins import builtin_manifest_documents

    manifest = next(
        json.loads(doc.payload)
        for doc in builtin_manifest_documents()
        if json.loads(doc.payload)["plugin_id"] == "beadlog-xtdb"
    )
    declared = {row["command"] for row in manifest["presentation"]["cli"]}
    assert declared == {"plugin beadlog-xtdb status", "plugin beadlog-xtdb doctor"}


def test_status_defaults_to_disabled_and_offline(monkeypatch) -> None:
    use_config(monkeypatch, {})
    result = invoke("status")
    assert result.exit_code == 0
    lines = result.output.splitlines()
    assert lines[0] == "beadlog-xtdb  disabled"
    assert "  package      installed" in lines
    assert "  dsn          host=127.0.0.1 port=5432 user=xtdb dbname=xtdb" in lines
    assert "  credential   not configured (no password)" in lines
    assert "  data_dir     unset" in lines
    assert any("bh config set plugins.beadlog-xtdb.enabled true" in line for line in lines)


def test_status_json_names_the_fnox_entry_never_the_value(monkeypatch) -> None:
    use_config(monkeypatch, {"plugins": {"beadlog-xtdb": ON}})
    monkeypatch.setattr(cli, "_password_source", lambda values: pytest.fail("status resolved"))
    result = invoke("status", "--json")
    assert result.exit_code == 0
    doc = json.loads(result.output)
    assert doc["schema"] == "beadlog-store-status/1"
    assert (doc["state"], doc["enabled"], doc["installed"]) == ("ready", True, True)
    assert doc["credential"] == {"configured": True, "profile": "xtdb", "key": "XTDB_PASSWORD"}
    assert "values" not in doc and SECRET not in result.output


def test_status_reports_a_missing_package_and_an_invalid_fragment(monkeypatch) -> None:
    use_config(monkeypatch, {"plugins": {"beadlog-xtdb": {"enabled": True}}})
    monkeypatch.setattr(cli, "package_installed", lambda: False)
    result = invoke("status")
    assert result.output.splitlines()[0] == "beadlog-xtdb  not-installed"
    assert "not installed (install `beadhive[xtdb]`)" in result.output

    bad = {"enabled": True, "dsn": f"host=127.0.0.1 password={SECRET}"}
    use_config(monkeypatch, {"plugins": {"beadlog-xtdb": bad}})
    result = invoke("status")
    assert result.output.splitlines()[0] == "beadlog-xtdb  invalid-config"
    assert SECRET not in result.output


def test_doctor_on_the_disabled_default_is_offline_and_exits_zero(monkeypatch, probes) -> None:
    def never(*args):
        raise AssertionError("no network while disabled")

    probes["probes"] = replace(
        probes["probes"], http_get_status=never, connect=never, tcp_open=never
    )
    monkeypatch.setattr(cli, "_password_source", lambda values: never)
    use_config(monkeypatch, {})
    result = invoke("doctor")
    assert result.exit_code == 0, result.output
    assert "info     disabled-plugin: beadlog-xtdb: disabled" in result.output
    assert "error    password: no password" in result.output  # reported, not fatal while off


def test_doctor_enabled_and_healthy_exits_zero_without_rendering_the_password(
    monkeypatch, probes
) -> None:
    use_config(monkeypatch, {"plugins": {"beadlog-xtdb": ON}})
    result = invoke("doctor", "--json")
    assert result.exit_code == 0, result.output
    doc = json.loads(result.output)
    assert [row["check"] for row in doc["checks"]] == [
        "image",
        "data-dir",
        "password",
        "bind",
        "health",
        "version",
        "auth",
    ]
    assert {row["severity"] for row in doc["checks"]} == {"ok"}
    assert SECRET not in result.output


def test_doctor_enabled_with_a_failure_exits_one_with_the_remedy(monkeypatch, probes) -> None:
    probes["probes"] = replace(probes["probes"], filesystem_type=lambda path: "tmpfs")
    use_config(monkeypatch, {"plugins": {"beadlog-xtdb": ON}})
    result = invoke("doctor")
    assert result.exit_code == 1
    assert "error    data-dir: /data/xtdb is on tmpfs: refused" in result.output
    assert "-> stop the node, move the directory to a real-disk path" in result.output


def test_doctor_enabled_without_the_package_is_an_install_error(monkeypatch) -> None:
    use_config(monkeypatch, {"plugins": {"beadlog-xtdb": ON}})
    monkeypatch.setattr(cli, "package_installed", lambda: False)
    result = invoke("doctor")
    assert result.exit_code == 1
    assert "error    package: the beadlog-xtdb plugin package is not installed" in result.output
    assert "beadhive[xtdb]" in result.output
