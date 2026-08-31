"""Compatibility facade between legacy integrations and the typed plugin kernel.

``Plugin`` and ``registry()`` remain import-compatible while callers migrate.  Optional
callbacks are inspected only here and projected as non-optional lifecycle participants or
capability ports.  Core callers therefore depend on kernel-owned event contracts and named
ports instead of reaching into a nullable callback bag.

The imports inside :func:`registry` are the one temporary cycle-breaking exception.  Owner:
``bh-qw9oi.6``'s compatibility ledger.  Removal trigger: every built-in constructs its runtime
adapter from the manifest/bootstrap composition layer rather than importing this facade.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar, cast

import typer

from .kernel.lifecycle import (
    EVENTS_BY_ID,
    Criticality,
    DeliveryPolicy,
    DeliveryReport,
    DeliveryStatus,
    HiveLifecycleContext,
    LifecycleDispatcher,
    LifecycleEvent,
    SubscriberBinding,
    WorktreeLifecycleContext,
)


@dataclass(frozen=True)
class Plugin:
    """Deprecated source-compatible plugin declaration.

    New callers must use the typed projections below.  Keeping this exact constructor surface
    lets downstream integrations migrate independently while the facade owns translation.
    """

    name: str
    cli: typer.Typer
    enabled: Callable[[Any, Any], bool]
    on_onboard: Callable[[Any], None] | None = None
    onboard_requires_opt_in: bool = False
    on_retire: Callable[[Path | str, Any, Any], None] | None = None
    readiness: Callable[[Any, Any], tuple[str, str] | None] | None = None
    wt_create: Callable[..., Path | None] | None = None
    wt_remove: Callable[..., bool] | None = None
    wt_creating: Callable[..., None] | None = None
    wt_created: Callable[..., None] | None = None


def registry() -> list[Plugin]:
    """Return optional built-ins in stable compatibility order.

    ``git-workspace`` is deliberately absent: it remains an unconditional dependency and its
    plugin-shaped CLI is mounted explicitly by the transport layer.
    """

    from . import (  # compatibility-facade exception; see module removal trigger
        herdr_plugin,
        hitch_plugin,
        observaloop,
        orca,
        repowise_plugin,
    )

    return [
        orca.PLUGIN,
        observaloop.PLUGIN,
        hitch_plugin.PLUGIN,
        herdr_plugin.PLUGIN,
        repowise_plugin.PLUGIN,
    ]


@dataclass(frozen=True)
class CliMount:
    """Transport-only projection; it is never part of ``PluginManifest``."""

    plugin_id: str
    app: typer.Typer


def cli_mounts() -> tuple[CliMount, ...]:
    return tuple(CliMount(plugin.name, plugin.cli) for plugin in registry())


ContextT = TypeVar("ContextT")


def _dispatch(binding: SubscriberBinding[ContextT], context: ContextT) -> DeliveryReport:
    """Deliver one compatibility subscriber through the kernel dispatcher."""

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(LifecycleDispatcher((binding,)).dispatch(binding.event, context))
    raise RuntimeError("legacy synchronous plugin facade cannot run inside an active event loop")


@dataclass(frozen=True)
class OnboardParticipant:
    plugin_id: str
    consent_only: bool
    _enabled: Callable[[Any, Any], bool]
    _callback: Callable[[Any], None]

    def enabled(self, cfg: Any, entry: Any) -> bool:
        return bool(self._enabled(cfg, entry))

    def deliver(self, ctx: Any) -> DeliveryReport:
        event = cast(LifecycleEvent[HiveLifecycleContext], EVENTS_BY_ID["hive.onboarding"])

        async def subscriber(_context: HiveLifecycleContext) -> None:
            self._callback(ctx)

        binding = SubscriberBinding(
            plugin_id=self.plugin_id,
            subscription_id=f"{self.plugin_id}.legacy-onboard",
            event=event,
            subscriber=subscriber,
            policy=DeliveryPolicy(criticality=Criticality.BEST_EFFORT),
        )
        context = HiveLifecycleContext(
            hive_id=str(getattr(ctx, "hive", self.plugin_id)),
            correlation_id=f"onboard:{self.plugin_id}",
        )
        return _dispatch(binding, context)


def onboard_participants() -> tuple[OnboardParticipant, ...]:
    return tuple(
        OnboardParticipant(
            plugin.name,
            plugin.onboard_requires_opt_in,
            plugin.enabled,
            plugin.on_onboard,
        )
        for plugin in registry()
        if plugin.on_onboard is not None
    )


@dataclass(frozen=True)
class RetireObserver:
    plugin_id: str
    _callback: Callable[[Path | str, Any, Any], None]

    def deliver(self, clone_path: Path | str, cfg: Any, entry: Any) -> DeliveryReport:
        event = cast(LifecycleEvent[HiveLifecycleContext], EVENTS_BY_ID["hive.retiring"])

        async def subscriber(_context: HiveLifecycleContext) -> None:
            self._callback(clone_path, cfg, entry)

        binding = SubscriberBinding(
            plugin_id=self.plugin_id,
            subscription_id=f"{self.plugin_id}.legacy-retire",
            event=event,
            subscriber=subscriber,
            policy=DeliveryPolicy(criticality=Criticality.BEST_EFFORT),
        )
        return _dispatch(
            binding,
            HiveLifecycleContext(
                hive_id=str(entry.get("prefix", self.plugin_id)),
                correlation_id=f"retire:{self.plugin_id}",
            ),
        )


def retire_observers(cfg: Any, entry: Any) -> tuple[RetireObserver, ...]:
    return tuple(
        RetireObserver(plugin.name, plugin.on_retire)
        for plugin in registry()
        if plugin.on_retire is not None and plugin.enabled(cfg, entry)
    )


@dataclass(frozen=True)
class ReadinessPort:
    plugin_id: str
    _enabled: Callable[[Any, Any], bool]
    _probe: Callable[[Any, Any], tuple[str, str] | None]

    def enabled(self, cfg: Any, entry: Any) -> bool:
        return bool(self._enabled(cfg, entry))

    def probe(self, cfg: Any, entry: Any) -> tuple[str, str] | None:
        return self._probe(cfg, entry)


def readiness_ports() -> tuple[ReadinessPort, ...]:
    return tuple(
        ReadinessPort(plugin.name, plugin.enabled, plugin.readiness)
        for plugin in registry()
        if plugin.readiness is not None
    )


@dataclass(frozen=True)
class WorktreeCreateRequest:
    main: Path
    branch: str
    target: Path
    start_point: str


@dataclass(frozen=True)
class WorktreeRemoveRequest:
    main: Path
    target: Path
    force: bool
    keep_branch: bool


@dataclass(frozen=True)
class WorktreeCreatePort:
    plugin_id: str
    _create: Callable[..., Path | None]

    def create(self, cfg: Any, entry: Any, request: WorktreeCreateRequest) -> Path | None:
        return self._create(
            cfg,
            entry,
            main=request.main,
            branch=request.branch,
            target=request.target,
            start_point=request.start_point,
        )


@dataclass(frozen=True)
class WorktreeRemovePort:
    plugin_id: str
    _remove: Callable[..., bool]

    def remove(self, cfg: Any, entry: Any, request: WorktreeRemoveRequest) -> bool:
        return bool(
            self._remove(
                cfg,
                entry,
                main=request.main,
                target=request.target,
                force=request.force,
                keep_branch=request.keep_branch,
            )
        )


def worktree_create_ports(cfg: Any, entry: Any) -> tuple[WorktreeCreatePort, ...]:
    return tuple(
        WorktreeCreatePort(plugin.name, plugin.wt_create)
        for plugin in registry()
        if plugin.wt_create is not None and plugin.enabled(cfg, entry)
    )


def worktree_remove_ports(cfg: Any, entry: Any) -> tuple[WorktreeRemovePort, ...]:
    return tuple(
        WorktreeRemovePort(plugin.name, plugin.wt_remove)
        for plugin in registry()
        if plugin.wt_remove is not None and plugin.enabled(cfg, entry)
    )


@dataclass(frozen=True)
class WorktreeObserver:
    plugin_id: str
    event: LifecycleEvent[WorktreeLifecycleContext]
    _callback: Callable[..., None]

    def deliver(
        self,
        cfg: Any,
        entry: Any,
        request: WorktreeCreateRequest,
    ) -> DeliveryReport:
        async def subscriber(_context: WorktreeLifecycleContext) -> None:
            kwargs: dict[str, Any] = {
                "main": request.main,
                "branch": request.branch,
                "target": request.target,
            }
            if self.event.id == "worktree.creating":
                kwargs["start_point"] = request.start_point
            self._callback(cfg, entry, **kwargs)

        binding = SubscriberBinding(
            plugin_id=self.plugin_id,
            subscription_id=f"{self.plugin_id}.legacy-{self.event.phase.value}",
            event=self.event,
            subscriber=subscriber,
            policy=DeliveryPolicy(criticality=Criticality.BEST_EFFORT),
        )
        context = WorktreeLifecycleContext(
            hive_id=str(entry.get("prefix", self.plugin_id)),
            worktree_id=request.target.name,
            correlation_id=f"{self.event.id}:{self.plugin_id}",
        )
        return _dispatch(binding, context)


def worktree_observers(hook: str, cfg: Any, entry: Any) -> tuple[WorktreeObserver, ...]:
    event_id = {"wt_creating": "worktree.creating", "wt_created": "worktree.created"}[hook]
    event = cast(LifecycleEvent[WorktreeLifecycleContext], EVENTS_BY_ID[event_id])
    observers: list[WorktreeObserver] = []
    for plugin in registry():
        callback = getattr(plugin, hook)
        if callback is not None and plugin.enabled(cfg, entry):
            observers.append(WorktreeObserver(plugin.name, event, callback))
    return tuple(observers)


def delivery_succeeded(report: DeliveryReport) -> bool:
    return all(delivery.status is DeliveryStatus.SUCCEEDED for delivery in report.deliveries)
