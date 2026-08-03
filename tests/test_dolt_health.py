"""dolt_health.py — store-engine liveness (bh-areg.3).

Constraint under test throughout: the probe is an ENDPOINT connection check, never a
bd-reported PID (bh-u562.1 finding 9 measured `bd dolt status --json` reporting a LIVE
external server as down). Real TCP sockets are used here — no `bd` subprocess anywhere in
this module or these tests.
"""

from __future__ import annotations

import json
import socket
import threading

from beadhive import dolt_health

# ---- test doubles: a real (throwaway) TCP listener -----------------------------


def _free_port() -> tuple[str, int]:
    """Bind an OS-assigned ephemeral port, then release it — 'not running' / 'wrong port'
    fixtures reuse this as a port nothing is listening on."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    host, port = sock.getsockname()
    sock.close()
    return host, port


class _FakeServer:
    """A real listening TCP socket on 127.0.0.1 that sends *greeting* to exactly one
    connection, then closes. Runs the accept() loop on a background thread so the test's main
    thread can call `probe_endpoint` synchronously, exactly like a real caller."""

    def __init__(self, greeting: bytes):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self.host, self.port = self._sock.getsockname()
        self._greeting = greeting
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        try:
            conn, _ = self._sock.accept()
        except OSError:
            return
        try:
            if self._greeting:
                conn.sendall(self._greeting)
        finally:
            conn.close()

    def close(self):
        self._sock.close()
        self._thread.join(timeout=1)


# ---- probe_endpoint: the three acceptance conditions ----------------------------


def test_probe_endpoint_reachable_when_the_mysql_handshake_byte_is_present():
    """'running': a real dolt sql-server's first byte back is protocol version 10 (0x0a)."""
    server = _FakeServer(greeting=bytes([0x0A]) + b"5.7.9-Vitess\x00")
    try:
        result = dolt_health.probe_endpoint(server.host, server.port, timeout=2.0)
    finally:
        server.close()

    assert result.reachable is True
    assert f"{server.host}:{server.port}" in result.detail


def test_probe_endpoint_unreachable_when_nothing_is_listening():
    """'not running': a closed port refuses the connection outright."""
    host, port = _free_port()

    result = dolt_health.probe_endpoint(host, port, timeout=1.0)

    assert result.reachable is False
    assert "refused" in result.detail or "unreachable" in result.detail


def test_probe_endpoint_unreachable_on_wrong_protocol():
    """'wrong-port': something IS listening (a real TCP accept succeeds), but it doesn't speak
    the MySQL protocol dolt's sql-server does — must not be misread as 'up'."""
    server = _FakeServer(greeting=b"SSH-2.0-OpenSSH_9.0\r\n")
    try:
        result = dolt_health.probe_endpoint(server.host, server.port, timeout=2.0)
    finally:
        server.close()

    assert result.reachable is False
    assert "not a MySQL/dolt" in result.detail or "protocol" in result.detail


def test_probe_endpoint_unreachable_when_connection_accepted_but_silent():
    """A listener that accepts but sends nothing back before closing — still not reachable."""
    server = _FakeServer(greeting=b"")
    try:
        result = dolt_health.probe_endpoint(server.host, server.port, timeout=1.0)
    finally:
        server.close()

    assert result.reachable is False


def test_probe_endpoint_times_out_cleanly_against_a_black_hole(monkeypatch):
    """A host that never completes the TCP handshake at all must not hang the caller."""

    class _NeverConnects:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            raise TimeoutError("timed out")

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(dolt_health.socket, "create_connection", lambda *a, **k: _NeverConnects())

    result = dolt_health.probe_endpoint("10.255.255.1", 3308, timeout=0.2)

    assert result.reachable is False
    assert "timed out" in result.detail


# ---- server_endpoint: env override vs mode (a)'s fixed default ------------------


def test_server_endpoint_defaults_to_the_shared_mode_a_address(monkeypatch):
    monkeypatch.delenv(dolt_health.ENV_SERVER_HOST, raising=False)
    monkeypatch.delenv(dolt_health.ENV_SERVER_PORT, raising=False)

    assert dolt_health.server_endpoint() == (
        dolt_health.DEFAULT_SHARED_SERVER_HOST,
        dolt_health.DEFAULT_SHARED_SERVER_PORT,
    )


def test_server_endpoint_honors_bd_own_env_override(monkeypatch):
    """bd's own env vars (not a new bh config key) win — exactly bd's own resolution order."""
    monkeypatch.setenv(dolt_health.ENV_SERVER_HOST, "10.0.0.5")
    monkeypatch.setenv(dolt_health.ENV_SERVER_PORT, "5000")

    assert dolt_health.server_endpoint() == ("10.0.0.5", 5000)


def test_server_endpoint_falls_back_to_default_port_on_unparseable_env(monkeypatch):
    monkeypatch.delenv(dolt_health.ENV_SERVER_HOST, raising=False)
    monkeypatch.setenv(dolt_health.ENV_SERVER_PORT, "not-a-port")

    assert dolt_health.server_endpoint()[1] == dolt_health.DEFAULT_SHARED_SERVER_PORT


# ---- shared_server_env_active ----------------------------------------------------


def test_shared_server_env_active_true_for_1(monkeypatch):
    monkeypatch.setenv(dolt_health.ENV_SHARED_SERVER, "1")
    assert dolt_health.shared_server_env_active() is True


def test_shared_server_env_active_true_for_true_case_insensitive(monkeypatch):
    monkeypatch.setenv(dolt_health.ENV_SHARED_SERVER, "True")
    assert dolt_health.shared_server_env_active() is True


def test_shared_server_env_active_false_when_unset(monkeypatch):
    monkeypatch.delenv(dolt_health.ENV_SHARED_SERVER, raising=False)
    assert dolt_health.shared_server_env_active() is False


def test_shared_server_env_active_false_for_0(monkeypatch):
    monkeypatch.setenv(dolt_health.ENV_SHARED_SERVER, "0")
    assert dolt_health.shared_server_env_active() is False


# ---- mismatch_reason: mirrors bd's own warnSharedServerEmbeddedMismatch ---------


def _write_metadata(hive, **fields):
    (hive / ".beads").mkdir(parents=True, exist_ok=True)
    (hive / ".beads" / "metadata.json").write_text(json.dumps(fields))


def test_mismatch_when_persisted_embedded_but_shared_server_env_is_active(tmp_path, monkeypatch):
    _write_metadata(tmp_path, dolt_mode="embedded")
    monkeypatch.setenv(dolt_health.ENV_SHARED_SERVER, "1")

    reason = dolt_health.mismatch_reason(tmp_path)

    assert reason is not None
    assert "embedded" in reason
    assert dolt_health.ENV_SHARED_SERVER in reason


def test_no_mismatch_when_persisted_embedded_and_shared_server_env_is_inactive(
    tmp_path, monkeypatch
):
    _write_metadata(tmp_path, dolt_mode="embedded")
    monkeypatch.delenv(dolt_health.ENV_SHARED_SERVER, raising=False)

    assert dolt_health.mismatch_reason(tmp_path) is None


def test_no_mismatch_when_persisted_server_and_shared_server_env_is_active(tmp_path, monkeypatch):
    """Persisted and active agree — nothing to report."""
    _write_metadata(tmp_path, dolt_mode="server")
    monkeypatch.setenv(dolt_health.ENV_SHARED_SERVER, "1")

    assert dolt_health.mismatch_reason(tmp_path) is None


def test_no_mismatch_when_metadata_is_absent(tmp_path, monkeypatch):
    monkeypatch.setenv(dolt_health.ENV_SHARED_SERVER, "1")

    assert dolt_health.mismatch_reason(tmp_path) is None


# ---- probe_shared_server: the server_endpoint() + probe_endpoint() convenience --


def test_probe_shared_server_uses_server_endpoint(monkeypatch):
    seen = {}

    def _fake_probe(host, port, *, timeout=dolt_health.DEFAULT_PROBE_TIMEOUT):
        seen["host"], seen["port"] = host, port
        return dolt_health.ProbeResult(True, "ok")

    monkeypatch.setenv(dolt_health.ENV_SERVER_HOST, "192.168.1.1")
    monkeypatch.setenv(dolt_health.ENV_SERVER_PORT, "4000")
    monkeypatch.setattr(dolt_health, "probe_endpoint", _fake_probe)

    result = dolt_health.probe_shared_server()

    assert seen == {"host": "192.168.1.1", "port": 4000}
    assert result.reachable is True
