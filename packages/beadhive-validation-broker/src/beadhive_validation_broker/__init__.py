"""Validation broker port (``docs/design/validation-broker-adr.md``).

A **library package** (``docs/design/package-class-library-vs-plugin-adr.md``): stdlib only, and
it never imports ``beadhive``. It holds the six-operation port (``submit``, ``wait_grant``,
``report``, ``cancel``, ``status``, ``events``), the typed job, reservation, lease and grant
model, the error types and an in-memory fake with an injected clock. The reusable conformance
scenarios live in :mod:`beadhive_validation_broker.contract`. Engines live elsewhere: the
slots engine that reproduces today's host admission is root-side
(``beadhive.validation_broker_slots``), and the local in-process engine is ``bh-qrixp``.
"""

from __future__ import annotations

from ._errors import BrokerError, BrokerUnavailable, GrantRefused, GrantTimeout, LeaseLost
from ._fake import InMemoryBroker, RecordedReport
from ._ports import BrakeControl, BrokerPort, Clock, ProcessLiveness
from ._types import (
    LEASE_ENV,
    ZERO,
    BrakeLevel,
    BrokerEvent,
    BrokerStatus,
    Capacity,
    EventKind,
    Grant,
    JobHandle,
    JobSpec,
    Lease,
    Outcome,
    Owner,
    PriorityClass,
    Reservation,
    Usage,
)

__all__ = [
    "LEASE_ENV",
    "ZERO",
    "BrakeControl",
    "BrakeLevel",
    "BrokerError",
    "BrokerEvent",
    "BrokerPort",
    "BrokerStatus",
    "BrokerUnavailable",
    "Capacity",
    "Clock",
    "EventKind",
    "Grant",
    "GrantRefused",
    "GrantTimeout",
    "InMemoryBroker",
    "JobHandle",
    "JobSpec",
    "Lease",
    "LeaseLost",
    "Outcome",
    "Owner",
    "PriorityClass",
    "ProcessLiveness",
    "RecordedReport",
    "Reservation",
    "Usage",
]
