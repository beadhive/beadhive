"""Store-engine health probes: the REAL bd/Dolt schema-migration version, and the local bd
binary's own supported version — the two numbers `bh-wnly` exists to compare.

THIS BEAD'S SLICE ONLY. `bh-areg.3` (a parallel, longer-running epic, not yet merged into
`main` as this module is written) independently ships an endpoint-LIVENESS probe
(`probe_shared_server` / `mismatch_reason`) into a module of this same name — deliberately, per
this bead's own instructions, so the two land in ONE file rather than two competing ones. If
you're reading this after that epic merged and see liveness-probe code here too, that's
expected convergence, not drift: reconcile at merge time, don't re-split.

THE TRAP THIS MODULE EXISTS TO AVOID — read before touching anything below that returns a
"schema_version". There are, confusingly, THREE different things bd calls "schema_version",
and only ONE of them is the migration-count integer (`v53`, `v59`, ...) the open-time error

    Error: failed to open Dolt store: schema version mismatch: database is at v59,
    binary knows up to v53 (6 migrations ahead)

actually means:

  1. ``bd dolt status --json``'s ``"schema_version"`` field is ``cmd/bd/output.go``'s
     ``JSONSchemaVersion`` constant — the CLI's OWN JSON envelope format version. It is
     hardcoded ``1`` and does not change across dolt migrations at all. Measured across all
     four bd server modes (bh-u562.1 finding 9): every payload shows ``"schema_version": 1``.
     Confirmed again here, freshly, against a real embedded-mode store on the machine that
     authored this bead: identical ``1``.
  2. ``bd migrate --inspect``'s (and ``bd info --schema``'s) ``"schema_version"`` field is
     ``store.GetLocalMetadata(ctx, "bd_version")`` — the RELEASE STRING of whichever bd binary
     last wrote the store (e.g. ``"HEAD-af076b6"``), not an integer and not a migration count.
     Measured directly against a real embedded-mode store (this bead's own scratch probe):
     ``bd migrate --inspect`` printed ``Schema Version: HEAD-af076b6`` for a store whose real
     migration version (see below) was ``59``.
  3. The REAL migration-count integer lives only in the store's own ``schema_migrations`` SQL
     table (``MAX(version)``) — bd's Go source (``internal/storage/schema/schema.go``,
     ``CurrentVersion``/``LatestVersion``) reads it that way internally, but exposes NO read-only
     CLI surface for it in the non-error path. ``bd sql`` can query it directly, but is refused
     in embedded mode (``'bd sql' is not yet supported in embedded mode``). The one thing that
     DOES reach it, for any mode where the on-disk Dolt data directory is locally reachable
     (embedded, owned, and a locally-hosted external server), is the ``dolt`` CLI itself,
     querying the data directory directly — bypassing bd's embedded-mode gate entirely, because
     the gate is bd's, not Dolt's. Verified empirically (this bead): a scratch ``bd init``
     store's ``dolt --data-dir <embeddeddolt>/<db> sql -q "SELECT MAX(version) ..." -r json``
     returned ``{"rows": [{"max_version": 59}]}`` — the exact number the open-time skew error
     would have reported, obtained WITHOUT triggering that error and WITHOUT any network access.

So: never read ``schema_version`` out of ``bd dolt status`` or ``bd migrate --inspect`` and
call it the migration version — both are real fields with real meanings, just not this one.
``probe_raw_schema_version`` below is the one function in bh that reads the true value, and it
does so by querying ``schema_migrations`` directly rather than trusting either decoy.

Constraint mirrored from bh-areg.3 (the sibling probe module this one shares a name with):
probe the STORE directly, never trust a bd-reported field that's been shown unreliable by
measurement (bh-u562.1 finding 9 again, for a different field this time).
"""

from __future__ import annotations

import json
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import config
from .run import run

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
