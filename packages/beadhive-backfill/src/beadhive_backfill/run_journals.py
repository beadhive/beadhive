"""Run-journal reader: ``<home>/run-journals/<hive>/<run_id>.jsonl`` -> typed activity records.

An optional source (``docs/design/run-journal-correlation-contract.md``): the journal is rich
host-local *activity*, never lifecycle authority, so these records only enrich a timeline
(``run.started`` / ``run.ended``-style facts). The package never imports ``beadhive``, so the
caller resolves the directory (``beadhive.run_journal.journal_root_for_hive``) and passes it in.

Defensive per the contract: a missing directory is a clean no-op (empty result, no error); a
trailing line without its newline may still be in flight and is ignored (``incomplete_tail``);
a complete line that is not a valid ``beadhive.run-journal/v1`` record, repeats a
``source_revision`` or drifts the file's identity is skipped and counted, never fatal. File order
is authoritative within one journal; journals are read in file-name order.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from beadlog_sources import bd_timestamp

VERSION = "beadhive.run-journal/v1"
SUFFIX = ".jsonl"
_IDENTITY = ("run_id", "hive", "bead", "driver", "provider", "manifest_digest")


@dataclass(frozen=True)
class RunRecord:
    """One valid journal line. ``at`` is ``timestamp_ms`` as a canonical UTC timestamp."""

    run_id: str
    hive: str
    bead: str | None  # None: the run cannot be joined to a bead and stays run-scoped
    driver: str
    provider: str
    manifest_digest: str
    writer: str
    source_revision: str
    timestamp_ms: int
    at: str
    kind: str
    phase: str | None
    outcome_code: str | None
    file: str


@dataclass(frozen=True)
class RunJournalRead:
    """Every valid record plus the counted problems. ``journals`` is the files visited."""

    records: tuple[RunRecord, ...] = ()
    skipped: Counter[str] = field(default_factory=Counter, hash=False)
    journals: int = 0

    def for_bead(self, bead: str) -> tuple[RunRecord, ...]:
        return tuple(record for record in self.records if record.bead == bead)


def _timestamp(millis: int) -> str | None:
    try:
        moment = datetime.fromtimestamp(millis / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None
    return bd_timestamp(moment.isoformat())


def _optional(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def parse_record(line: str, file: str) -> RunRecord | str:
    """One complete journal line as a :class:`RunRecord`, or the reason it is malformed."""
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return "malformed_line"
    if not isinstance(data, Mapping) or data.get("version") != VERSION:
        return "malformed_line"
    millis, activity = data.get("timestamp_ms"), data.get("activity")
    if isinstance(millis, bool) or not isinstance(millis, int) or millis < 0:
        return "bad_timestamp"
    at = _timestamp(millis)
    if at is None:
        return "bad_timestamp"
    kind = activity.get("kind") if isinstance(activity, Mapping) else None
    strings = (
        data.get(name)
        for name in ("run_id", "hive", "driver", "provider", "manifest_digest", "writer")
    )
    revision = data.get("source_revision")
    bead = data.get("bead")
    if (
        not isinstance(kind, str)
        or not kind
        or not all(isinstance(item, str) and item for item in strings)
        or not isinstance(revision, str)
        or not revision
        or not (bead is None or (isinstance(bead, str) and bead))
    ):
        return "malformed_line"
    assert isinstance(activity, Mapping)
    return RunRecord(
        run_id=data["run_id"],
        hive=data["hive"],
        bead=bead,
        driver=data["driver"],
        provider=data["provider"],
        manifest_digest=data["manifest_digest"],
        writer=data["writer"],
        source_revision=revision,
        timestamp_ms=millis,
        at=at,
        kind=kind,
        phase=_optional(activity.get("phase")),
        outcome_code=_optional(activity.get("outcome_code")),
        file=file,
    )


def read_journal_text(text: str, file: str = "") -> tuple[list[RunRecord], Counter[str]]:
    """The records of one journal's text, applying the contract's per-file rules."""
    skipped: Counter[str] = Counter()
    records: list[RunRecord] = []
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    elif lines:  # no trailing newline: the last line may still be in flight
        lines.pop()
        skipped["incomplete_tail"] += 1
    identity: tuple[Any, ...] | None = None
    revisions: set[str] = set()
    for line in lines:
        if not line.strip():
            continue
        record = parse_record(line, file)
        if isinstance(record, str):
            skipped[record] += 1
            continue
        key = tuple(getattr(record, name) for name in _IDENTITY)
        if identity is None:
            identity = key
        elif key != identity:
            skipped["identity_drift"] += 1
            continue
        if record.source_revision in revisions:
            skipped["duplicate_revision"] += 1
            continue
        revisions.add(record.source_revision)
        records.append(record)
    return records, skipped


def read_run_journals(directory: Path | str | None) -> RunJournalRead:
    """Read every ``*.jsonl`` under ``directory``; ``None`` or a missing directory is a no-op."""
    if directory is None:
        return RunJournalRead()
    root = Path(directory)
    if not root.is_dir():
        return RunJournalRead()
    records: list[RunRecord] = []
    skipped: Counter[str] = Counter()
    journals = 0
    for path in sorted(root.glob(f"*{SUFFIX}")):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            skipped["unreadable_file"] += 1
            continue
        journals += 1
        found, bad = read_journal_text(text, path.name)
        records.extend(found)
        skipped.update(bad)
    return RunJournalRead(tuple(records), skipped, journals)


__all__ = [
    "SUFFIX",
    "VERSION",
    "RunJournalRead",
    "RunRecord",
    "parse_record",
    "read_journal_text",
    "read_run_journals",
]
