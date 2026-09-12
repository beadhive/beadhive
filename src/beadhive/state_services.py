"""Live compatibility adapters for :mod:`beadhive.modules.state`.

Adapters are constructed per request so legacy monkeypatch seams and transport-specific resource
ownership remain live. The capability itself never imports filesystem, daemon, network, or
process implementations.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import UTC, datetime

from .modules.state import (
    AgentRunSnapshot,
    ProviderSnapshot,
    ReadProjectionService,
    RunJournalFrame,
    StateClock,
    StateNotifier,
    StreamFrame,
    StreamRequest,
    ValidationQuery,
    ValidationRecord,
    ValidationRecordService,
)

SnapshotCallback = Callable[[StreamRequest], ProviderSnapshot]
FramesCallback = Callable[[StreamRequest], Iterable[StreamFrame]]
AgentRunsCallback = Callable[[str, str, str], AgentRunSnapshot]
RunJournalCallback = Callable[[str, str, str, str], RunJournalFrame]
ValidationReadCallback = Callable[[str], ValidationRecord | None]
ValidationLatestCallback = Callable[[ValidationQuery], ValidationRecord | None]
ValidationWriteCallback = Callable[[ValidationRecord], ValidationRecord]
NotificationCallback = Callable[[str, str, datetime], None]


def _unbound(*_args):
    raise RuntimeError("state capability adapter operation is not bound")


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class CallbackNotifier:
    def __init__(self, completed: NotificationCallback | None = None) -> None:
        self._completed = completed or (lambda _operation, _identity, _observed_at: None)

    def completed(self, operation: str, identity: str, observed_at: datetime) -> None:
        self._completed(operation, identity, observed_at)


class CallbackStateProjectionReader:
    def __init__(
        self,
        *,
        snapshot: SnapshotCallback = _unbound,
        frames: FramesCallback = _unbound,
    ) -> None:
        self._snapshot = snapshot
        self._frames = frames

    def snapshot(self, request: StreamRequest) -> ProviderSnapshot:
        return self._snapshot(request)

    def frames(self, request: StreamRequest) -> Iterable[StreamFrame]:
        return self._frames(request)


class CallbackActivityProjectionReader:
    def __init__(
        self,
        *,
        agent_runs: AgentRunsCallback = _unbound,
        run_journal: RunJournalCallback = _unbound,
    ) -> None:
        self._agent_runs = agent_runs
        self._run_journal = run_journal

    def agent_runs(self, source: str, host_id: str, source_key: str) -> AgentRunSnapshot:
        return self._agent_runs(source, host_id, source_key)

    def run_journal(
        self, source: str, run_id: str, host_id: str, source_key: str
    ) -> RunJournalFrame:
        return self._run_journal(source, run_id, host_id, source_key)


class CallbackValidationRecordStorage:
    def __init__(
        self,
        *,
        read: ValidationReadCallback = _unbound,
        latest: ValidationLatestCallback = _unbound,
        write: ValidationWriteCallback = _unbound,
    ) -> None:
        self._read = read
        self._latest = latest
        self._write = write

    def read(self, run_id: str) -> ValidationRecord | None:
        return self._read(run_id)

    def latest(self, query: ValidationQuery) -> ValidationRecord | None:
        return self._latest(query)

    def write(self, record: ValidationRecord) -> ValidationRecord:
        return self._write(record)


def read_projection_service(
    *,
    snapshot: SnapshotCallback = _unbound,
    frames: FramesCallback = _unbound,
    agent_runs: AgentRunsCallback = _unbound,
    run_journal: RunJournalCallback = _unbound,
    clock: StateClock | None = None,
    notifier: StateNotifier | None = None,
) -> ReadProjectionService:
    return ReadProjectionService(
        state=CallbackStateProjectionReader(snapshot=snapshot, frames=frames),
        activity=CallbackActivityProjectionReader(
            agent_runs=agent_runs,
            run_journal=run_journal,
        ),
        clock=clock or SystemClock(),
        notifier=notifier or CallbackNotifier(),
    )


def validation_record_service(
    *,
    read: ValidationReadCallback = _unbound,
    latest: ValidationLatestCallback = _unbound,
    write: ValidationWriteCallback = _unbound,
    clock: StateClock | None = None,
    notifier: StateNotifier | None = None,
) -> ValidationRecordService:
    return ValidationRecordService(
        storage=CallbackValidationRecordStorage(read=read, latest=latest, write=write),
        clock=clock or SystemClock(),
        notifier=notifier or CallbackNotifier(),
    )
