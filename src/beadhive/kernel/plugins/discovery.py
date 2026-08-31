"""Deterministic, metadata-only plugin discovery and owner selection."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from ..lifecycle.contracts import (
    EVENTS_BY_ID,
    CompensationMode,
    CompensationPolicy,
    Criticality,
    DeliveryPolicy,
    Idempotency,
    RetryPolicy,
)
from .contracts import (
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
    SourceRead,
    Version,
    VersionRange,
)

_LOCAL_ID = re.compile(r"^[a-z][a-z0-9-]*$")
_CAPABILITY_ID = re.compile(r"^[a-z][a-z0-9-]*(?:\.[a-z][a-z0-9-]*)+$")
_EXECUTABLE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
_COMMAND = re.compile(r"^[a-z0-9-]+(?: [a-z0-9-]+)*$")
_CREDENTIAL_KINDS = {
    "environment-variable",
    "file-reference",
    "keyring",
    "oauth-profile",
}


class _ManifestError(ValueError):
    pass


class _ConfigError(ValueError):
    pass


def _closed_object(
    value: object,
    path: str,
    *,
    required: set[str],
    allowed: set[str] | None = None,
) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise _ManifestError(f"{path}: expected object")
    allowed = allowed or required
    missing = sorted(required - value.keys())
    if missing:
        raise _ManifestError(f"{path}: missing fields {missing}")
    extra = sorted(value.keys() - allowed)
    if extra:
        raise _ManifestError(f"{path}: undeclared fields {extra}")
    return value


def _array(value: object, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise _ManifestError(f"{path}: expected array")
    return value


def _string(value: object, path: str, *, nonempty: bool = True) -> str:
    if not isinstance(value, str) or (nonempty and not value):
        raise _ManifestError(f"{path}: expected {'non-empty ' if nonempty else ''}string")
    return value


def _integer(value: object, path: str, *, minimum: int | None = None) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise _ManifestError(f"{path}: expected integer")
    if minimum is not None and value < minimum:
        raise _ManifestError(f"{path}: expected integer >= {minimum}")
    return value


def _boolean(value: object, path: str) -> bool:
    if not isinstance(value, bool):
        raise _ManifestError(f"{path}: expected boolean")
    return value


def _matching(value: object, path: str, pattern: re.Pattern[str]) -> str:
    text = _string(value, path)
    if pattern.fullmatch(text) is None:
        raise _ManifestError(f"{path}: invalid value {text!r}")
    return text


def _enum(value: object, path: str, choices: set[str]) -> str:
    text = _string(value, path)
    if text not in choices:
        raise _ManifestError(f"{path}: expected one of {sorted(choices)}, got {text!r}")
    return text


def _unique(values: Sequence[object], path: str) -> None:
    rendered = [json.dumps(value, sort_keys=True, separators=(",", ":")) for value in values]
    if len(rendered) != len(set(rendered)):
        raise _ManifestError(f"{path}: duplicate array item")


def _unique_id(values: Sequence[str], path: str) -> None:
    duplicates = sorted(value for value in set(values) if values.count(value) > 1)
    if duplicates:
        raise _ManifestError(f"{path}: duplicate semantic identities {duplicates}")


def _version(value: object, path: str) -> Version:
    try:
        return Version.parse(_string(value, path))
    except ValueError as exc:
        raise _ManifestError(f"{path}: {exc}") from exc


def _version_range(value: object, path: str) -> VersionRange:
    obj = _closed_object(
        value,
        path,
        required={"minimum_inclusive", "maximum_exclusive"},
    )
    try:
        return VersionRange(
            _version(obj["minimum_inclusive"], f"{path}.minimum_inclusive"),
            _version(obj["maximum_exclusive"], f"{path}.maximum_exclusive"),
        )
    except ValueError as exc:
        raise _ManifestError(f"{path}: {exc}") from exc


def _decode_json(payload: bytes) -> Mapping[str, Any]:
    if not isinstance(payload, bytes):
        raise _ManifestError("$: manifest payload must be bytes")

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-JSON numeric constant {value}")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON member {key!r}")
            result[key] = value
        return result

    try:
        value = json.loads(
            payload.decode("utf-8"),
            parse_constant=reject_constant,
            object_pairs_hook=unique_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise _ManifestError(f"$: invalid UTF-8 JSON ({exc})") from exc
    return _closed_object(
        value,
        "$",
        required={
            "manifest_version",
            "plugin_id",
            "plugin_version",
            "compatibility",
            "capabilities",
            "lifecycle",
            "configuration",
            "presentation",
            "security",
        },
    )


def _parse_capabilities(value: object) -> tuple[CapabilityRef, ...]:
    obj = _closed_object(value, "$.capabilities", required={"provides"})
    items = _array(obj["provides"], "$.capabilities.provides")
    _unique(items, "$.capabilities.provides")
    result: list[CapabilityRef] = []
    for index, item in enumerate(items):
        path = f"$.capabilities.provides[{index}]"
        declaration = _closed_object(item, path, required={"id", "api_version"})
        result.append(
            CapabilityRef(
                _matching(declaration["id"], f"{path}.id", _CAPABILITY_ID),
                _integer(declaration["api_version"], f"{path}.api_version", minimum=1),
            )
        )
    _unique_id([item.capability_id for item in result], "$.capabilities.provides.id")
    return tuple(sorted(result))


def _parse_lifecycle(value: object, plugin_id: str) -> tuple[LifecycleSubscriptionDeclaration, ...]:
    obj = _closed_object(value, "$.lifecycle", required={"subscriptions"})
    items = _array(obj["subscriptions"], "$.lifecycle.subscriptions")
    _unique(items, "$.lifecycle.subscriptions")
    identities: list[str] = []
    declarations: list[LifecycleSubscriptionDeclaration] = []
    for index, item in enumerate(items):
        path = f"$.lifecycle.subscriptions[{index}]"
        sub = _closed_object(
            item,
            path,
            required={
                "id",
                "event",
                "order",
                "timeout_seconds",
                "criticality",
                "idempotency",
                "retry",
                "compensation",
            },
        )
        subscription_id = _matching(sub["id"], f"{path}.id", _CAPABILITY_ID)
        if not subscription_id.startswith(f"{plugin_id}."):
            raise _ManifestError(
                f"{path}.id: subscription must be qualified by plugin ID {plugin_id!r}"
            )
        identities.append(subscription_id)
        event = _matching(sub["event"], f"{path}.event", _CAPABILITY_ID)
        if event not in EVENTS_BY_ID:
            raise _ManifestError(f"{path}.event: unknown kernel lifecycle event {event!r}")
        order = _integer(sub["order"], f"{path}.order")
        timeout = _integer(sub["timeout_seconds"], f"{path}.timeout_seconds", minimum=1)
        criticality = _enum(sub["criticality"], f"{path}.criticality", {"best-effort", "blocking"})
        idempotency = _enum(
            sub["idempotency"],
            f"{path}.idempotency",
            {"not-applicable", "required"},
        )
        retry = _closed_object(
            sub["retry"],
            f"{path}.retry",
            required={"max_attempts", "backoff_seconds"},
        )
        attempts = _integer(retry["max_attempts"], f"{path}.retry.max_attempts", minimum=1)
        _integer(retry["backoff_seconds"], f"{path}.retry.backoff_seconds", minimum=0)
        if attempts > 1 and idempotency != "required":
            raise _ManifestError(f"{path}: retry requires idempotency='required'")
        compensation = _closed_object(
            sub["compensation"],
            f"{path}.compensation",
            required={"mode", "action_id"},
        )
        mode = _enum(
            compensation["mode"],
            f"{path}.compensation.mode",
            {"none", "required"},
        )
        action = compensation["action_id"]
        if action is not None:
            action = _matching(action, f"{path}.compensation.action_id", _CAPABILITY_ID)
        if (mode == "required") != (action is not None):
            raise _ManifestError(
                f"{path}.compensation: required mode and action_id must be declared together"
            )
        declarations.append(
            LifecycleSubscriptionDeclaration(
                subscription_id,
                event,
                DeliveryPolicy(
                    order=order,
                    timeout_seconds=timeout,
                    criticality=Criticality(criticality),
                    idempotency=Idempotency(idempotency),
                    retry=RetryPolicy(
                        attempts,
                        _integer(
                            retry["backoff_seconds"],
                            f"{path}.retry.backoff_seconds",
                            minimum=0,
                        ),
                    ),
                    compensation=CompensationPolicy(CompensationMode(mode), action),
                ),
            )
        )
    _unique_id(identities, "$.lifecycle.subscriptions.id")
    return tuple(
        sorted(
            declarations,
            key=lambda declaration: (
                declaration.policy.order,
                declaration.subscription_id,
            ),
        )
    )


def _parse_configuration(value: object, plugin_id: str) -> tuple[str, tuple[str, ...], str | None]:
    obj = _closed_object(
        value,
        "$.configuration",
        required={"namespace", "legacy_namespaces", "schema_artifact"},
    )
    namespace = _string(obj["namespace"], "$.configuration.namespace")
    expected = f"plugins.{plugin_id}"
    if namespace != expected:
        raise _ManifestError(f"$.configuration.namespace: expected {expected!r}, got {namespace!r}")
    legacy = _array(obj["legacy_namespaces"], "$.configuration.legacy_namespaces")
    legacy_namespaces = tuple(
        _string(item, f"$.configuration.legacy_namespaces[{index}]")
        for index, item in enumerate(legacy)
    )
    _unique(legacy, "$.configuration.legacy_namespaces")
    artifact = obj["schema_artifact"]
    if artifact is not None and not _string(artifact, "$.configuration.schema_artifact").startswith(
        "urn:beadhive:wire-schema:"
    ):
        raise _ManifestError(
            "$.configuration.schema_artifact: expected Beadhive wire-schema URN or null"
        )
    return namespace, tuple(sorted(legacy_namespaces)), artifact


def _parse_presentation(value: object, capability_ids: set[str]) -> tuple[CliProjection, ...]:
    obj = _closed_object(value, "$.presentation", required={"cli"})
    items = _array(obj["cli"], "$.presentation.cli")
    _unique(items, "$.presentation.cli")
    commands: list[str] = []
    projections: list[CliProjection] = []
    for index, item in enumerate(items):
        path = f"$.presentation.cli[{index}]"
        projection = _closed_object(item, path, required={"capability_id", "command"})
        capability_id = _matching(
            projection["capability_id"], f"{path}.capability_id", _CAPABILITY_ID
        )
        if capability_id not in capability_ids:
            raise _ManifestError(
                f"{path}.capability_id: {capability_id!r} is not provided by this plugin"
            )
        command = _matching(projection["command"], f"{path}.command", _COMMAND)
        commands.append(command)
        projections.append(CliProjection(capability_id, command))
    _unique_id(commands, "$.presentation.cli.command")
    return tuple(sorted(projections))


def _parse_security(
    value: object,
) -> tuple[
    tuple[PermissionRequirement, ...],
    tuple[ExecutableRequirement, ...],
    tuple[CredentialRequirement, ...],
]:
    obj = _closed_object(
        value,
        "$.security",
        required={"permissions", "executables", "credentials"},
    )
    permissions = _array(obj["permissions"], "$.security.permissions")
    _unique(permissions, "$.security.permissions")
    permission_ids: list[str] = []
    permission_requirements: list[PermissionRequirement] = []
    for index, item in enumerate(permissions):
        path = f"$.security.permissions[{index}]"
        permission = _closed_object(item, path, required={"id", "required", "purpose"})
        permission_id = _matching(permission["id"], f"{path}.id", _CAPABILITY_ID)
        permission_ids.append(permission_id)
        permission_requirements.append(
            PermissionRequirement(
                permission_id,
                _boolean(permission["required"], f"{path}.required"),
                _string(permission["purpose"], f"{path}.purpose"),
            )
        )
    _unique_id(permission_ids, "$.security.permissions.id")

    executables = _array(obj["executables"], "$.security.executables")
    _unique(executables, "$.security.executables")
    executable_ids: list[str] = []
    requirements: list[ExecutableRequirement] = []
    for index, item in enumerate(executables):
        path = f"$.security.executables[{index}]"
        executable = _closed_object(item, path, required={"name", "required", "version"})
        name = _matching(executable["name"], f"{path}.name", _EXECUTABLE)
        executable_ids.append(name)
        requirements.append(
            ExecutableRequirement(
                name,
                _boolean(executable["required"], f"{path}.required"),
                _version_range(executable["version"], f"{path}.version"),
            )
        )
    _unique_id(executable_ids, "$.security.executables.name")

    credentials = _array(obj["credentials"], "$.security.credentials")
    _unique(credentials, "$.security.credentials")
    credential_ids: list[str] = []
    credential_requirements: list[CredentialRequirement] = []
    for index, item in enumerate(credentials):
        path = f"$.security.credentials[{index}]"
        credential = _closed_object(
            item,
            path,
            required={"id", "required", "purpose", "sources"},
        )
        credential_id = _matching(credential["id"], f"{path}.id", _LOCAL_ID)
        credential_ids.append(credential_id)
        required = _boolean(credential["required"], f"{path}.required")
        purpose = _string(credential["purpose"], f"{path}.purpose")
        sources = _array(credential["sources"], f"{path}.sources")
        if not sources:
            raise _ManifestError(f"{path}.sources: expected at least one source")
        _unique(sources, f"{path}.sources")
        declared_sources: list[CredentialSource] = []
        for source_index, source_value in enumerate(sources):
            source_path = f"{path}.sources[{source_index}]"
            source = _closed_object(source_value, source_path, required={"kind", "name"})
            declared_sources.append(
                CredentialSource(
                    _enum(source["kind"], f"{source_path}.kind", _CREDENTIAL_KINDS),
                    _string(source["name"], f"{source_path}.name"),
                )
            )
        credential_requirements.append(
            CredentialRequirement(
                credential_id,
                required,
                purpose,
                tuple(sorted(declared_sources)),
            )
        )
    _unique_id(credential_ids, "$.security.credentials.id")
    return (
        tuple(sorted(permission_requirements)),
        tuple(sorted(requirements, key=lambda requirement: requirement.name)),
        tuple(sorted(credential_requirements)),
    )


def _parse_manifest(document: ManifestDocument) -> PluginManifest:
    obj = _decode_json(document.payload)
    if _integer(obj["manifest_version"], "$.manifest_version") != 1:
        raise _ManifestError("$.manifest_version: expected 1")
    plugin_id = _matching(obj["plugin_id"], "$.plugin_id", _LOCAL_ID)
    plugin_version = _version(obj["plugin_version"], "$.plugin_version")
    compatibility = _closed_object(
        obj["compatibility"],
        "$.compatibility",
        required={"beadhive", "plugin_kernel"},
    )
    capabilities = _parse_capabilities(obj["capabilities"])
    lifecycle = _parse_lifecycle(obj["lifecycle"], plugin_id)
    namespace, legacy_namespaces, schema_artifact = _parse_configuration(
        obj["configuration"], plugin_id
    )
    projections = _parse_presentation(
        obj["presentation"],
        {capability.capability_id for capability in capabilities},
    )
    permissions, executables, credentials = _parse_security(obj["security"])
    return PluginManifest(
        plugin_id=plugin_id,
        plugin_version=plugin_version,
        beadhive_compatibility=_version_range(
            compatibility["beadhive"], "$.compatibility.beadhive"
        ),
        kernel_compatibility=_version_range(
            compatibility["plugin_kernel"], "$.compatibility.plugin_kernel"
        ),
        capabilities=capabilities,
        lifecycle_subscriptions=lifecycle,
        configuration_legacy_namespaces=legacy_namespaces,
        configuration_schema_artifact=schema_artifact,
        cli_projections=projections,
        permissions=permissions,
        executables=executables,
        credentials=credentials,
        configuration_namespace=namespace,
    )


def _config_object(value: object, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _ConfigError(f"{path}: expected object")
    if any(not isinstance(key, str) for key in value):
        raise _ConfigError(f"{path}: keys must be strings")
    return value


def parse_kernel_config(
    value: Mapping[str, object] | PluginKernelConfig | None,
) -> PluginKernelConfig:
    """Parse the core-owned ``plugin_kernel`` section without reading ambient config."""

    if isinstance(value, PluginKernelConfig):
        value = {
            "enabled": value.enabled,
            "capability_owners": value.capability_owners,
            "allow_external_entry_points": value.allow_external_entry_points,
        }
    obj = _config_object({} if value is None else value, "plugin_kernel")
    allowed = {"enabled", "capability_owners", "allow_external_entry_points"}
    extra = sorted(set(obj) - allowed)
    if extra:
        raise _ConfigError(f"plugin_kernel: undeclared fields {extra}")
    enabled_obj = _config_object(obj.get("enabled", {}), "plugin_kernel.enabled")
    if any(not isinstance(plugin_id, str) for plugin_id in enabled_obj):
        raise _ConfigError("plugin_kernel.enabled: keys must be plugin ID strings")
    enabled: dict[str, bool] = {}
    for plugin_id in sorted(enabled_obj):
        state = enabled_obj[plugin_id]
        if _LOCAL_ID.fullmatch(plugin_id) is None or not isinstance(state, bool):
            raise _ConfigError(
                f"plugin_kernel.enabled.{plugin_id}: expected plugin ID mapped to boolean"
            )
        enabled[plugin_id] = state
    owners_obj = _config_object(obj.get("capability_owners", {}), "plugin_kernel.capability_owners")
    if any(not isinstance(capability_key, str) for capability_key in owners_obj):
        raise _ConfigError(
            "plugin_kernel.capability_owners: keys must be capability.id@MAJOR strings"
        )
    owners: dict[str, str] = {}
    for capability_key in sorted(owners_obj):
        owner = owners_obj[capability_key]
        try:
            capability_id, version_text = capability_key.rsplit("@", 1)
        except ValueError as exc:
            raise _ConfigError(
                f"plugin_kernel.capability_owners.{capability_key}: expected capability.id@MAJOR"
            ) from exc
        if (
            _CAPABILITY_ID.fullmatch(capability_id) is None
            or re.fullmatch(r"[1-9][0-9]*", version_text) is None
            or int(version_text) < 1
            or not isinstance(owner, str)
            or _LOCAL_ID.fullmatch(owner) is None
        ):
            raise _ConfigError(
                f"plugin_kernel.capability_owners.{capability_key}: "
                "expected capability.id@MAJOR mapped to plugin ID"
            )
        owners[f"{capability_id}@{int(version_text)}"] = owner
    external = obj.get("allow_external_entry_points", False)
    if not isinstance(external, bool):
        raise _ConfigError("plugin_kernel.allow_external_entry_points: expected boolean")
    return PluginKernelConfig(
        MappingProxyType(enabled),
        MappingProxyType(owners),
        external,
    )


@dataclass(frozen=True)
class BuiltInManifestSource:
    """A static Beadhive-owned manifest document list."""

    documents: tuple[ManifestDocument, ...]

    def read(self, *, allow_external: bool) -> SourceRead:
        del allow_external
        return SourceRead(
            tuple(
                sorted(
                    self.documents,
                    key=lambda document: (
                        document.provenance.source_name,
                        document.provenance.distribution,
                    ),
                )
            )
        )


@dataclass(frozen=True)
class ExternalEntryPointSource:
    """Policy-gated future entry-point metadata source.

    The injected loader may return only manifest bytes.  No implementation entry point or plugin
    object is represented by this contract.
    """

    entries: tuple[ExternalEntryPoint, ...]
    loader: ManifestLoader

    def read(self, *, allow_external: bool) -> SourceRead:
        entries = tuple(sorted(self.entries, key=lambda item: (item.name, item.distribution)))
        if not allow_external:
            return SourceRead(
                diagnostics=tuple(
                    PluginDiagnostic(
                        DiagnosticCode.EXTERNAL_LOADING_DISABLED,
                        DiagnosticSeverity.INFO,
                        "external manifest metadata was not loaded because policy is disabled",
                        provenance=ManifestProvenance(
                            "entry-point", entry.name, entry.distribution
                        ),
                    )
                    for entry in entries
                )
            )
        documents: list[ManifestDocument] = []
        diagnostics: list[PluginDiagnostic] = []
        for entry in entries:
            provenance = ManifestProvenance("entry-point", entry.name, entry.distribution)
            if entry.group != "beadhive.plugins.v1":
                diagnostics.append(
                    PluginDiagnostic(
                        DiagnosticCode.SOURCE_FAILURE,
                        DiagnosticSeverity.ERROR,
                        f"unsupported entry-point group {entry.group!r}",
                        provenance=provenance,
                    )
                )
                continue
            try:
                payload = self.loader(entry)
                if not isinstance(payload, bytes):
                    raise TypeError("entry-point manifest loader must return bytes")
                documents.append(ManifestDocument(provenance, payload))
            except Exception as exc:  # noqa: BLE001 - source failures become typed diagnostics
                diagnostics.append(
                    PluginDiagnostic(
                        DiagnosticCode.SOURCE_FAILURE,
                        DiagnosticSeverity.ERROR,
                        f"manifest metadata load failed: {type(exc).__name__}: {exc}",
                        provenance=provenance,
                    )
                )
        return SourceRead(tuple(documents), tuple(diagnostics))


def _diagnostic_key(diagnostic: PluginDiagnostic) -> tuple[str, str, str, str, str]:
    return (
        diagnostic.code.value,
        diagnostic.plugin_id,
        diagnostic.capability.render() if diagnostic.capability else "",
        diagnostic.provenance.render() if diagnostic.provenance else "",
        diagnostic.detail,
    )


def _executable_diagnostics(
    plugin: DiscoveredPlugin,
    host_executables: Mapping[str, str],
) -> tuple[PluginDiagnostic, ...]:
    diagnostics: list[PluginDiagnostic] = []
    for requirement in plugin.manifest.executables:
        found = host_executables.get(requirement.name)
        if found is None:
            diagnostics.append(
                PluginDiagnostic(
                    DiagnosticCode.MISSING_EXECUTABLE
                    if requirement.required
                    else DiagnosticCode.OPTIONAL_EXECUTABLE_MISSING,
                    DiagnosticSeverity.ERROR
                    if requirement.required
                    else DiagnosticSeverity.WARNING,
                    f"executable {requirement.name!r} is not declared available; "
                    f"required range {requirement.version.render()}",
                    plugin.manifest.plugin_id,
                    plugin.provenance,
                )
            )
            continue
        try:
            version = Version.parse(found)
        except (TypeError, ValueError) as exc:
            diagnostics.append(
                PluginDiagnostic(
                    DiagnosticCode.INCOMPATIBLE_EXECUTABLE,
                    (
                        DiagnosticSeverity.ERROR
                        if requirement.required
                        else DiagnosticSeverity.WARNING
                    ),
                    f"executable {requirement.name!r} has invalid version {found!r}: {exc}",
                    plugin.manifest.plugin_id,
                    plugin.provenance,
                )
            )
            continue
        if not requirement.version.contains(version):
            diagnostics.append(
                PluginDiagnostic(
                    DiagnosticCode.INCOMPATIBLE_EXECUTABLE,
                    (
                        DiagnosticSeverity.ERROR
                        if requirement.required
                        else DiagnosticSeverity.WARNING
                    ),
                    f"executable {requirement.name!r} version {version} is outside "
                    f"{requirement.version.render()}",
                    plugin.manifest.plugin_id,
                    plugin.provenance,
                )
            )
    return tuple(diagnostics)


def discover_plugins(
    sources: Sequence[ManifestSource],
    *,
    config: Mapping[str, object] | PluginKernelConfig | None,
    beadhive_version: str,
    kernel_version: str,
    host_executables: Mapping[str, str] | None = None,
) -> DiscoveryResult:
    """Discover, validate, filter, and select plugins using declared inputs only."""

    diagnostics: list[PluginDiagnostic] = []
    try:
        policy = parse_kernel_config(config)
        host = Version.parse(beadhive_version)
        kernel = Version.parse(kernel_version)
    except (_ConfigError, ValueError) as exc:
        diagnostic = PluginDiagnostic(
            DiagnosticCode.INVALID_CONFIG,
            DiagnosticSeverity.ERROR,
            str(exc),
        )
        return DiscoveryResult((), (), (diagnostic,))

    documents: list[ManifestDocument] = []
    for source in sources:
        try:
            read = source.read(allow_external=policy.allow_external_entry_points)
        except Exception as exc:  # noqa: BLE001 - source failures become typed diagnostics
            diagnostics.append(
                PluginDiagnostic(
                    DiagnosticCode.SOURCE_FAILURE,
                    DiagnosticSeverity.ERROR,
                    f"manifest source failed: {type(exc).__name__}: {exc}",
                )
            )
            continue
        documents.extend(read.documents)
        diagnostics.extend(read.diagnostics)
    documents.sort(key=lambda document: document.provenance)

    parsed: list[DiscoveredPlugin] = []
    for document in documents:
        try:
            parsed.append(DiscoveredPlugin(_parse_manifest(document), document.provenance))
        except _ManifestError as exc:
            diagnostics.append(
                PluginDiagnostic(
                    DiagnosticCode.INVALID_MANIFEST,
                    DiagnosticSeverity.ERROR,
                    str(exc),
                    provenance=document.provenance,
                )
            )

    by_id: dict[str, list[DiscoveredPlugin]] = defaultdict(list)
    for plugin in parsed:
        by_id[plugin.manifest.plugin_id].append(plugin)
    unique: list[DiscoveredPlugin] = []
    for plugin_id, group in sorted(by_id.items()):
        if len(group) > 1:
            sources_text = ", ".join(sorted(item.provenance.render() for item in group))
            diagnostics.append(
                PluginDiagnostic(
                    DiagnosticCode.DUPLICATE_PLUGIN_ID,
                    DiagnosticSeverity.ERROR,
                    f"plugin ID is declared by multiple sources: {sources_text}",
                    plugin_id,
                )
            )
        else:
            unique.append(group[0])

    available: list[DiscoveredPlugin] = []
    host_executables = host_executables or {}
    for plugin in unique:
        manifest = plugin.manifest
        if not manifest.beadhive_compatibility.contains(host):
            diagnostics.append(
                PluginDiagnostic(
                    DiagnosticCode.INCOMPATIBLE_BEADHIVE,
                    DiagnosticSeverity.ERROR,
                    f"host version {host} is outside {manifest.beadhive_compatibility.render()}",
                    manifest.plugin_id,
                    plugin.provenance,
                )
            )
            continue
        if not manifest.kernel_compatibility.contains(kernel):
            diagnostics.append(
                PluginDiagnostic(
                    DiagnosticCode.INCOMPATIBLE_KERNEL,
                    DiagnosticSeverity.ERROR,
                    f"kernel version {kernel} is outside {manifest.kernel_compatibility.render()}",
                    manifest.plugin_id,
                    plugin.provenance,
                )
            )
            continue
        if policy.enabled.get(manifest.plugin_id, True) is False:
            diagnostics.append(
                PluginDiagnostic(
                    DiagnosticCode.DISABLED_PLUGIN,
                    DiagnosticSeverity.INFO,
                    "plugin is disabled by plugin_kernel.enabled",
                    manifest.plugin_id,
                    plugin.provenance,
                )
            )
            continue
        executable_diagnostics = _executable_diagnostics(plugin, host_executables)
        diagnostics.extend(executable_diagnostics)
        if any(item.severity is DiagnosticSeverity.ERROR for item in executable_diagnostics):
            continue
        available.append(plugin)

    candidates: dict[CapabilityRef, list[DiscoveredPlugin]] = defaultdict(list)
    for plugin in available:
        for capability in plugin.manifest.capabilities:
            candidates[capability].append(plugin)
    selections: list[CapabilitySelection] = []
    used_owner_keys: set[str] = set()
    for capability, providers in sorted(candidates.items()):
        owner_key = capability.render()
        configured_owner = policy.capability_owners.get(owner_key)
        provider_ids = sorted(plugin.manifest.plugin_id for plugin in providers)
        if configured_owner is not None:
            used_owner_keys.add(owner_key)
            if configured_owner not in provider_ids:
                diagnostics.append(
                    PluginDiagnostic(
                        DiagnosticCode.INVALID_CONFIG,
                        DiagnosticSeverity.ERROR,
                        f"configured owner {configured_owner!r} is not an available provider; "
                        f"candidates={provider_ids}",
                        configured_owner,
                        capability=capability,
                    )
                )
                continue
            selections.append(CapabilitySelection(capability, configured_owner))
        elif len(providers) == 1:
            selections.append(CapabilitySelection(capability, provider_ids[0]))
        else:
            diagnostics.append(
                PluginDiagnostic(
                    DiagnosticCode.DUPLICATE_CAPABILITY,
                    DiagnosticSeverity.ERROR,
                    f"multiple providers {provider_ids}; configure "
                    f"plugin_kernel.capability_owners.{owner_key}",
                    capability=capability,
                )
            )
    for unused_key in sorted(policy.capability_owners.keys() - used_owner_keys):
        diagnostics.append(
            PluginDiagnostic(
                DiagnosticCode.INVALID_CONFIG,
                DiagnosticSeverity.ERROR,
                f"configured capability owner has no available provider for {unused_key}",
                policy.capability_owners[unused_key],
            )
        )

    return DiscoveryResult(
        tuple(sorted(available, key=lambda plugin: plugin.manifest.plugin_id)),
        tuple(sorted(selections, key=lambda selection: selection.capability)),
        tuple(sorted(diagnostics, key=_diagnostic_key)),
    )
