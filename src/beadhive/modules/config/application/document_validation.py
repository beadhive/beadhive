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

from ....hive_schema_contracts import HiveSchemaRecord
from ....host_manifest_contracts import HostManifest
from ..contracts import SCHEMA_VERSION, BeadhiveConfig, iter_schema_fields
from ..domain.ports import FleetConfigDocument
from .resolution import ConfigResolutionError, ResolutionInputs, resolve_config

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
    # The schema is private to this module; no caller receives a mutable copy.
    return Draft202012Validator(BeadhiveConfig.model_json_schema())


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
    for violation in _settings_validator().iter_errors(document):
        path = _safe_path(violation.absolute_path)
        raise DocumentValidationError(scope, path, f"schema_{violation.validator}") from None
    try:
        if scope == "fleet":
            resolve_config(ResolutionInputs(fleet=document))
        else:
            resolve_config(ResolutionInputs(host=document))
    except ConfigResolutionError as exc:
        first = exc.diagnostics[0]
        raise DocumentValidationError(
            scope, _safe_path(first.path.split(".")), first.code
        ) from None


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
        try:
            manifest = HostManifest.model_validate(raw)
        except ValidationError as exc:
            raise _type_failure("host", exc) from None
        if manifest.host_id != match.group(1):
            raise DocumentValidationError("host", "host_id", "path_identity_mismatch")
        return
    if match := _HIVE_PATH.fullmatch(path):
        raw = _parse_yaml(content, "hive")
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
        from ....signer_policy import SignerPolicyError, validate_allowed_signers

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


__all__ = (
    "DocumentValidationError",
    "validate_document",
    "validate_documents",
    "validate_settings_mapping",
)
