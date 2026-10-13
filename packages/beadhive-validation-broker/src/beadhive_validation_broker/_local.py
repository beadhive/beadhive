"""The local engine: a production in-process broker (validation-broker-adr, ``bh-qrixp``).

The bake-off's in-memory broker and group-aware packer (``bh-5mdar.10``) made production-grade
inside one bh process. Everything happens under one lock: admission decides and grants in a
single step, so there is no start race against the brake.

**Admission.** A job starts only when its *whole minimum reservation* fits what is free in every
dimension at once: cpu, memory and each named pool (``dolt-servers`` and friends). Free is
``min(budget, measured available - floor) - in use``: the configured budget clamped by what the
host reports free (:data:`~beadhive_validation_broker.Availability`) less the floor, minus every
top-level lease already granted. The measured term is not credited back with the memory running
leases already use, so it double-counts that memory: conservative by design, never optimistic.
Pools are never measured; they come from the budget alone. A reservation larger than the whole
budget is refused at ``submit`` (``GrantRefused``); one that merely does not fit *now* waits.

**Ordering** (:class:`PackerPolicy`, the ADR's ``packer=group`` defaults). Priority classes are
strict. Within a class, groups (gates) already admitted go first, then waiting groups in arrival
order (or, with ``group_major``, by ``queued work - aging x time waited``). Within a group the
longest expected key goes first (``laggard_first``): the laggard is the gate.

**Generalised EASY backfill.** Every admitted group's first blocked key, plus the first blocked
key of the head waiting group, holds a reservation at the earliest time the resource profile
fits it. A key may start now only if the profile stays feasible at every reservation start its
expected run overlaps, so a backfill never delays a reserved key. An overrunning lease is
assumed to end now.

**Flexible grants.** A grant is ``cpu_min`` plus spare CPU up to ``cpu_max``. Each group's
laggard (its longest unfinished key) is offered spare CPU first with no headroom kept
(``laggard_flex``); every other key keeps ``headroom_cpu`` back. Flex is drawn from the same
profile, so it can never delay a reservation either. Under the AMBER brake grants stay at
``cpu_min``; under RED nothing is admitted.

**Nested leases.** A job with ``parent_lease`` (``BH_VALIDATION_LEASE``) is served only from
that parent's grant, never from the host queue and never braked. One larger than the parent's
whole grant is refused (``GrantRefused``); one that fits later waits inside the parent.

**Killed clients.** Every admission pass first reclaims queued jobs and leases whose owner the
:data:`~beadhive_validation_broker.ProcessLiveness` probe reports dead, with every nested lease
under them, emitting ``ORPHAN_RECLAIMED``.

**Execution roots** (``bh-mf1o4``, opt-in). With ``roots`` configured, every top-level job is
placed on exactly one :class:`~beadhive_validation_broker.ExecutionRoot`, recorded as
``Grant.root``. The root's space is a counted pool (``root:<path>``) bounded by
``min(max_bytes, measured free bytes)`` through the
:data:`~beadhive_validation_broker.DiskSpace` port, so a job is admitted only when its
``disk_bytes`` fit that root; on a ``tmpfs`` root ``disk_bytes`` is added to the memory
reservation too. Roots are tried in configured order (the operator's preference) and the first
feasible one wins. A gate group is **pinned** to the root of its first admitted key while any of
its keys is live, so concurrent keys of one gate can share one checkout; a group is only pinned
to a root whose bound fits every queued key of the group. Nested jobs run in the parent's root
and carve their disk from the parent's grant. Without roots nothing here changes: ``disk_bytes``
is ignored and ``Grant.root`` is ``None``.

Durable leases, reconcile on restart and the brake's signals are out of scope here
(``bh-fmuqq.2``, ``bh-6a76l``).
"""

from __future__ import annotations

import itertools
import math
import threading
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field

from ._errors import BrokerUnavailable, GrantRefused, GrantTimeout, LeaseLost
from ._ports import Availability, Clock, ProcessLiveness
from ._roots import ROOT_POOL_PREFIX, DiskSpace, ExecutionRoot, validate_roots
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

__all__ = ["LocalEngine", "PackerPolicy"]

_QUEUED, _RUNNING, _DONE, _CANCELLED, _LOST = "queued", "running", "done", "cancelled", "lost"
_EPS = 1e-6
_OVERRUN_S = 1e-3  # an overrunning lease is assumed to end just after now


@dataclass(frozen=True)
class PackerPolicy:
    """The group-aware packer's knobs, with the ADR's defaults (``packer=group``)."""

    laggard_first: bool = True
    laggard_flex: bool = True
    easy: bool = True
    headroom_cpu: float = 8.0
    group_major: bool = False
    aging: float = 4.0  # consulted only with group_major

    def __post_init__(self) -> None:
        if self.headroom_cpu < 0 or self.aging < 0:
            raise ValueError("headroom_cpu and aging must be non-negative")


class _Profile:
    """Future availability: free now, the releases of running work, and EASY reservations."""

    def __init__(self, free: Capacity, now: float, running: list[tuple[float, Capacity]]) -> None:
        self.free = free
        self.now = now
        self.ends: list[list] = [[end, amounts] for end, amounts in running]
        self.reservations: list[tuple[float, float, Capacity]] = []  # (start, end, need)

    def cap_at(self, t: float) -> Capacity:
        cap = self.free
        for end, amounts in self.ends:
            if end <= t:
                cap = cap + amounts
        for start, end, need in self.reservations:
            if start <= t + _EPS < end:
                cap = cap - need
        return cap

    def _checkpoints(self, start: float, duration: float) -> list[float]:
        return [start] + [
            s for s, _e, _n in self.reservations if start + _EPS < s < start + duration
        ]

    def feasible(self, need: Capacity, start: float, duration: float) -> bool:
        return all(self.cap_at(t).fits(need) for t in self._checkpoints(start, duration))

    def slack_cpu(self, duration: float) -> float:
        """CPU that could be added to a job starting now without breaking a reservation."""
        return min(self.cap_at(t).cpu for t in self._checkpoints(self.now, duration))

    def start_now(self, need: Capacity, duration: float) -> int:
        self.free = self.free - need
        self.ends.append([self.now + duration, need])
        return len(self.ends) - 1

    def grow(self, index: int, cpu: float) -> None:
        extra = Capacity(cpu, 0)
        self.free = self.free - extra
        self.ends[index][1] = self.ends[index][1] + extra

    def earliest(self, need: Capacity, duration: float) -> float | None:
        """The earliest future start at which ``need`` fits for ``duration``, if any."""
        times = sorted(
            {end for end, _ in self.ends if end > self.now}
            | {end for _s, end, _n in self.reservations}
        )
        for t in times:
            if self.feasible(need, t, duration):
                return t
        return None

    def reserve(self, need: Capacity, duration: float) -> float | None:
        t = self.earliest(need, duration)
        if t is not None:
            self.reservations.append((t, t + duration, need))
        return t


@dataclass
class _Entry:
    spec: JobSpec
    handle: JobHandle
    arrival: int
    state: str = _QUEUED
    grant: Grant | None = None
    children: list[str] = field(default_factory=list)


@dataclass
class _Group:
    order: int
    first_seen: float
    admitted: bool = False
    root: ExecutionRoot | None = None  # pinned while any of the group's keys is live


class LocalEngine:
    """A :class:`~beadhive_validation_broker.BrokerPort` that schedules in this process."""

    name = "local"

    def __init__(
        self,
        budget: Capacity,
        clock: Clock,
        *,
        availability: Availability | None = None,
        floor: Capacity = ZERO,
        owner_alive: ProcessLiveness | None = None,
        policy: PackerPolicy | None = None,
        repoll_seconds: float = 1.0,
        roots: Sequence[ExecutionRoot] = (),
        disk_space: DiskSpace | None = None,
    ) -> None:
        if repoll_seconds <= 0:
            raise ValueError("repoll_seconds must be positive")
        if any(name.startswith(ROOT_POOL_PREFIX) for name in budget.pools):
            raise ValueError(f"pool names starting with {ROOT_POOL_PREFIX!r} are reserved")
        self._roots = validate_roots(roots)
        if self._roots and disk_space is None:
            raise ValueError("execution roots need a disk_space probe")
        self._disk_space = disk_space
        # Each root's bound joins the budget as a counted pool; without roots this is ``budget``.
        root_pools = {root.pool: root.max_bytes for root in self._roots}
        self._budget = Capacity(budget.cpu, budget.memory_bytes, {**budget.pools, **root_pools})
        self._clock = clock
        self._availability = availability
        self._floor = floor
        self._owner_alive: Callable[[Owner], bool] = owner_alive or (lambda _owner: True)
        self._policy = policy or PackerPolicy()
        self._repoll = repoll_seconds
        self._cond = threading.Condition(threading.RLock())
        self._entries: dict[str, _Entry] = {}
        self._groups: dict[str, _Group] = {}
        self._events: list[BrokerEvent] = []
        self._arrivals = itertools.count(1)
        self._group_order = itertools.count(1)
        self._available = True
        self._brake = BrakeLevel.GREEN
        self._probe_error: Exception | None = None
        self._measured: Capacity | None = None
        self._easy_reservations = 0
        self._backfill_refused = 0

    @property
    def policy(self) -> PackerPolicy:
        return self._policy

    @property
    def roots(self) -> tuple[ExecutionRoot, ...]:
        return self._roots

    # ---- controls (not part of the port) ------------------------------------------------

    def set_available(self, available: bool) -> None:
        """Mark the engine down or back up (every port operation raises while down)."""
        with self._cond:
            self._available = available
            self._cond.notify_all()

    def set_brake(self, level: BrakeLevel, reason: str) -> None:
        with self._cond:
            self._brake = level
            self._emit(EventKind.BRAKED, None, level=int(level), reason=reason)
            self._pump()

    def reap(self) -> None:
        """Reclaim dead owners' jobs and leases now, then re-run admission."""
        with self._cond:
            self._pump()

    # ---- the port ----------------------------------------------------------------------

    def submit(self, job: JobSpec) -> JobHandle:
        with self._cond:
            self._require_up()
            existing = self._entries.get(job.job_id)
            if existing is not None:
                return existing.handle
            if job.parent_lease is not None:
                self._check_nested(job)
            else:
                self._check_budget(job)
                self._probe()  # a failing availability probe refuses before anything is queued
            arrival = next(self._arrivals)
            handle = JobHandle(job.job_id, self.name, str(arrival))
            entry = _Entry(job, handle, arrival)
            self._entries[job.job_id] = entry
            if job.parent_lease is not None:
                self._lease_holder(job).children.append(job.job_id)
            elif job.group not in self._groups:
                self._groups[job.group] = _Group(next(self._group_order), self._clock())
            self._emit(EventKind.SUBMITTED, job.job_id, group=job.group)
            self._pump()
            return handle

    def wait_grant(self, handle: JobHandle, timeout: float | None = None) -> Grant:
        with self._cond:
            deadline = None if timeout is None else self._clock() + timeout
            while True:
                self._require_up()
                entry = self._entry(handle)
                self._pump()
                if entry.state in (_RUNNING, _DONE) and entry.grant is not None:
                    return entry.grant
                if entry.state != _QUEUED:
                    raise LeaseLost(f"{handle.job_id}: lease is {entry.state}")
                if self._probe_error is not None and entry.spec.parent_lease is None:
                    # Never leave a ghost in the queue for a client that is falling back.
                    self._end(entry, _CANCELLED, EventKind.CANCELLED, reason="broker unavailable")
                    raise BrokerUnavailable(
                        f"{handle.job_id}: availability probe failed: {self._probe_error}"
                    )
                remaining = None if deadline is None else deadline - self._clock()
                if remaining is not None and remaining <= 0:
                    raise GrantTimeout(f"{handle.job_id}: still queued")
                self._cond.wait(self._repoll if remaining is None else min(remaining, self._repoll))

    def report(self, handle: JobHandle, outcome: Outcome, usage: Usage) -> None:
        del usage  # accounting only; history and right-sizing belong to the caller's records
        with self._cond:
            self._require_up()
            entry = self._entry(handle)
            if entry.state == _DONE:
                return
            if entry.state != _RUNNING:
                raise LeaseLost(f"{handle.job_id}: lease is {entry.state}")
            self._end(entry, _DONE, EventKind.FINISHED, exit_code=outcome.exit_code)
            self._pump()

    def cancel(self, handle: JobHandle, reason: str) -> None:
        with self._cond:
            self._require_up()
            entry = self._entries.get(handle.job_id)
            if entry is None or entry.state in (_DONE, _CANCELLED, _LOST):
                return
            self._end(entry, _CANCELLED, EventKind.CANCELLED, reason=reason)
            self._pump()

    def status(self) -> BrokerStatus:
        with self._cond:
            self._require_up()
            self._probe()  # a fresh reading: BrokerUnavailable while the probe fails
            self._pump()
            states = [e.state for e in self._entries.values()]
            signals: dict[str, float] = {
                "easy_reservations": float(self._easy_reservations),
                "backfill_refused": float(self._backfill_refused),
            }
            if self._measured is not None:
                signals["measured_cpu"] = self._measured.cpu
                signals["measured_memory_bytes"] = float(self._measured.memory_bytes)
            for path, free in self._measure_roots().items():
                signals["root_free_bytes:" + path] = float(free)
            return BrokerStatus(
                backend=self.name,
                budget=self._budget,
                in_use=self._in_use(),
                queued=states.count(_QUEUED),
                running=states.count(_RUNNING),
                brake=self._brake,
                signals=signals,
            )

    def events(self, since: int = 0) -> Iterator[BrokerEvent]:
        with self._cond:
            self._require_up()
            return iter([e for e in self._events if e.seq > since])

    # ---- bookkeeping -------------------------------------------------------------------

    def _require_up(self) -> None:
        if not self._available:
            raise BrokerUnavailable("the local broker engine is marked down")

    def _entry(self, handle: JobHandle) -> _Entry:
        entry = self._entries.get(handle.job_id)
        if entry is None or entry.handle != handle:
            raise LeaseLost(f"{handle.job_id}: unknown job")
        return entry

    def _emit(self, kind: EventKind, job_id: str | None, **detail: object) -> None:
        self._events.append(BrokerEvent(len(self._events) + 1, kind, self._clock(), job_id, detail))

    def _top_running(self) -> list[_Entry]:
        return [
            e
            for e in self._entries.values()
            if e.state == _RUNNING and e.spec.parent_lease is None and e.grant is not None
        ]

    def _in_use(self) -> Capacity:
        used = ZERO
        for entry in self._top_running():
            assert entry.grant is not None
            used = used + entry.grant.amounts
        return used

    def _lease_holder(self, job: JobSpec) -> _Entry:
        for entry in self._entries.values():
            if entry.grant is not None and entry.grant.lease_id == job.parent_lease:
                return entry
        raise LeaseLost(f"{job.job_id}: parent lease {job.parent_lease} is gone")

    def _check_nested(self, job: JobSpec) -> None:
        parent = self._lease_holder(job)
        if parent.state != _RUNNING or parent.grant is None:
            raise LeaseLost(f"{job.job_id}: parent lease {job.parent_lease} is not running")
        if not parent.grant.amounts.fits(self._need(job, self._root_of(parent))):
            raise GrantRefused(f"{job.job_id}: reservation can never fit its parent's grant")

    def _check_budget(self, job: JobSpec) -> None:
        """Refuse at submit what can never fit the budget (on any root, or the pinned one)."""
        if not self._roots:
            if not self._budget.fits(job.reservation.minimum()):
                raise GrantRefused(f"{job.job_id}: reservation exceeds the host budget")
            return
        if not self._gate_roots(job.group, [job]):
            group = self._groups.get(job.group)
            pinned = group.root if group is not None else None
            where = f"its gate's root {pinned.path}" if pinned is not None else "any root"
            raise GrantRefused(
                f"{job.job_id}: reservation exceeds the budget of {where} (with its gate's "
                "other queued keys)"
            )

    # ---- execution roots ---------------------------------------------------------------

    def _root_of(self, entry: _Entry) -> ExecutionRoot | None:
        if entry.grant is None or entry.grant.root is None:
            return None
        return next(r for r in self._roots if r.path == entry.grant.root)

    @staticmethod
    def _need(spec: JobSpec, root: ExecutionRoot | None) -> Capacity:
        """The minimum reservation placed on ``root``: disk joins as the root's pool, and as
        memory too on a tmpfs root. Without a root it is exactly ``minimum()``."""
        base = spec.reservation.minimum()
        if root is None:
            return base
        disk = spec.reservation.disk_bytes
        memory = base.memory_bytes + (disk if root.memory_backed else 0)
        return Capacity(base.cpu, memory, {**base.pools, root.pool: disk})

    def _root_options(self, entry: _Entry) -> list[ExecutionRoot | None]:
        """The roots ``entry`` may start on now, in preference order."""
        if not self._roots:
            return [None]
        return list(self._gate_roots(entry.spec.group, []))

    def _gate_roots(self, group: str, extra: list[JobSpec]) -> list[ExecutionRoot]:
        """The roots a gate may run on: its pinned root, else every root whose bound fits each
        of its queued keys (plus ``extra``), so pinning can never strand a later key."""
        state = self._groups.get(group)
        options = [state.root] if state is not None and state.root is not None else self._roots
        specs = [
            e.spec
            for e in self._entries.values()
            if e.spec.group == group and e.spec.parent_lease is None and e.state == _QUEUED
        ] + extra
        return [
            root
            for root in options
            if all(self._budget.fits(self._need(spec, root)) for spec in specs)
        ]

    def _measure_roots(self) -> dict[str, int]:
        """Free bytes per root; an unreadable root reads as 0 (nothing is admitted to it)."""
        if self._disk_space is None:
            return {}
        free: dict[str, int] = {}
        for root in self._roots:
            try:
                free[root.path] = max(0, int(self._disk_space(root.path)))
            except Exception:  # noqa: BLE001 - an unreadable root is a full root, never optimistic
                free[root.path] = 0
        return free

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

    def _probe(self) -> Capacity | None:
        """Read availability; ``BrokerUnavailable`` (and remembered) when the probe fails."""
        if self._availability is None:
            self._probe_error = None
            return None
        try:
            measured = self._availability()
        except Exception as exc:  # noqa: BLE001 - any probe failure means "admit nothing"
            self._probe_error = exc
            raise BrokerUnavailable(f"availability probe failed: {exc}") from exc
        self._probe_error = None
        self._measured = measured
        return measured

    def _host_free(self) -> Capacity:
        """``min(budget, measured available - floor) - in use``, per dimension."""
        limit = self._budget
        measured = self._probe()
        if measured is not None:
            room = measured - self._floor
            pools = {
                name: min(amount, room.pools[name]) if name in measured.pools else amount
                for name, amount in self._budget.pools.items()
            }
            limit = Capacity(
                min(limit.cpu, room.cpu), min(limit.memory_bytes, room.memory_bytes), pools
            )
        if self._roots:
            # Like memory, measured free space is not credited back with what running leases
            # will still write: conservative by design.
            pools = dict(limit.pools)
            free = self._measure_roots()
            for root in self._roots:
                pools[root.pool] = min(pools[root.pool], free[root.path])
            limit = Capacity(limit.cpu, limit.memory_bytes, pools)
        return limit - self._in_use()

    # ---- admission ---------------------------------------------------------------------

    def _pump(self) -> None:
        self._reap_dead()
        self._serve_nested()
        self._admit_host()
        for name in [g for g in self._groups if not self._group_live(g)]:
            del self._groups[name]
        self._cond.notify_all()

    def _group_live(self, group: str) -> bool:
        return any(
            e.spec.group == group and e.spec.parent_lease is None and e.state in (_QUEUED, _RUNNING)
            for e in self._entries.values()
        )

    def _reap_dead(self) -> None:
        alive: dict[Owner, bool] = {}
        for entry in list(self._entries.values()):
            if entry.state not in (_QUEUED, _RUNNING):
                continue
            owner = entry.spec.owner
            if owner not in alive:
                alive[owner] = bool(self._owner_alive(owner))
            if not alive[owner]:
                self._end(entry, _LOST, EventKind.ORPHAN_RECLAIMED, reason="owner dead")

    def _serve_nested(self) -> None:
        for parent in list(self._entries.values()):
            if parent.state != _RUNNING or parent.grant is None or not parent.children:
                continue
            free = parent.grant.amounts
            children = [self._entries[c] for c in parent.children]
            for child in children:
                if child.state == _RUNNING and child.grant is not None:
                    free = free - child.grant.amounts
            root = self._root_of(parent)
            for child in sorted(children, key=lambda e: e.arrival):
                if child.state != _QUEUED or not free.fits(self._need(child.spec, root)):
                    continue
                cpu = min(child.spec.reservation.cpu_max, free.cpu)
                grant = self._admit(child, cpu, parent.grant.lease_id, root)
                free = free - grant.amounts

    def _admit_host(self) -> None:
        if self._brake >= BrakeLevel.RED:
            return
        candidates = [
            e for e in self._entries.values() if e.state == _QUEUED and e.spec.parent_lease is None
        ]
        if self._brake == BrakeLevel.AMBER:
            candidates = [e for e in candidates if e.spec.priority == PriorityClass.CRITICAL_PATH]
        if not candidates:
            return
        try:
            free = self._host_free()
        except BrokerUnavailable:
            return  # admit nothing on a failed probe; wait_grant reports it to the client
        now = self._clock()
        running = []
        for entry in self._top_running():
            assert entry.grant is not None
            end = entry.grant.granted_at + entry.spec.expected_duration_s
            running.append((max(now + _OVERRUN_S, end), entry.grant.amounts))
        profile = _Profile(free, now, running)
        chosen = self._pick(candidates, profile, now)
        if not chosen:
            return
        extra = self._flex(chosen, profile) if self._brake == BrakeLevel.GREEN else {}
        for entry, _index, root in chosen:
            reservation = entry.spec.reservation
            self._admit(entry, reservation.cpu_min + extra.get(entry.spec.job_id, 0.0), None, root)

    def _group_key(self, group: str, keys: list[_Entry], now: float) -> tuple:
        state = self._groups[group]
        priority = min(int(e.spec.priority) for e in keys)
        if state.admitted:
            return (priority, 0, state.first_seen, state.order)
        if self._policy.group_major:
            work = sum(e.spec.expected_duration_s for e in keys)
            return (priority, 1, work - self._policy.aging * (now - state.first_seen), state.order)
        return (priority, 1, state.first_seen, state.order)

    def _pick(
        self, candidates: list[_Entry], profile: _Profile, now: float
    ) -> list[tuple[_Entry, int, ExecutionRoot | None]]:
        groups: dict[str, list[_Entry]] = {}
        for entry in candidates:
            groups.setdefault(entry.spec.group, []).append(entry)
        policy = self._policy
        chosen: list[tuple[_Entry, int, ExecutionRoot | None]] = []
        head_taken = False
        for group in sorted(groups, key=lambda g: self._group_key(g, groups[g], now)):
            state = self._groups[group]
            if policy.laggard_first:
                keys = sorted(groups[group], key=lambda e: (-e.spec.expected_duration_s, e.arrival))
            else:
                keys = sorted(groups[group], key=lambda e: e.arrival)
            reserved = False
            for position, entry in enumerate(keys):
                duration = entry.spec.expected_duration_s
                needs = [(root, self._need(entry.spec, root)) for root in self._root_options(entry)]
                placed = next(
                    ((root, need) for root, need in needs if profile.feasible(need, now, duration)),
                    None,
                )
                if placed is not None:
                    root, need = placed
                    chosen.append((entry, profile.start_now(need, duration), root))
                    if root is not None:
                        state.root = root  # the gate's later keys share this root
                    if position == 0:
                        state.admitted = True  # its first key in order started: finish the group
                    continue
                if any(profile.free.fits(need) for _root, need in needs):
                    self._backfill_refused += 1  # fits now, but would delay a reservation
                if reserved or not policy.easy:
                    continue
                if state.admitted or not head_taken:
                    if not state.admitted:
                        head_taken = True
                        if not policy.group_major:
                            state.admitted = True  # arrival-order admission holds the head
                    if self._reserve(profile, needs, duration):
                        self._easy_reservations += 1
                    reserved = True
        return chosen

    @staticmethod
    def _reserve(
        profile: _Profile, needs: list[tuple[ExecutionRoot | None, Capacity]], duration: float
    ) -> bool:
        """Hold an EASY reservation on whichever root can start the key earliest."""
        best: tuple[float, Capacity] | None = None
        for _root, need in needs:
            t = profile.earliest(need, duration)
            if t is not None and (best is None or t < best[0]):
                best = (t, need)
        if best is None:
            return False
        return profile.reserve(best[1], duration) is not None

    def _is_laggard(self, entry: _Entry) -> bool:
        """The group's longest unfinished key (ties: the earliest arrival)."""
        peers = [
            e
            for e in self._entries.values()
            if e.spec.group == entry.spec.group
            and e.spec.parent_lease is None
            and (e.state in (_QUEUED, _RUNNING))
        ]
        longest = min(peers, key=lambda e: (-e.spec.expected_duration_s, e.arrival))
        return longest is entry

    def _flex(
        self, chosen: list[tuple[_Entry, int, ExecutionRoot | None]], profile: _Profile
    ) -> dict[str, float]:
        policy = self._policy
        laggards = {e.spec.job_id for e, _i, _r in chosen if self._is_laggard(e)}
        if policy.laggard_flex:
            ordered = [c for c in chosen if c[0].spec.job_id in laggards]
            ordered += [c for c in chosen if c[0].spec.job_id not in laggards]
        else:
            ordered = list(chosen)
        extra: dict[str, float] = {}
        for entry, index, _root in ordered:
            reservation = entry.spec.reservation
            want = reservation.cpu_max - reservation.cpu_min
            if want <= _EPS:
                continue
            keep = 0.0
            if not (policy.laggard_flex and entry.spec.job_id in laggards):
                keep = policy.headroom_cpu
            give = min(want, profile.slack_cpu(entry.spec.expected_duration_s) - keep)
            if give < want:
                give = math.floor(give + _EPS)  # partial flex in whole CPUs
            if give <= _EPS:
                continue
            extra[entry.spec.job_id] = give
            profile.grow(index, give)
        return extra

    def _admit(
        self, entry: _Entry, cpu: float, parent_lease: str | None, root: ExecutionRoot | None
    ) -> Grant:
        spec = entry.spec
        need = self._need(spec, root)
        amounts = Capacity(cpu, need.memory_bytes, dict(need.pools))
        entry.grant = Grant(
            lease_id=f"local-lease-{entry.arrival}",
            job_id=spec.job_id,
            amounts=amounts,
            parent_lease=parent_lease,
            granted_at=self._clock(),
            root=None if root is None else root.path,
        )
        entry.state = _RUNNING
        detail: dict[str, object] = {"lease": entry.grant.lease_id, "cpu": cpu}
        if root is not None:
            detail["root"] = root.path
        self._emit(EventKind.ADMITTED, spec.job_id, **detail)
        return entry.grant
