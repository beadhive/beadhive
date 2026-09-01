"""Pure configuration layering and typed, value-free provenance."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from pydantic import ValidationError

from ..contracts import SCHEMA_VERSION, BeadhiveConfig
from .partition import FLEET, is_host_overridable, partition_of


class SourceLayer(StrEnum):
    DEFAULT = "default"
    FLEET = "fleet"
    HOST = "host"
    HIVE = "hive"
    ENVIRONMENT = "environment"
    RUNTIME = "runtime"


@dataclass(frozen=True, slots=True)
class ValueProvenance:
    """Origin of one resolved leaf. Values are deliberately not representable."""

    layer: SourceLayer
    source_key: str


@dataclass(frozen=True, slots=True)
class ResolutionDiagnostic:
    """Secret-safe structured failure information."""

    code: str
    path: str
    layer: SourceLayer
    owner: str = "core"
    schema_version: int = SCHEMA_VERSION

    def render(self) -> str:
        return (
            f"{self.code}: {self.path} "
            f"(owner={self.owner}, layer={self.layer.value}, schema={self.schema_version})"
        )


class ConfigResolutionError(ValueError):
    """A typed configuration view could not be resolved safely."""

    def __init__(self, diagnostics: tuple[ResolutionDiagnostic, ...]):
        self.diagnostics = diagnostics
        super().__init__("; ".join(diagnostic.render() for diagnostic in diagnostics))


@dataclass(frozen=True, slots=True)
class ResolutionInputs:
    """All resolution sources, supplied explicitly in precedence order."""

    fleet: Mapping[str, Any] = field(default_factory=dict)
    host: Mapping[str, Any] = field(default_factory=dict)
    hive: Mapping[str, Any] = field(default_factory=dict)
    environment: Mapping[str, Any] = field(default_factory=dict)
    runtime: Mapping[str, Any] = field(default_factory=dict)
    runtime_allowed_paths: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True, slots=True)
class ResolvedConfig:
    """Canonical typed snapshot plus immutable provenance by dotted leaf."""

    settings: BeadhiveConfig
    provenance: Mapping[str, ValueProvenance]


class _PureBeadhiveConfig(BeadhiveConfig):
    """Validate the canonical contract without consulting ambient settings sources."""

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        del cls, settings_cls, env_settings, dotenv_settings, file_secret_settings
        return (init_settings,)


def _leaf_items(node: Any, prefix: str = ""):
    if isinstance(node, Mapping):
        for key, value in node.items():
            dotted = f"{prefix}.{key}" if prefix else str(key)
            yield from _leaf_items(value, dotted)
    elif prefix:
        yield prefix, node


def deep_merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(dict(base))
    for key, value in overlay.items():
        current = merged.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            merged[key] = deep_merge(current, value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _allows(path: str, allowlist: frozenset[str]) -> bool:
    return any(path == allowed or path.startswith(allowed + ".") for allowed in allowlist)


_MISSING_SCHEMA_VERSION = object()


def _schema_version_diagnostic(
    document: Mapping[str, Any], layer: SourceLayer
) -> ResolutionDiagnostic | None:
    found = document.get("schema_version", _MISSING_SCHEMA_VERSION)
    if found is _MISSING_SCHEMA_VERSION:
        return None
    if type(found) is not int:
        return ResolutionDiagnostic(
            code="invalid_schema_version",
            path="schema_version",
            layer=layer,
        )
    if found > SCHEMA_VERSION:
        return ResolutionDiagnostic(
            code="future_schema_version",
            path="schema_version",
            layer=layer,
            schema_version=found,
        )
    return None


def ensure_supported_schema_version(document: Mapping[str, Any], layer: SourceLayer) -> None:
    """Refuse a future document without including document values in the error."""

    diagnostic = _schema_version_diagnostic(document, layer)
    if diagnostic is not None:
        raise ConfigResolutionError((diagnostic,))


def _defaults() -> dict[str, Any]:
    return _PureBeadhiveConfig().model_dump(mode="python", by_alias=True)


def _record_layer(
    provenance: dict[str, ValueProvenance],
    overlay: Mapping[str, Any],
    layer: SourceLayer,
) -> None:
    for path, _value in _leaf_items(overlay):
        for existing in tuple(provenance):
            same_branch = existing.startswith(path + ".") or path.startswith(existing + ".")
            if existing == path or same_branch:
                del provenance[existing]
        provenance[path] = ValueProvenance(layer=layer, source_key=f"{layer.value}.{path}")


def _host_override_diagnostics(host: Mapping[str, Any]) -> tuple[ResolutionDiagnostic, ...]:
    return tuple(
        ResolutionDiagnostic(code="forbidden_host_override", path=path, layer=SourceLayer.HOST)
        for path, _value in _leaf_items(host)
        if partition_of(path) == FLEET and not is_host_overridable(path)
    )


def _runtime_diagnostics(inputs: ResolutionInputs) -> tuple[ResolutionDiagnostic, ...]:
    diagnostics = []
    for path, _value in _leaf_items(inputs.runtime):
        if path == "schema_version" or partition_of(path) == FLEET:
            code = "forbidden_runtime_override"
        elif not _allows(path, inputs.runtime_allowed_paths):
            code = "undeclared_runtime_override"
        else:
            continue
        diagnostics.append(ResolutionDiagnostic(code=code, path=path, layer=SourceLayer.RUNTIME))
    return tuple(diagnostics)


def resolve_config(inputs: ResolutionInputs) -> ResolvedConfig:
    """Resolve explicit sources as default < fleet < host < hive < environment < runtime."""

    for document, layer in (
        (inputs.fleet, SourceLayer.FLEET),
        (inputs.host, SourceLayer.HOST),
        (inputs.hive, SourceLayer.HIVE),
        (inputs.environment, SourceLayer.ENVIRONMENT),
        (inputs.runtime, SourceLayer.RUNTIME),
    ):
        ensure_supported_schema_version(document, layer)

    diagnostics = _host_override_diagnostics(inputs.host) if inputs.fleet else ()
    diagnostics += _runtime_diagnostics(inputs)
    if diagnostics:
        raise ConfigResolutionError(diagnostics)

    resolved = _defaults()
    provenance: dict[str, ValueProvenance] = {}
    _record_layer(provenance, resolved, SourceLayer.DEFAULT)
    for overlay, layer in (
        (inputs.fleet, SourceLayer.FLEET),
        (inputs.host, SourceLayer.HOST),
        (inputs.hive, SourceLayer.HIVE),
        (inputs.environment, SourceLayer.ENVIRONMENT),
        (inputs.runtime, SourceLayer.RUNTIME),
    ):
        resolved = deep_merge(resolved, overlay)
        _record_layer(provenance, overlay, layer)

    try:
        validated = _PureBeadhiveConfig(**resolved)
    except ValidationError as exc:
        safe = tuple(
            ResolutionDiagnostic(
                code=f"validation_{error['type']}",
                path=".".join(str(part) for part in error["loc"]),
                layer=provenance.get(
                    ".".join(str(part) for part in error["loc"]),
                    ValueProvenance(SourceLayer.DEFAULT, "default"),
                ).layer,
            )
            for error in exc.errors(include_input=False, include_context=False, include_url=False)
        )
        raise ConfigResolutionError(safe) from None

    settings = BeadhiveConfig.model_construct(
        _fields_set=validated.model_fields_set,
        **validated.__dict__,
    )
    return ResolvedConfig(settings=settings, provenance=MappingProxyType(provenance))


__all__ = (
    "ConfigResolutionError",
    "ResolvedConfig",
    "ResolutionDiagnostic",
    "ResolutionInputs",
    "SourceLayer",
    "ValueProvenance",
    "deep_merge",
    "ensure_supported_schema_version",
    "resolve_config",
)
