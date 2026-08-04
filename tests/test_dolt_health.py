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
import socket
import threading
from collections import namedtuple
from pathlib import Path

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


# ---- _metadata_dolt_database / embedded_store_dir ----------------------------------------


def test_embedded_store_dir_reads_dolt_database_from_metadata(tmp_path):
    hive_dir = tmp_path / "hive"
    (hive_dir / ".beads").mkdir(parents=True)
    (hive_dir / ".beads" / "metadata.json").write_text(json.dumps({"dolt_database": "beads"}))

    # Two candidate database dirs, mirroring the real repo's own embeddeddolt/ (beads + bh) —
    # a naive "just glob the one subdir" approach would be ambiguous here.
    (hive_dir / ".beads" / "embeddeddolt" / "beads").mkdir(parents=True)
    (hive_dir / ".beads" / "embeddeddolt" / "bh").mkdir(parents=True)

    assert dolt_health.embedded_store_dir(hive_dir) == (
        hive_dir / ".beads" / "embeddeddolt" / "beads"
    )


def test_embedded_store_dir_falls_back_without_metadata(tmp_path):
    hive_dir = tmp_path / "hive"
    hive_dir.mkdir()
    assert dolt_health.embedded_store_dir(hive_dir, database="fallback") == (
        hive_dir / ".beads" / "embeddeddolt" / "fallback"
    )


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
    database name (`embedded_store_dir`'s fallback)."""
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
    assert calls["version"] == 2  # each call still re-checks WHICH bd is on PATH


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
