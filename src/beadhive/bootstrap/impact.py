"""Composition root for impact resolution (Attested Green ADR, Amendment 1).

Turns a resolved hive `work.attest` config into domain :class:`AttestKey` values, and binds the
configured backend into an :class:`ImpactResolver` for explicit injection.

Backends come from plugins providing the ``build.impact`` capability (bh-3fcl0.2). Plugin-kernel
discovery reads manifests only and selects at most one enabled provider; a conflict fails closed
with the kernel's ``duplicate-capability`` diagnostic and binds nothing. Only the selected
provider's runtime binding is loaded, and only here. The resulting ``backends`` mapping is handed
to :func:`select_resolver`, which picks one by the configured name, so application code never
looks a backend up. With no config the result is ``native-full``: every key invalidated on every
change, exactly today's gate.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from importlib import import_module
from types import MappingProxyType
from typing import cast

from ..adapters.impact_git import GitTreeDiff
from ..kernel.plugins import (
    BUILD_IMPACT,
    CapabilityKey,
    DiagnosticCode,
    DiagnosticSeverity,
    ManifestSource,
    PluginDiagnostic,
    PluginKernelConfig,
    builtin_manifest_source,
    discover_plugins,
)
from ..modules.config.contracts import AttestConfig
from ..modules.work.application.impact import select_resolver
from ..modules.work.contracts.impact import ImpactBackend, ImpactResolver, TreeDiffPort
from ..modules.work.domain.impact import NATIVE_FULL, AttestKey

#: The typed request bootstrap makes for ``build.impact``: the capability plus its port.
BUILD_IMPACT_PORT = CapabilityKey(BUILD_IMPACT, ImpactBackend)

# Host and kernel versions checked against manifest compatibility ranges; the same values the
# legacy plugin facade (``beadhive.plugins``) composes built-in manifests with.
_BEADHIVE_VERSION = "0.15.1"
_KERNEL_VERSION = "1.0.0"

# Constructing a backend reads its build configuration (e.g. ``pants.toml``). These failures
# mean "not available on this checkout" and degrade to ``native-full`` with a fallback reason.
_UNAVAILABLE = (OSError, KeyError, ValueError)

# ``beadhive-pants`` (and any future ``build.impact`` provider package) is an optional extra
# (bh-mxjoy) — a selected manifest can outlive the package it names when the extra was never
# installed. Caught separately from ``_UNAVAILABLE`` so the diagnostic can name the fix.
_MISSING_PACKAGE = (ModuleNotFoundError,)


@dataclass(frozen=True)
class ImpactBackendProvider:
    """A first-party ``build.impact`` runtime binding, named by the provider's ``plugin_id``.

    ``load`` builds the backend for one repository. It is called only after discovery has
    selected this plugin, so an unselected provider's implementation is never imported.
    """

    plugin_id: str
    external_executable: str
    module: str
    object_name: str
    loader: Callable[[str], object] = field(repr=False, compare=False)

    def load(self, repo: str) -> object:
        return self.loader(repo)


def _pants_backend(repo: str) -> object:
    backend_type = import_module("beadhive_pants.impact").PantsImpactBackend
    return backend_type(repo)


#: Built-in runtime bindings for manifests that provide ``build.impact``.
BUILTIN_IMPACT_PROVIDERS: tuple[ImpactBackendProvider, ...] = (
    ImpactBackendProvider(
        "pants", "pants", "beadhive_pants.impact", "PantsImpactBackend", _pants_backend
    ),
)


@dataclass(frozen=True)
class ImpactBackends:
    """The ``backends`` mapping for :func:`select_resolver` plus the discovery errors behind it.

    ``backends`` holds at most the one selected provider, keyed by the backend's own ``name``
    (what ``work.attest.impact.backend`` configures). ``diagnostics`` carries discovery errors,
    such as a ``build.impact`` ownership conflict, for readiness surfaces to render.
    """

    backends: Mapping[str, ImpactBackend] = field(default_factory=lambda: MappingProxyType({}))
    diagnostics: tuple[PluginDiagnostic, ...] = ()


def attest_keys(attest: AttestConfig) -> tuple[AttestKey, ...]:
    """The configured key catalog as domain values, in declared order."""
    return tuple(
        AttestKey(
            name=k.name,
            cmd=k.cmd,
            policy=k.policy,
            selectors=dict(k.selectors),
            enabled=k.enabled,
            disabled_reason=k.disabled_reason or "",
            disabled_until=k.disabled_until,
        )
        for k in attest.keys
    )


def collect_impact_backends(
    repo: str,
    *,
    plugin_kernel: Mapping[str, object] | PluginKernelConfig | None = None,
    sources: Sequence[ManifestSource] | None = None,
    providers: Sequence[ImpactBackendProvider] = BUILTIN_IMPACT_PROVIDERS,
) -> ImpactBackends:
    """Build the ``build.impact`` backends for ``repo`` from enabled plugins.

    ``plugin_kernel`` is the core-owned ``plugin_kernel`` policy (enablement and
    ``capability_owners``). Discovery fails closed: with two enabled providers and no owner
    selection, no provider is selected and the conflict is returned as a diagnostic. A selected
    plugin without a runtime binding, or whose backend cannot be built for this checkout, yields
    no backend; :func:`select_resolver` then reports the configured name as not available.
    """
    discovery = discover_plugins(
        tuple(sources) if sources is not None else (builtin_manifest_source(),),
        config=plugin_kernel,
        beadhive_version=_BEADHIVE_VERSION,
        kernel_version=_KERNEL_VERSION,
    )
    owner = discovery.owner_of(BUILD_IMPACT_PORT.capability)
    provider = next((item for item in providers if item.plugin_id == owner), None)
    if provider is None:
        return ImpactBackends(diagnostics=discovery.errors)
    try:
        backend = provider.load(repo)
    except _MISSING_PACKAGE as exc:
        hint = (
            f"{provider.plugin_id}: build.impact plugin package is not installed ({exc}); "
            f"install 'beadhive[{provider.plugin_id}]' to enable it"
        )
        diagnostic = PluginDiagnostic(
            DiagnosticCode.BUILD_ATTEST_TAGS,
            DiagnosticSeverity.WARNING,
            hint,
            plugin_id=provider.plugin_id,
            capability=BUILD_IMPACT,
        )
        return ImpactBackends(diagnostics=(*discovery.errors, diagnostic))
    except _UNAVAILABLE:
        return ImpactBackends(diagnostics=discovery.errors)
    # Key by the backend's own name; select_resolver re-checks the port and the name before
    # binding, so a non-conforming provider degrades with its own fallback reason.
    name = getattr(backend, "name", None)
    key = name if isinstance(name, str) and name else provider.plugin_id
    return ImpactBackends(MappingProxyType({key: cast(ImpactBackend, backend)}), discovery.errors)


def impact_resolver(
    attest: AttestConfig | None = None,
    *,
    backends: Mapping[str, ImpactBackend] | None = None,
    tree_diff: TreeDiffPort | None = None,
    repo: str | None = None,
    plugin_kernel: Mapping[str, object] | PluginKernelConfig | None = None,
) -> ImpactResolver:
    """Bind a resolver from an already-resolved config, defaulting to ``native-full``.

    Explicit ``backends`` are used as given. Otherwise, when ``repo`` is supplied and a backend
    other than ``native-full`` is configured, the backends are collected from enabled
    ``build.impact`` plugins under the ``plugin_kernel`` policy.

    Config loading and layering stay outside this composition root so the bootstrap boundary
    does not depend back on the legacy configuration facade.
    """
    attest = attest if attest is not None else AttestConfig()
    if backends is None and repo is not None and attest.impact.backend != NATIVE_FULL:
        backends = collect_impact_backends(repo, plugin_kernel=plugin_kernel).backends
    return select_resolver(
        attest.impact.backend,
        tree_diff=tree_diff if tree_diff is not None else GitTreeDiff(),
        backends=backends,
        timeout_seconds=attest.impact.timeout_seconds,
    )


__all__ = [
    "BUILD_IMPACT_PORT",
    "BUILTIN_IMPACT_PROVIDERS",
    "ImpactBackendProvider",
    "ImpactBackends",
    "attest_keys",
    "collect_impact_backends",
    "impact_resolver",
]
