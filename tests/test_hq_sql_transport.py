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
from beadhive.modules.config.contracts import HqSqlConfig, HqSqlConnection


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
def test_invalid_inline_sql_secret_is_redacted_at_binding_and_cli_boundaries(monkeypatch, role):
    from beadhive import config_store, config_validate

    canary = "DUMMY_INLINE_SECRET_xyz"
    sql = {role: {"credential": {"password": canary}}}
    with pytest.raises(ValueError) as construction:
        hq_control_plane.SqlControlPlane(sql)
    assert canary not in str(construction.value)
    monkeypatch.setattr(hq_control_plane.config, "load_host", lambda: {"hq": {"sql": sql}})
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


@pytest.mark.parametrize("tls_mode", ["required", "disabled"])
def test_pinned_driver_aborts_a_dripping_response_at_total_deadline(tls_mode):
    connection_type = _strict_driver()
    connection = connection_type(
        host="127.0.0.1",
        user="fixture",
        password="fixture-secret",
        defer_connect=True,
        read_timeout=1,
        operation_deadline=time.monotonic() + 0.2,
        tls_mode=tls_mode,
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


@pytest.mark.parametrize("role", ["reader", "publisher", "runtime", "observer", "authority_writer"])
def test_tls_mode_is_host_local_and_does_not_propagate_between_bindings(role):
    assert partition_of(f"hq.sql.{role}.tls_mode") == HOST
    assert HqSqlConnection().tls_mode == "required"
    if role == "runtime":
        # Runtime completeness has a separate authority validator; a staged
        # connection can be validated independently of those authority pins.
        assert HqSqlConnection(tls_mode="disabled").tls_mode == "disabled"
    else:
        settings = HqSqlConfig.model_validate({role: {"tls_mode": "disabled"}})
        assert getattr(settings, role).tls_mode == "disabled"
        for other in ("reader", "publisher", "observer", "authority_writer"):
            binding = getattr(settings, other)
            if other != role and binding is not None:
                assert binding.tls_mode == "required"


@pytest.mark.parametrize("mode", ["preferred", "auto", "off", "", None, True, 0])
def test_unknown_tls_modes_fail_before_broker_and_redact_values(mode):
    from beadhive.hq_sql_transport import connect

    class RefusingBroker:
        def get(self, *_args, **_kwargs):
            raise AssertionError("invalid binding must not retrieve credentials")

    with pytest.raises(ValueError):
        HqSqlConnection.model_validate({"tls_mode": mode})
    with pytest.raises(SqlTransportError, match="invalid SQL connection binding"):
        connect({"tls_mode": mode}, RefusingBroker(), deadline=time.monotonic() + 1)


def test_disabled_bootstrap_preserves_authority_pins_without_tls_metadata():
    binding = {
        "tls_mode": "disabled",
        "host": "sql.example.test",
        "database": "config",
        "user": "reader",
        "credential": {
            "config_path": "/fixture/fnox.toml",
            "profile": "test",
            "key": "SQL",
        },
    }
    bootstrap = {
        "enabled": True,
        "reader": binding,
        "floor_path": "/fixture/floor.json",
        "backend_identity": "fixture",
        "generation": "fixture",
        "initial_revision": "a" * 32,
    }
    assert HqSqlConfig.model_validate(bootstrap).reader.tls_mode == "disabled"
    with pytest.raises(ValueError):
        HqSqlConfig.model_validate({**bootstrap, "reader": {**binding, "tls_mode": "required"}})
    with pytest.raises(ValueError):
        HqSqlConfig.model_validate({**bootstrap, "initial_revision": ""})
    runtime = {**binding, "database": "runtime", "user": "frame"}
    bootstrap.update(
        {
            "runtime": runtime,
            "runtime_backend_identity": "runtime",
            "runtime_generation": "gen",
            "runtime_initial_revision": "a" * 32,
            "runtime_floor_path": "/fixture/runtime.json",
            "runtime_operator_public_key": "ssh-ed25519 fixture",
        }
    )
    assert HqSqlConfig.model_validate(bootstrap).runtime.tls_mode == "disabled"
    with pytest.raises(ValueError):
        HqSqlConfig.model_validate({**bootstrap, "runtime_operator_public_key": ""})


@pytest.mark.parametrize("role", ["runtime", "observer", "authority_writer"])
@pytest.mark.parametrize(
    "reader_mode, runtime_mode", [("required", "disabled"), ("disabled", "required")]
)
def test_cross_database_reads_refuse_transport_policy_mismatch(role, reader_mode, runtime_mode):
    from beadhive.hq_sql_runtime import SqlRuntimeAuthority, SqlRuntimeError

    binding = {"host": "127.0.0.1", "port": 3308, "database": "config"}
    authority = SqlRuntimeAuthority(
        {
            "reader": {**binding, "tls_mode": reader_mode},
            role: {**binding, "database": "runtime", "tls_mode": runtime_mode},
        }
    )
    with pytest.raises(SqlRuntimeError, match="endpoint or transport policy mismatch"):
        authority.load_latest_config_at(None)
    with pytest.raises(SqlRuntimeError, match="endpoint mismatch"):
        authority.fresh_config_head_fence("a" * 32, deadline=time.monotonic() + 1)


@pytest.mark.parametrize("failure", ["endpoint", "broker", "driver", "pin"])
def test_plaintext_failures_redact_and_keep_validation_broker_and_driver_guards(
    monkeypatch, failure
):
    from beadhive import hq_sql_transport

    canary = "SECRET_MUST_NOT_ESCAPE_123"
    binding = {
        "tls_mode": "disabled",
        "host": "127.0.0.1",
        "database": "config",
        "user": "reader",
        "credential": {"config_path": "/fixture/fnox.toml", "profile": "test", "key": "SQL"},
    }
    calls = []

    class Broker:
        def get(self, reference, *, deadline):
            calls.append(reference)
            if failure == "broker":
                raise RuntimeError(canary)
            return canary

    if failure == "endpoint":
        binding["host"] = f"bad://{canary}"
    elif failure == "driver":

        def bad_driver():
            def fail(**kwargs):
                assert kwargs["ssl"] is None and kwargs["tls_mode"] == "disabled"
                raise RuntimeError(kwargs["password"])

            return fail

        monkeypatch.setattr(hq_sql_transport, "_strict_driver", bad_driver)
    elif failure == "pin":
        monkeypatch.setattr(hq_sql_transport.importlib.metadata, "version", lambda _name: "0.0.0")
    with pytest.raises(SqlTransportError) as error:
        hq_sql_transport.connect(binding, Broker(), deadline=time.monotonic() + 1)
    assert canary not in str(error.value)
    assert bool(calls) == (failure != "endpoint")
    if failure == "pin":
        assert "unsupported PyMySQL" in str(error.value)


@pytest.mark.parametrize("tls_mode", ["required", "disabled"])
def test_incomplete_credential_reference_fails_before_broker(tls_mode):
    from beadhive.hq_sql_transport import connect

    class RefusingBroker:
        def get(self, *_args, **_kwargs):
            raise AssertionError("incomplete credential must fail before broker")

    with pytest.raises(SqlTransportError, match="incomplete SQL connection binding"):
        connect(
            {
                "tls_mode": tls_mode,
                "host": "127.0.0.1",
                "user": "reader",
                "database": "config",
                "ca_file": "/fixture/ca.pem",
                "server_name": "127.0.0.1",
            },
            RefusingBroker(),
            deadline=time.monotonic() + 1,
        )
