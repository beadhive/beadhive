"""Host bootstrap and fixed broker/driver trust boundaries."""

from __future__ import annotations

import socket
import ssl
import threading
import time

import pytest
from pymysql.err import OperationalError

from beadhive import hq_control_plane
from beadhive.hq_sql_transport import (
    FnoxBroker,
    SqlTransportError,
    _resolve_ipv4,
    _strict_driver,
)
from beadhive.modules.config.application.partition import HOST, partition_of
from beadhive.modules.config.contracts import HqSqlConfig


def test_sql_bootstrap_selector_is_explicit_and_strict(monkeypatch):
    assert partition_of("hq.sql.enabled") == HOST
    assert partition_of("hq.sql.reader.credential.config_path") == HOST
    assert partition_of("hq.sql.publisher.credential.key") == HOST
    assert partition_of("hq.sql.runtime.user") == HOST
    assert partition_of("hq.sql.observer.ca_file") == HOST
    staged = {"hq": {"sql": {"reader": {"host": "dolt.example.test"}}}}
    monkeypatch.setattr(hq_control_plane.config, "load_host", lambda: staged)
    # Prepared reader metadata does not choose SQL or resolve any credential.
    plane = hq_control_plane.control_plane(hq_dir="/tmp/nonexistent-hq")
    assert isinstance(plane, hq_control_plane.GitControlPlane)
    staged["hq"]["sql"]["enabled"] = "false"
    with pytest.raises(ValueError):
        hq_control_plane.control_plane(hq_dir="/tmp/nonexistent-hq")
    staged["hq"]["sql"]["enabled"] = True
    with pytest.raises(ValueError, match="invalid SQL HOST binding"):
        hq_control_plane.control_plane(hq_dir="/tmp/nonexistent-hq")


@pytest.mark.parametrize("role", ["reader", "publisher", "observer", "authority_writer"])
def test_invalid_inline_sql_secret_is_redacted_at_binding_and_cli_boundaries(
    monkeypatch, role
):
    from beadhive import config_store, config_validate

    canary = "DUMMY_INLINE_SECRET_xyz"
    sql = {role: {"credential": {"password": canary}}}
    with pytest.raises(ValueError) as construction:
        hq_control_plane.SqlControlPlane(sql)
    assert canary not in str(construction.value)
    monkeypatch.setattr(
        hq_control_plane.config, "load_host", lambda: {"hq": {"sql": sql}}
    )
    with pytest.raises(ValueError) as selector:
        hq_control_plane.control_plane(hq_dir="/tmp/unused-hq")
    assert canary not in str(selector.value)
    with pytest.raises(ValueError) as attachment:
        hq_control_plane.attach_fleet_config(bootstrap={"sql": sql})
    assert canary not in str(attachment.value)

    class Api:
        @staticmethod
        def load_host():
            return {"hq": {"sql": sql}}

    with pytest.raises(ValueError) as effective:
        config_store.load(Api())
    assert canary not in str(effective.value)
    problems = config_validate.validate_config({"hq": {"sql": sql}})
    assert problems and canary not in str(problems)


def test_config_only_binding_never_resolves_runtime_credentials():
    class RefusingBroker:
        def get(self, *_args, **_kwargs):
            raise AssertionError("construction must not resolve a secret")

    settings = HqSqlConfig.model_validate(
        {
            "enabled": True,
            "reader": {
                "host": "sql.example.test",
                "database": "beadhive_hq_config",
                "user": "reader",
                "server_name": "sql.example.test",
                "ca_file": "/fixture/ca.pem",
                "credential": {
                    "config_path": "/fixture/fnox.toml",
                    "profile": "test",
                    "key": "SQL_READER",
                },
            },
            "floor_path": "/fixture/floor.json",
            "backend_identity": "fixture",
            "generation": "fixture-generation",
            "initial_revision": "a" * 32,
        }
    )
    plane = hq_control_plane.SqlControlPlane(settings.model_dump(), broker=RefusingBroker())
    assert plane.config_store().settings["reader"]["user"] == "reader"
    assert plane.authority_status()["authority_ready"] is False
    with pytest.raises(hq_control_plane.ControlPlaneError, match="AUTHORITY_NOT_READY"):
        plane.read_eligibility(None)
    with pytest.raises(hq_control_plane.ControlPlaneError, match="AUTHORITY_NOT_READY"):
        plane.watch_state("frame-test")


@pytest.mark.parametrize(
    "invalid",
    [
        {"reader": {"database": "bad;DROP"}},
        {"reader": {"port": "3308"}},
        {"reader": {"ca_file": "relative/ca.pem"}},
        {"reader": {"credential": {"config_path": "relative/fnox.toml"}}},
        {"unsupported": True},
    ],
)
def test_sql_host_metadata_rejects_invalid_values(invalid):
    with pytest.raises(ValueError):
        HqSqlConfig.model_validate(invalid)


def test_fnox_fixed_noninteractive_contract_and_redaction(tmp_path):
    executable = tmp_path / "fnox"
    executable.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = --version ]; then printf 'fnox 1.36.0\\n'; exit 0; fi\n"
        "printf 'fixture-password\\n'\n"
    )
    executable.chmod(0o700)
    broker = FnoxBroker(binary=str(executable))
    reference = {"config_path": str(tmp_path / "fnox.toml"), "profile": "test", "key": "SQL"}
    assert broker.get(reference, deadline=time.monotonic() + 2) == "fixture-password"
    executable.write_text("#!/bin/sh\necho 'fnox 1.36.0-evil'\n")
    with pytest.raises(SqlTransportError, match="unsupported fnox broker version"):
        broker.get(reference, deadline=time.monotonic() + 2)


def test_pinned_driver_refuses_missing_client_ssl_before_authentication():
    connection_type = _strict_driver()
    context = ssl.create_default_context()
    connection = connection_type(
        host="127.0.0.1",
        user="fixture",
        password="fixture-secret",
        ssl=context,
        defer_connect=True,
        operation_deadline=time.monotonic() + 2,
    )
    connection.server_capabilities = 0
    try:
        with pytest.raises(SqlTransportError, match="before authentication"):
            connection._request_authentication()
    finally:
        connection._abort_timer.cancel()


def test_hostname_resolution_is_bounded_before_any_credential(monkeypatch, tmp_path):
    from beadhive import hq_sql_transport

    resolver = tmp_path / "slow-python"
    resolver.write_text("#!/bin/sh\nexec sleep 1\n")
    resolver.chmod(0o700)
    monkeypatch.setattr(hq_sql_transport.sys, "executable", str(resolver))
    started = time.monotonic()
    with pytest.raises(SqlTransportError, match="bounded SQL endpoint resolution unavailable"):
        _resolve_ipv4("slow.example.test", deadline=started + 0.1)
    assert time.monotonic() - started < 0.8
    assert _resolve_ipv4("127.0.0.1", deadline=time.monotonic() + 0.1) == "127.0.0.1"


def test_failed_connection_constructor_closes_partially_owned_socket(monkeypatch):
    from pymysql.connections import Connection

    connection_type = _strict_driver()
    owned, peer = socket.socketpair()

    def partial_init(self, **_kwargs):
        self._sock = owned
        self._rfile = None

    monkeypatch.setattr(Connection, "__init__", partial_init)
    monkeypatch.setattr(
        connection_type,
        "_check_deadline",
        lambda _self: (_ for _ in ()).throw(SqlTransportError("fixture deadline")),
    )
    try:
        with pytest.raises(SqlTransportError, match="fixture deadline"):
            connection_type(operation_deadline=time.monotonic() + 1)
        assert owned.fileno() == -1
    finally:
        peer.close()


def test_pinned_driver_aborts_a_dripping_response_at_total_deadline():
    connection_type = _strict_driver()
    connection = connection_type(
        host="127.0.0.1",
        user="fixture",
        password="fixture-secret",
        defer_connect=True,
        read_timeout=1,
        operation_deadline=time.monotonic() + 0.2,
    )
    client, server = socket.socketpair()
    connection._sock = client
    connection._rfile = client.makefile("rb")
    stopped = threading.Event()

    def drip():
        while not stopped.wait(0.04):
            try:
                server.sendall(b"x")
            except OSError:
                return

    writer = threading.Thread(target=drip, daemon=True)
    writer.start()
    began = time.monotonic()
    try:
        with pytest.raises((SqlTransportError, OperationalError)):
            connection._read_bytes(64)
        assert time.monotonic() - began < 0.8
    finally:
        stopped.set()
        connection.close()
        server.close()
        writer.join(timeout=1)
