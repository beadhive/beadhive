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
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, TypeVar, cast

import typer

from .kernel.lifecycle import (
    EVENTS_BY_ID,
    DeliveryReport,
    DeliveryStatus,
    HiveLifecycleContext,
    HostLifecycleContext,
    LifecycleDispatcher,
    LifecycleEvent,
    SubscriberBinding,
    WorktreeLifecycleContext,
)
from .kernel.plugins import (
    DiscoveryResult,
    LifecycleSubscriptionDeclaration,
    PluginKernelConfig,
    builtin_manifest_source,
    discover_plugins,
    parse_kernel_config,
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
class _CompatibilityComposition:
    """One validated manifest/enablement snapshot for all legacy projections."""

    result: DiscoveryResult
    declarations: tuple[Plugin, ...]

    @property
    def selected_plugin_ids(self) -> frozenset[str]:
        return frozenset(selection.plugin_id for selection in self.result.capabilities)

    def declaration(self, plugin_id: str) -> Plugin | None:
        return next(
            (plugin for plugin in self.declarations if plugin.name == plugin_id),
            None,
        )

    def subscription(
        self,
        plugin_id: str,
        event_id: str,
    ) -> LifecycleSubscriptionDeclaration:
        discovered = next(
            plugin for plugin in self.result.plugins if plugin.manifest.plugin_id == plugin_id
        )
        matches = tuple(
            subscription
            for subscription in discovered.manifest.lifecycle_subscriptions
            if subscription.event == event_id
        )
        if len(matches) != 1:
            raise ValueError(
                f"plugin {plugin_id!r} must declare exactly one {event_id!r} subscription"
            )
        return matches[0]


def _kernel_policy(cfg: Any) -> PluginKernelConfig:
    if isinstance(cfg, PluginKernelConfig):
        return parse_kernel_config(cfg)
    if isinstance(cfg, Mapping):
        value = cfg.get("plugin_kernel", {})
        return parse_kernel_config(cast(Mapping[str, object], value))
    return parse_kernel_config(None)


def _compose(
    cfg: Any,
    entry: Any,
    *,
    force_enabled: frozenset[str] = frozenset(),
    honor_legacy_enablement: bool = True,
    host_executables: Mapping[str, str] | None = None,
) -> _CompatibilityComposition:
    """Validate policy first, then build one immutable built-in composition snapshot."""

    try:
        policy = _kernel_policy(cfg)
    except (TypeError, ValueError):
        raw = cfg.get("plugin_kernel", {}) if isinstance(cfg, Mapping) else cfg
        return _CompatibilityComposition(
            discover_plugins(
                [],
                config=raw,
                beadhive_version="0.15.1",
                kernel_version="1.0.0",
                host_executables=host_executables,
            ),
            (),
        )

    declarations = tuple(registry())
    enabled = dict(policy.enabled)
    for plugin in declarations:
        legacy_enabled = (
            True
            if not honor_legacy_enablement or plugin.name in force_enabled
            else bool(plugin.enabled(cfg, entry))
        )
        enabled[plugin.name] = legacy_enabled and policy.enabled.get(plugin.name, True)
    merged_policy = PluginKernelConfig(
        enabled=MappingProxyType(enabled),
        capability_owners=policy.capability_owners,
        allow_external_entry_points=policy.allow_external_entry_points,
    )
    return _CompatibilityComposition(
        discover_plugins(
            [builtin_manifest_source()],
            config=merged_policy,
            beadhive_version="0.15.1",
            kernel_version="1.0.0",
            host_executables=host_executables,
        ),
        declarations,
    )


def action_composition(
    cfg: Any,
    entry: Any,
    *,
    force_enabled: frozenset[str] = frozenset(),
) -> _CompatibilityComposition:
    """Capture one immutable compatibility composition for a complete host action."""

    return _compose(cfg, entry, force_enabled=force_enabled)


def discover_builtin_registry(
    cfg: Any,
    entry: Any,
    *,
    host_executables: dict[str, str] | None = None,
) -> DiscoveryResult:
    """Translate legacy enablement into the kernel's deterministic manifest composition."""

    return _compose(cfg, entry, host_executables=host_executables).result


@dataclass(frozen=True)
class CliMount:
    """Transport-only projection; it is never part of ``PluginManifest``."""

    plugin_id: str
    app: typer.Typer


def cli_mounts(cfg: Any = None, entry: Any = None) -> tuple[CliMount, ...]:
    composition = _compose(cfg, entry, honor_legacy_enablement=False)
    return tuple(
        CliMount(plugin.name, plugin.cli)
        for plugin in composition.declarations
        if plugin.name in composition.selected_plugin_ids
    )


ContextT = TypeVar("ContextT")


def _manifest_binding(
    composition: _CompatibilityComposition,
    plugin_id: str,
    event_id: str,
    subscriber: Callable[[ContextT], Any],
) -> SubscriberBinding[ContextT]:
    declaration = composition.subscription(plugin_id, event_id)
    event = cast(LifecycleEvent[ContextT], EVENTS_BY_ID[declaration.event])
    return SubscriberBinding(
        plugin_id,
        declaration.subscription_id,
        event,
        subscriber,
        declaration.policy,
    )


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
    _callback: Callable[[Any], None]
    _selected: bool | None = None
    _declaration: LifecycleSubscriptionDeclaration | None = None

    def resolve(self, cfg: Any, entry: Any, *, forced: bool = False) -> OnboardParticipant:
        composition = _compose(
            cfg,
            entry,
            force_enabled=frozenset({self.plugin_id}) if forced else frozenset(),
        )
        selected = self.plugin_id in composition.selected_plugin_ids
        return OnboardParticipant(
            self.plugin_id,
            self.consent_only,
            self._callback,
            selected,
            composition.subscription(self.plugin_id, "hive.onboarding") if selected else None,
        )

    def enabled(self, *, forced: bool = False) -> bool:
        if self._selected is None:
            raise RuntimeError("onboard participant must be resolved for one action")
        return self._selected and (forced or not self.consent_only)

    def deliver(self, ctx: Any) -> DeliveryReport:
        event_id = "hive.onboarding"
        if not self._selected:
            return DeliveryReport(event_id, ())
        assert self._declaration is not None

        async def subscriber(_context: HiveLifecycleContext) -> None:
            self._callback(ctx)

        event = cast(LifecycleEvent[HiveLifecycleContext], EVENTS_BY_ID[self._declaration.event])
        binding = SubscriberBinding(
            self.plugin_id,
            self._declaration.subscription_id,
            event,
            subscriber,
            self._declaration.policy,
        )
        context = HiveLifecycleContext(
            hive_id=str(getattr(ctx, "hive", self.plugin_id)),
            correlation_id=f"onboard:{self.plugin_id}",
        )
        return _dispatch(binding, context)


def onboard_participants(
    composition: _CompatibilityComposition | None = None,
) -> tuple[OnboardParticipant, ...]:
    resolved = composition is not None
    composition = composition or _compose(None, None, honor_legacy_enablement=False)
    return tuple(
        OnboardParticipant(
            plugin.name,
            plugin.onboard_requires_opt_in,
            plugin.on_onboard,
            plugin.name in composition.selected_plugin_ids if resolved else None,
            (
                composition.subscription(plugin.name, "hive.onboarding")
                if resolved and plugin.name in composition.selected_plugin_ids
                else None
            ),
        )
        for plugin in composition.declarations
        if plugin.on_onboard is not None
    )


@dataclass(frozen=True)
class RetireObserver:
    plugin_id: str
    _declaration: LifecycleSubscriptionDeclaration
    _callback: Callable[[Path | str, Any, Any], None]

    def deliver(self, clone_path: Path | str, cfg: Any, entry: Any) -> DeliveryReport:
        event = cast(LifecycleEvent[HiveLifecycleContext], EVENTS_BY_ID[self._declaration.event])

        async def subscriber(_context: HiveLifecycleContext) -> None:
            self._callback(clone_path, cfg, entry)

        binding = SubscriberBinding(
            self.plugin_id,
            self._declaration.subscription_id,
            event,
            subscriber,
            self._declaration.policy,
        )
        return _dispatch(
            binding,
            HiveLifecycleContext(
                hive_id=str(entry.get("prefix", self.plugin_id)),
                correlation_id=f"retire:{self.plugin_id}",
            ),
        )


def retire_observers(cfg: Any, entry: Any) -> tuple[RetireObserver, ...]:
    composition = _compose(cfg, entry)
    return tuple(
        RetireObserver(
            plugin.name,
            composition.subscription(plugin.name, "hive.retiring"),
            plugin.on_retire,
        )
        for plugin in composition.declarations
        if plugin.name in composition.selected_plugin_ids and plugin.on_retire is not None
    )


@dataclass(frozen=True)
class ReadinessPort:
    plugin_id: str
    _probe: Callable[[Any, Any], tuple[str, str] | None]
    _selected: bool
    _declaration: LifecycleSubscriptionDeclaration | None

    def enabled(self) -> bool:
        return self._selected

    def probe(self, cfg: Any, entry: Any) -> tuple[str, str] | None:
        if not self._selected:
            return None
        assert self._declaration is not None
        result: list[tuple[str, str] | None] = []

        async def subscriber(_context: HostLifecycleContext) -> None:
            result.append(self._probe(cfg, entry))

        event = cast(LifecycleEvent[HostLifecycleContext], EVENTS_BY_ID[self._declaration.event])
        binding = SubscriberBinding(
            self.plugin_id,
            self._declaration.subscription_id,
            event,
            subscriber,
            self._declaration.policy,
        )
        report = _dispatch(
            binding,
            HostLifecycleContext(
                host_id=str(entry.get("prefix", self.plugin_id))
                if isinstance(entry, Mapping)
                else self.plugin_id,
                correlation_id=f"readiness:{self.plugin_id}",
            ),
        )
        if not delivery_succeeded(report):
            error = report.deliveries[-1].attempts[-1].error
            return "off", f"readiness probe failed ({error})"
        return result[0]


def readiness_ports(cfg: Any = None, entry: Any = None) -> tuple[ReadinessPort, ...]:
    composition = _compose(cfg, entry)
    return tuple(
        ReadinessPort(
            plugin.name,
            plugin.readiness,
            plugin.name in composition.selected_plugin_ids,
            (
                composition.subscription(plugin.name, "host.readiness")
                if plugin.name in composition.selected_plugin_ids
                else None
            ),
        )
        for plugin in composition.declarations
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


def worktree_create_ports(
    cfg: Any,
    entry: Any,
    *,
    composition: _CompatibilityComposition | None = None,
) -> tuple[WorktreeCreatePort, ...]:
    composition = composition or _compose(cfg, entry)
    return tuple(
        WorktreeCreatePort(plugin.name, plugin.wt_create)
        for plugin in composition.declarations
        if plugin.name in composition.selected_plugin_ids and plugin.wt_create is not None
    )


def worktree_remove_ports(cfg: Any, entry: Any) -> tuple[WorktreeRemovePort, ...]:
    composition = _compose(cfg, entry)
    return tuple(
        WorktreeRemovePort(plugin.name, plugin.wt_remove)
        for plugin in composition.declarations
        if plugin.name in composition.selected_plugin_ids and plugin.wt_remove is not None
    )


@dataclass(frozen=True)
class WorktreeObserver:
    plugin_id: str
    event: LifecycleEvent[WorktreeLifecycleContext]
    declaration: LifecycleSubscriptionDeclaration
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
            self.plugin_id,
            self.declaration.subscription_id,
            self.event,
            subscriber,
            self.declaration.policy,
        )
        context = WorktreeLifecycleContext(
            hive_id=str(entry.get("prefix", self.plugin_id)),
            worktree_id=request.target.name,
            correlation_id=f"{self.event.id}:{self.plugin_id}",
        )
        return _dispatch(binding, context)


def worktree_observers(
    hook: str,
    cfg: Any,
    entry: Any,
    *,
    composition: _CompatibilityComposition | None = None,
) -> tuple[WorktreeObserver, ...]:
    event_id = {"wt_creating": "worktree.creating", "wt_created": "worktree.created"}[hook]
    event = cast(LifecycleEvent[WorktreeLifecycleContext], EVENTS_BY_ID[event_id])
    composition = composition or _compose(cfg, entry)
    observers: list[WorktreeObserver] = []
    for plugin in composition.declarations:
        callback = getattr(plugin, hook)
        if callback is not None and plugin.name in composition.selected_plugin_ids:
            observers.append(
                WorktreeObserver(
                    plugin.name,
                    event,
                    composition.subscription(plugin.name, event_id),
                    callback,
                )
            )
    return tuple(observers)


def delivery_succeeded(report: DeliveryReport) -> bool:
    return all(delivery.status is DeliveryStatus.SUCCEEDED for delivery in report.deliveries)
