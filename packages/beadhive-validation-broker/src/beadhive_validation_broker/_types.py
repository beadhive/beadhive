"""The typed job, reservation, lease and grant model (validation-broker-adr).

Seeded from the bake-off port (``bh-5mdar.1``) with memory in **bytes** and the lease-mode
contract spelled out: a nested request carries ``BH_VALIDATION_LEASE`` (:data:`LEASE_ENV`) as its
``parent_lease`` and draws only from that parent's grant; it never re-enters the host queue.
Everything is a frozen value; nothing here reads a clock, the environment or the filesystem.
"""

from __future__ import annotations

import enum
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from ._roots import ROOT_POOL_PREFIX

__all__ = [
    "LEASE_ENV",
    "BrakeLevel",
    "BrokerEvent",
    "BrokerStatus",
    "Capacity",
    "EventKind",
    "Grant",
    "JobHandle",
    "JobSpec",
    "Lease",
    "Outcome",
    "Owner",
    "PriorityClass",
    "Reservation",
    "Usage",
]

#: The environment variable that carries a lease id to a nested request (lease mode).
LEASE_ENV = "BH_VALIDATION_LEASE"

_MB = 1024 * 1024


class PriorityClass(enum.IntEnum):
    """Admission order across classes; lower value is admitted first."""

    CRITICAL_PATH = 0
    CHECK_SUBMIT = 1
    MERGE_LAND = 2
    BACKGROUND = 3  # fill only: never blocks foreground admission; the brake pauses it first


@dataclass(frozen=True)
class Capacity:
    """A resource vector: a host budget, amounts in use, or amounts granted."""

    cpu: float
    memory_bytes: int
    pools: Mapping[str, int] = field(default_factory=dict)

    def fits(self, other: Capacity, eps: float = 1e-9) -> bool:
        """True when ``other`` fits inside ``self`` in every dimension."""
        if other.cpu > self.cpu + eps or other.memory_bytes > self.memory_bytes:
            return False
        return all(amount <= self.pools.get(name, 0) for name, amount in other.pools.items())

    def __add__(self, other: Capacity) -> Capacity:
        pools = dict(self.pools)
        for name, amount in other.pools.items():
            pools[name] = pools.get(name, 0) + amount
        return Capacity(self.cpu + other.cpu, self.memory_bytes + other.memory_bytes, pools)

    def __sub__(self, other: Capacity) -> Capacity:
        pools = dict(self.pools)
        for name, amount in other.pools.items():
            pools[name] = pools.get(name, 0) - amount
        return Capacity(self.cpu - other.cpu, self.memory_bytes - other.memory_bytes, pools)


ZERO = Capacity(0.0, 0)


@dataclass(frozen=True)
class Reservation:
    """What a job needs before it may start. Whole reservation or nothing.

    ``cpu_min`` must be free for admission; the broker may grant up to ``cpu_max`` (the key's
    measured speedup knee). Memory and named pools (``dolt-servers`` and friends) are fixed
    amounts. ``disk_bytes`` is the run's expected footprint in its execution root (checkout and
    fence scratch); it is ignored unless the engine has execution roots (``bh-mf1o4``), and on a
    ``tmpfs`` root it also counts as memory.
    """

    cpu_min: float
    cpu_max: float
    memory_bytes: int
    pools: Mapping[str, int] = field(default_factory=dict)
    disk_bytes: int = 0

    def __post_init__(self) -> None:
        if self.cpu_min < 0 or self.cpu_max < self.cpu_min:
            raise ValueError("a reservation needs 0 <= cpu_min <= cpu_max")
        if (
            self.memory_bytes < 0
            or self.disk_bytes < 0
            or any(amount < 0 for amount in self.pools.values())
        ):
            raise ValueError("a reservation's memory, disk and pool amounts must be non-negative")

    def minimum(self) -> Capacity:
        return Capacity(self.cpu_min, self.memory_bytes, dict(self.pools))


@dataclass(frozen=True)
class Owner:
    """The client process that holds a lease. Liveness is pid alive with the same start token."""

    pid: int
    start_token: str | None = None
    host: str | None = None


@dataclass(frozen=True)
class JobSpec:
    """One opaque validation job (usually one attest key of one gate)."""

    job_id: str
    command: Sequence[str]
    reservation: Reservation
    priority: PriorityClass
    expected_duration_s: float  # from the key's own history, not its declaration
    group: str  # the gate this job helps unblock; urgency and completion are per group
    owner: Owner
    parent_lease: str | None = None  # BH_VALIDATION_LEASE of the parent, if nested
    key: str | None = None  # attest key name, for history and right-sizing
    cwd: str | None = None
    env: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class JobHandle:
    job_id: str
    backend: str
    token: str  # backend-native id (an in-memory sequence, a slots permit, a runner task id)


@dataclass(frozen=True)
class Grant:
    """The amounts actually granted, exported to the job through :meth:`env`.

    The grant reaches the client through its lease, never through a runner's environment, and it
    never enters ``cmd_hash``. ``root`` is the execution root the run was placed on (``bh-mf1o4``)
    or ``None`` when the engine has no roots: the run then uses today's locations.
    """

    lease_id: str
    job_id: str
    amounts: Capacity
    parent_lease: str | None = None
    granted_at: float = 0.0  # broker clock (monotonic seconds)
    root: str | None = None

    def env(self) -> dict[str, str]:
        """The framework-generic grant contract; runner profiles map it further."""
        out = {
            LEASE_ENV: self.lease_id,
            "BH_GRANT_CPUS": str(max(1, int(self.amounts.cpu))),
            "BH_GRANT_MEMORY_BYTES": str(self.amounts.memory_bytes),
            "BH_GRANT_MEMORY_MB": str(math.ceil(self.amounts.memory_bytes / _MB)),
        }
        for name, amount in self.amounts.pools.items():
            if name.startswith(ROOT_POOL_PREFIX):
                continue  # exported as BH_GRANT_ROOT / BH_GRANT_DISK_BYTES below
            out["BH_GRANT_" + name.upper().replace("-", "_")] = str(amount)
        if self.root is not None:
            out["BH_GRANT_ROOT"] = self.root
            disk = self.amounts.pools.get(ROOT_POOL_PREFIX + self.root, 0)
            out["BH_GRANT_DISK_BYTES"] = str(disk)
        return out


@dataclass(frozen=True)
class Lease:
    """A granted reservation held by a live owner.

    ``parent_lease`` makes it a nested lease: it is carved out of that parent's grant, and it
    ends when the parent does.
    """

    grant: Grant
    owner: Owner

    @property
    def lease_id(self) -> str:
        return self.grant.lease_id

    @property
    def parent_lease(self) -> str | None:
        return self.grant.parent_lease


@dataclass(frozen=True)
class Outcome:
    exit_code: int
    signal: int | None = None


@dataclass(frozen=True)
class Usage:
    wall_s: float
    cpu_s: float | None = None
    peak_memory_bytes: int | None = None


class BrakeLevel(enum.IntEnum):
    GREEN = 0  # admit normally
    AMBER = 1  # critical only, at minimum grants; background paused
    RED = 2  # no admission
    KILL = 3  # red sustained: kill the lowest-priority job, record exit 75 (retryable)


@dataclass(frozen=True)
class BrokerStatus:
    backend: str
    budget: Capacity
    in_use: Capacity
    queued: int
    running: int
    brake: BrakeLevel
    signals: Mapping[str, float] = field(default_factory=dict)


class EventKind(enum.StrEnum):
    SUBMITTED = "submitted"
    ADMITTED = "admitted"
    FINISHED = "finished"
    CANCELLED = "cancelled"
    BRAKED = "braked"
    STALLED = "stalled"
    ORPHAN_RECLAIMED = "orphan_reclaimed"


@dataclass(frozen=True)
class BrokerEvent:
    seq: int  # monotonically increasing per broker instance; resume with since=seq
    kind: EventKind
    at: float
    job_id: str | None = None
    detail: Mapping[str, object] = field(default_factory=dict)
