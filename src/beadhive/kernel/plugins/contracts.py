"""Forwarding facade — moved to the ``beadhive-plugins`` library package (bh-xh8ku.2).

The plugin manifest schema, capability-slot identity, and discovery-diagnostic contracts now
live in :mod:`beadhive_plugins.contracts`, a stdlib-only package with no dependency on
``beadhive``. This module re-exports the identical objects at the old import path so existing
consumers (including ``beadhive-pants`` and the other external plugin packages) keep working
unchanged; migrating them onto the new package directly is not required by this move.
"""

from __future__ import annotations

from beadhive_plugins.contracts import (
    BUILD_IMPACT,
    BUILD_VERIFY,
    BuildVerifier,
    CapabilityBindingError,
    CapabilityKey,
    CapabilityRef,
    CapabilitySelection,
    CliProjection,
    CredentialRequirement,
    CredentialSource,
    DiagnosticCode,
    DiagnosticSeverity,
    DiscoveredPlugin,
    DiscoveryResult,
    ExecutableRequirement,
    ExternalEntryPoint,
    LifecycleSubscriptionDeclaration,
    ManifestDocument,
    ManifestLoader,
    ManifestProvenance,
    ManifestSource,
    PermissionRequirement,
    PluginDiagnostic,
    PluginKernelConfig,
    PluginManifest,
    PortT,
    ProviderBinding,
    ProviderKey,
    SourceRead,
    Version,
    VersionRange,
)

__all__ = [
    "BUILD_IMPACT",
    "BUILD_VERIFY",
    "BuildVerifier",
    "CapabilityBindingError",
    "CapabilityKey",
    "CapabilityRef",
    "CapabilitySelection",
    "CliProjection",
    "CredentialRequirement",
    "CredentialSource",
    "DiagnosticCode",
    "DiagnosticSeverity",
    "DiscoveredPlugin",
    "DiscoveryResult",
    "ExecutableRequirement",
    "ExternalEntryPoint",
    "LifecycleSubscriptionDeclaration",
    "ManifestDocument",
    "ManifestLoader",
    "ManifestProvenance",
    "ManifestSource",
    "PermissionRequirement",
    "PluginDiagnostic",
    "PluginKernelConfig",
    "PluginManifest",
    "PortT",
    "ProviderBinding",
    "ProviderKey",
    "SourceRead",
    "Version",
    "VersionRange",
]
