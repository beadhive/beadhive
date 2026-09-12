"""Pure state capability services over fixture-backed ports."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from beadhive.modules.state import (
    AgentRunSnapshot,
    Coverage,
    Freshness,
    JournalFrameKind,
    ProviderSnapshot,
    ReadProjectionService,
    RunJournalFrame,
    SnapshotFrame,
    SnapshotReason,
    StreamRequest,
    StreamScope,
    ValidationQuery,
    ValidationRecord,
    ValidationRecordService,
)

NOW = datetime(2026, 9, 2, tzinfo=UTC)


class FixtureClock:
    def now(self):
        return NOW


class RecordingNotifier:
    def __init__(self):
        self.events = []

    def completed(self, operation, identity, observed_at):
        self.events.append((operation, identity, observed_at))


class FixtureValidationStorage:
    def __init__(self):
        self.records = {}

    def read(self, run_id):
        return self.records.get(run_id)

    def latest(self, query):
        matches = [
            record
            for record in self.records.values()
            if record.tree == query.tree and record.command_hash == query.command_hash
        ]
        return matches[-1] if matches else None

    def write(self, record):
        self.records[record.run_id] = record
        return record


def _record(run_id="run-1"):
    return ValidationRecord.from_mapping(
        {
            "schema": 2,
            "run_id": run_id,
            "lifecycle": "completed",
            "verdict": "green",
            "tree": "tree-1",
            "command_hash": "cmd-1",
            "provenance": {"source": "fixture"},
        }
    )


def test_validation_service_preserves_full_manifest_and_exact_identity() -> None:
    storage = FixtureValidationStorage()
    notifier = RecordingNotifier()
    service = ValidationRecordService(
        storage=storage,
        clock=FixtureClock(),
        notifier=notifier,
    )

    written = service.write(_record())
    read = service.read("run-1")
    latest = service.latest(ValidationQuery("tree-1", "cmd-1"))

    assert written is read is latest
    assert latest.to_mapping()["provenance"] == {"source": "fixture"}
    assert [event[:2] for event in notifier.events] == [
        ("validation.write", "run-1"),
        ("validation.read", "run-1"),
        ("validation.latest", "run-1"),
    ]
    assert all(event[2] == NOW for event in notifier.events)


def test_validation_service_rejects_storage_identity_drift_before_notification() -> None:
    storage = FixtureValidationStorage()
    storage.write = lambda _record: _record_with_id("other")
    notifier = RecordingNotifier()
    service = ValidationRecordService(
        storage=storage,
        clock=FixtureClock(),
        notifier=notifier,
    )

    with pytest.raises(ValueError, match="changed run identity"):
        service.write(_record())

    assert notifier.events == []


def _record_with_id(run_id):
    return ValidationRecord.from_mapping(
        {
            "run_id": run_id,
            "lifecycle": "completed",
            "verdict": "green",
            "tree": "tree-1",
            "command_hash": "cmd-1",
        }
    )


class FixtureStateReader:
    def __init__(self, snapshot, frames):
        self.value = snapshot
        self.values = tuple(frames)

    def snapshot(self, _request):
        return self.value

    def frames(self, _request):
        return iter(self.values)


class FixtureActivityReader:
    def __init__(self, agent_runs, journal):
        self.agent_runs_value = agent_runs
        self.journal_value = journal

    def agent_runs(self, _source, _host_id, _source_id):
        return self.agent_runs_value

    def run_journal(self, _source, _run_id, _host_id, _source_id):
        return self.journal_value


def test_read_service_projects_fixtures_without_storage_daemon_or_network() -> None:
    snapshot = ProviderSnapshot(StreamScope.HIVE, "rev-1", NOW.isoformat(), ())
    frame = SnapshotFrame(
        StreamScope.HIVE,
        "rev-1",
        NOW.isoformat(),
        (),
        SnapshotReason.INITIAL,
    )
    agent_runs = AgentRunSnapshot(
        "host-1",
        "source-1",
        "agent-rev",
        (),
        Coverage.COMPLETE,
        None,
        Freshness(state="fresh"),
    )
    journal = RunJournalFrame(
        JournalFrameKind.SNAPSHOT,
        "host-1",
        "source-2",
        "run-1",
        "journal-rev",
        None,
        (),
        Coverage.COMPLETE,
        None,
        Freshness(state="fresh"),
    )
    notifier = RecordingNotifier()
    service = ReadProjectionService(
        state=FixtureStateReader(snapshot, (frame,)),
        activity=FixtureActivityReader(agent_runs, journal),
        clock=FixtureClock(),
        notifier=notifier,
    )
    request = StreamRequest(StreamScope.HIVE, hive="github/beadhive/beadhive")

    assert service.snapshot(request) is snapshot
    assert tuple(service.frames(request)) == (frame,)
    assert service.agent_runs("fixture", "host-1", "source-1") is agent_runs
    assert service.run_journal("fixture", "run-1", "host-1", "source-2") is journal
    assert [event[0] for event in notifier.events] == [
        "projection.snapshot",
        "projection.frame",
        "projection.agent-runs",
        "projection.run-journal",
    ]


def test_read_service_preserves_semantic_source_identity_separate_from_lookup_key() -> None:
    snapshot = ProviderSnapshot(StreamScope.HIVE, "rev-1", NOW.isoformat(), ())
    agent_runs = AgentRunSnapshot(
        "host-1",
        "semantic-dispatch-source",
        "agent-rev",
        (),
        Coverage.COMPLETE,
        None,
        Freshness(state="fresh"),
    )
    journal = RunJournalFrame(
        JournalFrameKind.SNAPSHOT,
        "host-1",
        "semantic-journal-source",
        "run-1",
        "journal-rev",
        None,
        (),
        Coverage.COMPLETE,
        None,
        Freshness(state="fresh"),
    )
    service = ReadProjectionService(
        state=FixtureStateReader(snapshot, ()),
        activity=FixtureActivityReader(agent_runs, journal),
        clock=FixtureClock(),
        notifier=RecordingNotifier(),
    )

    assert service.agent_runs("fixture", "host-1", "poll-instance-a") is agent_runs
    assert service.run_journal("fixture", "run-1", "host-1", "poll-instance-b") is journal
    assert agent_runs.source_id == "semantic-dispatch-source"
    assert journal.source_id == "semantic-journal-source"


def test_read_service_rejects_journal_run_identity_drift() -> None:
    snapshot = ProviderSnapshot(StreamScope.HIVE, "rev-1", NOW.isoformat(), ())
    journal = RunJournalFrame(
        JournalFrameKind.SNAPSHOT,
        "host-1",
        "source-1",
        "other",
        None,
        None,
        (),
        Coverage.UNKNOWN,
        "source_missing",
        Freshness(),
    )
    service = ReadProjectionService(
        state=FixtureStateReader(snapshot, ()),
        activity=FixtureActivityReader(
            AgentRunSnapshot("host-1", "source-1", "rev", (), Coverage.UNKNOWN, None, Freshness()),
            journal,
        ),
        clock=FixtureClock(),
        notifier=RecordingNotifier(),
    )

    with pytest.raises(ValueError, match="changed run or host identity"):
        service.run_journal("fixture", "run-1", "host-1", "source-1")


def test_read_service_rejects_activity_host_identity_drift() -> None:
    snapshot = ProviderSnapshot(StreamScope.HIVE, "rev-1", NOW.isoformat(), ())
    agent_runs = AgentRunSnapshot(
        "other-host",
        "semantic-source",
        "agent-rev",
        (),
        Coverage.UNKNOWN,
        None,
        Freshness(),
    )
    service = ReadProjectionService(
        state=FixtureStateReader(snapshot, ()),
        activity=FixtureActivityReader(agent_runs, None),
        clock=FixtureClock(),
        notifier=RecordingNotifier(),
    )

    with pytest.raises(ValueError, match="changed host identity"):
        service.agent_runs("fixture", "host-1", "poll-instance")
