"""Store-engine liveness — whether the dolt SERVER a hive is configured for is actually up.

Embedded mode has no liveness question at all: the engine runs in-process, so if `bd` runs, the
engine runs. A server (bd's owned/shared/external modes) can be down, wedged, on the wrong
port, or belong to a different user — and before this module NOTHING in bh reported that
(bh-areg.3's own bead text).

Immediate scope is mode (a) only, per `docs/design/dolt-server-mode-adr.md`: bd spawns ONE
shared `dolt sql-server` per host, at the fixed default port 3308
(`doltserver.DefaultSharedServerPort`), bind address 127.0.0.1 unless overridden by bd's OWN
`BEADS_DOLT_SERVER_HOST` / `BEADS_DOLT_SERVER_PORT` env vars. Owned mode (an OS-ephemeral port
per project) and external/remote mode ((c)-local / (c)-remote, filed as `bh-z41i` / `bh-3mik`,
both explicitly downstream of mode (a) shipping) are NOT handled here — a hive whose persisted
`dolt_mode` is `"server"` is *assumed* to be mode (a), which is correct for every hive this
fleet's own migration (`bh-areg.4`) can produce today. Revisit this assumption the moment owned
or external adoption lands.

Constraint (bh-areg.3's own NOTES field, operator direction 2026-08-03) — probe the ENDPOINT,
never a bd-reported PID: bh-u562.1 finding 9 measured `bd dolt status --json` reporting
`"pid": 0, "running": false` for a LIVE external server that was answering real queries, and
`safety._bd_dolt_mode()` (which reads that same JSON) was independently found unreliable
outside the modes bd itself spawned (bh-areg.1's `store_locator.py`, same finding). A raw
connection to host:port behaves identically under mode (a), (c)-local, and (c)-remote; asking
bd for a PID does not. So the liveness probe here opens a real socket — it never shells out to
`bd dolt status`.

No new `DoltConfig` key is added (bh-areg.3's own constraint 2): bd already owns the mode/
endpoint declaration (`BEADS_DOLT_SHARED_SERVER` / `BEADS_DOLT_SERVER_HOST` / `_PORT`, or the
persisted `dolt_mode` in `.beads/metadata.json`); this module only ever reads what's already
there.

Public API
----------
- ``ProbeResult``            — reachable/detail outcome of an endpoint connection probe
- ``probe_endpoint(host, port)`` — low-level connect-and-verify-MySQL-handshake probe
- ``server_endpoint()``       — mode (a)'s host/port (env override, else the shared default)
- ``probe_shared_server()``   — ``probe_endpoint(*server_endpoint())`` convenience
- ``shared_server_env_active()`` — does THIS process's environment turn shared-server mode on
- ``mismatch_reason(hive_dir)`` — non-``None`` only when persisted `dolt_mode` disagrees with
  the currently-active env (mirrors bd's own `main.go:warnSharedServerEmbeddedMismatch`)
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from pathlib import Path

from . import store_locator

# bd's own shared-server defaults (internal/doltserver/doltserver.go) — read-only CONSTANTS
# mirrored here, never a bh config choice. A hive never picks these; bd does.
DEFAULT_SHARED_SERVER_HOST = "127.0.0.1"
DEFAULT_SHARED_SERVER_PORT = 3308  # doltserver.DefaultSharedServerPort

# bd's own env vars (internal/configfile/configfile.go, internal/doltserver/doltserver.go) —
# read-only detection of what bd itself already reads, not a bh config surface.
ENV_SHARED_SERVER = "BEADS_DOLT_SHARED_SERVER"
ENV_SERVER_HOST = "BEADS_DOLT_SERVER_HOST"
ENV_SERVER_PORT = "BEADS_DOLT_SERVER_PORT"

# MySQL wire protocol: the server's very first byte back is the protocol version. Every
# MySQL-protocol server (dolt's sql-server included) sends protocol version 10 (0x0a) as
# literally the first byte of its greeting, before any auth exchange — checking it distinguishes
# "a dolt/MySQL server answered" from "SOME service answered" (an unrelated listener parked on
# the probed port) without needing credentials, matching the "wrong-port" acceptance condition.
_MYSQL_PROTOCOL_V10 = 0x0A

DEFAULT_PROBE_TIMEOUT = 2.0  # seconds — a local loopback connect; generous, still bounded


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of an endpoint connection probe.

    ``reachable`` is True only when a TCP connect succeeded AND the first byte back looked
    like a MySQL-protocol handshake — a bare TCP accept is not enough to trust: a wrong port
    pointed at an unrelated listening service would otherwise read as "up" (see module
    docstring's "wrong-port" acceptance condition).
    """

    reachable: bool
    detail: str


def probe_endpoint(host: str, port: int, *, timeout: float = DEFAULT_PROBE_TIMEOUT) -> ProbeResult:
    """Endpoint-based liveness probe: connect to *host*:*port* and read back one byte.

    Never trusts a bd-reported PID (bh-u562.1 finding 9; this bead's own NOTES constraint 1).
    Read-only and side-effect-free: no auth attempted, no query sent, nothing written.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            greeting = sock.recv(1)
    except TimeoutError:
        return ProbeResult(False, f"{host}:{port} timed out after {timeout:g}s")
    except ConnectionRefusedError:
        return ProbeResult(False, f"{host}:{port} refused the connection — nothing listening")
    except OSError as exc:
        return ProbeResult(False, f"{host}:{port} unreachable — {exc}")
    if not greeting:
        return ProbeResult(False, f"{host}:{port} accepted the connection but sent nothing back")
    if greeting[0] != _MYSQL_PROTOCOL_V10:
        return ProbeResult(
            False,
            f"{host}:{port} answered, but not with a MySQL/dolt protocol handshake — wrong "
            "port, or a different service is listening there",
        )
    return ProbeResult(True, f"{host}:{port} reachable")


def server_endpoint() -> tuple[str, int]:
    """Host/port bd's mode-(a) shared server actually binds: bd's own env override
    (`BEADS_DOLT_SERVER_HOST` / `_PORT`) if set — exactly bd's own resolution order — else
    the fixed shared-mode default (127.0.0.1:3308)."""
    host = os.environ.get(ENV_SERVER_HOST) or DEFAULT_SHARED_SERVER_HOST
    port_raw = os.environ.get(ENV_SERVER_PORT)
    if port_raw:
        try:
            return host, int(port_raw)
        except ValueError:
            pass
    return host, DEFAULT_SHARED_SERVER_PORT


def probe_shared_server(*, timeout: float = DEFAULT_PROBE_TIMEOUT) -> ProbeResult:
    """Convenience: probe mode (a)'s shared-server endpoint (``server_endpoint()``)."""
    host, port = server_endpoint()
    return probe_endpoint(host, port, timeout=timeout)


def shared_server_env_active() -> bool:
    """True iff THIS process's environment turns bd's shared-server mode on for this
    invocation — mirrors bd's own `doltserver.IsSharedServerMode()` env-var check
    (`BEADS_DOLT_SHARED_SERVER` = "1" or "true", case-insensitive)."""
    v = os.environ.get(ENV_SHARED_SERVER, "")
    return v == "1" or v.strip().lower() == "true"


def mismatch_reason(hive_dir: Path) -> str | None:
    """Non-``None`` only when this run's active engine mode disagrees with what's persisted in
    ``hive_dir``'s ``.beads/metadata.json`` — i.e. shared-server mode is active for this
    process, but the committed metadata still pins `dolt_mode: "embedded"`.

    Mirrors bd's own `main.go:warnSharedServerEmbeddedMismatch`: bd's env wins for THIS
    invocation regardless of the committed metadata.json, and bd never rewrites that file to
    match, so the drift persists silently until an operator notices (or `bh doctor` says so —
    bd's own warning only fires on a live `bd` invocation an operator happens to be watching)."""
    if store_locator.dolt_mode(hive_dir) != "embedded":
        return None
    if not shared_server_env_active():
        return None
    return (
        f"shared-server mode is active ({ENV_SHARED_SERVER}) but "
        f'{hive_dir}/.beads/metadata.json pins dolt_mode="embedded" — bd uses the shared '
        'server for this run anyway; commit dolt_mode="server" to metadata.json to make '
        f"that durable, or unset {ENV_SHARED_SERVER} to stay embedded"
    )
