"""Typed, framework-independent lifecycle contracts."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Generic, Protocol, TypeVar

ContextT = TypeVar("ContextT")
_DOTTED_ID = re.compile(r"^[a-z][a-z0-9-]*(?:\.[a-z][a-z0-9-]*)+$")


def _require_text(value: str, field_name: str) -> None:
    if not value.strip():
        raise ValueError(f"{field_name} must be non-empty")


class LifecycleFamily(StrEnum):
    PLUGIN = "plugin"
    HIVE = "hive"
    WORKTREE = "worktree"
    AGENT_LAUNCH = "agent-launch"
    HOST = "host"


class PluginLifecyclePhase(StrEnum):
    DISCOVERED = "discovered"
    VALIDATED = "validated"
    CONFIGURED = "configured"
    STARTED = "started"
    READY = "ready"
    STOPPING = "stopping"
    STOPPED = "stopped"


class HiveLifecyclePhase(StrEnum):
    ONBOARDING = "onboarding"
    ONBOARDED = "onboarded"
    RETIRING = "retiring"
    RETIRED = "retired"


class WorktreeLifecyclePhase(StrEnum):
    PREPARE = "prepare"
    CREATING = "creating"
    CREATED = "created"
    REMOVING = "removing"
    REMOVED = "removed"


class AgentLaunchLifecyclePhase(StrEnum):
    PREPARE = "prepare"
    ADAPTER_COMMIT = "adapter-commit"
    CORE_COMMIT = "core-commit"
    ABORT = "abort"


class HostLifecyclePhase(StrEnum):
    CONFIGURE = "configure"
    STARTUP = "startup"
    READINESS = "readiness"
    DRAIN = "drain"
    SHUTDOWN = "shutdown"
    TELEMETRY_FLUSH = "telemetry-flush"


LifecyclePhase = (
    PluginLifecyclePhase
    | HiveLifecyclePhase
    | WorktreeLifecyclePhase
    | AgentLaunchLifecyclePhase
    | HostLifecyclePhase
)


@dataclass(frozen=True, slots=True)
class PluginLifecycleContext:
    plugin_id: str
    correlation_id: str

    def __post_init__(self) -> None:
        _require_text(self.plugin_id, "plugin_id")
        _require_text(self.correlation_id, "correlation_id")


@dataclass(frozen=True, slots=True)
class HiveLifecycleContext:
    hive_id: str
    correlation_id: str

    def __post_init__(self) -> None:
        _require_text(self.hive_id, "hive_id")
        _require_text(self.correlation_id, "correlation_id")


@dataclass(frozen=True, slots=True)
class WorktreeLifecycleContext:
    hive_id: str
    worktree_id: str
    correlation_id: str

    def __post_init__(self) -> None:
        _require_text(self.hive_id, "hive_id")
        _require_text(self.worktree_id, "worktree_id")
        _require_text(self.correlation_id, "correlation_id")


@dataclass(frozen=True, slots=True)
class AgentLaunchLifecycleContext:
    hive_id: str
    bead_id: str
    launch_id: str
    correlation_id: str

    def __post_init__(self) -> None:
        _require_text(self.hive_id, "hive_id")
        _require_text(self.bead_id, "bead_id")
        _require_text(self.launch_id, "launch_id")
        _require_text(self.correlation_id, "correlation_id")


@dataclass(frozen=True, slots=True)
class HostLifecycleContext:
    host_id: str
    correlation_id: str

    def __post_init__(self) -> None:
        _require_text(self.host_id, "host_id")
        _require_text(self.correlation_id, "correlation_id")


@dataclass(frozen=True, slots=True)
class LifecycleEvent(Generic[ContextT]):
    family: LifecycleFamily
    phase: LifecyclePhase
    order: int
    context_type: type[ContextT]

    @property
    def id(self) -> str:
        return f"{self.family.value}.{self.phase.value}"


def _events(
    family: LifecycleFamily,
    context_type: type[ContextT],
    phases: tuple[LifecyclePhase, ...],
) -> tuple[LifecycleEvent[ContextT], ...]:
    return tuple(
        LifecycleEvent(family=family, phase=phase, order=index * 10, context_type=context_type)
        for index, phase in enumerate(phases, start=1)
    )


PLUGIN_EVENTS = _events(LifecycleFamily.PLUGIN, PluginLifecycleContext, tuple(PluginLifecyclePhase))
HIVE_EVENTS = _events(LifecycleFamily.HIVE, HiveLifecycleContext, tuple(HiveLifecyclePhase))
WORKTREE_EVENTS = _events(
    LifecycleFamily.WORKTREE, WorktreeLifecycleContext, tuple(WorktreeLifecyclePhase)
)
AGENT_LAUNCH_EVENTS = _events(
    LifecycleFamily.AGENT_LAUNCH,
    AgentLaunchLifecycleContext,
    tuple(AgentLaunchLifecyclePhase),
)
HOST_EVENTS = _events(LifecycleFamily.HOST, HostLifecycleContext, tuple(HostLifecyclePhase))
ALL_LIFECYCLE_EVENTS = (
    *PLUGIN_EVENTS,
    *HIVE_EVENTS,
    *WORKTREE_EVENTS,
    *AGENT_LAUNCH_EVENTS,
    *HOST_EVENTS,
)
EVENTS_BY_ID = MappingProxyType({event.id: event for event in ALL_LIFECYCLE_EVENTS})


class Criticality(StrEnum):
    BEST_EFFORT = "best-effort"
    BLOCKING = "blocking"


class Idempotency(StrEnum):
    NOT_APPLICABLE = "not-applicable"
    REQUIRED = "required"


class CompensationMode(StrEnum):
    NONE = "none"
    REQUIRED = "required"


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 1
    backoff_seconds: float = 0.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least one")
        if self.backoff_seconds < 0 or not math.isfinite(self.backoff_seconds):
            raise ValueError("backoff_seconds must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class CompensationPolicy:
    mode: CompensationMode = CompensationMode.NONE
    action_id: str | None = None

    def __post_init__(self) -> None:
        if self.mode is CompensationMode.REQUIRED:
            if self.action_id is None or not _DOTTED_ID.fullmatch(self.action_id):
                raise ValueError("required compensation needs a dotted action_id")
        elif self.action_id is not None:
            raise ValueError("none compensation cannot declare action_id")


@dataclass(frozen=True, slots=True)
class DeliveryPolicy:
    order: int = 0
    timeout_seconds: float = 30.0
    criticality: Criticality = Criticality.BEST_EFFORT
    idempotency: Idempotency = Idempotency.NOT_APPLICABLE
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    compensation: CompensationPolicy = field(default_factory=CompensationPolicy)

    def __post_init__(self) -> None:
        if type(self.order) is not int:
            raise TypeError("order must be an integer")
        if self.timeout_seconds <= 0 or not math.isfinite(self.timeout_seconds):
            raise ValueError("timeout_seconds must be finite and greater than zero")
        if self.retry.max_attempts > 1 and self.idempotency is not Idempotency.REQUIRED:
            raise ValueError("retries require an idempotent subscriber")


class LifecycleSubscriber(Protocol[ContextT]):
    async def __call__(self, context: ContextT) -> None: ...


class LifecycleCompensator(Protocol[ContextT]):
    async def __call__(self, context: ContextT) -> None: ...


@dataclass(frozen=True, slots=True)
class SubscriberBinding(Generic[ContextT]):
    plugin_id: str
    subscription_id: str
    event: LifecycleEvent[ContextT]
    subscriber: LifecycleSubscriber[ContextT]
    policy: DeliveryPolicy = field(default_factory=DeliveryPolicy)
    compensator: LifecycleCompensator[ContextT] | None = None

    def __post_init__(self) -> None:
        _require_text(self.plugin_id, "plugin_id")
        if not _DOTTED_ID.fullmatch(self.subscription_id):
            raise ValueError("subscription_id must be a dotted identifier")
        if not self.subscription_id.startswith(f"{self.plugin_id}."):
            raise ValueError("subscription_id must be qualified by plugin_id")
        needs_compensation = self.policy.compensation.mode is CompensationMode.REQUIRED
        if needs_compensation != (self.compensator is not None):
            raise ValueError("compensator binding must match compensation policy")
