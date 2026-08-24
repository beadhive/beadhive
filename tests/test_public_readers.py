"""Public bead/runtime reader composition and adversarial correlation coverage (bh-e8s3i.4)."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from beadhive import public_readers, state_stream
from beadhive.agent_run_summary import AgentRunState, AgentRunSummary, Freshness
from beadhive.state_stream_process import StreamProcessScope

NOW = datetime(2026, 8, 24, tzinfo=UTC).isoformat().replace("+00:00", "Z")
DIGEST = "sha256:" + "a" * 64


def _issue(status: str = "open") -> state_stream.StreamIssue:
    return state_stream.StreamIssue(
        id="bh-1",
        hive="beadhive",
        issue_type="task",
        status=status,
        priority="P1",
        title="Public readers",
        updated_at=NOW,
    )


def _snapshot(revision: str, status: str = "open") -> state_stream.ProviderSnapshot:
    return state_stream.ProviderSnapshot("hive", revision, NOW, (_issue(status),))


class FiniteProvider:
    name = "finite"

    def __init__(self, events):
        self.events = tuple(events)

    def updates(self, _request):
        yield from self.events


def _journal_record(
    revision: str,
    *,
    run_id: str = "outer-run",
    hive: str = "github/beadhive/beadhive",
    bead: str | None = "bh-1",
    continuation: str | None = None,
) -> dict:
    return {
        "version": "beadhive.run-journal/v1",
        "source_revision": revision,
        "timestamp_ms": 1,
        "run_id": run_id,
        "hive": hive,
        "bead": bead,
        "driver": "baml",
        "provider": "claude-code",
        "manifest_digest": DIGEST,
        "provider_continuation": continuation,
        "writer": "beadhive.local-loop",
        "activity": {"kind": "run.created", "phase": "planned"},
    }


def _write_records(path: Path, records: list[dict]) -> None:
    path.write_text("".join(json.dumps(record) + "\n" for record in records))


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[-1].split()[0]
    except OSError:
        return False
    return state != "Z"


def _wait_gone(pid: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return False


def test_hive_correlation_uses_full_registry_identity_and_repo_slug_not_prefix() -> None:
    correlation = public_readers.HiveCorrelation.from_registry_entry(
        {
            "provider": "github",
            "org": "beadhive",
            "repo": "beadhive",
            "prefix": "bh",
        }
    )

    assert correlation.registered_identity == "github/beadhive/beadhive"
    assert correlation.repo_slug == "beadhive"
    assert correlation.matches(stream_hive="beadhive", journal_hive="github/beadhive/beadhive")
    assert not correlation.matches(stream_hive="bh", journal_hive="github/beadhive/beadhive")
    assert correlation.matches_bead(
        stream_hive="beadhive",
        stream_bead="bh-1",
        journal_hive="github/beadhive/beadhive",
        journal_bead="bh-1",
    )
    assert not correlation.matches_bead(
        stream_hive="beadhive",
        stream_bead="bh-1",
        journal_hive="github/beadhive/beadhive",
        journal_bead=None,
    )


def test_bead_reader_yields_canonical_provider_frames_unchanged() -> None:
    events = (_snapshot("opaque:first"), _snapshot("opaque:second", "closed"))
    request = state_stream.StreamRequest("hive", hive="beadhive")
    expected = list(state_stream.stream_frames(FiniteProvider(events), request))
    reader = public_readers.BeadFrameReader(lambda _processes: FiniteProvider(events))

    assert list(reader.frames(request)) == expected
    assert isinstance(expected[0], state_stream.SnapshotFrame)
    assert isinstance(expected[1], state_stream.DeltaFrame)


@pytest.mark.skipif(sys.platform != "linux", reason="real process-group assertion uses /proc")
def test_closing_bead_reader_reaps_successful_provider_descendants(tmp_path: Path) -> None:
    pid_path = tmp_path / "descendant.pid"
    script = (
        "import os,time\n"
        "child=os.fork()\n"
        "if child == 0:\n"
        "    os.close(0); os.close(1); os.close(2); time.sleep(300); os._exit(0)\n"
        f"open({str(pid_path)!r}, 'w').write(str(child))\n"
    )

    class ProcessProvider:
        name = "process-provider"

        def __init__(self, processes: StreamProcessScope):
            self.processes = processes

        def updates(self, _request):
            result = self.processes.run([sys.executable, "-c", script])
            assert result.returncode == 0
            yield _snapshot("opaque:one")

    reader = public_readers.BeadFrameReader(
        lambda processes: ProcessProvider(processes),
        process_scope_factory=lambda: StreamProcessScope(timeout=5, term_grace=0.1),
    )
    frames = reader.frames(state_stream.StreamRequest("hive", hive="beadhive"))
    assert isinstance(next(frames), state_stream.SnapshotFrame)
    descendant = int(pid_path.read_text())
    assert _alive(descendant)

    frames.close()

    assert _wait_gone(descendant)


def test_agent_summary_snapshot_has_host_source_revision_and_no_fabricated_run_join(
    tmp_path: Path,
) -> None:
    sink = tmp_path / "dispatch.jsonl"
    sink.write_text(
        json.dumps(
            {
                "event": "seat_spawned",
                "timestamp": "2026-08-24T00:00:00Z",
                "bead": "bh-1",
                "role": "developer",
                "session_id": "seat-session-not-outer-run",
            }
        )
        + "\n"
    )

    snapshot = public_readers.read_agent_run_snapshot(
        sink, host_id="host-a", source_id="dispatch:beadhive"
    )

    assert snapshot.host_id == "host-a"
    assert snapshot.source_id == "dispatch:beadhive"
    assert snapshot.revision.startswith("opaque:")
    assert snapshot.coverage is public_readers.Coverage.COMPLETE
    assert snapshot.summaries[0].state is AgentRunState.STARTING
    assert snapshot.summaries[0].session_id == "seat-session-not-outer-run"
    assert snapshot.journal_correlation is public_readers.SummaryJournalCorrelation.UNAVAILABLE
    assert "do not carry the journal outer run_id" in snapshot.journal_correlation_reason


def test_missing_and_failed_summary_sources_are_not_authoritative_empty(tmp_path: Path) -> None:
    missing = public_readers.read_agent_run_snapshot(
        tmp_path / "missing.jsonl", host_id="host-a", source_id="dispatch:missing"
    )
    assert missing.summaries == ()
    assert missing.coverage is public_readers.Coverage.UNKNOWN
    assert missing.coverage_reason == "source_missing"

    sink = tmp_path / "dispatch.jsonl"
    sink.write_text("{}\n")

    def failed_projection(_path):
        raise RuntimeError("adversarial reader failure")

    failed = public_readers.read_agent_run_snapshot(
        sink,
        host_id="host-a",
        source_id="dispatch:failed",
        summary_loader=failed_projection,
    )
    assert failed.summaries == ()
    assert failed.coverage is public_readers.Coverage.DEGRADED
    assert failed.coverage_reason == "summary_projection_failed"


def test_summary_content_revision_changes_and_copied_freshness_stays_unknown(
    tmp_path: Path,
) -> None:
    sink = tmp_path / "dispatch.jsonl"
    sink.write_text("{}\n")
    first = public_readers.read_agent_run_snapshot(
        sink, host_id="host-a", source_id="copy", copied=True
    )
    sink.write_text('{"event":"foreign"}\n')
    second = public_readers.read_agent_run_snapshot(
        sink, host_id="host-a", source_id="copy", copied=True
    )

    assert first.revision != second.revision
    assert first.freshness.state == second.freshness.state == "unknown"
    assert first.freshness.detail == "copied source; live writer freshness unknown"


def test_copied_source_overrides_an_injected_optimistic_summary_freshness(tmp_path: Path) -> None:
    sink = tmp_path / "copy.jsonl"
    sink.write_text("{}\n")
    optimistic = AgentRunSummary(
        bead="bh-1",
        session_id="seat-session",
        state=AgentRunState.ACTIVE,
        freshness=Freshness(state="fresh"),
    )
    snapshot = public_readers.read_agent_run_snapshot(
        sink,
        host_id="host-a",
        source_id="copy",
        copied=True,
        summary_loader=lambda _path: [optimistic],
    )

    assert snapshot.summaries[0].freshness.state == "unknown"
    assert snapshot.summaries[0].freshness.detail == (
        "copied source; live writer freshness unknown"
    )


def test_journal_tail_is_run_scoped_snapshot_then_exact_revision_delta(tmp_path: Path) -> None:
    path = tmp_path / "outer-run.jsonl"
    first = _journal_record("opaque:first")
    second = _journal_record("opaque:second", continuation="provider-thread")
    _write_records(path, [first])
    stop = public_readers.StopToken()
    frames = public_readers.RunJournalTailReader(
        path, "outer-run", "host-a", "journal:outer-run", poll_interval=0
    ).frames(stop)

    snapshot = next(frames)
    assert snapshot.frame is public_readers.JournalFrameKind.SNAPSHOT
    assert snapshot.source_revision == "opaque:first"
    assert snapshot.records == (first,)
    _write_records(path, [first, second])
    delta = next(frames)
    assert delta.frame is public_readers.JournalFrameKind.DELTA
    assert delta.since_revision == "opaque:first"
    assert delta.source_revision == "opaque:second"
    assert delta.records == (second,)
    stop.stop()
    with pytest.raises(StopIteration):
        next(frames)


def test_unknown_journal_revision_requires_resync_then_full_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "outer-run.jsonl"
    record = _journal_record("opaque:current")
    _write_records(path, [record])
    stop = public_readers.StopToken()
    frames = public_readers.RunJournalTailReader(
        path, "outer-run", "host-a", "journal:outer-run"
    ).frames(stop, since_revision="opaque:missing")

    reset = next(frames)
    snapshot = next(frames)
    assert reset.frame is public_readers.JournalFrameKind.RESYNC
    assert reset.resync_reason is public_readers.JournalResyncReason.UNKNOWN_REVISION
    assert reset.records == ()
    assert snapshot.frame is public_readers.JournalFrameKind.SNAPSHOT
    assert snapshot.records == (record,)
    stop.stop()


def test_journal_source_replacement_resyncs_instead_of_inventing_cursor_order(
    tmp_path: Path,
) -> None:
    path = tmp_path / "outer-run.jsonl"
    first = _journal_record("opaque:first")
    replacement = _journal_record("opaque:replacement")
    _write_records(path, [first])
    stop = public_readers.StopToken()
    frames = public_readers.RunJournalTailReader(
        path, "outer-run", "host-a", "journal:outer-run", poll_interval=0
    ).frames(stop)
    next(frames)
    _write_records(path, [replacement])

    reset = next(frames)
    snapshot = next(frames)
    assert reset.frame is public_readers.JournalFrameKind.RESYNC
    assert reset.since_revision == "opaque:first"
    assert reset.resync_reason is public_readers.JournalResyncReason.SOURCE_RESET
    assert snapshot.frame is public_readers.JournalFrameKind.SNAPSHOT
    assert snapshot.records == (replacement,)
    stop.stop()


@pytest.mark.parametrize(
    ("bad_record", "reason"),
    [
        (_journal_record("opaque:second", hive="github/other/repo"), "identity_drift"),
        (_journal_record("opaque:first"), "duplicate_source_revision"),
        (
            _journal_record("opaque:second", continuation="outer-run"),
            "provider_continuation_aliases_run_id",
        ),
        (
            _journal_record("opaque:second") | {"timestamp_ms": True},
            "invalid_complete_record",
        ),
    ],
)
def test_adversarial_journal_records_degrade_without_merging_identity(
    tmp_path: Path, bad_record: dict, reason: str
) -> None:
    path = tmp_path / "outer-run.jsonl"
    first = _journal_record("opaque:first")
    _write_records(path, [first, bad_record])
    stop = public_readers.StopToken()
    frame = next(
        public_readers.RunJournalTailReader(
            path, "outer-run", "host-a", "journal:outer-run"
        ).frames(stop)
    )

    assert frame.coverage is public_readers.Coverage.DEGRADED
    assert frame.coverage_reason == reason
    assert frame.records == (first,)


def test_legacy_session_id_is_invalid_and_never_becomes_outer_run_correlation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "outer-run.jsonl"
    first = _journal_record("opaque:first")
    legacy = _journal_record("opaque:second")
    legacy["session_id"] = "outer-run"
    _write_records(path, [first, legacy])
    frame = next(
        public_readers.RunJournalTailReader(
            path, "outer-run", "host-a", "journal:outer-run"
        ).frames(public_readers.StopToken())
    )

    assert frame.coverage is public_readers.Coverage.DEGRADED
    assert frame.coverage_reason == "invalid_complete_record"
    assert frame.records == (first,)


def test_null_bead_journal_is_preserved_and_never_falsely_correlated(tmp_path: Path) -> None:
    path = tmp_path / "outer-run.jsonl"
    record = _journal_record("opaque:first", bead=None)
    _write_records(path, [record])
    stop = public_readers.StopToken()
    frame = next(
        public_readers.RunJournalTailReader(
            path, "outer-run", "host-a", "journal:outer-run"
        ).frames(stop)
    )
    correlation = public_readers.HiveCorrelation("github/beadhive/beadhive", "beadhive")

    assert frame.records[0]["bead"] is None
    assert not correlation.matches_bead(
        stream_hive="beadhive",
        stream_bead="bh-1",
        journal_hive=str(frame.records[0]["hive"]),
        journal_bead=frame.records[0]["bead"],
    )


def test_missing_and_copied_journal_sources_expose_unknown_coverage_and_freshness(
    tmp_path: Path,
) -> None:
    missing = next(
        public_readers.RunJournalTailReader(
            tmp_path / "missing.jsonl", "outer-run", "host-a", "journal:missing"
        ).frames(public_readers.StopToken())
    )
    assert missing.records == ()
    assert missing.coverage is public_readers.Coverage.UNKNOWN
    assert missing.coverage_reason == "source_missing"

    path = tmp_path / "copy.jsonl"
    _write_records(path, [_journal_record("opaque:first")])
    copied = next(
        public_readers.RunJournalTailReader(
            path, "outer-run", "host-a", "journal:copy", copied=True
        ).frames(public_readers.StopToken())
    )
    assert copied.coverage is public_readers.Coverage.COMPLETE
    assert copied.freshness.state == "unknown"
    assert copied.freshness.detail == "copied source; live writer freshness unknown"


def test_stop_token_interrupts_a_long_journal_poll_wait(tmp_path: Path) -> None:
    path = tmp_path / "outer-run.jsonl"
    _write_records(path, [_journal_record("opaque:first")])
    stop = public_readers.StopToken()
    frames = public_readers.RunJournalTailReader(
        path, "outer-run", "host-a", "journal:outer-run", poll_interval=60
    ).frames(stop)
    next(frames)
    finished = threading.Event()

    def wait_for_next() -> None:
        with pytest.raises(StopIteration):
            next(frames)
        finished.set()

    worker = threading.Thread(target=wait_for_next)
    worker.start()
    time.sleep(0.05)
    stop.stop()
    worker.join(timeout=2)

    assert finished.is_set()
    assert not worker.is_alive()
