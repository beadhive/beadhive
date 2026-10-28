"""``FileCheckpointStore``: intake's file-backed :class:`CheckpointStore` (I6).

One small JSON file per key under a directory in the hive's private ``.bh/`` state
(``<hive>/.bh/intake/checkpoints``), named by the SHA-256 of the key so a key can never escape the
directory. A checkpoint is an opaque, local, non-secret resume token (the tracker's own high-water
mark); losing the file only forces a full pass, which the ``(key, revision)`` upsert makes
idempotent, so failover loss is acceptable and no bead record is kept (external-intake-module-adr.md
section 4).

Writes are atomic (temp file in the same directory, then ``os.replace``), so a crash leaves either
the old mark or the new one and never a torn file. Single-writer by construction: discovery holds
the binding's ``IntakeLock`` while it ``put``s. An unreadable or malformed file reads as "no
checkpoint" (a full pass), never as an error that blocks discovery.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
from collections.abc import Callable
from pathlib import Path

from beadhive.modules.intake.contracts import Checkpoint

SCHEMA = 1


class FileCheckpointStore:
    """A :class:`~beadhive.modules.intake.contracts.CheckpointStore` over a directory (given, or
    produced on first use by a callable so building the store resolves nothing)."""

    def __init__(self, directory: Path | Callable[[], Path]) -> None:
        self._directory = directory

    def _root(self) -> Path:
        return self._directory() if callable(self._directory) else self._directory

    def _path(self, binding_key: str) -> Path:
        name = hashlib.sha256(binding_key.encode("utf-8")).hexdigest()
        return self._root() / f"{name}.json"

    def get(self, binding_key: str) -> Checkpoint | None:
        try:
            raw = json.loads(self._path(binding_key).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(raw, dict) or raw.get("schema") != SCHEMA:
            return None
        token = raw.get("token")
        return Checkpoint(token) if isinstance(token, str) and token else None

    def put(self, binding_key: str, checkpoint: Checkpoint) -> None:
        path = self._path(binding_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        document = {"schema": SCHEMA, "key": binding_key, "token": checkpoint.token}
        handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".ck-", suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as out:
                json.dump(document, out, sort_keys=True)
                out.flush()
                os.fsync(out.fileno())
            os.replace(temporary, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            raise


__all__ = ["SCHEMA", "FileCheckpointStore"]
