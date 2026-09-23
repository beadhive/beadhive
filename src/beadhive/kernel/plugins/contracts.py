"""Public contracts for declarative plugin discovery and capability binding.

This module is intentionally stdlib-only.  Importing it describes metadata and ports; it does
not discover plugins, read configuration, inspect the host, or import an integration.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Generic, Protocol, TypeVar, runtime_checkable

from ..lifecycle.contracts import DeliveryPolicy


class DiagnosticSeverity(StrEnum):
    """Stable severity values rendered by discovery diagnostics."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class DiagnosticCode(StrEnum):
    """Stable machine-readable discovery failure identities."""

    INVALID_MANIFEST = "invalid-manifest"
    INVALID_CONFIG = "invalid-config"
    INCOMPATIBLE_BEADHIVE = "incompatible-beadhive"
    INCOMPATIBLE_KERNEL = "incompatible-plugin-kernel"
    DUPLICATE_PLUGIN_ID = "duplicate-plugin-id"
    DUPLICATE_CAPABILITY = "duplicate-capability"
    DISABLED_PLUGIN = "disabled-plugin"
    MISSING_EXECUTABLE = "missing-executable"
    INCOMPATIBLE_EXECUTABLE = "incompatible-executable"
    OPTIONAL_EXECUTABLE_MISSING = "optional-executable-missing"
    EXTERNAL_LOADING_DISABLED = "external-loading-disabled"
    SOURCE_FAILURE = "manifest-source-failure"


@dataclass(frozen=True, order=True)
class Version:
    """A strict three-component release semantic version."""

    major: int
    minor: int
    patch: int

    @classmethod
    def parse(cls, value: str) -> Version:
        if (
            not isinstance(value, str)
            or re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", value) is None
        ):
            raise ValueError(f"expected strict MAJOR.MINOR.PATCH, got {value!r}")
        parts = value.split(".")
        return cls(*(int(part) for part in parts))

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


@dataclass(frozen=True)
class VersionRange:
    """A half-open compatibility range."""

    minimum_inclusive: Version
    maximum_exclusive: Version

    def __post_init__(self) -> None:
        if self.minimum_inclusive >= self.maximum_exclusive:
            raise ValueError("minimum_inclusive must be lower than maximum_exclusive")

    def contains(self, version: Version) -> bool:
        return self.minimum_inclusive <= version < self.maximum_exclusive

    def render(self) -> str:
        return f"[{self.minimum_inclusive},{self.maximum_exclusive})"


@dataclass(frozen=True, order=True)
class CapabilityRef:
    """Stable capability identity including its typed-port API major."""

    capability_id: str
    api_version: int

    def render(self) -> str:
        return f"{self.capability_id}@{self.api_version}"


@dataclass(frozen=True)
class ExecutableRequirement:
    name: str
    required: bool
    version: VersionRange


@dataclass(frozen=True)
class LifecycleSubscriptionDeclaration:
    subscription_id: str
    event: str
    policy: DeliveryPolicy


@dataclass(frozen=True, order=True)
class CliProjection:
    capability_id: str
    command: str


@dataclass(frozen=True, order=True)
class PermissionRequirement:
    permission_id: str
    required: bool
    purpose: str


@dataclass(frozen=True, order=True)
class CredentialSource:
    kind: str
    name: str


@dataclass(frozen=True, order=True)
class CredentialRequirement:
    credential_id: str
    required: bool
    purpose: str
    sources: tuple[CredentialSource, ...]


@dataclass(frozen=True)
class PluginManifest:
    """Validated manifest data used by discovery and composition.

    The original JSON object is deliberately not retained: discovery outputs contain only the
    immutable declarations needed by later bootstrap steps, never live callables or secrets.
    """

    plugin_id: str
    plugin_version: Version
    beadhive_compatibility: VersionRange
    kernel_compatibility: VersionRange
    capabilities: tuple[CapabilityRef, ...]
    lifecycle_subscriptions: tuple[LifecycleSubscriptionDeclaration, ...]
    configuration_legacy_namespaces: tuple[str, ...]
    configuration_schema_artifact: str | None
    cli_projections: tuple[CliProjection, ...]
    permissions: tuple[PermissionRequirement, ...]
    executables: tuple[ExecutableRequirement, ...]
    credentials: tuple[CredentialRequirement, ...]
    configuration_namespace: str


@dataclass(frozen=True, order=True)
class ManifestProvenance:
    """Where manifest metadata came from; never self-asserted by the manifest."""

    source_kind: str
    source_name: str
    distribution: str = ""

    def render(self) -> str:
        suffix = f" distribution={self.distribution}" if self.distribution else ""
        return f"{self.source_kind}:{self.source_name}{suffix}"


@dataclass(frozen=True)
class ManifestDocument:
    """One declared manifest byte source and its trusted provenance."""

    provenance: ManifestProvenance
    payload: bytes


@dataclass(frozen=True, order=True)
class ExternalEntryPoint:
    """Distribution metadata for the reserved future manifest entry-point seam."""

    name: str
    distribution: str
    group: str = "beadhive.plugins.v1"


@dataclass(frozen=True)
class SourceRead:
    documents: tuple[ManifestDocument, ...] = ()
    diagnostics: tuple[PluginDiagnostic, ...] = ()


class ManifestSource(Protocol):
    """A declared metadata source invoked by discovery and nothing else."""

    def read(self, *, allow_external: bool) -> SourceRead: ...


@dataclass(frozen=True)
class PluginDiagnostic:
    """Stable diagnostic with deterministic text rendering."""

    code: DiagnosticCode
    severity: DiagnosticSeverity
    detail: str
    plugin_id: str = "?"
    provenance: ManifestProvenance | None = None
    capability: CapabilityRef | None = None

    def render(self) -> str:
        fields = [
            f"plugin-kernel[{self.code.value}]",
            f"severity={self.severity.value}",
            f"plugin={self.plugin_id}",
        ]
        if self.capability is not None:
            fields.append(f"capability={self.capability.render()}")
        if self.provenance is not None:
            fields.append(f"source={self.provenance.render()}")
        return " ".join(fields) + f": {self.detail}"


@dataclass(frozen=True)
class DiscoveredPlugin:
    manifest: PluginManifest
    provenance: ManifestProvenance


@dataclass(frozen=True)
class CapabilitySelection:
    capability: CapabilityRef
    plugin_id: str


@dataclass(frozen=True)
class DiscoveryResult:
    """Deterministic metadata-only discovery output."""

    plugins: tuple[DiscoveredPlugin, ...]
    capabilities: tuple[CapabilitySelection, ...]
    diagnostics: tuple[PluginDiagnostic, ...]

    @property
    def errors(self) -> tuple[PluginDiagnostic, ...]:
        return tuple(
            diagnostic
            for diagnostic in self.diagnostics
            if diagnostic.severity is DiagnosticSeverity.ERROR
        )

    def owner_of(self, capability: CapabilityRef) -> str | None:
        """Bootstrap-only metadata lookup used before injecting an application port."""

        return next(
            (
                selection.plugin_id
                for selection in self.capabilities
                if selection.capability == capability
            ),
            None,
        )


@dataclass(frozen=True)
class PluginKernelConfig:
    """Core-owned discovery and ownership policy, parsed before source access."""

    enabled: Mapping[str, bool] = field(
        default_factory=lambda: MappingProxyType({}),
    )
    capability_owners: Mapping[str, str] = field(
        default_factory=lambda: MappingProxyType({}),
    )
    allow_external_entry_points: bool = False


PortT = TypeVar("PortT")


@dataclass(frozen=True)
class CapabilityKey(Generic[PortT]):
    """A named application-port request made explicitly at composition time."""

    capability: CapabilityRef
    port_type: type[PortT]


@dataclass(frozen=True, order=True)
class ProviderKey:
    plugin_id: str
    capability: CapabilityRef


@dataclass(frozen=True)
class ProviderBinding(Generic[PortT]):
    key: ProviderKey
    port: PortT


class CapabilityBindingError(LookupError):
    """Composition could not bind a selected provider to the requested typed port."""


#: Build-graph impact analysis (Attested Green ADR, Amendment 1). Its typed port is
#: ``beadhive.modules.work.contracts.impact.ImpactBackend``, unchanged: the port belongs to the
#: work module, so this stdlib-only kernel contract names the capability and bootstrap pairs it
#: with the port in a :class:`CapabilityKey`. Every selected provider's answer still runs inside
#: the core's fail-closed resolver; a provider only answers questions.
BUILD_IMPACT = CapabilityRef("build.impact", 1)

#: Build-system health checks (ownership completeness, proven-test drift, key-tag coverage)
#: reported through :class:`BuildVerifier`.
BUILD_VERIFY = CapabilityRef("build.verify", 1)


@runtime_checkable
class BuildVerifier(Protocol):
    """Typed port for :data:`BUILD_VERIFY`: inspect one repository and report findings.

    Verification is read-only and never raises for a finding; an empty tuple means healthy.
    """

    def verify(self, repo: str) -> tuple[PluginDiagnostic, ...]: ...


ManifestLoader = Callable[[ExternalEntryPoint], bytes]
