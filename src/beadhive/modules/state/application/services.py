"""Application services for durable records and existing read projections."""

from __future__ import annotations

from collections.abc import Iterator

from ..contracts import (
    ActivityProjectionReader,
    StateClock,
    StateNotifier,
    StateProjectionReader,
    ValidationRecordStorage,
)
from ..domain.projections import AgentRunSnapshot, RunJournalFrame
from ..domain.stream import ProviderSnapshot, StreamFrame, StreamRequest
from ..domain.validation import ValidationQuery, ValidationRecord


class ValidationRecordService:
    """Coordinate typed validation facts over a replaceable durable store."""

    def __init__(
        self,
        *,
        storage: ValidationRecordStorage,
        clock: StateClock,
        notifier: StateNotifier,
    ) -> None:
        self._storage = storage
        self._clock = clock
        self._notifier = notifier

    def read(self, run_id: str) -> ValidationRecord | None:
        record = self._storage.read(run_id)
        if record is not None:
            if record.run_id != run_id:
                raise ValueError("validation storage changed run identity")
            self._notifier.completed("validation.read", record.run_id, self._clock.now())
        return record

    def latest(self, query: ValidationQuery) -> ValidationRecord | None:
        record = self._storage.latest(query)
        if record is not None:
            if record.tree != query.tree or record.command_hash != query.command_hash:
                raise ValueError("validation storage changed query identity")
            self._notifier.completed("validation.latest", record.run_id, self._clock.now())
        return record

    def write(self, record: ValidationRecord) -> ValidationRecord:
        written = self._storage.write(record)
        if written.run_id != record.run_id:
            raise ValueError("validation storage changed run identity")
        self._notifier.completed("validation.write", written.run_id, self._clock.now())
        return written


class ReadProjectionService:
    """Typed query boundary for existing snapshot, replay, and activity readers.

    This is deliberately read-side only. Commands still use their established application
    services and durable authorities; no generalized event store or command model is introduced.
    """

    def __init__(
        self,
        *,
        state: StateProjectionReader,
        activity: ActivityProjectionReader,
        clock: StateClock,
        notifier: StateNotifier,
    ) -> None:
        self._state = state
        self._activity = activity
        self._clock = clock
        self._notifier = notifier

    def snapshot(self, request: StreamRequest) -> ProviderSnapshot:
        snapshot = self._state.snapshot(request)
        self._notifier.completed("projection.snapshot", snapshot.revision, self._clock.now())
        return snapshot

    def frames(self, request: StreamRequest) -> Iterator[StreamFrame]:
        for frame in self._state.frames(request):
            identity = str(getattr(frame, "revision", ""))
            self._notifier.completed("projection.frame", identity, self._clock.now())
            yield frame

    def agent_runs(self, source: str, host_id: str, source_key: str) -> AgentRunSnapshot:
        result = self._activity.agent_runs(source, host_id, source_key)
        # ``source_key`` identifies the requested reader instance. ``result.source_id`` is the
        # source's durable semantic identity and is allowed to differ (for example when a new
        # polling process reads the same dispatch summary). The host boundary is owned here and
        # must remain fail-closed.
        if result.host_id != host_id:
            raise ValueError("activity reader changed host identity")
        self._notifier.completed("projection.agent-runs", result.revision, self._clock.now())
        return result

    def run_journal(
        self, source: str, run_id: str, host_id: str, source_key: str
    ) -> RunJournalFrame:
        result = self._activity.run_journal(source, run_id, host_id, source_key)
        if result.run_id != run_id or result.host_id != host_id:
            raise ValueError("activity reader changed run or host identity")
        self._notifier.completed("projection.run-journal", result.run_id, self._clock.now())
        return result
