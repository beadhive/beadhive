"""Pure validation of persisted HQ documents before publication and on read.

The settings JSON Schema is generated from the canonical Pydantic contract. It
checks raw wire types before Pydantic's settings coercion; pure resolution then
checks field and cross-field policy without consulting ambient settings sources.
No validated document is rewritten or normalized here.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from functools import lru_cache
from typing import Any

from jsonschema import Draft202012Validator
from pydantic import ValidationError
from ruamel.yaml import YAML

from .complexity import tier_names
from .hive_schema_contracts import HiveSchemaRecord
from .host_manifest_contracts import DEPRECATED_ROLE_ALIASES, HostManifest
from .modules.config.application.resolution import (
    ConfigResolutionError,
    ResolutionInputs,
    resolve_config,
)
from .modules.config.contracts import (
    SCHEMA_VERSION,
    BeadhiveConfig,
    LegacyBeadsConfig,
    ManagedRepoEntry,
    RoutingTierConfig,
    iter_schema_fields,
)
from .modules.config.domain.ports import FleetConfigDocument

_HOST_PATH = re.compile(r"hosts/([A-Za-z0-9_-]+)\.yaml\Z")
_HIVE_PATH = re.compile(r"hives/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)\.yaml\Z")
_WORKSPACE_PATH = re.compile(r"workspace(?:-[A-Za-z0-9_-]+)?\.toml\Z")
_PROVIDERS = frozenset({"github", "gitlab", "gitea"})
_PROVIDER_STRINGS = frozenset({"name", "path", "url", "env_var"})
_PROVIDER_LISTS = frozenset({"include", "exclude"})


class DocumentValidationError(ValueError):
    """A value-free, path-specific failure of a persisted document contract."""

    def __init__(self, kind: str, path: str, code: str):
        self.kind, self.path, self.code = kind, path, code
        super().__init__(f"{kind} document invalid at {path}: {code}")


@lru_cache(maxsize=1)
def _settings_validator() -> Draft202012Validator:
    # The official v1 JSON Schema is a published compatibility artifact. Its
    # historical before-validators advertised the *output* enum shape, while
    # persisted YAML supplies canonical tier names and legacy empty literals.
    # Derive only those input exceptions from the canonical validators for this
    # private raw-input gate; leave the public wire schema unchanged.
    schema = BeadhiveConfig.model_json_schema()
    routing = schema["$defs"]["RoutingTierConfig"]["properties"]
    tier_fields = RoutingTierConfig.__pydantic_decorators__.field_validators[
        "_canonical_complexity_tier"
    ].info.fields
    for field in tier_fields:
        routing[field] = {"type": "string", "enum": list(tier_names())}
    repo = schema["$defs"]["ManagedRepoEntry"]["properties"]
    empty_fields = ManagedRepoEntry.__pydantic_decorators__.field_validators[
        "_empty_string_is_unset"
    ].info.fields
    for field in empty_fields:
        repo[field]["anyOf"].append({"const": ""})
    beads = schema["$defs"]["LegacyBeadsConfig"]["properties"]
    legacy_engine_fields = LegacyBeadsConfig.__pydantic_decorators__.field_validators[
        "_empty_engine_is_default"
    ].info.fields
    for field in legacy_engine_fields:
        beads[field]["anyOf"].append({"const": ""})
    return Draft202012Validator(schema)


@lru_cache(maxsize=1)
def _host_validator() -> Draft202012Validator:
    schema = HostManifest.model_json_schema()
    # The canonical before-validator still accepts these explicitly deprecated
    # spellings so existing enrolled hosts remain readable until re-recorded.
    schema["properties"]["role"]["enum"].extend(DEPRECATED_ROLE_ALIASES)
    return Draft202012Validator(schema)


@lru_cache(maxsize=1)
def _hive_validator() -> Draft202012Validator:
    return Draft202012Validator(HiveSchemaRecord.model_json_schema())


def _check_raw_schema(kind: str, validator: Draft202012Validator, raw: Mapping) -> None:
    for violation in validator.iter_errors(raw):
        raise DocumentValidationError(
            kind, _safe_path(violation.absolute_path), f"schema_{violation.validator}"
        ) from None


def _type_failure(kind: str, exc: ValidationError) -> DocumentValidationError:
    first = exc.errors(include_input=False, include_context=False, include_url=False)[0]
    path = _safe_path(first["loc"])
    return DocumentValidationError(kind, path, f"validation_{first['type']}")


@lru_cache(maxsize=1)
def _known_path_parts() -> frozenset[str]:
    return (
        frozenset(
            part.removesuffix("[]")
            for field in iter_schema_fields()
            for part in field.path.split(".")
        )
        | frozenset(HostManifest.model_fields)
        | frozenset(HiveSchemaRecord.model_fields)
    )


def _safe_path(parts) -> str:
    # Dynamic map keys and array values come from operator input and can carry
    # credentials. Only canonical field names may appear in diagnostics.
    return (
        ".".join(
            str(part) if isinstance(part, str) and part in _known_path_parts() else "<item>"
            for part in parts
        )
        or "<root>"
    )


def validate_settings_mapping(document: Mapping[str, Any], *, scope: str) -> None:
    """Validate a HOST or FLEET fragment without env, credentials, or HOST bootstrap.

    A missing version is legacy input, checked against today's full canonical
    contract without adding a stamp. Every explicitly declared version must be
    the current positive integer, including when it appears in a HOST fragment.
    """

    if scope not in {"host", "fleet"}:
        raise ValueError("settings scope must be host or fleet")
    if not isinstance(document, Mapping):
        raise DocumentValidationError(scope, "<root>", "mapping_required")
    if "schema_version" in document:
        version = document["schema_version"]
        if type(version) is not int or version != SCHEMA_VERSION:
            raise DocumentValidationError(scope, "schema_version", "unsupported_schema_version")
    _check_raw_schema(scope, _settings_validator(), document)
    try:
        if scope == "fleet":
            resolve_config(ResolutionInputs(fleet=document))
        else:
            resolve_config(ResolutionInputs(host=document))
    except ConfigResolutionError as exc:
        first = exc.diagnostics[0]
        # Keep the established actionable hint for this canonical cross-field
        # rejection without rendering the rejected root path or other values.
        code = first.code
        if first.path == "git_workspace" and code == "validation_value_error":
            code = "internal_root_forbidden_use_BH_HOME"
        raise DocumentValidationError(scope, _safe_path(first.path.split(".")), code) from None


def _parse_yaml(content: str, kind: str) -> Mapping[str, Any]:
    try:
        yaml = YAML(typ="safe", pure=True)
        yaml.allow_duplicate_keys = False
        parsed = yaml.load(content)
    except Exception:
        raise DocumentValidationError(kind, "<root>", "yaml_syntax") from None
    if not isinstance(parsed, Mapping):
        raise DocumentValidationError(kind, "<root>", "mapping_required")
    return parsed


def _validate_workspace(content: str) -> None:
    try:
        parsed = tomllib.loads(content)
    except (ValueError, UnicodeError):
        raise DocumentValidationError("workspace", "<root>", "toml_syntax") from None
    providers = parsed.get("provider", [])
    if not isinstance(providers, list):
        raise DocumentValidationError("workspace", "provider", "array_required")
    for index, entry in enumerate(providers):
        path = f"provider.{index}"
        if not isinstance(entry, dict):
            raise DocumentValidationError("workspace", path, "mapping_required")
        if not isinstance(entry.get("provider"), str) or entry["provider"] not in _PROVIDERS:
            raise DocumentValidationError("workspace", path + ".provider", "unknown_provider")
        for key in ("name", "path"):
            if key not in entry:
                raise DocumentValidationError("workspace", path + "." + key, "required")
        for key in _PROVIDER_STRINGS & entry.keys():
            if not isinstance(entry[key], str):
                raise DocumentValidationError("workspace", path + "." + key, "string_required")
        for key in _PROVIDER_LISTS & entry.keys():
            if not isinstance(entry[key], list) or any(
                not isinstance(item, str) for item in entry[key]
            ):
                raise DocumentValidationError(
                    "workspace", path + "." + key, "string_array_required"
                )
        bool_keys = {"auth_http"}
        if entry["provider"] in {"github", "gitea"}:
            bool_keys.add("skip_forks")
        for key in bool_keys & entry.keys():
            if type(entry[key]) is not bool:
                raise DocumentValidationError("workspace", path + "." + key, "bool_required")
        # Unknown keys are opaque and ignored by pinned git-workspace 1.10.1.


def validate_document(path: str, content: str) -> None:
    """Validate one canonical HQ document without changing its content."""

    if not isinstance(path, str) or not isinstance(content, str):
        raise DocumentValidationError("document", "<root>", "path_or_content_type")
    if path == "fleet.yaml":
        validate_settings_mapping(_parse_yaml(content, "fleet"), scope="fleet")
        return
    if match := _HOST_PATH.fullmatch(path):
        raw = dict(_parse_yaml(content, "host"))
        # Existing hosts.load and fleet_roster.load read an unstamped legacy
        # manifest as active, including older pre-frame records.
        raw.setdefault("state", "active")
        _check_raw_schema("host", _host_validator(), raw)
        try:
            manifest = HostManifest.model_validate(raw)
        except ValidationError as exc:
            raise _type_failure("host", exc) from None
        if manifest.host_id != match.group(1):
            raise DocumentValidationError("host", "host_id", "path_identity_mismatch")
        return
    if match := _HIVE_PATH.fullmatch(path):
        raw = _parse_yaml(content, "hive")
        _check_raw_schema("hive", _hive_validator(), raw)
        try:
            record = HiveSchemaRecord.model_validate(raw)
        except ValidationError as exc:
            raise _type_failure("hive", exc) from None
        if (record.provider, record.org, record.repo) != match.groups():
            raise DocumentValidationError("hive", "identity", "path_identity_mismatch")
        return
    if _WORKSPACE_PATH.fullmatch(path):
        _validate_workspace(content)
        return
    if path == "allowed_signers":
        from .signer_policy import SignerPolicyError, validate_allowed_signers

        try:
            validate_allowed_signers(content)
        except SignerPolicyError as exc:
            raise DocumentValidationError("allowed_signers", f"line.{exc.line}", exc.code) from None
        return
    raise DocumentValidationError("document", "<root>", "unsupported_path")


def validate_documents(documents: tuple[FleetConfigDocument, ...]) -> None:
    """Validate every ordered raw document; never derive a replacement snapshot."""

    if not isinstance(documents, tuple):
        raise DocumentValidationError("snapshot", "<root>", "tuple_required")
    seen: set[str] = set()
    for document in documents:
        if not isinstance(document, FleetConfigDocument):
            raise DocumentValidationError("snapshot", "<root>", "document_required")
        if not isinstance(document.path, str):
            raise DocumentValidationError("snapshot", "<root>", "document_path_type")
        if document.path in seen:
            raise DocumentValidationError("snapshot", "<root>", "duplicate_path")
        seen.add(document.path)
        validate_document(document.path, document.content)
    if "fleet.yaml" not in seen:
        raise DocumentValidationError("snapshot", "fleet.yaml", "required")


def validate_repair_carrier(documents: tuple[FleetConfigDocument, ...]) -> None:
    """Check a privileged opaque repair view without granting settings validity.

    A malformed declared schema may be repaired, but the carrier still has to
    be bounded, parseable, on an allowed path, and free of embedded credentials.
    The bytes and order are left untouched.
    """
    if not isinstance(documents, tuple) or not documents:
        raise DocumentValidationError("snapshot", "<root>", "tuple_required")
    seen: set[str] = set()
    size = 0

    def reject_secrets(node: Any) -> None:
        if isinstance(node, Mapping):
            for key, value in node.items():
                if re.fullmatch(
                    r"(?i)(?:password|secret|token|api[_-]?key|private[_-]?key|credential)",
                    str(key),
                ):
                    raise DocumentValidationError("snapshot", "<root>", "credential_key")
                reject_secrets(value)
        elif isinstance(node, list):
            for item in node:
                reject_secrets(item)
        elif isinstance(node, str) and re.search(r"://[^/@\s]+:[^/@\s]+@", node):
            raise DocumentValidationError("snapshot", "<root>", "credential_uri")

    for document in documents:
        if not isinstance(document, FleetConfigDocument) or not isinstance(document.content, str):
            raise DocumentValidationError("snapshot", "<root>", "document_required")
        path = document.path
        if not isinstance(path, str) or path in seen:
            raise DocumentValidationError("snapshot", "<root>", "duplicate_or_invalid_path")
        seen.add(path)
        size += len(path.encode("utf-8")) + len(document.content.encode("utf-8"))
        if size > 4 * 1024 * 1024:
            raise DocumentValidationError("snapshot", "<root>", "size_bound")
        if path == "allowed_signers":
            if "PRIVATE KEY" in document.content:
                raise DocumentValidationError("snapshot", "<root>", "private_key")
            continue
        if _WORKSPACE_PATH.fullmatch(path):
            try:
                parsed = tomllib.loads(document.content)
            except (ValueError, UnicodeError):
                raise DocumentValidationError("snapshot", "<root>", "toml_syntax") from None
        elif path == "fleet.yaml" or _HOST_PATH.fullmatch(path) or _HIVE_PATH.fullmatch(path):
            parsed = _parse_yaml(document.content, "snapshot")
        else:
            raise DocumentValidationError("snapshot", "<root>", "unsupported_path")
        reject_secrets(parsed)
    if "fleet.yaml" not in seen:
        raise DocumentValidationError("snapshot", "fleet.yaml", "required")


__all__ = (
    "DocumentValidationError",
    "validate_document",
    "validate_documents",
    "validate_repair_carrier",
    "validate_settings_mapping",
)
