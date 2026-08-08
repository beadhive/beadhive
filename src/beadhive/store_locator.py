"""Where a hive's Dolt store actually lives, and whether it's embedded — as FILESYSTEM FACTS,
never a ``bd dolt status --json`` mode inference (bh-areg.1's binding reconciliation:
``safety._bd_dolt_mode()`` parses that command's ``"mode"`` key, which bd only emits for two
of its four modes and gets flat-out wrong for a third — measured against a real bd binary,
bh-u562.1 finding 9). Both facts this module answers are read straight off disk: no
subprocess, no live probe, so callers pay nothing to check speculatively.

* ``embedded_store_dir`` / ``has_embedded_store`` — the bare ``<hive>/.beads/embeddeddolt/``
  parent bd's legacy in-process engine keeps its data under, and whether that directory has
  content right now.
* ``embedded_database_dir`` / ``dolt_database`` — the ONE database inside that parent
  (bh-u562.1 finding 8: ``<hive>/.beads/embeddeddolt/<db>/``), which is what ``dolt
  --data-dir`` must be pointed at. A hive's ``embeddeddolt/`` can hold more than one (measured:
  this repo's own holds both ``beads`` and ``bh``), so the two are genuinely different facts
  and are named differently on purpose — ``bh-z9h7`` exists because they were briefly both
  called ``embedded_store_dir``, in two modules, returning paths one directory level apart.
* ``dolt_mode`` / ``is_embedded_mode`` — what bd itself persisted as this hive's engine mode,
  read from ``.beads/metadata.json``'s own ``dolt_mode`` field (measured against a real bd
  binary: ``"embedded"`` for embedded mode, ``"server"`` for every one of owned/shared/
  external — bd does not distinguish those three at this layer, which is fine, since this
  module only ever needs "embedded, or not"). Deliberately NOT the file ``bd dolt status
  --json`` renders from: that command's own JSON shape is what's ambiguous (finding 9);
  metadata.json is bd's static, already-committed answer to "how was this hive configured",
  unaffected by whether the directory it names currently has anything in it — which is
  exactly the fact a restore needs (recovering a destroyed store is the whole point).

* ``server_store_dir`` / ``store_dir`` — the same "parent that holds this hive's databases"
  fact for bd's shared server, and the MODE-AWARE selector between the two. Under a server the
  databases move out of the hive tree entirely, to ``~/.beads/shared-server/dolt/`` — but only
  the parent changes: ``<db>/.dolt/…`` below it is byte-identical to embedded's (measured,
  ``bh-ukit.2``). Every path fact here is therefore one join off :func:`store_dir`.

ONE place these are derived, so hq.py / hq_restore.py / host_fence.py share this instead of
independently re-deriving the same paths and drifting apart, which is how this went wrong the
first time (bh-kobw, bh-u562.1) and again in a different shape (bh-z9h7).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# The store's own directory name under `.beads/` for bd's legacy embedded engine (bh-u562.1,
# finding 8) — NOT shared with owned mode's sibling `.beads/dolt/`.
EMBEDDED_STORE_NAME = "embeddeddolt"

# bd's shared server keeps every database under `<shared-server dir>/dolt/<db>` (bh-u562.1
# finding 8, re-measured in bh-ukit.2 against bd HEAD-af076b6). `BEADS_SHARED_SERVER_DIR` is
# bd's OWN env override for the root — read here, never a bh config surface.
SERVER_STORE_NAME = "dolt"
ENV_SHARED_SERVER_DIR = "BEADS_SHARED_SERVER_DIR"
_DEFAULT_SHARED_SERVER_DIR = Path.home() / ".beads" / "shared-server"

_METADATA_REL = Path(".beads") / "metadata.json"

# The metadata key naming this hive's database ON THE SHARED SERVER, kept separate from bd's own
# `dolt_database` (which names the embedded DIRECTORY). See :func:`server_database` for why the
# two must not be the same key. Additive: bd and older bh ignore it.
SERVER_DATABASE_KEY = "dolt_server_database"


def _read_metadata(hive_dir: Path) -> dict:
    """``.beads/metadata.json`` as a dict, or ``{}`` when absent/unreadable/not an object — the
    one read every metadata-derived fact below goes through."""
    try:
        data = json.loads((Path(hive_dir) / _METADATA_REL).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def embedded_store_dir(hive_dir: Path) -> Path:
    """Where bd's embedded engine keeps its store under ``hive_dir`` — a pure path join, not a
    probe. Callers check ``.is_dir()`` themselves (see :func:`has_embedded_store`)."""
    return Path(hive_dir) / ".beads" / EMBEDDED_STORE_NAME


def has_embedded_store(hive_dir: Path) -> bool:
    """The FILESYSTEM FACT the backup path needs: is the embedded store directory present and
    readable, checked directly — never inferred from a ``bd dolt status`` mode string."""
    return embedded_store_dir(hive_dir).is_dir()


def dolt_database(hive_dir: Path, fallback: str = "") -> str:
    """Which database under :func:`embedded_store_dir` bd actually opens for ``hive_dir`` — read
    from ``.beads/metadata.json``'s ``dolt_database`` key, never assumed. Guessing "the only
    subdirectory" or "the one named after the prefix" is unsafe (a hive can hold more than one);
    metadata.json is bd's own record. Falls back to ``fallback``, or the hive directory's own
    name when no fallback is given."""
    data = _read_metadata(hive_dir)
    name = data.get("dolt_database")
    return str(name) if name else (fallback or Path(hive_dir).name)


def embedded_database_dir(hive_dir: Path, *, database: str = "") -> Path:
    """The real Dolt data directory for an embedded-mode hive —
    ``<hive_dir>/.beads/embeddeddolt/<database>/``, the directory ``dolt --data-dir`` must point
    at. NOT :func:`embedded_store_dir`, which is its parent and may hold several databases."""
    return embedded_store_dir(hive_dir) / (database or dolt_database(hive_dir))


def sanitize_database_name(name: str) -> str:
    """A hive prefix rendered as a Dolt/MySQL-safe database identifier: anything outside
    ``[A-Za-z0-9_]`` becomes ``_`` (so ``bh-infra`` -> ``bh_infra``, ``ag-cp`` -> ``ag_cp``),
    runs collapse, and a leading digit is prefixed since bare identifiers can't start with one.

    This is the SAME shape bd already produces for the hives that happen to have distinct names
    today (``ag_cp`` / ``ag_rt``) — codified here rather than left to coincidence."""
    out = "".join(ch if (ch.isalnum() and ch.isascii()) or ch == "_" else "_" for ch in name)
    while "__" in out:
        out = out.replace("__", "_")
    out = out.strip("_")
    return f"db_{out}" if out[:1].isdigit() else out


def server_database(hive_dir: Path, fallback: str = "") -> str:
    """Which database bd's SHARED SERVER opens for ``hive_dir`` — a DIFFERENT fact from
    :func:`dolt_database`, and deliberately a different metadata key (bh-g5ujg).

    ``dolt_database`` names a directory INSIDE one repo, where nothing requires uniqueness; bd
    defaults it to ``beads`` for almost every hive. On a shared server that same string becomes a
    database name in a namespace every hive shares, so six hives collapse onto one store. Keeping
    the server name in its own additive key means migrating one host never rewrites the key an
    UN-migrated host still resolves its embedded directory through (``.beads/metadata.json`` is
    git-tracked, so a rewrite would propagate on pull and break the other host immediately).

    Resolution order:
      1. an explicit ``dolt_server_database`` — always wins, so a name persisted at migrate time
         is never recomputed out from under a working hive;
      2. for a hive ALREADY in server mode with no such key, its ``dolt_database`` — the
         pre-key behavior, grandfathered (``observaloop`` must not become ``obs``);
      3. otherwise the sanitized ``fallback`` (the hive prefix, which bh already enforces unique
         fleet-wide), so a hive being migrated gets a collision-free name by construction.
    """
    name = _read_metadata(hive_dir).get(SERVER_DATABASE_KEY)
    if name:
        return str(name)
    if not is_embedded_mode(hive_dir):
        return dolt_database(hive_dir, fallback)
    return sanitize_database_name(fallback) if fallback else dolt_database(hive_dir)


def shared_server_dir() -> Path:
    """bd's shared-server root — ``$BEADS_SHARED_SERVER_DIR`` if bd's own env override is set,
    else ``~/.beads/shared-server``. Host-wide, not per-hive: one server per host serves every
    server-mode hive on it."""
    root = os.environ.get(ENV_SHARED_SERVER_DIR)
    return Path(root).expanduser() if root else _DEFAULT_SHARED_SERVER_DIR


def server_store_dir() -> Path:
    """The parent holding every server-mode database — :func:`shared_server_dir` / ``dolt``.
    The server-mode counterpart of :func:`embedded_store_dir`, and the ONLY thing that differs
    between the two modes: below this, ``<db>/.dolt/…`` is identical (bh-ukit.2)."""
    return shared_server_dir() / SERVER_STORE_NAME


def store_dir(hive_dir: Path) -> Path:
    """MODE-AWARE: the parent that holds ``hive_dir``'s databases, per bd's own persisted
    ``dolt_mode`` — :func:`embedded_store_dir` for embedded, :func:`server_store_dir` for
    server. Prefer this over either half wherever a caller means "wherever this hive's data
    actually is" (``host_fence``'s transport-repo discovery is the motivating case): the
    hive-relative assumption is precisely what broke under a server."""
    return embedded_store_dir(hive_dir) if is_embedded_mode(hive_dir) else server_store_dir()


def database_dir(hive_dir: Path, *, database: str = "") -> Path:
    """MODE-AWARE per-database directory — :func:`store_dir` / ``<database>``. The mode-aware
    counterpart of :func:`embedded_database_dir`, kept distinct from it by the same naming rule
    bh-z9h7 established: the parent and the one database never share a name.

    The NAME is mode-aware too (bh-g5ujg), not just the parent: embedded resolves through
    ``dolt_database``, server through :func:`server_database`. For a hive with no
    ``dolt_server_database`` key the two agree, so this is unchanged for every hive that predates
    that key."""
    if database:
        return store_dir(hive_dir) / database
    name = dolt_database(hive_dir) if is_embedded_mode(hive_dir) else server_database(hive_dir)
    return store_dir(hive_dir) / name


def dolt_mode(hive_dir: Path) -> str | None:
    """bd's own persisted ``dolt_mode`` for ``hive_dir``, read straight from
    ``.beads/metadata.json`` — a plain file bd writes once at ``bd init`` and does not change
    without deliberate action, so this answers correctly even when the store directory itself
    is missing or destroyed (the case a restore exists to recover from). Returns ``None`` when
    ``.beads`` (or metadata.json) doesn't exist, isn't readable, or carries no ``dolt_mode``
    key — callers MUST treat ``None`` as unknown, never as "assume embedded"."""
    mode = _read_metadata(hive_dir).get("dolt_mode")
    return mode if isinstance(mode, str) and mode else None


def is_embedded_mode(hive_dir: Path) -> bool:
    """Whether bd's own persisted config says ``hive_dir`` is embedded-mode. False for every
    non-embedded flavor (owned/shared/external all persist ``dolt_mode: "server"``, measured)
    AND for unknown/unreadable metadata — never a silent "assume embedded"."""
    return dolt_mode(hive_dir) == "embedded"


def ensure_server_mode_persisted(hive_dir: Path) -> bool:
    """Persist ``dolt_mode: "server"`` into ``hive_dir``'s ``.beads/metadata.json`` if it isn't
    already there. The write-side counterpart to :func:`dolt_mode`, for the ONE mutation every
    caller minting/bootstrapping a hive straight onto server mode needs: a per-invocation
    activation (``--shared-server`` / ``BEADS_DOLT_SHARED_SERVER=1``) is NOT durable on its own
    — bd's own ``main.go:warnSharedServerEmbeddedMismatch`` documents this exact drift, and
    :func:`is_embedded_mode` depends on it never happening (bh-areg.1/bh-areg.4/bh-areg.7's
    shared constraint). Still a pure file read+write, no subprocess — callers own asserting the
    durable ``dolt.shared-server`` CONFIG key themselves via their own ``bd config set`` (a real
    bd invocation, deliberately kept out of this module, per the module docstring's "no
    subprocess" promise).

    Returns True iff it had to write — the common, measured case (a FRESH, non-``--reinit-
    local`` ``bd init``/``bd bootstrap`` already persists this correctly on its own) is a
    no-op; callers should treat a True return as worth a visible warning, never a silent
    patch (see ``onboard._ensure_server_mode_persisted``). Also a no-op (returns False,
    writes nothing) when ``hive_dir/.beads`` doesn't exist at all — there is no store here to
    persist a mode for; never manufactures one."""
    if dolt_mode(hive_dir) == "server":
        return False
    path = Path(hive_dir) / _METADATA_REL
    if not path.parent.is_dir():
        return False
    data = _read_metadata(hive_dir)
    data["dolt_mode"] = "server"
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    return True


def ensure_server_database_persisted(hive_dir: Path, database: str) -> bool:
    """Persist ``dolt_server_database: <database>`` into ``.beads/metadata.json``, the write-side
    counterpart to :func:`server_database` (bh-g5ujg).

    Migration MUST call this: the name is otherwise only ever DERIVED from the prefix, and a
    derivation is not a record. Re-deriving later is exactly how an already-migrated hive gets
    "corrected" onto a name its store isn't under — ``observaloop`` (prefix ``obs``) is the live
    example. Writing it down makes resolution order 1 in :func:`server_database` win forever.

    Deliberately does NOT touch ``dolt_database``: that key still names the embedded directory,
    including the moved-aside pre-migrate copy a rollback restores. Returns True iff it wrote;
    a no-op (False) when the value already matches or ``.beads`` doesn't exist."""
    if not database or _read_metadata(hive_dir).get(SERVER_DATABASE_KEY) == database:
        return False
    path = Path(hive_dir) / _METADATA_REL
    if not path.parent.is_dir():
        return False
    data = _read_metadata(hive_dir)
    data[SERVER_DATABASE_KEY] = database
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    return True
