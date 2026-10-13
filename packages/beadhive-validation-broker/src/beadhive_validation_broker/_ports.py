"""The broker port (validation-broker-adr): six operations behind one small protocol.

Three cross-cutting rules every engine and adapter keeps:

* **Backend down** -> every operation raises :class:`BrokerUnavailable`. Clients then fall back
  to today's file-lock host slots and warn; validation never blocks on the broker.
* **Killed client** -> the broker notices the dead :class:`Owner` and reclaims the lease (and
  every nested lease under it), emitting ``ORPHAN_RECLAIMED``.
* **Nested request** -> a :class:`JobSpec` with ``parent_lease`` draws only from that parent's
  grant and never re-enters the host queue (no hold-and-wait; the hq-vbha deadlock).

Nothing here touches verdicts: ``report`` is accounting only.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Protocol, runtime_checkable

from ._types import (
    BrakeLevel,
    BrokerEvent,
    BrokerStatus,
    Capacity,
    Grant,
    JobHandle,
    JobSpec,
    Outcome,
    Owner,
    Usage,
)

__all__ = ["Availability", "BrakeControl", "BrokerPort", "Clock", "ProcessLiveness"]

#: A monotonic-seconds source. The fake takes one so tests own time; production passes
#: ``time.monotonic``.
Clock = Callable[[], float]


@runtime_checkable
class BrokerPort(Protocol):
    name: str

    def submit(self, job: JobSpec) -> JobHandle:
        """Enqueue ``job``. Never blocks on capacity.

        * ``parent_lease`` set -> resolved against that lease only; ``LeaseLost`` if the parent
          is gone, ``GrantRefused`` if the child can never fit the parent's grant. A nested job
          never enters the host queue.
        * reservation larger than the whole budget -> ``GrantRefused``.
        * backend down -> ``BrokerUnavailable``.
        """
        ...

    def wait_grant(self, handle: JobHandle, timeout: float | None = None) -> Grant:
        """Block until the whole reservation is granted.

        * timeout -> ``GrantTimeout`` (the job stays queued).
        * cancelled or owner reclaimed while waiting -> ``LeaseLost``.
        * backend dies while waiting -> ``BrokerUnavailable``; the client falls back to slots
          and does not resubmit.
        """
        ...

    def report(self, handle: JobHandle, outcome: Outcome, usage: Usage) -> None:
        """Release the lease and record outcome and usage. Idempotent.

        ``LeaseLost`` if the lease was already reclaimed; accounting only, the caller's verdict
        still stands. Releasing a parent revokes its nested leases.
        """
        ...

    def cancel(self, handle: JobHandle, reason: str) -> None:
        """Drop a queued job or release a running lease. Idempotent; never raises
        ``LeaseLost``. Cancelling a parent cancels its nested jobs."""
        ...

    def status(self) -> BrokerStatus:
        """Point-in-time snapshot. ``BrokerUnavailable`` if down."""
        ...

    def events(self, since: int = 0) -> Iterator[BrokerEvent]:
        """Events with ``seq > since`` that exist now (a non-blocking snapshot; poll to follow).
        A restarted backend may start a new sequence; clients detect it by ``seq`` going
        backwards."""
        ...


class BrakeControl(Protocol):
    """Optional: an engine whose admission brake a test or the host seat can set."""

    def set_brake(self, level: BrakeLevel, reason: str) -> None: ...


#: Whether a lease owner is still alive (pid alive with the same start token).
ProcessLiveness = Callable[[Owner], bool]


#: What the host has free right now (cpu, memory bytes and, optionally, measured pools). The local
#: engine clamps its configured budget with it; a failing probe raises and admits nothing.
#: Production passes :class:`~beadhive_validation_broker.MeminfoAvailability`; tests pass
#: :class:`~beadhive_validation_broker.FakeAvailability`.
Availability = Callable[[], Capacity]
