"""``FileOutboxIndex``: intake's file-backed :class:`OutboxIndex` (``bh-pc9rf``).

One small JSON document (``<hive>/.bh/intake/outbox-index.json``) mapping each outbox id to the
report that holds it, so ``outbox show`` / ``approve`` / ``send`` read ONE report instead of
scanning every report's outbox (on the factory, 129 store reads per lookup).

It is a cache, never a source of truth: the sender confirms every answer against the report's own
document and on a miss scans and calls :meth:`FileOutboxIndex.rebuild`. That makes the concurrency
story simple:

* writers (``add``, ``rebuild``) serialise on an ``fcntl.flock`` of a sibling lock file, read the
  current index, merge, and publish atomically (temp file in the same directory, then
  ``os.replace``), so a reader sees the old index or the new one, never a torn file;
* readers take no lock; an unreadable, malformed or foreign-schema file reads as empty (one scan
  rebuilds it), never as an error that blocks a send;
* an update a concurrent ``rebuild`` overwrites only costs one later scan;
* an index that cannot be resolved or written (no private ``.bh/`` state, a read-only disk) is
  no index: lookups miss and updates are dropped, so the cache can never fail a send.

Local, non-secret, never published (the ids and bead ids are already in the operational store).
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import tempfile
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path

from beadhive.modules.intake.contracts import IntakeStoreError

SCHEMA = 1
INDEX_FILE = "outbox-index.json"

_log = logging.getLogger(__name__)


class FileOutboxIndex:
    """An :class:`~beadhive.modules.intake.contracts.OutboxIndex` over one file (given, or
    produced on first use by a callable so building the index resolves nothing)."""

    def __init__(self, path: Path | Callable[[], Path]) -> None:
        self._path = path

    def _file(self) -> Path | None:
        try:
            return self._path() if callable(self._path) else self._path
        except (OSError, IntakeStoreError) as exc:
            _log.debug("no outbox index: %s", exc)
            return None

    def _read(self, path: Path) -> dict[str, str]:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict) or raw.get("schema") != SCHEMA:
            return {}
        records = raw.get("records")
        if not isinstance(records, dict):
            return {}
        return {k: v for k, v in records.items() if isinstance(k, str) and isinstance(v, str)}

    def report_of(self, outbox_id: str) -> str | None:
        path = self._file()
        return (self._read(path).get(outbox_id) or None) if path is not None else None

    def add(self, entries: Mapping[str, str]) -> None:
        if entries:
            self._update(lambda current: {**current, **entries})

    def rebuild(self, entries: Mapping[str, str]) -> None:
        self._update(lambda _current: dict(entries))

    def _update(self, change: Callable[[dict[str, str]], dict[str, str]]) -> None:
        path = self._file()
        if path is None:
            return
        try:
            with self._locked(path):
                current = self._read(path)
                wanted = change(current)
                if wanted != current:  # nothing new: no rewrite
                    self._publish(path, wanted)
        except OSError as exc:  # a cache: a failed write is a later scan, never a failed send
            _log.debug("outbox index not written: %s", exc)

    @staticmethod
    @contextlib.contextmanager
    def _locked(path: Path) -> Iterator[None]:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.with_name(f".{path.name}.lock").open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)  # a writer holds it for one small rewrite
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _publish(path: Path, records: Mapping[str, str]) -> None:
        document = {"schema": SCHEMA, "records": dict(sorted(records.items()))}
        handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".ob-index-", suffix=".tmp")
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


__all__ = ["INDEX_FILE", "SCHEMA", "FileOutboxIndex"]
