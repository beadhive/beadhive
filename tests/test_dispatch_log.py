"""The aggregate per-hive dispatch log sink (bh-e7r9q.5) — format guarantees and the
concurrent-writer contract that keeps it parseable.

`test_concurrent_writers_produce_parseable_lines` is the test the OPERATOR DECISION explicitly
asked for: N real OS processes hammering one sink concurrently, then asserting every resulting
line still `json.loads`-parses. Not simulated with threads — a thread-based version would not
exercise the actual `O_APPEND` guarantee this module's docstring rests its claim on.
"""

from __future__ import annotations

import json
import os

from beadhive import dispatch_log, log
from harness.processes import process_context


def test_truncate_field_leaves_short_values_untouched():
    assert dispatch_log.truncate_field("short") == "short"


def test_truncate_field_bounds_long_values():
    long_value = "x" * 5000
    out = dispatch_log.truncate_field(long_value)
    assert len(out) < len(long_value)
    assert out.startswith("x" * 100)
    assert "truncated" in out


def test_hive_slug_matches_sanitized_hive_key():
    entry = {"provider": "github", "org": "beadhive", "repo": "beadhive"}
    assert dispatch_log.hive_slug(entry) == "github-beadhive-beadhive"


def test_sink_path_for_slug_lives_under_dispatch_dir():
    path = dispatch_log.sink_path_for_slug("some-hive")
    assert path.parent == dispatch_log.sink_dir()
    assert path.name == "some-hive.jsonl"


def test_tail_records_returns_empty_list_for_missing_file(tmp_path):
    assert dispatch_log.tail_records(tmp_path / "nope.jsonl") == []


def test_tail_records_skips_unparseable_lines_rather_than_raising(tmp_path):
    path = tmp_path / "sink.jsonl"
    path.write_text('{"event": "a"}\nNOT JSON\n{"event": "b"}\n')
    records = dispatch_log.tail_records(path)
    assert [r["event"] for r in records] == ["a", "b"]


def test_tail_records_respects_the_lines_limit(tmp_path):
    path = tmp_path / "sink.jsonl"
    path.write_text("\n".join(json.dumps({"event": i}) for i in range(10)) + "\n")
    records = dispatch_log.tail_records(path, lines=3)
    assert [r["event"] for r in records] == [7, 8, 9]


# ---- concurrent writers: the operator-mandated proof -----------------------------------


def _write_records(sink_path: str, writer_id: int, count: int) -> None:
    """Run in a CHILD PROCESS: point the log pipeline at *sink_path* and emit *count* records —
    mirrors what an actual `bh work loop` child does under `BH_DISPATCH_LOG_SINK`."""
    log._configured = False  # each child process gets a fresh pipeline, like a real process
    log.add_file_sink(sink_path)
    logger = log.get_logger(f"writer-{writer_id}")
    for i in range(count):
        logger.info(
            "seat_spawned",
            bead=f"bh-test.{writer_id}.{i}",
            role="developer",
            action="dispatch",
            pid=os.getpid(),
            pgid=os.getpid(),
            session_id=f"{writer_id}-{i}",
        )


def test_concurrent_writers_produce_parseable_lines(tmp_path):
    sink = tmp_path / "concurrent.jsonl"
    n_writers = 6
    n_records = 40

    ctx = process_context()
    procs = [
        ctx.Process(target=_write_records, args=(str(sink), writer_id, n_records))
        for writer_id in range(n_writers)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=30)
        assert p.exitcode == 0

    lines = sink.read_text().splitlines()
    assert len(lines) == n_writers * n_records

    seen = set()
    for line in lines:
        record = json.loads(line)  # every single line MUST parse — no interleaving, no torn writes
        assert record["event"] == "seat_spawned"
        seen.add(record["session_id"])

    assert len(seen) == n_writers * n_records  # no dropped/duplicated records either
