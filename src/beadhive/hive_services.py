"""Production adapters and composition for :mod:`beadhive.modules.hives`.

The module stays pure.  This outer layer binds its ports to the established registry,
workspace-root, readiness, onboarding, retirement, and plugin-kernel compatibility surfaces.
No service is cached so test and integration patch points remain live for every command.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import MappingProxyType

from . import hive, hive_identity, registry, retire
from . import onboard as onboard_module
from .modules.hives import (
    DiscoverHivesResult,
    HiveDiagnostic,
    HiveIdentity,
    HiveLifecycleService,
    HiveListRequest,
    HiveListResult,
    HiveStatusRequest,
    HiveStatusResult,
    OnboardCheck,
    OnboardHiveRequest,
    OnboardHiveResult,
    ReadinessRequest,
    ReadinessResult,
    RegisterHiveRequest,
    RegisterHiveResult,
    RetireHiveRequest,
    RetireHiveResult,
    RetireScope,
)


def identity_from_legacy_triplet(hive_id: str) -> HiveIdentity:
    """Preserve the legacy CLI parser's diagnostics while returning the typed identity."""

    provider, organization, repository = hive._parse_triplet(hive_id)
    return HiveIdentity(provider, organization, repository)


class LegacyHiveRegistry:
    def __init__(self, config_loader: Callable[[], dict]) -> None:
        self._load_config = config_loader

    def register(self, request: RegisterHiveRequest) -> RegisterHiveResult:
        identity = request.identity
        hive.add(
            identity.canonical_id,
            prefix=request.prefix,
            kind=request.kind,
            upstream=request.upstream,
        )
        entry = registry.find_entry(
            self._load_config(),
            identity.provider,
            identity.organization,
            identity.repository,
        )
        if entry is None:
            raise RuntimeError(f"{identity.canonical_id} was not registered")
        return RegisterHiveResult(
            identity,
            str(entry["prefix"]),
            str(entry.get("kind", "")),
        )

    def discover(self) -> DiscoverHivesResult:
        payload = hive.available(self._load_config())
        return DiscoverHivesResult(
            tuple(payload["candidates"]),
            tuple(payload["registered"]),
        )

    def list(self, request: HiveListRequest) -> HiveListResult:
        discovered = self.discover()
        try:
            page = hive_identity.identity_page(
                self._load_config(), limit=request.limit, cursor=request.cursor
            )
        except hive_identity.HiveIdentityContractError as exc:
            return HiveListResult(
                discovered,
                diagnostics=(HiveDiagnostic(exc.code, exc.detail),),
            )
        except (OSError, ValueError, TypeError):
            page = hive_identity.unavailable_page(limit=request.limit)
        return HiveListResult(discovered, page=page)

    def status(self, request: HiveStatusRequest) -> HiveStatusResult:
        cfg = self._load_config()
        payload = hive.status_payload(cfg)
        if request.hive_id:
            entry = registry.resolve_hive(cfg, request.hive_id)
            key = f"{entry['provider']}/{entry['org']}/{entry['repo']}"
            payload = {
                **payload,
                "hives": [
                    row
                    for row in payload["hives"]
                    if f"{row['provider']}/{row['org']}/{row['repo']}" == key
                ],
            }
        return HiveStatusResult(
            tuple(payload["candidates"]),
            tuple(payload["collisions"]),
            tuple(payload["violations"]),
            tuple(payload["hives"]),
        )


class AcceptedWorkspaceRealizer:
    """Adapter over bh-cgcg's accepted root-resolution choke point."""

    def __init__(self, root: Callable[[], str]) -> None:
        self._root = root

    def target_for(self, identity: HiveIdentity) -> str:
        return str(
            Path(self._root()) / identity.provider / identity.organization / identity.repository
        )


class LegacyDependencyProbe:
    def __init__(self, probe: Callable[[bool, str | None], ReadinessResult]) -> None:
        self._probe = probe

    def readiness(self, request: ReadinessRequest) -> ReadinessResult:
        return self._probe(request.verbose, request.cwd)


OnboardExecutor = Callable[..., onboard_module.OnboardPlan | None]
_ORIGINAL_HIVE_ONBOARD = hive.onboard


def _live_onboard_executor(hive_id: str, **kwargs) -> onboard_module.OnboardPlan | None:
    """Use structured production execution while preserving the established patch seam.

    Older callers and tests patch :func:`beadhive.hive.onboard` at the transport boundary.  The
    unpatched facade is presentation-owning, so production must never call it from the kernel
    adapter.  A live replacement, however, is an injected test/application seam and is invoked
    before the adapter synthesizes its transport-neutral result.
    """

    if hive.onboard is not _ORIGINAL_HIVE_ONBOARD:
        hive.onboard(hive_id, **kwargs)
        return None
    return hive.execute_onboard(hive_id, **kwargs)


class KernelHiveLifecycle:
    """Concrete lifecycle adapter retaining the kernel-backed plugin projections.

    ``hive.onboard`` reaches ``plugins.action_composition`` / ``onboard_participants`` and
    ``retire`` reaches ``plugins.retire_observers``.  Those compatibility projections bind
    manifest-declared ``hive.*`` events to ``LifecycleDispatcher`` with their declared
    blocking/best-effort policies; this adapter deliberately does not rediscover callbacks.
    """

    def __init__(
        self,
        config_loader: Callable[[], dict],
        onboard_executor: OnboardExecutor = hive.execute_onboard,
    ) -> None:
        self._load_config = config_loader
        self._execute_onboard = onboard_executor

    def onboard(self, request: OnboardHiveRequest, *, target: str) -> OnboardHiveResult:
        identity = request.identity
        pre_exists = Path(target).exists()
        _, warnings = registry.derive_prefix(
            identity.provider,
            identity.organization,
            identity.repository,
            request.kind,
            self._load_config(),
        )
        plan = self._execute_onboard(
            identity.canonical_id,
            clone_url=request.clone_url,
            furnish=request.furnish,
            claude=request.claude,
            skills=request.skills,
            observaloop=request.observaloop,
            agents=request.agents,
            opencode=request.opencode,
            codex=request.codex,
            global_grant=request.global_grant,
            plugins=list(request.plugins),
            force=request.force,
            kind=request.kind,
            prefix=request.prefix,
            yes=request.yes,
            dry_run=request.dry_run,
            skip_check=request.skip_check,
            hub_sync=request.hub_sync,
        )
        entry = registry.find_entry(
            self._load_config(),
            identity.provider,
            identity.organization,
            identity.repository,
        )
        return OnboardHiveResult(
            identity,
            target,
            cloned=(plan.cloned if plan is not None else False) or not pre_exists,
            registered=(plan.registered if plan is not None else False) or entry is not None,
            prefix=(plan.prefix if plan is not None else "")
            or (str(entry["prefix"]) if entry else ""),
            synced=(
                plan.hub_synced
                if plan is not None
                else not request.dry_run and request.hub_sync is not False
            ),
            kind=(plan.kind if plan is not None else "") or request.kind,
            successful=plan.successful if plan is not None else True,
            dry_run=plan.dry_run if plan is not None else request.dry_run,
            checks=tuple(
                OnboardCheck(
                    result.id,
                    result.label,
                    result.ok,
                    result.detail,
                    result.overridable,
                    result.skipped,
                )
                for result in (plan.checks if plan is not None else ())
            ),
            steps=tuple(plan.steps_run if plan is not None else ()),
            installers=tuple(plan.installers_run if plan is not None else ()),
            warnings=tuple((*warnings, *(plan.warnings if plan is not None else ()))),
        )

    def retire(self, request: RetireHiveRequest) -> RetireHiveResult:
        operation = (
            retire.execute_retire_hive
            if request.scope is RetireScope.FLEET
            else retire.execute_reclaim_hive
        )
        plan = operation(
            request.hive_id,
            dry_run=request.dry_run,
            backup=request.backup,
            confirm=request.confirm,
            purge=request.purge,
        )
        teardown = plan.teardown
        details = MappingProxyType(
            {
                "verdict": str(plan.verdict),
                "backed_up": plan.backed_up,
                "backup_actions": tuple(plan.backup_actions),
                "worktrees_removed": tuple(teardown.removed) if teardown else (),
                "worktrees_dirty": tuple(teardown.dirty) if teardown else (),
                "worktrees_failed": tuple(teardown.failed) if teardown else (),
            }
        )
        return RetireHiveResult(
            request.hive_id,
            request.scope,
            plan.clone_path,
            plan.dry_run,
            plan.unregistered,
            archived_to=plan.archived_to,
            purged=plan.purged,
            plugins_notified=tuple(plan.plugins_notified),
            successful=plan.successful,
            events=tuple(plan.events),
            details=details,
        )


def hive_lifecycle_service(
    *,
    config_loader: Callable[[], dict] | None = None,
    workspace_root_resolver: Callable[[], str] | None = None,
    readiness_probe: Callable[[bool, str | None], ReadinessResult] | None = None,
) -> HiveLifecycleService:
    def readiness_not_bound(_verbose: bool, _cwd: str | None) -> ReadinessResult:
        raise RuntimeError("readiness adapter is not bound for this composition")

    # ``hive`` is the retained legacy adapter, so its live config-facade reference is the
    # compatibility default.  Keeping the loader explicit on each concrete adapter prevents
    # this new composition root from becoming another broad config consumer.
    load_config = config_loader or hive.config.load
    return HiveLifecycleService(
        LegacyHiveRegistry(load_config),
        AcceptedWorkspaceRealizer(workspace_root_resolver or hive.workspace_root),
        LegacyDependencyProbe(readiness_probe or readiness_not_bound),
        KernelHiveLifecycle(load_config, _live_onboard_executor),
    )


def readiness_result(
    request: ReadinessRequest,
    *,
    probe: Callable[[bool, str | None], ReadinessResult],
) -> ReadinessResult:
    """Compatibility entry that avoids exposing nullable plugin callback vocabulary."""

    return hive_lifecycle_service(readiness_probe=probe).readiness(request)
