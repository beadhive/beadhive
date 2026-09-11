"""Outbound ports for durable state and query projections."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Protocol

from ..domain.projections import AgentRunSnapshot, RunJournalFrame
from ..domain.stream import ProviderSnapshot, StreamFrame, StreamRequest
from ..domain.validation import ValidationQuery, ValidationRecord


class ValidationRecordStorage(Protocol):
    """Persist and query canonical validation run facts."""

    def read(self, run_id: str) -> ValidationRecord | None: ...

    def latest(self, query: ValidationQuery) -> ValidationRecord | None: ...

    def write(self, record: ValidationRecord) -> ValidationRecord: ...


class StateProjectionReader(Protocol):
    """Read existing snapshot/replay projections without owning mutation authority."""

    def snapshot(self, request: StreamRequest) -> ProviderSnapshot: ...

    def frames(self, request: StreamRequest) -> Iterable[StreamFrame]: ...


class ActivityProjectionReader(Protocol):
    """Read activity projections by host/run identity and a reader-owned lookup key.

    The lookup key selects a source but is not required to equal the returned projection's
    durable semantic ``source_id``.
    """

    def agent_runs(self, source: str, host_id: str, source_key: str) -> AgentRunSnapshot: ...

    def run_journal(
        self, source: str, run_id: str, host_id: str, source_key: str
    ) -> RunJournalFrame: ...


class StateClock(Protocol):
    def now(self) -> datetime: ...


class StateNotifier(Protocol):
    """Observe successful projection/record operations; never grants lifecycle authority."""

    def completed(self, operation: str, identity: str, observed_at: datetime) -> None: ...
