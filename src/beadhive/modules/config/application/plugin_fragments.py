"""Metadata-only composition of versioned plugin configuration fragments.

The configuration capability owns this projection.  It intentionally knows only
the declarative manifest fields and canonical config models: importing this module
must never import an optional integration or consult plugin discovery at runtime.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Protocol

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ValidationError

from ..contracts import HerdrConfig, HitchConfig, ObservaloopConfig, OrcaConfig, RepowiseConfig
from .resolution import SourceLayer

_URN_PREFIX = "urn:beadhive:wire-schema:plugin-config:"
_FRAGMENT_MODELS: Mapping[str, type[BaseModel]] = MappingProxyType(
    {
        "herdr": HerdrConfig,
        "hitch": HitchConfig,
        "observaloop": ObservaloopConfig,
        "orca": OrcaConfig,
        "repowise": RepowiseConfig,
    }
)


class ManifestLike(Protocol):
    plugin_id: str
    configuration_namespace: str
    configuration_schema_artifact: str | None
    plugin_version: str
    provenance: TrustedManifestProvenance


@dataclass(frozen=True, slots=True)
class TrustedManifestProvenance:
    """The bounded, trusted manifest identity safe to carry into config provenance."""

    source_kind: str
    source_name: str
    distribution: str = ""


@dataclass(frozen=True, slots=True)
class PluginConfigManifest:
    """Typed manifest declaration consumed by fragment composition, never raw manifest bytes."""

    plugin_id: str
    configuration_namespace: str
    configuration_schema_artifact: str | None
    plugin_version: str
    provenance: TrustedManifestProvenance


@dataclass(frozen=True, slots=True)
class FragmentDiagnostic:
    """Value-free, stable composition result suitable for user diagnostics."""

    code: str
    plugin_id: str
    path: str


@dataclass(frozen=True, slots=True)
class PluginConfigFragment:
    plugin_id: str
    namespace: str
    artifact_id: str
    contract_version: int
    schema: Mapping[str, Any]
    digest: str


@dataclass(frozen=True, slots=True)
class FragmentValueProvenance:
    """Value-free origin record for one composed plugin configuration leaf."""

    plugin_id: str
    plugin_version: str
    namespace: str
    manifest_provenance: TrustedManifestProvenance
    schema_artifact: str
    schema_digest: str
    fragment_major: int
    source_paths: tuple[str, ...]
    source_layer: SourceLayer


@dataclass(frozen=True, slots=True)
class ComposedPluginConfig:
    """Active immutable plugin values plus opaque retained persisted subtrees."""

    active: Mapping[str, Mapping[str, Any]]
    retained: Mapping[str, Any]
    provenance: Mapping[str, FragmentValueProvenance]
    diagnostics: tuple[FragmentDiagnostic, ...]


def _artifact_id(plugin_id: str, version: int = 1) -> str:
    return f"{_URN_PREFIX}{plugin_id}:{version}"


def _schema_bytes(schema: Mapping[str, Any]) -> bytes:
    return (json.dumps(dict(schema), ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


def _freeze(value: Any) -> Any:
    """Return a recursive immutable snapshot without retaining caller-owned containers."""

    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze(item) for item in value)
    return copy.deepcopy(value)


def _fragment(plugin_id: str, model: type[BaseModel]) -> PluginConfigFragment:
    schema = model.model_json_schema(by_alias=True, mode="validation")
    schema["$id"] = _artifact_id(plugin_id)
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["version"] = 1
    # Construction and JSON Schema validation are deliberately both checked: a
    # model default must remain valid when exposed as a published fragment.
    defaults = model().model_dump(mode="json", by_alias=True)
    Draft202012Validator(schema).validate(defaults)
    return PluginConfigFragment(
        plugin_id,
        f"plugins.{plugin_id}",
        _artifact_id(plugin_id),
        1,
        MappingProxyType(schema),
        hashlib.sha256(_schema_bytes(schema)).hexdigest(),
    )


def builtin_plugin_fragments() -> tuple[PluginConfigFragment, ...]:
    """Return all checked built-in fragments in canonical namespace order."""

    return tuple(
        sorted(
            (_fragment(plugin_id, model) for plugin_id, model in _FRAGMENT_MODELS.items()),
            key=lambda fragment: fragment.namespace,
        )
    )


def fragment_schema_bytes(fragment: PluginConfigFragment) -> bytes:
    """Return stable publication bytes for one fragment."""

    return _schema_bytes(fragment.schema)


def _diagnostic(code: str, plugin_id: str, path: str) -> FragmentDiagnostic:
    return FragmentDiagnostic(code, plugin_id, path)


def _namespace_conflicts(manifests: Iterable[ManifestLike]) -> tuple[FragmentDiagnostic, ...]:
    rows = sorted(
        ((manifest.plugin_id, manifest.configuration_namespace) for manifest in manifests),
        key=lambda row: (row[1], row[0]),
    )
    diagnostics: list[FragmentDiagnostic] = []
    for index, (plugin_id, namespace) in enumerate(rows):
        for other_id, other_namespace in rows[index + 1 :]:
            if namespace == other_namespace or other_namespace.startswith(namespace + "."):
                diagnostics.append(_diagnostic("duplicate_namespace", plugin_id, namespace))
                diagnostics.append(_diagnostic("duplicate_namespace", other_id, other_namespace))
    return tuple(diagnostics)


def _duplicate_plugin_ids(manifests: Iterable[ManifestLike]) -> tuple[FragmentDiagnostic, ...]:
    """Reject every duplicate ID before a mapping could make one silently win."""

    counts: dict[str, int] = {}
    for manifest in manifests:
        counts[manifest.plugin_id] = counts.get(manifest.plugin_id, 0) + 1
    return tuple(
        _diagnostic("duplicate_plugin_id", plugin_id, f"plugins.{plugin_id}")
        for plugin_id in sorted(plugin_id for plugin_id, count in counts.items() if count > 1)
    )


def _leaf_paths(value: Any, prefix: str) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return (prefix,)
    paths: list[str] = []
    for key in sorted(value):
        if not isinstance(key, str):
            continue
        paths.extend(_leaf_paths(value[key], f"{prefix}.{key}"))
    return tuple(paths)


def _persisted_source_paths(
    field_path: str,
    canonical: Mapping[str, Any] | None,
    legacy: Mapping[str, Any] | None,
    plugin_id: str,
) -> tuple[str, ...]:
    parts = field_path.split(".")
    paths: list[str] = []
    for root, source in ((f"plugins.{plugin_id}", canonical), (plugin_id, legacy)):
        cursor: Any = source
        for part in parts:
            if not isinstance(cursor, Mapping) or part not in cursor:
                break
            cursor = cursor[part]
        else:
            paths.append(f"{root}.{field_path}")
    return tuple(sorted(paths))


def _provenance_for(
    fragment: PluginConfigFragment,
    manifest: ManifestLike,
    value: Mapping[str, Any],
    canonical: Mapping[str, Any] | None,
    legacy: Mapping[str, Any] | None,
    source_layer: SourceLayer,
) -> dict[str, FragmentValueProvenance]:
    records: dict[str, FragmentValueProvenance] = {}
    for dotted in _leaf_paths(value, ""):
        field_path = dotted.removeprefix(".")
        source_paths = _persisted_source_paths(field_path, canonical, legacy, fragment.plugin_id)
        records[f"{fragment.namespace}.{field_path}"] = FragmentValueProvenance(
            plugin_id=fragment.plugin_id,
            plugin_version=manifest.plugin_version,
            namespace=fragment.namespace,
            manifest_provenance=manifest.provenance,
            schema_artifact=fragment.artifact_id,
            schema_digest=fragment.digest,
            fragment_major=fragment.contract_version,
            source_paths=source_paths,
            source_layer=source_layer if source_paths else SourceLayer.DEFAULT,
        )
    return records


def compose_plugin_config(
    document: Mapping[str, Any],
    manifests: Iterable[ManifestLike],
    *,
    available_plugin_ids: Iterable[str],
    disabled_plugin_ids: Iterable[str] = (),
    source_layer: SourceLayer,
) -> ComposedPluginConfig:
    """Compose available manifest fragments while retaining every inactive subtree.

    ``available_plugin_ids`` is supplied by the bootstrap/discovery composition
    root after installation and enablement checks.  The function deliberately
    does not discover, install, or import plugins itself.
    """

    fragments = {fragment.plugin_id: fragment for fragment in builtin_plugin_fragments()}
    manifests = tuple(manifests)
    if not isinstance(source_layer, SourceLayer):
        raise TypeError("source_layer must be a SourceLayer")
    available = frozenset(available_plugin_ids)
    disabled = frozenset(disabled_plugin_ids)
    diagnostics = list(_namespace_conflicts(manifests)) + list(_duplicate_plugin_ids(manifests))
    invalid = {item.plugin_id for item in diagnostics}
    declared = {
        manifest.plugin_id: manifest for manifest in manifests if manifest.plugin_id not in invalid
    }
    plugins = document.get("plugins", {})
    if not isinstance(plugins, Mapping):
        return ComposedPluginConfig(
            _freeze({}),
            _freeze({}),
            _freeze({}),
            (_diagnostic("invalid_config", "plugins", "plugins"),),
        )

    active: dict[str, Mapping[str, Any]] = {}
    retained: dict[str, Any] = {}
    provenance: dict[str, FragmentValueProvenance] = {}
    for plugin_id, fragment in fragments.items():
        canonical_present = plugin_id in plugins
        legacy_present = plugin_id in document
        if not canonical_present and not legacy_present:
            continue
        canonical = plugins.get(plugin_id)
        legacy = document.get(plugin_id)
        if canonical_present and legacy_present and canonical != legacy:
            retained[plugin_id] = _freeze(canonical)
            diagnostics.append(_diagnostic("legacy_conflict", plugin_id, fragment.namespace))
            continue
        raw = canonical if canonical_present else legacy
        canonical_mapping = canonical if isinstance(canonical, Mapping) else None
        legacy_mapping = legacy if isinstance(legacy, Mapping) else None
        manifest = declared.get(plugin_id)
        if manifest is None:
            retained[plugin_id] = _freeze(raw)
            diagnostics.append(_diagnostic("unavailable_plugin", plugin_id, fragment.namespace))
            continue
        if (
            manifest.configuration_namespace != fragment.namespace
            or manifest.configuration_schema_artifact != fragment.artifact_id
        ):
            retained[plugin_id] = _freeze(raw)
            invalid.add(plugin_id)
            diagnostics.append(
                _diagnostic("incompatible_fragment_version", plugin_id, fragment.namespace)
            )
            continue
        if not isinstance(raw, Mapping):
            retained[plugin_id] = _freeze(raw)
            diagnostics.append(_diagnostic("invalid_config", plugin_id, fragment.namespace))
            continue
        try:
            value = _FRAGMENT_MODELS[plugin_id].model_validate(raw).model_dump(mode="python")
        except ValidationError:
            retained[plugin_id] = _freeze(raw)
            diagnostics.append(_diagnostic("invalid_config", plugin_id, fragment.namespace))
            continue
        records = _provenance_for(
            fragment,
            manifest,
            value,
            canonical_mapping,
            legacy_mapping,
            source_layer,
        )
        if plugin_id in disabled:
            retained[plugin_id] = _freeze(raw)
            provenance.update(records)
            diagnostics.append(_diagnostic("disabled_plugin", plugin_id, fragment.namespace))
            continue
        if plugin_id not in available or plugin_id in invalid:
            retained[plugin_id] = _freeze(raw)
            diagnostics.append(_diagnostic("unavailable_plugin", plugin_id, fragment.namespace))
            continue
        active[plugin_id] = _freeze(value)
        provenance.update(records)

    for plugin_id in sorted(set(plugins) - set(fragments)):
        retained[plugin_id] = _freeze(plugins[plugin_id])
        diagnostics.append(_diagnostic("unknown_plugin_config", plugin_id, f"plugins.{plugin_id}"))

    return ComposedPluginConfig(
        _freeze(active),
        _freeze(retained),
        _freeze(provenance),
        tuple(sorted(diagnostics, key=lambda item: (item.code, item.plugin_id, item.path))),
    )


def compose_config_json_schema(core_schema: Mapping[str, Any]) -> dict[str, Any]:
    """Add declared plugin namespaces to the published core config schema only."""

    schema = copy.deepcopy(dict(core_schema))
    fragments = builtin_plugin_fragments()
    schema["properties"]["plugins"] = {
        "type": "object",
        "description": "Plugin-owned configuration fragments retained independently of activation.",
        "properties": {
            # Inline the checked fragment projection as well as publishing it
            # separately.  The wire compatibility checker intentionally accepts
            # only local references, and this keeps the composed artifact usable
            # by ordinary JSON Schema consumers without a registry resolver.
            fragment.plugin_id: dict(fragment.schema)
            for fragment in fragments
        },
        # Unknown namespaces are preserved as opaque inactive data by policy.
        "additionalProperties": True,
    }
    return schema


__all__ = (
    "ComposedPluginConfig",
    "FragmentDiagnostic",
    "FragmentValueProvenance",
    "PluginConfigManifest",
    "PluginConfigFragment",
    "TrustedManifestProvenance",
    "builtin_plugin_fragments",
    "compose_config_json_schema",
    "compose_plugin_config",
    "fragment_schema_bytes",
)
