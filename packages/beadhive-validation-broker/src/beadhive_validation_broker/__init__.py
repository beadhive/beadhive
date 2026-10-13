"""Validation broker port (``docs/design/validation-broker-adr.md``).

A **library package** (``docs/design/package-class-library-vs-plugin-adr.md``): stdlib only, and
it never imports ``beadhive``. It holds the six-operation port (``submit``, ``wait_grant``,
``report``, ``cancel``, ``status``, ``events``), the typed job, reservation, lease and grant
model, the execution roots a key run is placed on (``bh-mf1o4``), the error types, an
in-memory fake with an injected clock, and the local in-process engine (:class:`LocalEngine`,
``bh-qrixp``) with its stdlib host probes and the :class:`FailoverBroker` that falls back to
another engine loudly. The reusable conformance scenarios live in
:mod:`beadhive_validation_broker.contract`. The slots engine that reproduces today's host
admission is root-side (``beadhive.validation_broker_slots``), as is the engine selection that
reads configuration (``beadhive.validation_broker_engine``) and the run directory that
materialises a run inside its execution root (``beadhive.validation_run_root``).
"""

from __future__ import annotations

from ._errors import BrokerError, BrokerUnavailable, GrantRefused, GrantTimeout, LeaseLost
from ._failover import FailoverBroker
from ._fake import FakeAvailability, FakeDiskSpace, InMemoryBroker, RecordedReport
from ._host import MeminfoAvailability, current_owner, host_cpu_count, pid_alive, statvfs_free
from ._local import LocalEngine, PackerPolicy
from ._ports import Availability, BrakeControl, BrokerPort, Clock, ProcessLiveness
from ._roots import ROOT_POOL_PREFIX, DiskSpace, ExecutionRoot, RootKind, validate_roots
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
    "ROOT_POOL_PREFIX",
    "ZERO",
    "Availability",
    "BrakeControl",
    "BrakeLevel",
    "BrokerError",
    "BrokerEvent",
    "BrokerPort",
    "BrokerStatus",
    "BrokerUnavailable",
    "Capacity",
    "Clock",
    "DiskSpace",
    "EventKind",
    "ExecutionRoot",
    "FailoverBroker",
    "FakeAvailability",
    "FakeDiskSpace",
    "Grant",
    "GrantRefused",
    "GrantTimeout",
    "InMemoryBroker",
    "JobHandle",
    "JobSpec",
    "Lease",
    "LeaseLost",
    "LocalEngine",
    "MeminfoAvailability",
    "Outcome",
    "Owner",
    "PackerPolicy",
    "PriorityClass",
    "ProcessLiveness",
    "RecordedReport",
    "Reservation",
    "RootKind",
    "Usage",
    "current_owner",
    "host_cpu_count",
    "pid_alive",
    "statvfs_free",
    "validate_roots",
]
