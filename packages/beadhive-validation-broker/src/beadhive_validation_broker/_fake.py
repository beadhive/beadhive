"""An in-memory broker engine: the test double that ships with the port.

Deterministic by construction: it owns no wall time (an injected :data:`Clock`), starts no
thread and never blocks. Admission happens eagerly inside ``submit`` and after every release, so
``wait_grant`` either returns the grant, raises ``GrantTimeout`` (still queued), or raises
``LeaseLost``; a test advances the world by calling ``report`` / ``cancel`` / ``reap``.

It is the reference for the safety contract (:mod:`beadhive_validation_broker.contract`), not a
scheduler: it admits in ``(priority, arrival)`` order and lets a later job backfill past one that
does not fit. The group-aware packer, durable leases and a real process-liveness probe belong to
the local engine (``bh-qrixp``).
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

from ._errors import BrokerUnavailable, GrantRefused, GrantTimeout, LeaseLost
from ._ports import Clock, ProcessLiveness
from ._types import (
    ZERO,
    BrakeLevel,
    BrokerEvent,
    BrokerStatus,
    Capacity,
    EventKind,
    Grant,
    JobHandle,
    JobSpec,
    Outcome,
    Owner,
    PriorityClass,
    Usage,
)

__all__ = ["FakeAvailability", "FakeDiskSpace", "InMemoryBroker", "RecordedReport"]

_QUEUED, _RUNNING, _DONE, _CANCELLED, _LOST = "queued", "running", "done", "cancelled", "lost"


@dataclass(frozen=True)
class RecordedReport:
    """What ``report`` recorded for one lease (history and right-sizing input)."""

    job_id: str
    outcome: Outcome
    usage: Usage


class FakeAvailability:
    """An :data:`~beadhive_validation_broker.Availability` a test sets by hand.

    ``set`` changes what the host reports free; ``fail`` makes the next reads raise, the way a
    missing ``/proc/meminfo`` does. ``reads`` counts probes, so a test can prove one happened.
    """

    def __init__(self, available: Capacity) -> None:
        self.available = available
        self.error: Exception | None = None
        self.reads = 0

    def set(self, available: Capacity) -> None:
        self.available = available

    def fail(self, error: Exception | None = None) -> None:
        self.error = error or OSError("availability probe failed")

    def recover(self) -> None:
        self.error = None

    def __call__(self) -> Capacity:
        self.reads += 1
        if self.error is not None:
            raise self.error
        return self.available


class FakeDiskSpace:
    """A :data:`~beadhive_validation_broker.DiskSpace` a test sets by hand, per root path.

    ``set`` changes the free bytes a path reports; ``fail`` makes reads of one path raise, the way
    an unmounted or unreadable root does. A path never set raises too. ``reads`` counts probes.
    """

    def __init__(self, free: dict[str, int] | None = None) -> None:
        self.free: dict[str, int] = dict(free or {})
        self.failing: set[str] = set()
        self.reads = 0

    def set(self, path: str, free_bytes: int) -> None:
        self.free[path] = free_bytes

    def fail(self, path: str) -> None:
        self.failing.add(path)

    def recover(self, path: str) -> None:
        self.failing.discard(path)

    def __call__(self, path: str) -> int:
        self.reads += 1
        if path in self.failing or path not in self.free:
            raise OSError(f"cannot read free space at {path}")
        return self.free[path]


@dataclass
class _Entry:
    spec: JobSpec
    handle: JobHandle
    arrival: int
    state: str = _QUEUED
    grant: Grant | None = None
    children: list[str] = field(default_factory=list)


class InMemoryBroker:
    """A :class:`~beadhive_validation_broker.BrokerPort` over an in-memory ledger."""

    name = "memory"

    def __init__(
        self,
        budget: Capacity,
        clock: Clock,
        *,
        owner_alive: ProcessLiveness | None = None,
    ) -> None:
        self._budget = budget
        self._clock = clock
        self._owner_alive: Callable[[Owner], bool] = owner_alive or (lambda _owner: True)
        self._lock = threading.RLock()
        self._entries: dict[str, _Entry] = {}
        self._events: list[BrokerEvent] = []
        self._arrivals = 0
        self._available = True
        self._brake = BrakeLevel.GREEN
        self.reports: list[RecordedReport] = []

    # ---- test controls (not part of the port) ------------------------------------------

    def set_available(self, available: bool) -> None:
        """Simulate the backend going down (every port operation raises ``BrokerUnavailable``)."""
        with self._lock:
            self._available = available

    def set_brake(self, level: BrakeLevel, reason: str) -> None:
        with self._lock:
            self._brake = level
            self._emit(EventKind.BRAKED, None, level=int(level), reason=reason)
            self._pump()

    def reap(self) -> None:
        """Reclaim every lease (and queued job) whose owner is no longer alive."""
        with self._lock:
            for entry in list(self._entries.values()):
                if entry.spec.parent_lease is None and entry.state in (_QUEUED, _RUNNING):
                    if not self._owner_alive(entry.spec.owner):
                        self._reclaim(entry, EventKind.ORPHAN_RECLAIMED, "owner dead")
            self._pump()

    def in_use(self) -> Capacity:
        """Host capacity held by top-level running leases (nested draws are inside these)."""
        with self._lock:
            return self._in_use()

    # ---- the port ----------------------------------------------------------------------

    def submit(self, job: JobSpec) -> JobHandle:
        with self._lock:
            self._require_up()
            existing = self._entries.get(job.job_id)
            if existing is not None:
                return existing.handle
            if job.parent_lease is not None:
                self._check_nested(job)
            elif not self._budget.fits(job.reservation.minimum()):
                raise GrantRefused(f"{job.job_id}: reservation exceeds the host budget")
            self._arrivals += 1
            handle = JobHandle(job.job_id, self.name, str(self._arrivals))
            entry = _Entry(job, handle, self._arrivals)
            self._entries[job.job_id] = entry
            if job.parent_lease is not None:
                self._entries[self._parent_of(job).spec.job_id].children.append(job.job_id)
            self._emit(EventKind.SUBMITTED, job.job_id, group=job.group)
            self._pump()
            return handle

    def wait_grant(self, handle: JobHandle, timeout: float | None = None) -> Grant:
        del timeout  # the fake never blocks: a queued job is reported as not yet granted
        with self._lock:
            self._require_up()
            entry = self._entry(handle)
            if entry.state in (_RUNNING, _DONE) and entry.grant is not None:
                return entry.grant
            if entry.state == _QUEUED:
                raise GrantTimeout(f"{handle.job_id}: still queued")
            raise LeaseLost(f"{handle.job_id}: lease is {entry.state}")

    def report(self, handle: JobHandle, outcome: Outcome, usage: Usage) -> None:
        with self._lock:
            self._require_up()
            entry = self._entry(handle)
            if entry.state == _DONE:
                return
            if entry.state != _RUNNING:
                raise LeaseLost(f"{handle.job_id}: lease is {entry.state}")
            self.reports.append(RecordedReport(handle.job_id, outcome, usage))
            self._end(entry, _DONE, EventKind.FINISHED, exit_code=outcome.exit_code)
            self._pump()

    def cancel(self, handle: JobHandle, reason: str) -> None:
        with self._lock:
            self._require_up()
            entry = self._entries.get(handle.job_id)
            if entry is None or entry.state in (_DONE, _CANCELLED, _LOST):
                return
            self._end(entry, _CANCELLED, EventKind.CANCELLED, reason=reason)
            self._pump()

    def status(self) -> BrokerStatus:
        with self._lock:
            self._require_up()
            states = [e.state for e in self._entries.values()]
            return BrokerStatus(
                backend=self.name,
                budget=self._budget,
                in_use=self._in_use(),
                queued=states.count(_QUEUED),
                running=states.count(_RUNNING),
                brake=self._brake,
            )

    def events(self, since: int = 0) -> Iterator[BrokerEvent]:
        with self._lock:
            self._require_up()
            return iter([e for e in self._events if e.seq > since])

    # ---- internals ---------------------------------------------------------------------

    def _require_up(self) -> None:
        if not self._available:
            raise BrokerUnavailable("the in-memory broker is marked down")

    def _entry(self, handle: JobHandle) -> _Entry:
        entry = self._entries.get(handle.job_id)
        if entry is None:
            raise LeaseLost(f"{handle.job_id}: unknown job")
        return entry

    def _emit(self, kind: EventKind, job_id: str | None, **detail: object) -> None:
        self._events.append(BrokerEvent(len(self._events) + 1, kind, self._clock(), job_id, detail))

    def _in_use(self) -> Capacity:
        used = ZERO
        for entry in self._entries.values():
            if entry.state == _RUNNING and entry.spec.parent_lease is None and entry.grant:
                used = used + entry.grant.amounts
        return used

    def _parent_of(self, job: JobSpec) -> _Entry:
        for entry in self._entries.values():
            if entry.grant is not None and entry.grant.lease_id == job.parent_lease:
                return entry
        raise LeaseLost(f"{job.job_id}: parent lease {job.parent_lease} is gone")

    def _check_nested(self, job: JobSpec) -> None:
        parent = self._parent_of(job)
        if parent.state != _RUNNING or parent.grant is None:
            raise LeaseLost(f"{job.job_id}: parent lease {job.parent_lease} is not running")
        if not parent.grant.amounts.fits(job.reservation.minimum()):
            raise GrantRefused(f"{job.job_id}: reservation can never fit its parent's grant")

    def _children_in_use(self, parent: _Entry) -> Capacity:
        used = ZERO
        for child_id in parent.children:
            child = self._entries[child_id]
            if child.state == _RUNNING and child.grant is not None:
                used = used + child.grant.amounts
        return used

    def _pump(self) -> None:
        """Admit every queued job that fits, nested first, then host jobs by priority."""
        queued = sorted(
            (e for e in self._entries.values() if e.state == _QUEUED),
            key=lambda e: (e.spec.parent_lease is None, int(e.spec.priority), e.arrival),
        )
        for entry in queued:
            self._try_admit(entry)

    def _try_admit(self, entry: _Entry) -> None:
        spec = entry.spec
        if spec.parent_lease is not None:
            parent = self._parent_of(spec)
            if parent.grant is None:
                return
            free = parent.grant.amounts - self._children_in_use(parent)
            if free.fits(spec.reservation.minimum()):
                self._admit(entry, min(spec.reservation.cpu_max, free.cpu), parent.grant.lease_id)
            return
        if self._brake >= BrakeLevel.RED:
            return
        if self._brake == BrakeLevel.AMBER and spec.priority != PriorityClass.CRITICAL_PATH:
            return
        free = self._budget - self._in_use()
        if not free.fits(spec.reservation.minimum()):
            return
        cpu = spec.reservation.cpu_min
        if self._brake == BrakeLevel.GREEN:
            cpu = min(spec.reservation.cpu_max, free.cpu)
        self._admit(entry, cpu, None)

    def _admit(self, entry: _Entry, cpu: float, parent_lease: str | None) -> None:
        spec = entry.spec
        amounts = Capacity(cpu, spec.reservation.memory_bytes, dict(spec.reservation.pools))
        entry.grant = Grant(
            lease_id=f"lease-{entry.arrival}",
            job_id=spec.job_id,
            amounts=amounts,
            parent_lease=parent_lease,
            granted_at=self._clock(),
        )
        entry.state = _RUNNING
        self._emit(EventKind.ADMITTED, spec.job_id, lease=entry.grant.lease_id)

    def _end(self, entry: _Entry, state: str, kind: EventKind, **detail: object) -> None:
        for child_id in list(entry.children):
            child = self._entries[child_id]
            if child.state in (_QUEUED, _RUNNING):
                if state == _LOST:
                    self._end(child, _LOST, EventKind.ORPHAN_RECLAIMED, reason="parent reclaimed")
                else:
                    self._end(child, _CANCELLED, EventKind.CANCELLED, reason="parent released")
        entry.state = state
        self._emit(kind, entry.spec.job_id, **detail)

    def _reclaim(self, entry: _Entry, kind: EventKind, reason: str) -> None:
        self._end(entry, _LOST, kind, reason=reason)
