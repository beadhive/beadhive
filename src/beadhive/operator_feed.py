"""Atomic source-install and cursor ownership shared by operator REST and SSE."""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from . import operator_contract
from .operator_sources import ExactHive, OperatorSourceError, OperatorSources
from .public_readers import RunJournalFrame


@dataclass(frozen=True)
class FeedInstall:
    """One snapshot installation observed while its per-hive lock is still held."""

    hive_id: str
    previous: Mapping[str, object] | None
    current: Mapping[str, object]
    source_revision: str


@dataclass(frozen=True)
class FeedTransition:
    """A changed installed source state awaiting event-sequence allocation."""

    hive_id: str
    previous: Mapping[str, object]
    current: Mapping[str, object]
    source_revision: str
    producer_epoch: str
    base_sequence: int
    reset_reason: str | None = None


@dataclass(frozen=True)
class FeedPulse:
    """An event allocation which does not replace the installed source snapshot."""

    hive_id: str
    snapshot: Mapping[str, object]
    source_revision: str
    producer_epoch: str
    base_sequence: int


@dataclass(frozen=True)
class ActivityInstall:
    run_id: str
    hive_id: str
    producer_epoch: str
    previous_records: tuple[Mapping[str, Any], ...]
    current_records: tuple[Mapping[str, Any], ...]
    source_revision: str


@dataclass
class _HiveState:
    lock: threading.RLock = field(default_factory=threading.RLock)
    producer_epoch: str = field(default_factory=lambda: uuid.uuid4().hex)
    sequence: int = 0
    source_key: tuple[str, str] | None = None
    snapshot: dict[str, object] | None = None
    discontinuity_reason: str | None = None


@dataclass
class _ActivityState:
    lock: threading.RLock = field(default_factory=threading.RLock)
    producer_epoch: str = field(default_factory=lambda: uuid.uuid4().hex)
    records: tuple[Mapping[str, Any], ...] = ()
    journal: RunJournalFrame | None = None
    initialized: bool = False


InstallObserver = Callable[[FeedInstall], None]
ActivityObserver = Callable[[ActivityInstall], None]
TransitionHandler = Callable[[FeedTransition], int]
PulseHandler = Callable[[FeedPulse], int]


class OperatorFeed:
    """Long-lived per-source coordinator with atomic snapshot/cursor handoffs.

    ``bh-76a7z.9`` extends this object by registering an install observer and maintaining
    replay/subscriber state from those transitions.  A REST handler never reads a provider and
    then obtains a cursor separately: :meth:`snapshot_with_cursor` is the sole boundary.
    """

    def __init__(
        self,
        sources: OperatorSources,
        *,
        now_millis: Callable[[], int] | None = None,
    ) -> None:
        self.sources = sources
        self._now_millis = now_millis or (lambda: time.time_ns() // 1_000_000)
        self._states_lock = threading.Lock()
        self._hives: dict[str, _HiveState] = {}
        self._activities: dict[tuple[str, str], _ActivityState] = {}
        self._observers_lock = threading.Lock()
        self._install_observers: list[InstallObserver] = []
        self._activity_observers: list[ActivityObserver] = []
        self._transition_handler: TransitionHandler | None = None

    def _hive_state(self, hive_id: str) -> _HiveState:
        with self._states_lock:
            return self._hives.setdefault(hive_id, _HiveState())

    def _activity_state(self, hive_id: str, run_id: str) -> _ActivityState:
        with self._states_lock:
            return self._activities.setdefault((hive_id, run_id), _ActivityState())

    def register_install_observer(self, observer: InstallObserver) -> Callable[[], None]:
        """Observe installed transitions under the same lock used by snapshot reads."""

        with self._observers_lock:
            self._install_observers.append(observer)

        def remove() -> None:
            with self._observers_lock:
                if observer in self._install_observers:
                    self._install_observers.remove(observer)

        return remove

    def register_activity_observer(self, observer: ActivityObserver) -> Callable[[], None]:
        with self._observers_lock:
            self._activity_observers.append(observer)

        def remove() -> None:
            with self._observers_lock:
                if observer in self._activity_observers:
                    self._activity_observers.remove(observer)

        return remove

    def register_transition_handler(self, handler: TransitionHandler) -> Callable[[], None]:
        """Install the relay's sole changed-snapshot event allocator.

        The callback runs synchronously under the affected hive lock and returns how many
        positive, contiguous event sequences it installed after ``base_sequence``.  The feed
        advances the snapshot cursor by exactly that count before publishing the finalized
        :class:`FeedInstall` to observers.  Without a relay, a changed source state advances by
        one abstract transition so REST-only cursor ordering remains truthful.
        """

        with self._observers_lock:
            if self._transition_handler is not None:
                raise RuntimeError("an operator feed transition handler is already registered")
            self._transition_handler = handler

        def remove() -> None:
            with self._observers_lock:
                if self._transition_handler is handler:
                    self._transition_handler = None

        return remove

    def _allocate_transition(self, transition: FeedTransition) -> int:
        with self._observers_lock:
            handler = self._transition_handler
        count = 1 if handler is None else handler(transition)
        if type(count) is not int or count < 1:
            raise RuntimeError("operator feed transitions must install at least one event")
        return count

    def _notify_install(self, install: FeedInstall) -> None:
        with self._observers_lock:
            observers = tuple(self._install_observers)
        for observer in observers:
            observer(install)

    def _notify_activity(self, install: ActivityInstall) -> None:
        with self._observers_lock:
            observers = tuple(self._activity_observers)
        for observer in observers:
            observer(install)

    def snapshot_with_cursor(self, identity: str) -> dict[str, object]:
        hive = self.sources.resolve_hive(identity)
        state = self._hive_state(hive.identity)
        with state.lock:
            try:
                bead_state, runtime_state = self.sources.refresh_hive(hive)
            except Exception:
                if state.snapshot is not None:
                    state.discontinuity_reason = "authoritative source continuity was interrupted"
                raise
            source_key = (bead_state.revision, runtime_state.revision)
            reset_reason = state.discontinuity_reason
            if (
                state.snapshot is not None
                and state.source_key == source_key
                and reset_reason is None
            ):
                return state.snapshot

            previous = state.snapshot
            producer_epoch = uuid.uuid4().hex if reset_reason is not None else state.producer_epoch
            base_sequence = 0 if reset_reason is not None else state.sequence
            observed_at = self._now_millis()
            snapshot = operator_contract.hive_operator_snapshot(
                hive.entry,
                bead_state,
                runtime_state,
                producer_epoch=producer_epoch,
                sequence=base_sequence,
                observed_at=observed_at,
            )
            if previous is not None:
                count = self._allocate_transition(
                    FeedTransition(
                        hive_id=hive.identity,
                        previous=previous,
                        current=snapshot,
                        source_revision=str(snapshot["revision"]),
                        producer_epoch=producer_epoch,
                        base_sequence=base_sequence,
                        reset_reason=reset_reason,
                    )
                )
                base_sequence += count
                snapshot["cursor"]["sequence"] = base_sequence  # type: ignore[index]
            state.producer_epoch = producer_epoch
            state.sequence = base_sequence
            state.source_key = source_key
            state.snapshot = snapshot
            state.discontinuity_reason = None
            self._notify_install(
                FeedInstall(
                    hive_id=hive.identity,
                    previous=previous,
                    current=snapshot,
                    source_revision=str(snapshot["revision"]),
                )
            )
            return snapshot

    def allocate_events(self, identity: str, handler: PulseHandler) -> int:
        """Allocate non-source events while keeping the snapshot cursor atomic.

        Heartbeats use this boundary so their real event sequences can never advance ahead of
        the cursor returned by :meth:`snapshot_with_cursor`.
        """

        hive = self.sources.resolve_hive(identity)
        state = self._hive_state(hive.identity)
        with state.lock:
            if state.snapshot is None:
                raise OperatorSourceError(
                    "snapshot_required",
                    "An authoritative snapshot is required before event subscription.",
                    status_code=409,
                )
            count = handler(
                FeedPulse(
                    hive_id=hive.identity,
                    snapshot=state.snapshot,
                    source_revision=str(state.snapshot["revision"]),
                    producer_epoch=state.producer_epoch,
                    base_sequence=state.sequence,
                )
            )
            if type(count) is not int or count < 1:
                raise RuntimeError(
                    "operator feed event allocations must install at least one event"
                )
            state.sequence += count
            cursor = state.snapshot["cursor"]
            assert isinstance(cursor, dict)
            cursor["sequence"] = state.sequence
            cursor["observedAt"] = self._now_millis()
            return state.sequence

    def installed_snapshot(self, identity: str) -> Mapping[str, object] | None:
        """Return the installed object and cursor together; never refresh a source."""

        hive = self.sources.resolve_hive(identity)
        state = self._hive_state(hive.identity)
        with state.lock:
            return state.snapshot

    def cancel_source_reads(self) -> None:
        """Cancel daemon-owned source process trees before relay worker drain."""

        self.sources.close()

    @staticmethod
    def _is_append(
        previous: tuple[Mapping[str, Any], ...], current: tuple[Mapping[str, Any], ...]
    ) -> bool:
        return len(current) >= len(previous) and current[: len(previous)] == previous

    def activity_with_cursor(
        self,
        run_id: str,
        *,
        after: tuple[str, int] | None = None,
    ) -> dict[str, object]:
        hive, path = self.sources.locate_run(run_id)
        state = self._activity_state(hive.identity, run_id)
        with state.lock:
            journal = self.sources.read_run(hive, path, run_id)
            records = tuple(dict(record) for record in journal.records)
            changed = not state.initialized or records != state.records
            previous_records = state.records
            if state.initialized and changed and not self._is_append(state.records, records):
                state.producer_epoch = uuid.uuid4().hex
            if changed:
                state.records = records
                state.journal = journal
                state.initialized = True
                self._notify_activity(
                    ActivityInstall(
                        run_id=run_id,
                        hive_id=hive.identity,
                        producer_epoch=state.producer_epoch,
                        previous_records=previous_records,
                        current_records=records,
                        source_revision=str(journal.source_revision),
                    )
                )
            else:
                # Coverage/freshness may change without changing the append-only records.
                state.journal = journal

            if after is None:
                kind = "snapshot"
                base_sequence = 0
            else:
                epoch, base_sequence = after
                if epoch != state.producer_epoch:
                    raise OperatorSourceError(
                        "activity_cursor_expired",
                        "The activity cursor belongs to an expired producer epoch.",
                        status_code=410,
                    )
                if base_sequence < 0 or base_sequence > len(records):
                    raise OperatorSourceError(
                        "invalid_activity_cursor",
                        "The activity cursor is outside the installed run history.",
                        status_code=409,
                    )
                kind = "delta"
            assert state.journal is not None
            return operator_contract.run_activity_frame(
                state.journal,
                records,
                producer_epoch=state.producer_epoch,
                base_sequence=base_sequence,
                kind=kind,
            )

    def resolve_run(self, run_id: str) -> tuple[ExactHive, str]:
        """Expose exact run ownership without leaking its host-local path."""

        hive, _path = self.sources.locate_run(run_id)
        return hive, run_id
