"""Store-engine health — is the dolt engine a hive depends on UP, and is its schema READABLE.

Two probes that arrived independently and now live together. Keep the distinction in mind when
editing: they answer different questions, over different transports, on different timescales.

  LIVENESS (bh-areg.3)   Is the dolt SERVER a hive is configured for actually accepting
                         connections? Opens a real socket to host:port. Sub-second, no
                         subprocess.
  SCHEMA VERSION (bh-wnly)  What migration version is this store at, and what does the local bd
                         binary support? Shells out to `dolt`/`bd sql`. Seconds, cached.

## Liveness (bh-areg.3)

Embedded mode has no liveness question at all: the engine runs in-process, so if `bd` runs, the
engine runs. A server (bd's owned/shared/external modes) can be down, wedged, on the wrong
port, or belong to a different user — and before this module NOTHING in bh reported that.

Immediate scope is mode (a) only, per `docs/design/dolt-server-mode-adr.md`: bd spawns ONE
shared `dolt sql-server` per host, at the fixed default port 3308
(`doltserver.DefaultSharedServerPort`), bind address 127.0.0.1 unless overridden by bd's OWN
`BEADS_DOLT_SERVER_HOST` / `BEADS_DOLT_SERVER_PORT` env vars. Owned mode (an OS-ephemeral port
per project) and external/remote mode ((c)-local / (c)-remote, filed as `bh-z41i` / `bh-3mik`,
both explicitly downstream of mode (a) shipping) are NOT handled here — a hive whose persisted
`dolt_mode` is `"server"` is *assumed* to be mode (a), which is correct for every hive this
fleet's own migration (`bh-areg.4`) can produce today. Revisit that assumption the moment owned
or external adoption lands.

Constraint (bh-areg.3's NOTES, operator direction 2026-08-03) — probe the ENDPOINT, never a
bd-reported PID: bh-u562.1 finding 9 measured `bd dolt status --json` reporting
`"pid": 0, "running": false` for a LIVE external server that was answering real queries, and
`safety._bd_dolt_mode()` (which reads that same JSON) was independently found unreliable
outside the modes bd itself spawned. A raw connection to host:port behaves identically under
mode (a), (c)-local and (c)-remote; asking bd for a PID does not. So the liveness probe opens a
real socket — it never shells out to `bd dolt status`.

Note the MySQL packet framing in `probe_endpoint`: the protocol-version byte is the 5th byte on
the wire, behind a 4-byte header (3-byte little-endian payload length + 1-byte sequence
number), NOT the first. Reading byte 0 rejects every real server. That bug shipped once and was
caught only by probing a live server; the fixtures in `tests/test_dolt_health.py` now emit
spec-correct framing so the suite cannot pass over it again.

## Schema version (bh-wnly)

A bd binary REFUSES to open a store whose schema is newer than it supports ("database is at
v59, binary knows up to v53"). That is a hard failure at open time, after the operator has
already committed to the operation. These probes make it a preflight instead.

TWO DECOYS, both measured, neither usable — do not "simplify" onto either:
  `bd dolt status --json` -> `schema_version`   is `cmd/bd/output.go`'s `JSONSchemaVersion`,
                                                a hardcoded `1` describing the CLI's JSON
                                                envelope. It never varies.
  `bd migrate --inspect` -> `schema_version`    is `GetLocalMetadata(ctx, "bd_version")`, the bd
                                                RELEASE STRING (e.g. "HEAD-af076b6").
The real integer lives only in the store's own `schema_migrations` table, which is what
`SCHEMA_MIGRATIONS_QUERY` reads — matching bd's own `internal/storage/schema/schema.go`
`CurrentVersion`.

`bd sql --json` and `dolt ... sql -r json` return DIFFERENT shapes for the same query — a bare
array `[{"max_version": 59}]` versus a `{"rows": [...]}` envelope. `_parse_max_version` accepts
both; a parser that handled only the envelope silently returned None for every server-mode
hive, which is the fleet's chosen mode.

No new `DoltConfig` key is added by either half: bd already owns the mode/endpoint declaration
(`BEADS_DOLT_SHARED_SERVER` / `BEADS_DOLT_SERVER_HOST` / `_PORT`, or the persisted `dolt_mode`
in `.beads/metadata.json`); this module only ever reads what is already there.

KNOWN DUPLICATION, recorded rather than silently accepted: `embedded_store_dir` below and
`store_locator.embedded_store_dir` share a name and disagree — this one returns the
per-database directory `dolt --data-dir` needs, `store_locator`'s returns the bare
`embeddeddolt/` parent. They were written on separate branches and met at merge. Consolidating
them is filed separately; until then, import deliberately and check which one you want.
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import config, store_locator
from .run import run

# bd's own shared-server defaults (internal/doltserver/doltserver.go) — read-only CONSTANTS
# mirrored here, never a bh config choice. A hive never picks these; bd does.
DEFAULT_SHARED_SERVER_HOST = "127.0.0.1"
DEFAULT_SHARED_SERVER_PORT = 3308  # doltserver.DefaultSharedServerPort

# bd's own env vars (internal/configfile/configfile.go, internal/doltserver/doltserver.go) —
# read-only detection of what bd itself already reads, not a bh config surface.
ENV_SHARED_SERVER = "BEADS_DOLT_SHARED_SERVER"
ENV_SERVER_HOST = "BEADS_DOLT_SERVER_HOST"
ENV_SERVER_PORT = "BEADS_DOLT_SERVER_PORT"

# MySQL wire protocol: every packet, the initial handshake included, is prefixed with a 4-byte
# header — a 3-byte little-endian payload length, then a 1-byte sequence number — BEFORE the
# payload begins. The protocol-version byte is the first byte of the *payload*, i.e. the 5th
# byte on the wire (index 4), not the 1st. Confirmed against a real `dolt sql-server` handshake:
#   4a 00 00 00 0a 38 2e 30 2e 33 33 00 ...
#   ^^ byte 0 = 0x4a (length header)      ^^ byte 4 = 0x0a (the actual protocol version)
# Checking it distinguishes "a dolt/MySQL server answered" from "SOME service answered" (an
# unrelated listener parked on the probed port) without needing credentials, matching the
# "wrong-port" acceptance condition — but only once the header is skipped.
_MYSQL_PROTOCOL_V10 = 0x0A
_MYSQL_HEADER_LEN = 4  # 3-byte payload length + 1-byte sequence number
_MYSQL_PROTOCOL_VERSION_OFFSET = 4  # first payload byte == 5th byte on the wire

DEFAULT_PROBE_TIMEOUT = 2.0  # seconds — a local loopback connect; generous, still bounded


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of an endpoint connection probe.

    ``reachable`` is True only when a TCP connect succeeded AND the protocol-version byte —
    the 5th byte on the wire, past the 4-byte packet header (see ``_MYSQL_HEADER_LEN``) — looked
    like a MySQL-protocol handshake. A bare TCP accept is not enough to trust: a wrong port
    pointed at an unrelated listening service would otherwise read as "up" (see module
    docstring's "wrong-port" acceptance condition).
    """

    reachable: bool
    detail: str


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    """Read exactly *n* bytes, looping on short reads — ``recv`` may return fewer bytes than
    requested even on a healthy connection. Returns whatever was read (possibly < *n*) if the
    peer closes early; the caller treats a short buffer as a legible probe failure, not an
    ``IndexError``."""
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            break
        buf += chunk
    return buf


def probe_endpoint(host: str, port: int, *, timeout: float = DEFAULT_PROBE_TIMEOUT) -> ProbeResult:
    """Endpoint-based liveness probe: connect to *host*:*port* and read past the 4-byte MySQL
    packet header to the protocol-version byte.

    Never trusts a bd-reported PID (bh-u562.1 finding 9; this bead's own NOTES constraint 1).
    Read-only and side-effect-free: no auth attempted, no query sent, nothing written.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            greeting = _recv_exact(sock, _MYSQL_PROTOCOL_VERSION_OFFSET + 1)
    except TimeoutError:
        return ProbeResult(False, f"{host}:{port} timed out after {timeout:g}s")
    except ConnectionRefusedError:
        return ProbeResult(False, f"{host}:{port} refused the connection — nothing listening")
    except OSError as exc:
        return ProbeResult(False, f"{host}:{port} unreachable — {exc}")
    if not greeting:
        return ProbeResult(False, f"{host}:{port} accepted the connection but sent nothing back")
    if len(greeting) <= _MYSQL_PROTOCOL_VERSION_OFFSET:
        return ProbeResult(
            False,
            f"{host}:{port} accepted the connection but closed before sending a full MySQL "
            f"packet header ({len(greeting)} byte(s) received) — not a MySQL/dolt server",
        )
    if greeting[_MYSQL_PROTOCOL_VERSION_OFFSET] != _MYSQL_PROTOCOL_V10:
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

# The one SQL query this whole module exists to run — see the module docstring's trap section.
# Aliased explicitly (not `SELECT MAX(version)`) so the JSON row key is stable across dolt
# versions rather than depending on dolt's own default column-naming convention.
SCHEMA_MIGRATIONS_QUERY = "SELECT MAX(version) AS max_version FROM schema_migrations"

DEFAULT_SCHEMA_PROBE_TIMEOUT = 15.0  # seconds — a local dolt/bd subprocess, no network involved
SCRATCH_INIT_TIMEOUT = 30.0  # seconds — a throwaway `bd init` used only to learn LatestVersion()

_LOCAL_VERSION_CACHE_FILENAME = "bd-schema-version.json"


@dataclass(frozen=True)
class SchemaProbeResult:
    """Outcome of a raw schema-version probe. ``version`` is the true migration-count integer
    (see module docstring) or ``None`` when it could not be determined; ``detail`` explains
    either the value's provenance or, on failure, why not — never blank."""

    version: int | None
    detail: str


def _parse_max_version(stdout: str) -> int | None:
    """Extract the first row's ``max_version`` from either of TWO real, DIFFERENT shapes the two
    tools this module shells out to actually produce for the identical query — confirmed by
    review against a live server-mode hive (`github/briancripe/testfoo`), not assumed:

      * `dolt ... sql -r json` (the embedded-mode path) wraps rows in an envelope:
        ``{"rows": [{"max_version": 59}]}``.
      * `bd sql --json` (the server-mode path) returns a BARE JSON array, no envelope at all:
        ``[{"max_version": 59}]``. Treating this as a shape mismatch (returning `None`) was a
        real bug this bead's own review caught: `_scratch_probe_local_version`/
        `probe_embedded_schema_version` only ever hit the first shape, so a test double built
        from that same call site encoded the mistaken assumption into
        `probe_server_schema_version`'s own test rather than catching it.

    Returns ``None`` on any OTHER shape mismatch — a probe that can't parse its own tool's
    output must report "unknown", never guess."""
    try:
        data = json.loads(stdout or "")
    except ValueError:
        return None
    if isinstance(data, dict):
        rows = data.get("rows")
    elif isinstance(data, list):
        rows = data
    else:
        return None
    if not isinstance(rows, list) or not rows:
        return None
    row = rows[0]
    if not isinstance(row, dict):
        return None
    value = row.get("max_version")
    return value if isinstance(value, int) else None


# ---- embedded mode: query the on-disk Dolt data directory directly --------------------------


def _metadata_dolt_database(hive_dir: Path, fallback: str) -> str:
    """The database subdirectory name under ``.beads/embeddeddolt/`` — read from
    ``.beads/metadata.json``'s ``dolt_database`` key, never assumed. A hive's embeddeddolt/ can
    hold more than one subdirectory (measured: this repo's own has both ``beads`` and ``bh``),
    so guessing "the only one" or "the one named after the prefix" is unsafe — metadata.json is
    bd's own record of which one it actually opens."""
    try:
        data = json.loads((hive_dir / ".beads" / "metadata.json").read_text())
    except (OSError, ValueError):
        return fallback
    name = data.get("dolt_database") if isinstance(data, dict) else None
    return str(name) if name else fallback


def embedded_store_dir(hive_dir: Path, *, database: str = "") -> Path:
    """Where an embedded-mode hive's real Dolt data directory lives:
    ``<hive_dir>/.beads/embeddeddolt/<database>/`` — the directory `dolt --data-dir` must point
    at (NOT the bare ``embeddeddolt/`` directory, which may hold more than one database)."""
    db = database or _metadata_dolt_database(hive_dir, hive_dir.name)
    return hive_dir / ".beads" / "embeddeddolt" / db


def probe_embedded_schema_version(
    db_dir: Path, *, timeout: float = DEFAULT_SCHEMA_PROBE_TIMEOUT
) -> SchemaProbeResult:
    """The real migration version for an embedded-mode store, queried directly via the ``dolt``
    CLI against ``db_dir`` — bypassing bd's embedded-mode ``bd sql`` refusal entirely, since the
    refusal is bd's own gate, not Dolt's (see module docstring finding 3). Read-only: a bare
    ``SELECT`` against an existing store; verified (this bead) to leave ``dolt_status`` empty
    afterward — nothing is staged or committed by running it."""
    if not (db_dir / ".dolt").is_dir():
        return SchemaProbeResult(None, f"{db_dir} is not a Dolt data directory (no .dolt/)")
    res = run(
        ["dolt", "--data-dir", str(db_dir), "sql", "-q", SCHEMA_MIGRATIONS_QUERY, "-r", "json"],
        check=False,
        capture=True,
        timeout=timeout,
    )
    if res.returncode != 0:
        detail = (res.stderr or res.stdout or "dolt sql failed").strip().splitlines()[:1]
        return SchemaProbeResult(None, f"dolt sql against {db_dir} failed: {' '.join(detail)}")
    version = _parse_max_version(res.stdout)
    if version is None:
        return SchemaProbeResult(None, f"could not parse a schema_migrations row from {db_dir}")
    return SchemaProbeResult(version, f"read directly from {db_dir}/schema_migrations")


# ---- server modes (owned/shared/external): `bd sql` over the live connection ----------------


def probe_server_schema_version(
    hive_dir: Path, *, timeout: float = DEFAULT_SCHEMA_PROBE_TIMEOUT
) -> SchemaProbeResult:
    """The real migration version for a server-mode (owned/shared/external) hive, via
    ``bd sql --json`` — the SQL path `bd` itself refuses only for embedded mode (measured:
    embedded is the specific one that errors with "'bd sql' is not yet supported in embedded
    mode"), so it is the natural fit here.

    UNVERIFIED against a live server-mode store as of this bead: no registered hive in this
    fleet runs owned/shared/external mode yet (`docs/design/dolt-server-mode-adr.md`'s
    migration is `bh-areg`'s own separate, not-yet-landed work), and this bead's own
    instructions forbid touching the one real shared-server hive that exists
    (`github/briancripe/testfoo`, port 3308). Best-effort and defensive by construction: any
    unexpected shape parses to ``None`` rather than raising, so an unverified assumption here
    can degrade to "unknown" but never to a wrong number."""
    res = run(
        ["bd", "-C", str(hive_dir), "sql", "-q", SCHEMA_MIGRATIONS_QUERY, "--json"],
        check=False,
        capture=True,
        timeout=timeout,
    )
    if res.returncode != 0:
        detail = (res.stderr or res.stdout or "bd sql failed").strip().splitlines()[:1]
        return SchemaProbeResult(None, f"bd sql against {hive_dir} failed: {' '.join(detail)}")
    version = _parse_max_version(res.stdout)
    if version is None:
        return SchemaProbeResult(None, f"could not parse a schema_migrations row from {hive_dir}")
    return SchemaProbeResult(version, f"read via `bd sql` against {hive_dir}")


# ---- mode dispatch ---------------------------------------------------------------------------

# Modes `bd dolt status --json`'s "mode" key reports that mean "a live SQL server, query it over
# the connection" rather than "an on-disk embedded store, query the data dir directly". Mirrors
# bh-u562.1 finding 9's shape table: shared reports "external"; owned/local-external report no
# "mode" key at all (both handled by the `is not "embedded"` fallback in `probe_raw_schema_version`
# below, not enumerated here, since bd's own mode string is unreliable for those two — same
# finding).
_EMBEDDED_MODE = "embedded"


def probe_raw_schema_version(
    hive_dir: Path, *, dolt_mode: str | None, timeout: float = DEFAULT_SCHEMA_PROBE_TIMEOUT
) -> SchemaProbeResult:
    """Dispatch to the embedded-direct or server-`bd sql` probe by `dolt_mode` (as reported by
    `safety._bd_dolt_mode`, the mechanism this bead's branch has available — `bh-areg.1`'s more
    precise `store_locator.dolt_mode` supersedes it once that epic lands). `dolt_mode=None`
    (bd could not report a mode at all) is treated as embedded-shaped: measured (bh-u562.1
    finding 9) that owned and local-external modes ALSO report no "mode" key, and probing the
    on-disk directory is harmless (it just won't exist) when the guess is wrong."""
    if dolt_mode is None or dolt_mode == _EMBEDDED_MODE:
        return probe_embedded_schema_version(embedded_store_dir(hive_dir), timeout=timeout)
    return probe_server_schema_version(hive_dir, timeout=timeout)


# ---- local bd binary's own supported version (LatestVersion()) ------------------------------


def _local_cache_path() -> Path:
    return config.cache_dir() / _LOCAL_VERSION_CACHE_FILENAME


def _local_bd_version_string(*, timeout: float) -> str | None:
    res = run(["bd", "--version"], check=False, capture=True, timeout=timeout)
    if res.returncode != 0:
        return None
    out = (res.stdout or res.stderr or "").strip()
    return out or None


def _read_local_cache() -> dict:
    try:
        data = json.loads(_local_cache_path().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_local_cache(data: dict) -> None:
    path = _local_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def _scratch_probe_local_version(timeout: float) -> SchemaProbeResult:
    """Learn the LOCAL bd binary's own `LatestVersion()` empirically: no CLI surface reports it
    directly in the non-error path (module docstring, finding 3's own gap applies here too), so
    this inits a THROWAWAY store in an OS temp directory — never a registered hive, never
    `~/.beadhive` — and reads its freshly-created schema version, which is `LatestVersion()` by
    construction (a fresh `bd init` runs every migration the binary knows). Cached by the caller
    so this only runs once per bd binary, not once per check (`local_bd_schema_version`)."""
    with tempfile.TemporaryDirectory(prefix="bh-wnly-schema-probe-") as tmp:
        scratch = Path(tmp)
        prefix = f"schemaprobe{uuid.uuid4().hex[:8]}"
        init = run(
            ["bd", "init", "--prefix", prefix, "--non-interactive"],
            check=False,
            capture=True,
            cwd=str(scratch),
            timeout=timeout,
        )
        if init.returncode != 0:
            detail = (init.stderr or init.stdout or "bd init failed").strip().splitlines()[:1]
            return SchemaProbeResult(None, f"scratch probe init failed: {' '.join(detail)}")
        probed = probe_embedded_schema_version(
            embedded_store_dir(scratch, database=prefix), timeout=timeout
        )
        if probed.version is None:
            return probed
        return SchemaProbeResult(probed.version, "measured via a throwaway scratch `bd init`")


def local_bd_schema_version(*, timeout: float = SCRATCH_INIT_TIMEOUT) -> SchemaProbeResult:
    """The LOCAL bd binary's own supported schema version — the other half of the comparison
    `schema_skew_advisory` needs. Cached under `config.cache_dir()`, keyed by `bd --version`'s
    exact output, so the (relatively expensive — a real `bd init`) scratch probe only runs once
    per bd binary rather than once per invocation; a `brew upgrade beads` changes the version
    string and naturally invalidates the cache."""
    version_string = _local_bd_version_string(timeout=timeout)
    if version_string is None:
        return SchemaProbeResult(None, "bd is not available (`bd --version` failed)")
    cache = _read_local_cache()
    cached = cache.get(version_string)
    if isinstance(cached, dict) and isinstance(cached.get("version"), int):
        return SchemaProbeResult(cached["version"], f"cached: {cached.get('detail', '')}".strip())

    probed = _scratch_probe_local_version(timeout)
    if probed.version is not None:
        cache[version_string] = {"version": probed.version, "detail": probed.detail}
        _write_local_cache(cache)
    return probed


# ---- the advisory (bh-gnqc's shape + tone) ---------------------------------------------------


def schema_skew_advisory(
    hive_label: str,
    local: SchemaProbeResult,
    recorded_version: int | None,
) -> str | None:
    """A one-paragraph, bh-gnqc-toned warning when the LOCAL bd's supported version is BELOW a
    hive's recorded schema version — else ``None``. Advisory, never blocking (this bead's own
    scope boundary): it does not migrate, pin, or refuse anything, only names both numbers and
    the likely cause so the operator can decide.

    Silent (returns ``None``) whenever either side is unknown — an unconfirmed local version or
    an absent recorded value is "nothing to compare", not "confirmed safe"; callers that need to
    distinguish "no skew" from "couldn't check" read `local`/`recorded_version` themselves (see
    `hive_schema`'s staleness helpers for the HQ-recorded side of that distinction — AC4's
    "never a false all-clear" is enforced by the CALLER not treating a silent return here as a
    green light, not by this function guessing)."""
    if local.version is None or recorded_version is None:
        return None
    if local.version >= recorded_version:
        return None
    return (
        f"⚠ hive '{hive_label}': recorded schema version v{recorded_version} is AHEAD of this "
        f"host's bd (supports up to v{local.version}, {recorded_version - local.version} "
        f"migration(s) behind) — opening this hive's store with this bd will likely fail with "
        f'"schema version mismatch". Likely cause: a HEAD/newer bd wrote to this hive on '
        f"another host. Escapes: upgrade this host's bd, or run it against a store that hasn't "
        f"advanced past what this bd supports. Detection only — nothing was migrated, pinned, "
        f"or blocked."
    )
