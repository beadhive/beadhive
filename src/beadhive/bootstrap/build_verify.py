"""Composition root for plugin-provided build-system verification."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from importlib import import_module

from ..kernel.plugins import (
    BUILD_VERIFY,
    BuildVerifier,
    ManifestSource,
    PluginDiagnostic,
    PluginKernelConfig,
    builtin_manifest_source,
    discover_plugins,
)
from ..modules.config.contracts import AttestConfig

_BEADHIVE_VERSION = "0.15.1"
_KERNEL_VERSION = "1.0.0"


@dataclass(frozen=True)
class BuildVerifierProvider:
    plugin_id: str
    loader: Callable[[Mapping[str, str]], BuildVerifier] = field(repr=False, compare=False)


def _pants_verifier(selectors: Mapping[str, str]) -> BuildVerifier:
    verifier_type = import_module("beadhive_pants.verify").PantsBuildVerifier
    return verifier_type(selectors)


BUILTIN_BUILD_VERIFIER_PROVIDERS = (BuildVerifierProvider("pants", _pants_verifier),)


def collect_build_verify_diagnostics(
    repo: str,
    attest: AttestConfig,
    *,
    plugin_kernel: Mapping[str, object] | PluginKernelConfig | None = None,
    sources: Sequence[ManifestSource] | None = None,
    providers: Sequence[BuildVerifierProvider] = BUILTIN_BUILD_VERIFIER_PROVIDERS,
) -> tuple[PluginDiagnostic, ...]:
    """Discover the selected verifier and return its stable diagnostics."""
    discovery = discover_plugins(
        tuple(sources) if sources is not None else (builtin_manifest_source(),),
        config=plugin_kernel,
        beadhive_version=_BEADHIVE_VERSION,
        kernel_version=_KERNEL_VERSION,
    )
    owner = discovery.owner_of(BUILD_VERIFY)
    provider = next((item for item in providers if item.plugin_id == owner), None)
    if provider is None:
        return discovery.errors
    selectors = {
        key.name: selector
        for key in attest.keys
        if key.enabled and (selector := key.selectors.get(provider.plugin_id))
    }
    try:
        verifier = provider.loader(selectors)
        return (*discovery.errors, *verifier.verify(repo))
    except (OSError, RuntimeError, ValueError) as exc:
        from ..kernel.plugins import DiagnosticCode, DiagnosticSeverity

        return (
            *discovery.errors,
            PluginDiagnostic(
                DiagnosticCode.BUILD_ATTEST_TAGS,
                DiagnosticSeverity.ERROR,
                f"FAILED: verifier unavailable: {exc}",
                plugin_id=provider.plugin_id,
                capability=BUILD_VERIFY,
            ),
        )


__all__ = [
    "BUILTIN_BUILD_VERIFIER_PROVIDERS",
    "BuildVerifierProvider",
    "collect_build_verify_diagnostics",
]
