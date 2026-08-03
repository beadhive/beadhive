"""Where a hive's Dolt store actually lives, and whether it's embedded — as FILESYSTEM FACTS,
never a ``bd dolt status --json`` mode inference (bh-areg.1's binding reconciliation:
``safety._bd_dolt_mode()`` parses that command's ``"mode"`` key, which bd only emits for two
of its four modes and gets flat-out wrong for a third — measured against a real bd binary,
bh-u562.1 finding 9). Both facts this module answers are read straight off disk: no
subprocess, no live probe, so callers pay nothing to check speculatively.

* ``embedded_store_dir`` / ``has_embedded_store`` — where bd's legacy in-process engine puts
  its data (bh-u562.1 finding 8: ``<hive>/.beads/embeddeddolt/<db>/``), and whether that
  directory has content right now.
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


def embedded_store_dir(hive_dir: Path) -> Path:
    """Where bd's embedded engine keeps its store under ``hive_dir`` — a pure path join, not a
    probe. Callers check ``.is_dir()`` themselves (see :func:`has_embedded_store`)."""
    return Path(hive_dir) / ".beads" / EMBEDDED_STORE_NAME


def has_embedded_store(hive_dir: Path) -> bool:
    """The FILESYSTEM FACT the backup path needs: is the embedded store directory present and
    readable, checked directly — never inferred from a ``bd dolt status`` mode string."""
    return embedded_store_dir(hive_dir).is_dir()


def dolt_mode(hive_dir: Path) -> str | None:
    """bd's own persisted ``dolt_mode`` for ``hive_dir``, read straight from
    ``.beads/metadata.json`` — a plain file bd writes once at ``bd init`` and does not change
    without deliberate action, so this answers correctly even when the store directory itself
    is missing or destroyed (the case a restore exists to recover from). Returns ``None`` when
    ``.beads`` (or metadata.json) doesn't exist, isn't readable, or carries no ``dolt_mode``
    key — callers MUST treat ``None`` as unknown, never as "assume embedded"."""
    path = Path(hive_dir) / _METADATA_REL
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    mode = data.get("dolt_mode") if isinstance(data, dict) else None
    return mode if isinstance(mode, str) and mode else None


def is_embedded_mode(hive_dir: Path) -> bool:
    """Whether bd's own persisted config says ``hive_dir`` is embedded-mode. False for every
    non-embedded flavor (owned/shared/external all persist ``dolt_mode: "server"``, measured)
    AND for unknown/unreadable metadata — never a silent "assume embedded"."""
    return dolt_mode(hive_dir) == "embedded"
