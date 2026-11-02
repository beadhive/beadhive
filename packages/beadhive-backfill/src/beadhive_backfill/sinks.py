"""Store-neutral adapters for the envelope's durable ports.

- :class:`JsonFileAuditSink` keeps the latest audit record as a JSON file beside the plan,
  replaced atomically on every record (complexity's ``--audit`` file, generalized).
- :class:`ArchivePreStateExporter` writes the pre-apply baseline as a ``.bdlg`` archive. A hive
  wires a store-native exporter instead when its restore tooling wants that (bd: ``bd export``).
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Collection, Sequence
from pathlib import Path

from beadlog import BeadEvent, Generator, Group, write_archive

from .contracts import AuditEntry
from .plan import GENERATOR


class JsonFileAuditSink:
    """Persists each :class:`AuditEntry` to ``path`` (write to a sibling, then rename)."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)

    def record(self, entry: AuditEntry) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=self.path.parent,
            prefix=f".{self.path.name}.",
            delete=False,
        ) as handle:
            json.dump(entry.to_json(), handle, sort_keys=True, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        temporary.replace(self.path)


class ArchivePreStateExporter:
    """Writes the baseline the engine read as a ``.bdlg`` recovery archive at ``path``.

    With ``only`` (bead ids) the archive holds just those beads' events: the recovery artifact of
    a small incremental run is the beads it may change, not the whole corpus."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        groups: Sequence[Group],
        generator: Generator = GENERATOR,
        only: Collection[str] | None = None,
    ) -> None:
        self.path = Path(path)
        self.groups = tuple(groups)
        self.generator = generator
        self.only = None if only is None else frozenset(only)

    def export_pre_state(self, baseline: Sequence[BeadEvent]) -> str:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        events = (
            baseline
            if self.only is None
            else [event for event in baseline if event.bead.id in self.only]
        )
        write_archive(self.path, events, groups=self.groups, generator=self.generator)
        return str(self.path)
