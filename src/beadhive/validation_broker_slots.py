"""The slots engine: today's host admission behind the validation broker port.

:class:`SlotsEngine` implements :class:`beadhive_validation_broker.BrokerPort` over
:func:`beadhive.validation_admission.host_slot`, so a broker client can be written once against
the port and still get exactly the admission it gets today: the file-lock counting semaphore
under ``validation_slots`` / ``BH_VALIDATION_SLOTS``, the ``MemAvailable`` floor, the telemetry
and the user-facing messages. It lives root-side, not in the stdlib-only library package,
because that behaviour is root code (``typer``, ``platformdirs``, ``otel``).

Mapping, deliberately thin:

* ``submit`` starts a holder thread that enters ``host_slot`` and keeps the permit until the lease
  ends. ``host_slot`` is a blocking context manager with no cancel point, so a queued job that is
  cancelled is released the moment its permit would have been granted.
* ``wait_grant`` waits for that permit. The grant echoes the reservation minimum: a slot is a
  count, not a resource, so nothing is enforced beyond today's semantics. A ``typer.Exit`` (the
  retryable 75 from the memory floor) raised inside admission is re-raised here unchanged.
* A nested request (``parent_lease``) is served from the parent's grant without taking a slot,
  never queueing host-wide; one that exceeds the parent's grant is refused.
* ``report`` and ``cancel`` release the permit; both are idempotent.

No production call site uses this yet (``bh-uljpc`` is the port only).
"""

from __future__ import annotations

import itertools
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from beadhive_validation_broker import (
    BrakeLevel,
    BrokerEvent,
    BrokerStatus,
    Capacity,
    EventKind,
    Grant,
    GrantRefused,
    GrantTimeout,
    JobHandle,
    JobSpec,
    LeaseLost,
    Outcome,
    Usage,
)

from . import validation_admission

__all__ = ["SlotsEngine"]


@dataclass
class _Held:
    spec: JobSpec
    handle: JobHandle
    admitted: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)
    ended: threading.Event = field(default_factory=threading.Event)
    grant: Grant | None = None
    error: BaseException | None = None
    cancelled: bool = False
    state: str = "queued"
    children: list[str] = field(default_factory=list)
    parent_id: str | None = None


class SlotsEngine:
    """A :class:`~beadhive_validation_broker.BrokerPort` over the host file-lock slots."""

    name = "slots"

    def __init__(
        self,
        cfg: dict,
        entry=None,
        *,
        phase: str = "validation",
        root: Path | None = None,
    ) -> None:
        self._cfg = cfg
        self._entry = entry
        self._phase = phase
        self._root = root
        self._lock = threading.RLock()
        self._jobs: dict[str, _Held] = {}
        self._events: list[BrokerEvent] = []
        self._lease_ids = itertools.count(1)

    # ---- the port ----------------------------------------------------------------------

    def submit(self, job: JobSpec) -> JobHandle:
        with self._lock:
            existing = self._jobs.get(job.job_id)
            if existing is not None:
                return existing.handle
            handle = JobHandle(job.job_id, self.name, f"slot-job-{job.job_id}")
            held = _Held(job, handle)
            if job.parent_lease is not None:
                self._admit_nested(held)
            else:
                self._jobs[job.job_id] = held
                threading.Thread(
                    target=self._hold_slot, args=(held,), name=f"slots-{job.job_id}", daemon=True
                ).start()
            self._emit(EventKind.SUBMITTED, job.job_id, group=job.group)
            return handle

    def wait_grant(self, handle: JobHandle, timeout: float | None = None) -> Grant:
        held = self._held(handle)
        if not held.admitted.wait(timeout):
            raise GrantTimeout(f"{handle.job_id}: still waiting for a validation slot")
        if held.error is not None:
            raise held.error
        if held.grant is None:
            raise LeaseLost(f"{handle.job_id}: lease is {held.state}")
        if held.state not in ("running", "done"):
            raise LeaseLost(f"{handle.job_id}: lease is {held.state}")
        return held.grant

    def report(self, handle: JobHandle, outcome: Outcome, usage: Usage) -> None:
        del usage  # accounting only; today's admission records none
        held = self._held(handle)
        with self._lock:
            if held.state == "done":
                return
            if held.state != "running":
                raise LeaseLost(f"{handle.job_id}: lease is {held.state}")
            self._finish(held, "done", EventKind.FINISHED, exit_code=outcome.exit_code)

    def cancel(self, handle: JobHandle, reason: str) -> None:
        with self._lock:
            held = self._jobs.get(handle.job_id)
            if held is None or held.state in ("done", "cancelled"):
                return
            held.cancelled = True
            self._finish(held, "cancelled", EventKind.CANCELLED, reason=reason)

    def status(self) -> BrokerStatus:
        with self._lock:
            slots = validation_admission.configured_slots(self._cfg, self._entry)
            states = [h.state for h in self._jobs.values()]
            running = sum(
                1
                for h in self._jobs.values()
                if h.state == "running" and h.spec.parent_lease is None
            )
            return BrokerStatus(
                backend=self.name,
                budget=Capacity(float(slots), 0),
                in_use=Capacity(float(running), 0),
                queued=states.count("queued"),
                running=states.count("running"),
                brake=BrakeLevel.GREEN,
            )

    def events(self, since: int = 0) -> Iterator[BrokerEvent]:
        with self._lock:
            return iter([e for e in self._events if e.seq > since])

    # ---- internals ---------------------------------------------------------------------

    def _held(self, handle: JobHandle) -> _Held:
        with self._lock:
            held = self._jobs.get(handle.job_id)
        if held is None:
            raise LeaseLost(f"{handle.job_id}: unknown job")
        return held

    def _emit(self, kind: EventKind, job_id: str | None, **detail: object) -> None:
        self._events.append(
            BrokerEvent(len(self._events) + 1, kind, time.monotonic(), job_id, detail)
        )

    def _grant(self, held: _Held, parent_lease: str | None) -> Grant:
        return Grant(
            lease_id=f"slots-lease-{next(self._lease_ids)}",
            job_id=held.spec.job_id,
            amounts=held.spec.reservation.minimum(),
            parent_lease=parent_lease,
            granted_at=time.monotonic(),
        )

    def _hold_slot(self, held: _Held) -> None:
        """Enter today's ``host_slot`` and keep the permit until the lease ends."""
        try:
            with validation_admission.host_slot(
                self._cfg, self._entry, phase=self._phase, root=self._root
            ):
                with self._lock:
                    if held.cancelled:
                        return
                    held.grant = self._grant(held, None)
                    held.state = "running"
                    self._emit(EventKind.ADMITTED, held.spec.job_id, lease=held.grant.lease_id)
                held.admitted.set()
                held.release.wait()
        except BaseException as exc:  # noqa: BLE001 - re-raised in wait_grant, e.g. typer.Exit(75)
            held.error = exc
            held.state = "lost"
        finally:
            held.admitted.set()
            held.ended.set()

    def _admit_nested(self, held: _Held) -> None:
        spec = held.spec
        parent = next(
            (
                p
                for p in self._jobs.values()
                if p.grant is not None and p.grant.lease_id == spec.parent_lease
            ),
            None,
        )
        if parent is None or parent.state != "running" or parent.grant is None:
            raise LeaseLost(f"{spec.job_id}: parent lease {spec.parent_lease} is gone")
        if not parent.grant.amounts.fits(spec.reservation.minimum()):
            raise GrantRefused(f"{spec.job_id}: reservation can never fit its parent's grant")
        held.parent_id = parent.spec.job_id
        self._jobs[spec.job_id] = held
        parent.children.append(spec.job_id)
        # Never host-queued and never hold-and-wait on the host: if the parent's grant is spent
        # by siblings, the request waits inside that grant until a sibling ends.
        self._serve_nested(parent)

    def _serve_nested(self, parent: _Held) -> None:
        if parent.grant is None or parent.state != "running":
            return
        free = parent.grant.amounts
        for child_id in parent.children:
            child = self._jobs[child_id]
            if child.state == "running" and child.grant is not None:
                free = free - child.grant.amounts
        for child_id in parent.children:
            child = self._jobs[child_id]
            if child.state != "queued":
                continue
            need = child.spec.reservation.minimum()
            if not free.fits(need):
                continue
            child.grant = self._grant(child, parent.grant.lease_id)
            child.state = "running"
            free = free - child.grant.amounts
            self._emit(EventKind.ADMITTED, child.spec.job_id, lease=child.grant.lease_id)
            child.admitted.set()

    def _finish(self, held: _Held, state: str, kind: EventKind, **detail: object) -> None:
        for child_id in held.children:
            child = self._jobs[child_id]
            if child.state in ("queued", "running"):
                self._finish(child, "cancelled", EventKind.CANCELLED, reason="parent released")
        held.state = state
        self._emit(kind, held.spec.job_id, **detail)
        held.release.set()
        held.admitted.set()
        if held.parent_id is not None:
            self._serve_nested(self._jobs[held.parent_id])
