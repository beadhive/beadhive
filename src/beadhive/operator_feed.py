"""Atomic source-install and cursor ownership shared by operator REST and SSE."""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import Any

from . import operator_contract
from .modules.state import Coverage, RunJournalFrame
from .operator_sources import ExactHive, OperatorSourceError, OperatorSources
from .public_readers import RunDirectoryEntry


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
    reset_reason: str | None = None
    added_records: tuple[Mapping[str, Any], ...] | None = None
    sequence_offset: int = 0
    first_occurred_at: int | None = None


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
    durable_records: tuple[Mapping[str, Any], ...] = ()
    durable_total: int = 0
    announced_sequence: int = 0
    journal_fingerprints: tuple[str, ...] = ()
    journal: RunJournalFrame | None = None
    initialized: bool = False
    retained_bytes: int = 0
    users: int = 0
    discontinuity_reason: str | None = None
    source_identity: _ActivitySourceIdentity | None = None
    owner_generation: str | None = None


@dataclass(frozen=True)
class _ActivitySourceIdentity:
    """Stable discovered ownership facts; mutable journal content is intentionally excluded."""

    hive_id: str
    run_id: str
    path: str
    device: int | None
    inode: int | None
    root_device: int | None
    root_inode: int | None


@dataclass(frozen=True)
class _RunOwnership:
    """Latest successfully installed source identity for one host-wide run id."""

    source_identity: _ActivitySourceIdentity
    generation: str


@dataclass
class _HiveAdmission:
    generation: str = field(default_factory=lambda: uuid.uuid4().hex)
    users: int = 0
    removing: bool = False


@dataclass(frozen=True)
class _HivePin:
    hive_id: str
    generation: str
    admission: _HiveAdmission


@dataclass(frozen=True)
class _ActivityDiscovery:
    clock: int
    run_id: str


InstallObserver = Callable[[FeedInstall], None]
ActivityObserver = Callable[[ActivityInstall], None]
DurableActivityReader = Callable[
    [str, RunJournalFrame, int],
    tuple[tuple[Mapping[str, Any], ...], str | None, bool, int],
]
TransitionHandler = Callable[[FeedTransition], int]
PulseHandler = Callable[[FeedPulse], int]
DEFAULT_MAX_CACHED_ACTIVITY_RUNS = 64
DEFAULT_MAX_CACHED_ACTIVITY_BYTES = 16 * 1_048_576


class OperatorFeed:
    """Long-lived per-source coordinator with atomic snapshot/cursor handoffs.

    ``bh-76a7z.9`` extends this object by registering an install observer and maintaining
    replay/subscriber state from those transitions.  A REST handler never reads a provider and
    then obtains a cursor separately: :meth:`snapshot_with_cursor` is the sole boundary.

    Run activity state is a bounded LRU. Eviction discards its producer epoch, so any old cursor
    deterministically receives the existing explicit expiry response after source rehydration.
    """

    def __init__(
        self,
        sources: OperatorSources,
        *,
        now_millis: Callable[[], int] | None = None,
        max_cached_activity_runs: int = DEFAULT_MAX_CACHED_ACTIVITY_RUNS,
        max_cached_activity_bytes: int = DEFAULT_MAX_CACHED_ACTIVITY_BYTES,
    ) -> None:
        if min(max_cached_activity_runs, max_cached_activity_bytes) < 1:
            raise ValueError("activity cache bounds must be positive")
        self.sources = sources
        self._now_millis = now_millis or (lambda: time.time_ns() // 1_000_000)
        self._states_lock = threading.Lock()
        self._hives: dict[str, _HiveState] = {}
        self.max_cached_activity_runs = max_cached_activity_runs
        self.max_cached_activity_bytes = max_cached_activity_bytes
        self.max_hive_admissions = max_cached_activity_runs
        self.max_run_ownerships = max_cached_activity_runs
        # ``locate_run`` is host-wide, so an unresolved discovery can belong to any hive.  Keep
        # that globally conservative teardown window no larger than the already-authoritative
        # activity cache bound; excess calls fail retryably instead of creating unbounded removal
        # coupling.
        self.max_activity_discoveries = max_cached_activity_runs
        self._cached_activity_bytes = 0
        self._activities: OrderedDict[tuple[str, str], _ActivityState] = OrderedDict()
        self._run_ownership_lock = threading.Lock()
        self._run_ownerships: OrderedDict[str, _RunOwnership] = OrderedDict()
        # Source I/O stays concurrent.  Only the bounded install/return window is serialized for
        # an exact run, so H -> OTHER -> H cannot return under H's pre-move producer epoch.
        self._run_install_locks = tuple(threading.RLock() for _ in range(self.max_run_ownerships))
        self._admission_condition = threading.Condition()
        self._admissions: dict[str, _HiveAdmission] = {}
        self._removing_hives: set[str] = set()
        self._hive_admission_rejections = 0
        self._removal_clock = 0
        self._removal_markers: dict[str, int] = {}
        self._activity_discoveries: dict[int, _ActivityDiscovery] = {}
        self._next_activity_discovery = 0
        self._observers_lock = threading.Lock()
        self._install_observers: list[InstallObserver] = []
        self._activity_observers: list[ActivityObserver] = []
        self._durable_activity_reader: DurableActivityReader | None = None
        self._transition_handler: TransitionHandler | None = None

    def _hive_state(self, hive_id: str) -> _HiveState:
        with self._states_lock:
            return self._hives.setdefault(hive_id, _HiveState())

    @staticmethod
    def _expired_hive_generation() -> OperatorSourceError:
        return OperatorSourceError(
            "hive_generation_expired",
            "The hive was removed while the authoritative read was in progress.",
            status_code=409,
            retryable=True,
        )

    def _release_hive_pin(self, pin: _HivePin) -> None:
        with self._admission_condition:
            pin.admission.users -= 1
            if (
                pin.admission.users == 0
                and not pin.admission.removing
                and self._admissions.get(pin.hive_id) is pin.admission
            ):
                self._admissions.pop(pin.hive_id, None)
            self._admission_condition.notify_all()

    def _finish_activity_discovery_locked(self, discovery_id: int) -> None:
        self._activity_discoveries.pop(discovery_id, None)
        oldest = min(
            (discovery.clock for discovery in self._activity_discoveries.values()),
            default=self._removal_clock,
        )
        self._removal_markers = {
            hive_id: marker for hive_id, marker in self._removal_markers.items() if marker > oldest
        }
        self._admission_condition.notify_all()

    def _acquire_hive_pin_locked(self, hive_id: str) -> _HivePin:
        admission = self._admissions.get(hive_id)
        if admission is None:
            if hive_id in self._removing_hives:
                raise self._expired_hive_generation()
            if len(self._admissions) >= self.max_hive_admissions:
                self._hive_admission_rejections += 1
                raise OperatorSourceError(
                    "hive_admission_capacity",
                    "The bounded hive read capacity is currently exhausted.",
                    status_code=503,
                    retryable=True,
                )
            admission = _HiveAdmission()
            self._admissions[hive_id] = admission
        if admission.removing:
            raise self._expired_hive_generation()
        admission.users += 1
        return _HivePin(hive_id, admission.generation, admission)

    @contextmanager
    def _hive_pin(self, hive_id: str) -> Iterator[_HivePin]:
        with self._admission_condition:
            pin = self._acquire_hive_pin_locked(hive_id)
        try:
            yield pin
        finally:
            self._release_hive_pin(pin)

    @contextmanager
    def _activity_hive_pin(
        self, run_id: str
    ) -> Iterator[tuple[ExactHive, RunDirectoryEntry, _HivePin]]:
        pin: _HivePin | None = None
        with self._admission_condition:
            if len(self._activity_discoveries) >= self.max_activity_discoveries:
                raise OperatorSourceError(
                    "activity_discovery_capacity",
                    "The bounded run discovery capacity is currently exhausted.",
                    status_code=503,
                    retryable=True,
                )
            self._next_activity_discovery += 1
            discovery_id = self._next_activity_discovery
            discovery_clock = self._removal_clock
            self._activity_discoveries[discovery_id] = _ActivityDiscovery(
                clock=discovery_clock,
                run_id=run_id,
            )
        discovering = True
        try:
            hive, source = self.sources.locate_run(run_id)
            if self.sources.resolve_hive(hive.identity) != hive:
                raise self._expired_hive_generation()
            with self._admission_condition:
                if self._removal_markers.get(hive.identity, -1) > discovery_clock:
                    raise self._expired_hive_generation()
                # Atomically hand the globally conservative discovery pin to its exact hive.
                # A removal either captured the unresolved discovery or observes this hive pin;
                # there is no stale cached-owner gap between the two admission domains.
                pin = self._acquire_hive_pin_locked(hive.identity)
                self._finish_activity_discovery_locked(discovery_id)
                discovering = False
            yield hive, source, pin
        finally:
            if pin is not None:
                self._release_hive_pin(pin)
            if discovering:
                with self._admission_condition:
                    self._finish_activity_discovery_locked(discovery_id)

    @contextmanager
    def _publication_pin(self, pin: _HivePin) -> Iterator[None]:
        with self._admission_condition:
            current = self._admissions.get(pin.hive_id)
            if (
                current is not pin.admission
                or current.removing
                or current.generation != pin.generation
            ):
                raise self._expired_hive_generation()
            yield

    def _evict_activity_states_locked(self, *, reserve_entries: int = 0) -> None:
        while (
            len(self._activities) + reserve_entries > self.max_cached_activity_runs
            or self._cached_activity_bytes > self.max_cached_activity_bytes
        ):
            candidate = next(
                ((key, state) for key, state in self._activities.items() if state.users == 0),
                None,
            )
            if candidate is None:
                return
            key, state = candidate
            del self._activities[key]
            self._cached_activity_bytes -= state.retained_bytes

    @contextmanager
    def _locked_activity_state(self, hive_id: str, run_id: str) -> Iterator[_ActivityState]:
        key = (hive_id, run_id)
        with self._states_lock:
            state = self._activities.get(key)
            if state is None:
                self._evict_activity_states_locked(reserve_entries=1)
                if len(self._activities) >= self.max_cached_activity_runs:
                    raise OperatorSourceError(
                        "activity_cache_saturated",
                        "The bounded activity cache is busy; retry the exact run read.",
                        status_code=503,
                        retryable=True,
                    )
                state = _ActivityState()
                self._activities[key] = state
            else:
                self._activities.move_to_end(key)
            state.users += 1
        state.lock.acquire()
        try:
            yield state
        finally:
            state.lock.release()
            with self._states_lock:
                state.users -= 1
                if self._activities.get(key) is state:
                    self._evict_activity_states_locked()

    def _set_activity_retained_bytes(
        self,
        key: tuple[str, str],
        state: _ActivityState,
        records: tuple[Mapping[str, Any], ...],
    ) -> None:
        retained_bytes = 256 + sum(
            len(json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8"))
            for record in records
        )
        with self._states_lock:
            if self._activities.get(key) is state:
                self._cached_activity_bytes += retained_bytes - state.retained_bytes
            state.retained_bytes = retained_bytes

    @property
    def cached_activity_bytes(self) -> int:
        with self._states_lock:
            return self._cached_activity_bytes

    @property
    def cached_activity_runs(self) -> int:
        with self._states_lock:
            return len(self._activities)

    @property
    def hive_admissions(self) -> int:
        with self._admission_condition:
            return len(self._admissions)

    @property
    def hive_admission_rejections(self) -> int:
        with self._admission_condition:
            return self._hive_admission_rejections

    @property
    def cached_run_ownerships(self) -> int:
        with self._run_ownership_lock:
            return len(self._run_ownerships)

    def _run_install_lock(self, run_id: str) -> threading.RLock:
        return self._run_install_locks[hash(run_id) % len(self._run_install_locks)]

    def _claim_run_ownership(self, run_id: str, source_identity: _ActivitySourceIdentity) -> str:
        """Return a generation that changes on every observed host-wide owner transition."""

        with self._run_ownership_lock:
            ownership = self._run_ownerships.get(run_id)
            if ownership is None or ownership.source_identity != source_identity:
                ownership = _RunOwnership(
                    source_identity=source_identity,
                    generation=uuid.uuid4().hex,
                )
                self._run_ownerships[run_id] = ownership
            self._run_ownerships.move_to_end(run_id)
            while len(self._run_ownerships) > self.max_run_ownerships:
                self._run_ownerships.popitem(last=False)
            return ownership.generation

    def tracked_hive_ids(self) -> frozenset[str]:
        """Exact hive identities retained by snapshot or run-cache state."""

        with self._states_lock:
            tracked = frozenset(self._hives) | frozenset(hive_id for hive_id, _ in self._activities)
        with self._run_ownership_lock:
            ownership_hives = frozenset(
                ownership.source_identity.hive_id for ownership in self._run_ownerships.values()
            )
        with self._admission_condition:
            return (
                tracked
                | ownership_hives
                | frozenset(self._admissions)
                | frozenset(self._removing_hives)
            )

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

    def configure_durable_activity_reader(self, reader: DurableActivityReader) -> None:
        """Attach the daemon store before traffic reaches this feed."""

        with self._observers_lock:
            if self._durable_activity_reader is not None:
                raise RuntimeError("a durable activity reader is already configured")
            self._durable_activity_reader = reader

    @staticmethod
    def _combined_activity_revision(journal_revision: str, durable_revision: str) -> str:
        encoded = json.dumps([journal_revision, durable_revision], separators=(",", ":")).encode()
        return f"opaque:activity-composite:{hashlib.sha256(encoded).hexdigest()}"

    def snapshot_with_cursor(self, identity: str) -> dict[str, object]:
        with self._hive_pin(identity) as pin:
            hive = self.sources.resolve_hive(identity)
            state = self._hive_state(hive.identity)
            with state.lock:
                try:
                    bead_state, runtime_state = self.sources.refresh_hive(hive)
                except Exception:
                    if state.snapshot is not None:
                        state.discontinuity_reason = (
                            "authoritative source continuity was interrupted"
                        )
                    raise
                source_key = (bead_state.revision, runtime_state.revision)
                reset_reason = state.discontinuity_reason
                with self._publication_pin(pin):
                    if (
                        state.snapshot is not None
                        and state.source_key == source_key
                        and reset_reason is None
                    ):
                        return state.snapshot

                    previous = state.snapshot
                    producer_epoch = (
                        uuid.uuid4().hex if reset_reason is not None else state.producer_epoch
                    )
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

        with self._hive_pin(identity) as pin:
            hive = self.sources.resolve_hive(identity)
            state = self._hive_state(hive.identity)
            with state.lock, self._publication_pin(pin):
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

        with self._hive_pin(identity) as pin:
            hive = self.sources.resolve_hive(identity)
            state = self._hive_state(hive.identity)
            with state.lock, self._publication_pin(pin):
                return state.snapshot

    def begin_hive_removal(self, hive_id: str) -> None:
        """Fence one hive generation, drain its readers, then forget feed-owned state.

        The caller supplies the already canonical identity because a removed registry row can no
        longer be resolved.  New calls fail while removal is active; calls which captured a source
        before removal cannot install or return it after the generation changes.  The fence stays
        closed until :meth:`finish_hive_removal` so a relay can clear its state in the same removal
        generation.
        """

        with self._admission_condition:
            while hive_id in self._removing_hives:
                self._admission_condition.wait()
            self._removing_hives.add(hive_id)
            admission = self._admissions.get(hive_id)
            self._removal_clock += 1
            self._removal_markers[hive_id] = self._removal_clock
            if admission is not None:
                admission.removing = True
                admission.generation = uuid.uuid4().hex
            draining_discoveries = frozenset(self._activity_discoveries)
            while (admission is not None and admission.users) or draining_discoveries.intersection(
                self._activity_discoveries
            ):
                self._admission_condition.wait()

        try:
            with self._states_lock:
                hive_state = self._hives.get(hive_id)
            if hive_state is not None:
                with hive_state.lock:
                    with self._states_lock:
                        if self._hives.get(hive_id) is hive_state:
                            self._hives.pop(hive_id, None)

            while True:
                with self._states_lock:
                    candidate = next(
                        (
                            (key, state)
                            for key, state in self._activities.items()
                            if key[0] == hive_id
                        ),
                        None,
                    )
                if candidate is None:
                    break
                key, activity_state = candidate
                with activity_state.lock:
                    with self._states_lock:
                        if self._activities.get(key) is activity_state:
                            self._activities.pop(key, None)
                            self._cached_activity_bytes -= activity_state.retained_bytes
            with self._run_ownership_lock:
                self._run_ownerships = OrderedDict(
                    (run_id, ownership)
                    for run_id, ownership in self._run_ownerships.items()
                    if ownership.source_identity.hive_id != hive_id
                )
        except BaseException:
            self.finish_hive_removal(hive_id)
            raise

    def finish_hive_removal(self, hive_id: str) -> None:
        """Open admission for a fresh generation after every owner cleared its state."""

        with self._admission_condition:
            if hive_id not in self._removing_hives:
                return
            self._removal_clock += 1
            self._removal_markers[hive_id] = self._removal_clock
            self._admissions.pop(hive_id, None)
            self._removing_hives.discard(hive_id)
            oldest = min(
                (discovery.clock for discovery in self._activity_discoveries.values()),
                default=self._removal_clock,
            )
            self._removal_markers = {
                tracked_hive: marker
                for tracked_hive, marker in self._removal_markers.items()
                if marker > oldest
            }
            self._admission_condition.notify_all()

    def remove_hive(self, hive_id: str) -> None:
        """Atomically remove feed-owned state for callers without a composed relay."""

        self.begin_hive_removal(hive_id)
        self.finish_hive_removal(hive_id)

    def cancel_source_reads(self) -> None:
        """Cancel daemon-owned source process trees before relay worker drain."""

        self.sources.close()

    @staticmethod
    def _is_append(
        previous: tuple[Mapping[str, Any], ...], current: tuple[Mapping[str, Any], ...]
    ) -> bool:
        return len(current) >= len(previous) and current[: len(previous)] == previous

    @staticmethod
    def _activity_discontinuity_reason(error: BaseException) -> str:
        if isinstance(error, OperatorSourceError):
            return error.code
        if isinstance(error, asyncio.CancelledError):
            return "activity_read_cancelled"
        return "activity_source_interrupted"

    def mark_activity_discontinuous(self, run_id: str, reason: str) -> None:
        """Fence only the cached cursor domain for one exact outer run."""

        if not reason:
            raise ValueError("activity discontinuity reason cannot be empty")
        with self._states_lock:
            candidates = tuple(
                state
                for (_hive_id, cached_run_id), state in self._activities.items()
                if cached_run_id == run_id
            )
        for state in candidates:
            with state.lock:
                state.discontinuity_reason = reason

    def mark_hive_discontinuous(self, hive_id: str, reason: str) -> None:
        """Fence a snapshot result that its cancelled direct reader never observed."""

        if not reason:
            raise ValueError("hive discontinuity reason cannot be empty")
        with self._states_lock:
            state = self._hives.get(hive_id)
        if state is not None:
            with state.lock:
                state.discontinuity_reason = reason

    def _mark_activity_error(self, run_id: str, error: BaseException) -> None:
        self.mark_activity_discontinuous(run_id, self._activity_discontinuity_reason(error))

    @staticmethod
    def _activity_source_identity(source: RunDirectoryEntry) -> _ActivitySourceIdentity:
        return _ActivitySourceIdentity(
            hive_id=source.hive_id,
            run_id=source.run_id,
            path=str(source.path),
            device=source.device,
            inode=source.inode,
            root_device=source.root_device,
            root_inode=source.root_inode,
        )

    def _durable_activity_page(
        self,
        *,
        run_id: str,
        hive: ExactHive,
        source: RunDirectoryEntry,
        pin: _HivePin,
        state: _ActivityState,
        journal: RunJournalFrame,
        after: tuple[str, int] | None,
    ) -> dict[str, object]:
        """Serve one durable page while retaining only bounded cursor metadata."""

        assert self._durable_activity_reader is not None
        journal_records = tuple(dict(record) for record in journal.records)
        fingerprints = tuple(
            hashlib.sha256(
                json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            for record in journal_records
        )
        source_identity = self._activity_source_identity(source)
        with self._run_install_lock(run_id), self._publication_pin(pin):
            owner_generation = self._claim_run_ownership(run_id, source_identity)
            reset_reason = state.discontinuity_reason
            if reset_reason is None and state.initialized:
                if state.source_identity != source_identity:
                    reset_reason = "activity_source_changed"
                elif state.owner_generation != owner_generation:
                    reset_reason = "activity_owner_changed"
                elif fingerprints != state.journal_fingerprints:
                    append_only = fingerprints[: len(state.journal_fingerprints)] == (
                        state.journal_fingerprints
                    )
                    # Once durable rows have sequence positions after the journal, any journal
                    # edit moves those positions and therefore requires an explicit new epoch.
                    if not append_only or state.durable_total:
                        reset_reason = "activity_history_rewritten"

            if reset_reason is not None:
                state.producer_epoch = uuid.uuid4().hex
                state.announced_sequence = 0
                after = None

            journal_count = len(journal_records)
            if after is None:
                kind = "reset" if reset_reason is not None else "snapshot"
                base_sequence = 0
                durable_offset = 0
                journal_page = journal_records
            else:
                epoch, base_sequence = after
                if epoch != state.producer_epoch:
                    raise OperatorSourceError(
                        "activity_cursor_expired",
                        "The activity cursor belongs to an expired producer epoch.",
                        status_code=410,
                    )
                if base_sequence < 0:
                    raise OperatorSourceError(
                        "invalid_activity_cursor",
                        "The activity cursor is outside the installed run history.",
                        status_code=409,
                    )
                kind = "delta"
                durable_offset = max(0, base_sequence - journal_count)
                journal_page = journal_records[base_sequence:]

            durable_page, durable_revision, _complete, durable_total = (
                self._durable_activity_reader(run_id, journal, durable_offset)
            )
            known_sequence = journal_count + durable_total
            if base_sequence > known_sequence:
                raise OperatorSourceError(
                    "invalid_activity_cursor",
                    "The activity cursor is outside the installed run history.",
                    status_code=409,
                )
            records = journal_page + tuple(dict(record) for record in durable_page)
            page_end = base_sequence + len(records)
            complete = page_end >= known_sequence
            if durable_revision is not None:
                journal = replace(
                    journal,
                    source_revision=self._combined_activity_revision(
                        str(journal.source_revision), durable_revision
                    ),
                )
            if not complete:
                journal = replace(
                    journal,
                    coverage=Coverage.PARTIAL,
                    coverage_reason="durable_activity_page_truncated",
                )

            previous_announced = state.announced_sequence
            state.records = ()
            state.durable_records = ()
            state.durable_total = durable_total
            state.journal_fingerprints = fingerprints
            state.journal = replace(journal, records=())
            state.initialized = True
            state.source_identity = source_identity
            state.owner_generation = owner_generation
            state.discontinuity_reason = None
            self._set_activity_retained_bytes((hive.identity, run_id), state, ())

            if reset_reason is not None:
                self._notify_activity(
                    ActivityInstall(
                        run_id=run_id,
                        hive_id=hive.identity,
                        producer_epoch=state.producer_epoch,
                        previous_records=(),
                        current_records=(),
                        source_revision=str(journal.source_revision),
                        reset_reason=reset_reason,
                    )
                )
            elif page_end > previous_announced:
                first_new = max(0, previous_announced - base_sequence)
                added = records[first_new:]
                if added:
                    sequence_offset = base_sequence + first_new
                    self._notify_activity(
                        ActivityInstall(
                            run_id=run_id,
                            hive_id=hive.identity,
                            producer_epoch=state.producer_epoch,
                            previous_records=(),
                            current_records=(),
                            source_revision=str(journal.source_revision),
                            added_records=added,
                            sequence_offset=sequence_offset,
                            first_occurred_at=int(journal_records[0]["timestamp_ms"]),
                        )
                    )
                    state.announced_sequence = page_end

            return operator_contract.run_activity_page_frame(
                journal,
                records,
                producer_epoch=state.producer_epoch,
                base_sequence=base_sequence,
                kind=kind,
                reset_reason=reset_reason,
            )

    def activity_with_cursor(
        self,
        run_id: str,
        *,
        after: tuple[str, int] | None = None,
    ) -> dict[str, object]:
        located = False
        try:
            with self._activity_hive_pin(run_id) as (hive, source, pin):
                located = True
                key = (hive.identity, run_id)
                with self._locked_activity_state(hive.identity, run_id) as state:
                    try:
                        journal = self.sources.read_run(hive, source, run_id)
                    except (Exception, asyncio.CancelledError) as exc:
                        state.discontinuity_reason = self._activity_discontinuity_reason(exc)
                        raise
                    if self._durable_activity_reader is not None:
                        return self._durable_activity_page(
                            run_id=run_id,
                            hive=hive,
                            source=source,
                            pin=pin,
                            state=state,
                            journal=journal,
                            after=after,
                        )
                    journal_records = tuple(dict(record) for record in journal.records)
                    durable_records: tuple[Mapping[str, Any], ...] = ()
                    durable_revision = None
                    durable_complete = True
                    source_identity = self._activity_source_identity(source)
                    records = journal_records + tuple(dict(record) for record in durable_records)
                    if durable_revision is not None:
                        journal = replace(
                            journal,
                            source_revision=self._combined_activity_revision(
                                str(journal.source_revision), durable_revision
                            ),
                            coverage=(journal.coverage if durable_complete else Coverage.PARTIAL),
                            coverage_reason=(
                                journal.coverage_reason
                                if durable_complete
                                else "durable_activity_page_truncated"
                            ),
                        )
                    with self._run_install_lock(run_id), self._publication_pin(pin):
                        owner_generation = self._claim_run_ownership(run_id, source_identity)
                        reset_reason = state.discontinuity_reason
                        if reset_reason is None and state.initialized:
                            if state.source_identity != source_identity:
                                reset_reason = "activity_source_changed"
                            elif state.owner_generation != owner_generation:
                                reset_reason = "activity_owner_changed"
                        changed = (
                            not state.initialized
                            or records != state.records
                            or reset_reason is not None
                        )
                        previous_records = state.records
                        history_rewritten = (
                            state.initialized
                            and changed
                            and not self._is_append(state.records, records)
                        )
                        install_reset_reason = reset_reason
                        if reset_reason is not None:
                            state.producer_epoch = uuid.uuid4().hex
                        elif history_rewritten:
                            state.producer_epoch = uuid.uuid4().hex
                            install_reset_reason = "activity_history_rewritten"
                        if changed:
                            state.records = records
                            state.durable_records = durable_records
                            state.journal = replace(journal, records=records)
                            state.initialized = True
                            state.source_identity = source_identity
                            state.owner_generation = owner_generation
                            self._set_activity_retained_bytes(key, state, records)
                            self._notify_activity(
                                ActivityInstall(
                                    run_id=run_id,
                                    hive_id=hive.identity,
                                    producer_epoch=state.producer_epoch,
                                    previous_records=previous_records,
                                    current_records=records,
                                    source_revision=str(journal.source_revision),
                                    reset_reason=install_reset_reason,
                                )
                            )
                        else:
                            # Coverage/freshness may change without changing the append-only
                            # records.
                            state.journal = replace(journal, records=records)

                        if reset_reason is not None:
                            kind = "reset"
                            base_sequence = 0
                        elif after is None:
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
                        response = operator_contract.run_activity_frame(
                            state.journal,
                            records,
                            producer_epoch=state.producer_epoch,
                            base_sequence=base_sequence,
                            kind=kind,
                            reset_reason=reset_reason,
                        )
                        state.discontinuity_reason = None
                        return response
        except (Exception, asyncio.CancelledError) as exc:
            if not located and not (
                isinstance(exc, OperatorSourceError) and exc.code == "hive_generation_expired"
            ):
                self._mark_activity_error(run_id, exc)
            raise

    def project_latest_durable_activity(self, run_id: str) -> dict[str, object]:
        """Install the newest committed row without hydrating an unbounded history."""

        snapshot = self.activity_with_cursor(run_id)
        if self._durable_activity_reader is None:
            return snapshot
        key = (str(snapshot["hiveId"]), run_id)
        with self._states_lock:
            state = self._activities.get(key)
            if state is None:
                return snapshot
            known_sequence = len(state.journal_fingerprints) + state.durable_total
            epoch = state.producer_epoch
        if int(snapshot["sequence"]) >= known_sequence:
            return snapshot
        return self.activity_with_cursor(run_id, after=(epoch, known_sequence - 1))

    def resolve_run(self, run_id: str) -> tuple[ExactHive, str]:
        """Expose exact run ownership without leaking its host-local path."""

        hive, _source = self.sources.locate_run(run_id)
        return hive, run_id
