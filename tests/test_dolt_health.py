"""`dolt_health` — store-engine liveness (bh-areg.3) and schema-version probing (bh-wnly).

Two independently-written suites that met when `main` merged into the epic. Both halves are
kept whole; see the module's own docstring for why the two probes coexist.

TWO FIXTURE LESSONS ARE ENCODED HERE, and both cost a review round to find. Do not "simplify"
either back:

  MySQL FRAMING. `_FakeServer` emits a spec-correct packet — 3-byte little-endian payload
  length + 1-byte sequence number, THEN the protocol-version byte. An earlier fixture sent the
  version byte bare, which made the suite pass over a probe that rejected every real server.
  A double that skips framing validates the bug instead of the behaviour.

  `bd sql --json` SHAPE. The server-mode probe's double returns a BARE ARRAY
  (`[{"max_version": 59}]`), because that is what `bd sql --json` actually returns — not the
  `{"rows": [...]}` envelope `dolt ... sql -r json` produces on the embedded path. A double
  built on the envelope made every server-mode probe silently return None while the suite
  stayed green, for the fleet's chosen storage mode.
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections import namedtuple
from pathlib import Path

import pytest

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


def _mysql_handshake_packet(payload: bytes, *, sequence_id: int = 0) -> bytes:
    """Build a spec-correct MySQL wire packet: a 4-byte header (3-byte little-endian payload
    length + 1-byte sequence number) followed by *payload*. Every MySQL/dolt packet — the
    initial handshake included — is framed this way; the protocol-version byte is the first
    byte of the payload, i.e. the 5th byte on the wire, never the 1st. Mirrors a real
    `dolt sql-server` handshake, e.g. `4a 00 00 00 0a 38 2e 30 2e 33 33 00 ...` — byte 0 is the
    length header, byte 4 is the protocol version."""
    header = len(payload).to_bytes(3, "little") + bytes([sequence_id])
    return header + payload


def _mysql_greeting(protocol_version: int = 0x0A) -> bytes:
    """A realistic (framed) MySQL/dolt handshake greeting carrying *protocol_version* as the
    first payload byte."""
    payload = bytes([protocol_version]) + b"8.0.33-Dolt\x00" + b"\x00" * 8
    return _mysql_handshake_packet(payload)


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
    """'running': a real dolt sql-server's protocol version (10 / 0x0a) is the 5th byte on the
    wire — the first payload byte, AFTER the 4-byte packet header (3-byte length + 1-byte
    sequence number). A framing bug that checks byte 0 instead of byte 4 would reject every
    real server; this fixture is spec-correct framing so that bug can't hide behind the test."""
    server = _FakeServer(greeting=_mysql_greeting())
    try:
        result = dolt_health.probe_endpoint(server.host, server.port, timeout=2.0)
    finally:
        server.close()

    assert result.reachable is True
    assert f"{server.host}:{server.port}" in result.detail


def test_probe_endpoint_unreachable_when_packet_header_present_but_protocol_byte_differs():
    """A correctly-framed packet whose protocol-version byte (index 4) is NOT 0x0a — e.g. a
    non-dolt MySQL-wire-protocol server on an unexpected version — must not be misread as
    reachable just because the framing is otherwise well-formed."""
    server = _FakeServer(greeting=_mysql_greeting(protocol_version=0x09))
    try:
        result = dolt_health.probe_endpoint(server.host, server.port, timeout=2.0)
    finally:
        server.close()

    assert result.reachable is False


def test_probe_endpoint_unreachable_when_fewer_than_five_bytes_arrive():
    """A listener that accepts and sends a short burst — fewer than the 4-byte header plus the
    protocol-version byte — must fail legibly (a ProbeResult), never raise IndexError."""
    server = _FakeServer(greeting=bytes([0x4A, 0x00]))
    try:
        result = dolt_health.probe_endpoint(server.host, server.port, timeout=2.0)
    finally:
        server.close()

    assert result.reachable is False


def test_probe_endpoint_loops_past_partial_recv_to_read_the_full_header(monkeypatch):
    """`recv()` may return fewer bytes than requested even on a healthy connection (TCP makes
    no framing guarantee). The probe must loop until it has all 5 bytes it needs, not read once
    and give up."""
    full_greeting = _mysql_greeting()

    class _FragmentedSocket:
        def __init__(self, data: bytes):
            self._chunks = [data[i : i + 1] for i in range(len(data))]  # one byte at a time

        def settimeout(self, _):
            pass

        def recv(self, n):
            return self._chunks.pop(0) if self._chunks else b""

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(
        dolt_health.socket,
        "create_connection",
        lambda *a, **k: _FragmentedSocket(full_greeting),
    )

    result = dolt_health.probe_endpoint("127.0.0.1", 3308, timeout=2.0)

    assert result.reachable is True


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


Completed = namedtuple("Completed", "returncode stdout stderr")


def _json_rows(**row) -> str:
    """`dolt ... sql -r json`'s real shape — an envelope."""
    return json.dumps({"rows": [row]})


def _json_bare_array(**row) -> str:
    """`bd sql --json`'s real shape — NO envelope, a bare array (confirmed by review against a
    live server-mode hive, `github/briancripe/testfoo`; `_json_rows`'s envelope is a DIFFERENT
    tool's shape and must never stand in for this one — that mix-up is exactly the bug review
    caught: a test double built from the embedded shape silently validated the server path
    against the wrong format)."""
    return json.dumps([row])


# ---- _parse_max_version -----------------------------------------------------------------


def test_parse_max_version_reads_the_aliased_column_from_the_dolt_cli_envelope_shape():
    assert dolt_health._parse_max_version(_json_rows(max_version=59)) == 59


def test_parse_max_version_reads_the_aliased_column_from_bd_sqls_bare_array_shape():
    """The bug review caught: `bd sql --json` returns `[{"max_version": 59}]`, not
    `{"rows": [...]}`. Both real shapes must parse to the same value."""
    assert dolt_health._parse_max_version(_json_bare_array(max_version=59)) == 59


def test_parse_max_version_none_on_garbage():
    assert dolt_health._parse_max_version("not json") is None
    assert dolt_health._parse_max_version(json.dumps({"rows": []})) is None
    assert dolt_health._parse_max_version(json.dumps({"rows": [{}]})) is None
    assert dolt_health._parse_max_version(json.dumps({"rows": [{"max_version": "59"}]})) is None
    assert dolt_health._parse_max_version(json.dumps([])) is None
    assert dolt_health._parse_max_version(json.dumps([{}])) is None
    assert dolt_health._parse_max_version(json.dumps("neither a dict nor a list")) is None


# ---- probe_embedded_schema_version -------------------------------------------------------


def test_embedded_probe_reads_the_real_int_not_the_json_envelope_decoy(tmp_path, monkeypatch):
    """The trap this bead exists to avoid: `bd dolt status --json`'s `schema_version` is always
    `1` (the CLI's own JSON envelope version). This probe must never be that number by
    construction — it doesn't even call `bd dolt status` — verified here by returning 59 from
    the faked `dolt sql` call and asserting the probe reports exactly that, not 1."""
    db_dir = tmp_path / "embeddeddolt" / "beads"
    (db_dir / ".dolt").mkdir(parents=True)

    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return Completed(0, _json_rows(max_version=59), "")

    monkeypatch.setattr(dolt_health, "run", fake_run)
    result = dolt_health.probe_embedded_schema_version(db_dir)

    assert result.version == 59
    assert result.version != 1  # the JSONSchemaVersion decoy this bead's own trap section names
    assert calls[0][:2] == ["dolt", "--data-dir"]
    assert str(db_dir) in calls[0]
    assert "sql" in calls[0]


def test_embedded_probe_none_when_not_a_dolt_dir(tmp_path):
    result = dolt_health.probe_embedded_schema_version(tmp_path / "nope")
    assert result.version is None
    assert "not a Dolt data directory" in result.detail


def test_embedded_probe_none_on_nonzero_exit(tmp_path, monkeypatch):
    db_dir = tmp_path / "db"
    (db_dir / ".dolt").mkdir(parents=True)
    monkeypatch.setattr(dolt_health, "run", lambda *a, **k: Completed(1, "", "boom"))
    result = dolt_health.probe_embedded_schema_version(db_dir)
    assert result.version is None
    assert "boom" in result.detail


# ---- probe_server_schema_version + dispatch ------------------------------------------------


def test_server_probe_uses_bd_sql(tmp_path, monkeypatch):
    """`bd sql --json` returns a BARE array, not the `dolt ... sql -r json` envelope — the
    double here must emit what the real tool actually returns (`_json_bare_array`), confirmed
    against a live server-mode hive by review; a double built from the embedded shape
    (`_json_rows`) would validate against the wrong format and silently mask a real parse
    failure, as it did before this fix."""
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return Completed(0, _json_bare_array(max_version=12), "")

    monkeypatch.setattr(dolt_health, "run", fake_run)
    result = dolt_health.probe_server_schema_version(tmp_path)
    assert result.version == 12
    assert calls[0][0] == "bd"
    assert "sql" in calls[0]


def test_server_probe_succeeds_despite_nonzero_exit_when_a_bd_warning_is_the_only_stderr(
    tmp_path, monkeypatch
):
    """bh-j50yv: defensive coverage for a shape a real `bd`/`dolt` invocation does not
    currently produce (measured: a `.beads` permissions `Warning:` exits 0, not 1 — see
    `test_server_probe_reports_the_real_error_not_a_buried_warning` for the actual repro). If
    a future `bd`/`dolt` build ever DOES pair a nonzero exit with an all-warning stderr, the
    optimistic stdout-parse must still win — not read as a failed probe."""
    monkeypatch.setattr(
        dolt_health,
        "run",
        lambda *a, **k: Completed(
            1,
            _json_bare_array(max_version=62),
            f"Warning: {tmp_path}/.beads has permissions 0755 (recommended: 0700). Run: "
            f"chmod 700 {tmp_path}/.beads",
        ),
    )
    result = dolt_health.probe_server_schema_version(tmp_path)
    assert result.version == 62


def test_server_probe_fails_on_nonzero_exit_with_a_real_error_and_no_parseable_stdout(
    tmp_path, monkeypatch
):
    """The opposite case must still fail: a nonzero exit with no usable stdout and a REAL
    error on stderr is a genuine probe failure, warning-filter or not."""
    monkeypatch.setattr(dolt_health, "run", lambda *a, **k: Completed(1, "", "connection refused"))
    result = dolt_health.probe_server_schema_version(tmp_path)
    assert result.version is None
    assert "connection refused" in result.detail


def test_server_probe_reports_the_real_error_not_a_buried_warning(tmp_path, monkeypatch):
    """bh-j50yv's actual repro, measured directly: `bd sql` against an embedded-mode store
    exits 1 with a `.beads` permissions `Warning:` as stderr's FIRST line and the real cause
    (`Error: 'bd sql' is not yet supported in embedded mode`) on its second. The probe was
    already failing correctly here (returncode-keyed, as `main` always was) — the bug was the
    OLD detail extraction (`(res.stderr or res.stdout or ...).splitlines()[:1]`) reporting the
    first line, i.e. the harmless warning, and discarding the real error beneath it."""
    monkeypatch.setattr(
        dolt_health,
        "run",
        lambda *a, **k: Completed(
            1,
            "",
            f"Warning: {tmp_path}/.beads has permissions 0755 (recommended: 0700)\n"
            "Error: 'bd sql' is not yet supported in embedded mode",
        ),
    )
    result = dolt_health.probe_server_schema_version(tmp_path)
    assert result.version is None
    assert "not yet supported in embedded mode" in result.detail
    assert "0755" not in result.detail


def test_embedded_probe_succeeds_despite_nonzero_exit_when_a_bd_warning_is_the_only_stderr(
    tmp_path, monkeypatch
):
    """Same defensive coverage, embedded path (bh-j50yv) — see the server-probe test above for
    why this exit/stderr combination isn't the real repro."""
    db_dir = tmp_path / "db"
    (db_dir / ".dolt").mkdir(parents=True)
    monkeypatch.setattr(
        dolt_health,
        "run",
        lambda *a, **k: Completed(
            1, _json_rows(max_version=59), f"Warning: {db_dir} has permissions 0755"
        ),
    )
    result = dolt_health.probe_embedded_schema_version(db_dir)
    assert result.version == 59


def test_server_probe_returns_none_if_it_were_fed_the_wrong_envelope_shape():
    """Documents the exact bug review caught, as a standing regression guard: if
    `_parse_max_version` only understood `dolt`'s `{"rows": [...]}` envelope, `bd sql --json`'s
    real bare-array output would silently parse to `None` — a probe that can never succeed for
    any owned/shared/external hive. This must NOT be the case after the fix."""
    assert dolt_health._parse_max_version(_json_bare_array(max_version=59)) == 59


def test_probe_raw_schema_version_dispatches_embedded(tmp_path, monkeypatch):
    (tmp_path / ".beads" / "embeddeddolt" / "x" / ".dolt").mkdir(parents=True)
    (tmp_path / ".beads" / "metadata.json").write_text(json.dumps({"dolt_database": "x"}))
    seen = {}

    def fake_embedded(db_dir, **kw):
        seen["db_dir"] = db_dir
        return dolt_health.SchemaProbeResult(7, "ok")

    monkeypatch.setattr(dolt_health, "probe_embedded_schema_version", fake_embedded)
    result = dolt_health.probe_raw_schema_version(tmp_path, dolt_mode="embedded")
    assert result.version == 7
    assert seen["db_dir"] == tmp_path / ".beads" / "embeddeddolt" / "x"


def test_probe_raw_schema_version_dispatches_none_mode_to_embedded_too(tmp_path, monkeypatch):
    """Measured (bh-u562.1 finding 9): owned + local-external modes also report no "mode" key
    at all — `dolt_mode=None` must not be routed to the server probe by default."""
    called = {}
    monkeypatch.setattr(
        dolt_health,
        "probe_embedded_schema_version",
        lambda *a, **k: called.setdefault("embedded", True) or dolt_health.SchemaProbeResult(3, ""),
    )
    monkeypatch.setattr(
        dolt_health,
        "probe_server_schema_version",
        lambda *a, **k: called.setdefault("server", True) or dolt_health.SchemaProbeResult(3, ""),
    )
    dolt_health.probe_raw_schema_version(tmp_path, dolt_mode=None)
    assert called == {"embedded": True}


def test_probe_raw_schema_version_dispatches_server_mode(tmp_path, monkeypatch):
    called = {}
    monkeypatch.setattr(
        dolt_health,
        "probe_server_schema_version",
        lambda *a, **k: called.setdefault("server", True) or dolt_health.SchemaProbeResult(3, ""),
    )
    dolt_health.probe_raw_schema_version(tmp_path, dolt_mode="external")
    assert called == {"server": True}


# ---- local_bd_schema_version + caching ------------------------------------------------------


def test_local_bd_schema_version_none_when_bd_missing(monkeypatch):
    monkeypatch.setattr(dolt_health, "run", lambda *a, **k: Completed(1, "", "not found"))
    result = dolt_health.local_bd_schema_version()
    assert result.version is None


def _fake_bd_init_scratch(cmd, cwd) -> None:
    """Mirror real `bd init`'s one observable side effect this probe depends on: a
    `.beads/embeddeddolt/<prefix>/.dolt/` directory under `cwd` — the `--prefix` value is the
    database name (`store_locator.embedded_database_dir`'s fallback)."""
    prefix = cmd[cmd.index("--prefix") + 1]
    (Path(cwd) / ".beads" / "embeddeddolt" / prefix / ".dolt").mkdir(parents=True)


def test_local_bd_schema_version_caches_by_bd_version_string(tmp_path, monkeypatch):
    monkeypatch.setattr(dolt_health.config, "cache_dir", lambda: tmp_path)
    calls = {"init": 0, "version": 0}

    def fake_run(cmd, **kw):
        if cmd[:2] == ["bd", "--version"]:
            calls["version"] += 1
            return Completed(0, "bd version 9.9.9", "")
        if cmd[:2] == ["bd", "init"]:
            calls["init"] += 1
            _fake_bd_init_scratch(cmd, kw["cwd"])
            return Completed(0, "", "")
        if cmd[0] == "dolt":
            return Completed(0, _json_rows(max_version=42), "")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(dolt_health, "run", fake_run)

    first = dolt_health.local_bd_schema_version()
    second = dolt_health.local_bd_schema_version()

    assert first.version == 42
    assert second.version == 42
    assert calls["init"] == 1  # the real (expensive) scratch probe only ran once
    # `bd --version` is memoized for the process since bh-i6e5g — the binary cannot change
    # under a running process, and doctor was paying for that question 12 times a run.
    assert calls["version"] == 1


def test_local_bd_schema_version_reprobes_after_a_bd_upgrade(tmp_path, monkeypatch):
    monkeypatch.setattr(dolt_health.config, "cache_dir", lambda: tmp_path)
    state = {"version": "bd version 1.0.0", "n": 42}

    def fake_run(cmd, **kw):
        if cmd[:2] == ["bd", "--version"]:
            return Completed(0, state["version"], "")
        if cmd[:2] == ["bd", "init"]:
            _fake_bd_init_scratch(cmd, kw["cwd"])
            return Completed(0, "", "")
        if cmd[0] == "dolt":
            return Completed(0, _json_rows(max_version=state["n"]), "")
        raise AssertionError(cmd)

    monkeypatch.setattr(dolt_health, "run", fake_run)

    before = dolt_health.local_bd_schema_version()
    state["version"] = "bd version 2.0.0"
    state["n"] = 59
    dolt_health._local_bd_version_string.cache_clear()  # an upgrade is a NEW process's view
    after = dolt_health.local_bd_schema_version()

    assert before.version == 42
    assert after.version == 59  # a version-string change busts the cache, not stale forever


# ---- schema_skew_advisory --------------------------------------------------------------------


def test_advisory_fires_when_local_behind_recorded():
    local = dolt_health.SchemaProbeResult(53, "")
    advisory = dolt_health.schema_skew_advisory("bh", local, 59)
    assert advisory is not None
    assert "v53" in advisory
    assert "v59" in advisory
    assert "bh" in advisory


def test_advisory_silent_when_local_at_or_ahead_of_recorded():
    local = dolt_health.SchemaProbeResult(59, "")
    assert dolt_health.schema_skew_advisory("bh", local, 59) is None
    assert dolt_health.schema_skew_advisory("bh", local, 12) is None


def test_advisory_silent_when_local_unknown():
    local = dolt_health.SchemaProbeResult(None, "bd unavailable")
    assert dolt_health.schema_skew_advisory("bh", local, 59) is None


def test_advisory_silent_when_recorded_unknown():
    local = dolt_health.SchemaProbeResult(53, "")
    assert dolt_health.schema_skew_advisory("bh", local, None) is None


# ---- a zombie reads as a healthy server (bh-hqmcl) -------------------------------------------
#
# bh-xonqg landed DETECTION for the mode-(a) endpoint; reconciliation and provisioning
# idempotence did not. The orphan on beadhive-factory started 2026-08-05, survived a deliberate
# host wipe/reinstall, and kept LISTENing on 127.0.0.1:3308 while its datadir was unlinked
# underneath it — so every probe above answered "✓ reachable", truthfully and uselessly. These
# tests pin the missing half: what is ACTUALLY running, what each one serves, and which of the
# three disagreeing sources of truth bh believes.


def _ps(monkeypatch, stdout: str, returncode: int = 0):
    monkeypatch.setattr(
        dolt_health,
        "run",
        lambda *a, **k: Completed(returncode, stdout, ""),
    )


#: `ps -o pid=,args=` emits NO header (that is what the trailing `=` buys), so the fixtures below
#: carry none either — a double that invents a header would let a header-skipping parser pass
#: while the real, headerless output silently lost its first row.
_PS_HEADER = ""


def test_running_servers_lists_each_server_and_what_it_serves(monkeypatch, tmp_path):
    live = tmp_path / "live"
    live.mkdir()
    _ps(
        monkeypatch,
        _PS_HEADER
        + f"   1234 /nix/store/x/bin/dolt sql-server --config {live}/dolt-server-config.yaml\n",
    )
    monkeypatch.setattr(dolt_health, "_proc_cwd", lambda pid: (str(live), True))

    servers = dolt_health.running_servers()

    assert [s.pid for s in servers] == [1234]
    assert servers[0].datadir == str(live)
    assert servers[0].datadir_exists is True
    # The launch command an operator otherwise reconstructs from /proc/<pid>/cmdline by hand —
    # which is exactly what the 2026-08-08 maintenance window had to do.
    assert servers[0].config_path.endswith("dolt-server-config.yaml")


def test_a_deleted_datadir_is_reported_as_a_zombie(monkeypatch):
    """The measured state, and the one an endpoint probe cannot see. procfs marks an unlinked
    cwd with ' (deleted)' — no cooperation from dolt or bd required."""
    _ps(
        monkeypatch,
        _PS_HEADER + "   4321 /nix/store/x/bin/dolt sql-server --config /gone/c.yaml\n",
    )
    monkeypatch.setattr(
        dolt_health.os, "readlink", lambda p: "/home/bees/tmp/pytest-369/store (deleted)"
    )

    servers = dolt_health.running_servers()

    assert servers[0].datadir_exists is False
    assert servers[0].datadir == "/home/bees/tmp/pytest-369/store"
    assert dolt_health.zombies(servers) == servers


def test_a_ps_row_that_merely_mentions_dolt_is_not_counted(monkeypatch):
    """The probe must not find itself. Every `ps` row of a shell running this check mentions
    'dolt sql-server' somewhere; a matcher that counts those manufactures servers — the same
    class of false finding this whole epic is about, pointed at itself."""
    _ps(
        monkeypatch,
        _PS_HEADER
        + "   5000 /bin/bash -c pgrep -fa 'dolt sql-server' | head\n"
        + "   5001 grep --color=auto dolt sql-server\n"
        + "   5002 ps -eo pid,args\n",
    )
    assert dolt_health.running_servers() == []


def test_a_shell_wrapped_dolt_is_still_a_server(monkeypatch, tmp_path):
    """A wrapper script renders as `<interpreter> <script> sql-server …`, so the dolt token is at
    argv[1] rather than argv[0]. Matching only argv[0] would report ZERO servers on a host whose
    dolt is wrapped — a silent all-clear, which is the failure mode this section exists to end."""
    _ps(monkeypatch, _PS_HEADER + f"   6001 /bin/sh /opt/bin/dolt sql-server --config {tmp_path}\n")
    monkeypatch.setattr(dolt_health, "_proc_cwd", lambda pid: (str(tmp_path), True))
    assert [s.pid for s in dolt_health.running_servers()] == [6001]


def test_an_unavailable_ps_yields_nothing_rather_than_raising(monkeypatch):
    """A diagnostic that can fail the verb it diagnoses is a diagnostic that gets removed."""
    _ps(monkeypatch, "", returncode=1)
    assert dolt_health.running_servers() == []


def test_ps_is_invoked_unwrapped_and_headerless(monkeypatch, tmp_path):
    """``-eww`` IS LOAD-BEARING, and this repo has already paid for the lesson once.

    `ps` truncates each command line to ``$COLUMNS`` EVEN WHEN ITS OUTPUT IS A PIPE. Measured on
    beadhive-factory at ``COLUMNS=80``: `ps -eo pid,args` loses the ``sql-server`` token from
    ``/nix/store/…-dolt-2.2.3/bin/dolt sql-server --config …`` entirely, so `running_servers`
    returned NOTHING and `bh doctor` printed "dolt servers on this host: 0" — on an interactive
    terminal, from the detector written to stop precisely that silent all-clear. It surfaced as
    a fenced `-n auto` gate failure, because pytest sets COLUMNS in its xdist workers.

    ``tests/harness/world.py::orphaned_dolt_servers`` carries the same flag with the same
    warning (bh-7wp2y, "a silent-no-op backstop is worse than none"). Pinned here so the next
    edit cannot quietly drop it, and so the header handling stays matched to `-o …=`.
    """
    seen = {}

    def spy(cmd, **kw):
        seen["cmd"] = cmd
        return Completed(0, "", "")

    monkeypatch.setattr(dolt_health, "run", spy)
    dolt_health.running_servers()

    assert "-eww" in seen["cmd"], seen["cmd"]
    assert "pid=,args=" in seen["cmd"], seen["cmd"]  # the trailing `=` suppresses the header


def test_the_first_row_is_data_not_a_header(monkeypatch, tmp_path):
    """The other half of `-o pid=,args=`: with no header, a parser that skips line 0 drops a
    real server — and the one it drops is arbitrary, so the loss is invisible."""
    _ps(monkeypatch, f"   7001 /usr/bin/dolt sql-server --config {tmp_path}/c.yaml\n")
    monkeypatch.setattr(dolt_health, "_proc_cwd", lambda pid: (str(tmp_path), True))
    assert [s.pid for s in dolt_health.running_servers()] == [7001]


def test_an_unreadable_procfs_is_unknown_not_deleted(monkeypatch):
    """macOS has no /proc. Guessing 'deleted' there would manufacture a zombie on every
    non-Linux host — the manufactured-finding class bh-7m2h9 was filed about."""

    def boom(_path):
        raise OSError("no /proc here")

    monkeypatch.setattr(dolt_health.os, "readlink", boom)
    assert dolt_health._proc_cwd(1) == ("", True)


def test_reconcile_states_which_of_the_three_is_authoritative(monkeypatch):
    """The bead's first criterion. On beadhive-factory config said `docker`, the filesystem had
    no shared-server dir at all, and a native nix dolt was answering on 3308 from a deleted
    datadir — and bh could not say which to believe."""
    monkeypatch.setattr(dolt_health.config, "load", lambda: {"dolt": {"backend": "docker"}})
    monkeypatch.setattr(
        dolt_health,
        "running_servers",
        lambda: [
            dolt_health.RunningServer(
                pid=999, datadir="/gone", datadir_exists=False, config_path="", role="unknown"
            )
        ],
    )

    rec = dolt_health.reconcile()

    assert rec.backend == "docker"
    assert rec.authoritative == "the running process"
    assert "NO LONGER EXISTS" in rec.detail
    assert "999" in rec.detail


def test_reconcile_says_nothing_is_serving_when_nothing_is(monkeypatch):
    """The other disagreement: config declares an intention that is not in force. Silence would
    read as agreement."""
    monkeypatch.setattr(dolt_health.config, "load", lambda: {"dolt": {"backend": "shared-server"}})
    monkeypatch.setattr(dolt_health, "running_servers", list)

    rec = dolt_health.reconcile()

    assert "no dolt sql-server is running" in rec.detail
    assert "not in force" in rec.detail


def test_reconcile_counts_the_category_nothing_previously_named(monkeypatch):
    """'How many dolt servers should be running on this host' had no stated answer at all — bd
    starts one per CACHED hive as well as the shared one, so eight were found here and nobody
    could tell the leaks from the fleet."""
    monkeypatch.setattr(dolt_health.config, "load", lambda: {"dolt": {"backend": "shared-server"}})
    monkeypatch.setattr(
        dolt_health,
        "running_servers",
        lambda: [
            dolt_health.RunningServer(1, "/s", True, "", "shared"),
            dolt_health.RunningServer(2, "/c1", True, "", "cache"),
            dolt_health.RunningServer(3, "/c2", True, "", "cache"),
            dolt_health.RunningServer(4, "/tmp/x", True, "", "unknown"),
        ],
    )

    rec = dolt_health.reconcile()

    assert "1 shared" in rec.detail
    assert "2 per-cache" in rec.detail
    assert "1 unattributed" in rec.detail


def test_reconcile_survives_an_unloadable_config(monkeypatch):
    """A reconciliation that crashes on a broken config is useless precisely when the config is
    one of the three things that disagree."""

    def boom():
        raise RuntimeError("config is a mess")

    monkeypatch.setattr(dolt_health.config, "load", boom)
    monkeypatch.setattr(dolt_health, "running_servers", list)
    assert dolt_health.reconcile().backend == "(unset)"


@pytest.mark.skipif(sys.platform != "linux", reason="procfs is Linux-only")
def test_a_real_process_on_a_real_deleted_datadir_is_seen_as_a_zombie(tmp_path, monkeypatch):
    """The end-to-end arm (bh-hqmcl AC3): a REAL process, real `ps`, real `/proc/<pid>/cwd`.

    Every other test in this section fakes `os.readlink` to produce the `" (deleted)"` marker,
    so together they prove the PARSING and prove nothing about the MECHANISM. The orphan this
    bead is about was a real process holding a real unlinked directory — if procfs did not
    actually mark it, the whole detector would be a thoroughly-tested no-op. So: start a process
    named `dolt` with `sql-server` in its argv, chdir it into a directory, delete the directory
    out from under it, and read what bh reads.

    COLUMNS IS SET EXPLICITLY (bh-8swlq), and that is what makes this a guard rather than a
    coincidence. `ps` only truncates to `$COLUMNS` when it is set, so with it unset this passes
    on a pre-fix tree — measured, line=442 chars, `sql-server` intact. It caught the truncation
    bug the first time only because pytest sets COLUMNS in its xdist workers, i.e. under
    `-n auto` and not serially. Inheriting that from the runner is not a guarantee; setting it
    is.
    """
    monkeypatch.setenv("COLUMNS", "80")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_dolt = fake_bin / "dolt"
    fake_dolt.write_text("#!/bin/sh\nwhile true; do sleep 1; done\n")
    fake_dolt.chmod(0o755)
    datadir = tmp_path / "store"
    datadir.mkdir()

    proc = subprocess.Popen(
        [str(fake_dolt), "sql-server", "--config", str(datadir / "dolt-server-config.yaml")],
        cwd=str(datadir),
    )
    try:
        deadline = time.monotonic() + 15
        mine: list = []
        while time.monotonic() < deadline:
            mine = [s for s in dolt_health.running_servers() if s.pid == proc.pid]
            if mine:
                break
            time.sleep(0.1)
        assert mine, "a real `dolt sql-server` process was not found by running_servers()"
        assert mine[0].datadir_exists is True
        assert proc.pid not in [z.pid for z in dolt_health.zombies()]

        shutil.rmtree(datadir)  # unlink the datadir out from under the running server

        after = [s for s in dolt_health.running_servers() if s.pid == proc.pid]
        assert after, "the process is still alive and must still be listed"
        assert after[0].datadir_exists is False, "procfs did not mark the unlinked cwd"
        assert after[0].datadir == str(datadir)
        assert proc.pid in [z.pid for z in dolt_health.zombies()]
    finally:
        proc.kill()
        proc.wait(timeout=10)


# ---- bulk_schema_versions: shape A + its per-hive fallback (bh-0gvs3) -------


def _bulk_res(rows):
    import json as _j

    class R:
        returncode = 0
        stdout = _j.dumps(rows)

    return R()


def test_bulk_schema_versions_reads_every_hive_in_one_call(tmp_path, monkeypatch):
    a, b = tmp_path / "a", tmp_path / "b"
    calls = []

    def fake_sql(store, query, **kw):
        calls.append(query)
        return _bulk_res([{"db": "bh", "max_version": 62}, {"db": "obs", "max_version": 61}])

    monkeypatch.setattr(dolt_health.fleet, "sql", fake_sql)
    out = dolt_health.bulk_schema_versions([(a, "bh"), (b, "obs")])
    assert len(calls) == 1, "the whole point: ONE query, not one per hive"
    assert out[a].version == 62 and out[b].version == 61


def test_bulk_schema_versions_omits_a_hive_the_server_did_not_answer_for(tmp_path, monkeypatch):
    """A missing key means UNANSWERED — the caller falls back per hive. Never 'unknown'."""
    a, b = tmp_path / "a", tmp_path / "b"
    monkeypatch.setattr(
        dolt_health.fleet, "sql", lambda *a_, **k: _bulk_res([{"db": "bh", "max_version": 62}])
    )
    out = dolt_health.bulk_schema_versions([(a, "bh"), (b, "obs")])
    assert a in out and b not in out


def test_bulk_schema_versions_returns_empty_on_query_failure(tmp_path, monkeypatch):
    """Whole-pass fallback to shape B — the safe direction."""

    class Fail:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(dolt_health.fleet, "sql", lambda *a_, **k: Fail())
    assert dolt_health.bulk_schema_versions([(tmp_path / "a", "bh")]) == {}


def test_bulk_schema_versions_drops_a_database_name_outside_the_identifier_charset(
    tmp_path, monkeypatch
):
    """Nothing unvetted reaches the query text — a qualified name cannot be a bind parameter."""
    seen = []
    monkeypatch.setattr(
        dolt_health.fleet, "sql", lambda s, q, **k: (seen.append(q), _bulk_res([]))[1]
    )
    dolt_health.bulk_schema_versions([(tmp_path / "a", "bh; DROP TABLE issues")])
    assert not seen, "a non-identifier database name must be dropped, not quoted into the query"


def test_bulk_schema_versions_on_no_targets_makes_no_call(monkeypatch):
    """An all-embedded fleet: no server databases to qualify, so no query at all."""
    monkeypatch.setattr(
        dolt_health.fleet,
        "sql",
        lambda *a_, **k: pytest.fail("must not query with nothing to query for"),
    )
    assert dolt_health.bulk_schema_versions([]) == {}
