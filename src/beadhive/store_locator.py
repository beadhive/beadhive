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

ONE place these are derived, so hq.py / hq_restore.py (and eventually host_fence.py —
bh-areg.6, out of scope here) share this instead of independently re-deriving the same paths
and drifting apart, which is how this went wrong the first time (bh-kobw, bh-u562.1).
"""

from __future__ import annotations

import json
from pathlib import Path

# The store's own directory name under `.beads/` for bd's legacy embedded engine (bh-u562.1,
# finding 8) — NOT shared with owned mode's sibling `.beads/dolt/`.
EMBEDDED_STORE_NAME = "embeddeddolt"

_METADATA_REL = Path(".beads") / "metadata.json"


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
