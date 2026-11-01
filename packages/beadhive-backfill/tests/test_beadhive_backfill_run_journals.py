"""The optional run-journal reader: a missing directory is a clean no-op; bad lines are counted.

``EXAMPLE`` is ``docs/schemas/run-journal-v1.example.jsonl`` (the accepted contract's own
fixture), embedded so the package tests stay hermetic.
"""

from __future__ import annotations

import json
from pathlib import Path

from beadhive_backfill.run_journals import RunJournalRead, read_journal_text, read_run_journals

IDENTITY = {
    "version": "beadhive.run-journal/v1",
    "run_id": "run-7f6f40e1",
    "hive": "github/beadhive/beadhive",
    "bead": "bh-example.1",
    "driver": "baml",
    "provider": "claude-code",
    "manifest_digest": "sha256:" + "a" * 64,
    "provider_continuation": None,
}


def line(revision: str, millis: int, kind: str, **extra: object) -> str:
    record = {
        **IDENTITY,
        "source_revision": revision,
        "timestamp_ms": millis,
        "writer": "beadhive.local-loop",
        "activity": {"kind": kind, **extra},
    }
    return json.dumps(record, separators=(",", ":"))


EXAMPLE = "\n".join(
    [
        line("opaque:01", 1787544000000, "run.created", phase="planned"),
        line("opaque:02", 1787544000042, "process.spawned", phase="starting"),
        line(
            "opaque:04", 1787544019011, "process.harvested", phase="finished", outcome_code="done"
        ),
    ]
)


def test_missing_directory_is_a_clean_noop(tmp_path: Path) -> None:
    assert read_run_journals(tmp_path / "run-journals" / "no-such-hive") == RunJournalRead()
    assert read_run_journals(None) == RunJournalRead()
    assert read_run_journals(tmp_path / "a-file.jsonl") == RunJournalRead()  # not a directory


def test_empty_directory_is_a_noop(tmp_path: Path) -> None:
    read = read_run_journals(tmp_path)
    assert read.records == () and not read.skipped and read.journals == 0


def test_reads_records_in_file_order_with_utc_times(tmp_path: Path) -> None:
    (tmp_path / "run-7f6f40e1.jsonl").write_text(EXAMPLE + "\n")
    read = read_run_journals(tmp_path)
    assert read.journals == 1 and not read.skipped
    assert [r.kind for r in read.records] == ["run.created", "process.spawned", "process.harvested"]
    first, last = read.records[0], read.records[-1]
    assert first.at == "2026-08-24T04:00:00Z"
    assert (first.run_id, first.bead, first.driver, first.provider) == (
        "run-7f6f40e1",
        "bh-example.1",
        "baml",
        "claude-code",
    )
    assert (last.phase, last.outcome_code, last.timestamp_ms) == ("finished", "done", 1787544019011)
    assert read.for_bead("bh-example.1") == read.records and read.for_bead("bh-other") == ()


def test_journals_are_read_in_file_name_order(tmp_path: Path) -> None:
    (tmp_path / "run-b.jsonl").write_text(
        line("b:1", 2000, "run.created").replace("7f6f40e1", "b") + "\n"
    )
    (tmp_path / "run-a.jsonl").write_text(
        line("a:1", 1000, "run.created").replace("7f6f40e1", "a") + "\n"
    )
    (tmp_path / "notes.txt").write_text("ignored")
    read = read_run_journals(tmp_path)
    assert [r.run_id for r in read.records] == ["run-a", "run-b"] and read.journals == 2


def test_malformed_lines_are_counted_not_fatal() -> None:
    text = "\n".join(
        [
            line("r:1", 1000, "run.created"),
            "{not json",
            "[1, 2]",
            json.dumps(
                {**IDENTITY, "source_revision": "r:2", "timestamp_ms": "soon", "writer": "w"}
            ),
            json.dumps({**IDENTITY, "version": "beadhive.run-journal/v2"}),
            line("r:1", 1001, "run.created"),
            line("r:3", 1002, "process.spawned"),
            "",
        ]
    )
    records, skipped = read_journal_text(text + "\n")
    assert [r.source_revision for r in records] == ["r:1", "r:3"]
    assert dict(skipped) == {"malformed_line": 3, "bad_timestamp": 1, "duplicate_revision": 1}


def test_trailing_line_without_newline_is_in_flight(tmp_path: Path) -> None:
    (tmp_path / "run.jsonl").write_text(
        line("r:1", 1000, "run.created") + "\n" + line("r:2", 1001, "x")[:40]
    )
    read = read_run_journals(tmp_path)
    assert [r.source_revision for r in read.records] == ["r:1"]
    assert dict(read.skipped) == {"incomplete_tail": 1}


def test_identity_drift_is_not_merged() -> None:
    drifted = json.loads(line("r:2", 1001, "process.spawned"))
    drifted["bead"] = "bh-other"
    records, skipped = read_journal_text(
        line("r:1", 1000, "run.created") + "\n" + json.dumps(drifted) + "\n"
    )
    assert [r.source_revision for r in records] == ["r:1"]
    assert dict(skipped) == {"identity_drift": 1}


def test_run_without_a_bead_stays_run_scoped() -> None:
    record = json.loads(line("r:1", 1000, "run.created"))
    record["bead"] = None
    records, skipped = read_journal_text(json.dumps(record) + "\n")
    assert [r.bead for r in records] == [None] and not skipped


def test_unreadable_file_is_counted(tmp_path: Path) -> None:
    (tmp_path / "bad.jsonl").write_bytes(b"\xff\xfe\x00 not utf-8\n")
    (tmp_path / "ok.jsonl").write_text(line("r:1", 1000, "run.created") + "\n")
    read = read_run_journals(tmp_path)
    assert len(read.records) == 1 and dict(read.skipped) == {"unreadable_file": 1}
