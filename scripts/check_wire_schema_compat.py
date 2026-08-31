#!/usr/bin/env python3
"""Validate published wire releases and reject same-major compatibility breaks.

The candidate is the working tree. The baseline is the merge base with the CI target branch
(`BH_WIRE_SCHEMA_BASE_REF`, default `main`). Published release directories present at the base
are immutable; a new release is compared with the base's latest release when both share a major.
JSON Schema artifacts use full schema compatibility; operation-catalog data uses append-only
semantic compatibility after validation against its separately manifested schema.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from jsonschema import Draft202012Validator

WIRE_INDEX = Path("docs/schemas/wire/index.json")
SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
SCHEMA_ARTIFACT_PREFIX = "urn:beadhive:wire-schema:"
OPERATION_CATALOG_ARTIFACT_ID = "urn:beadhive:wire-catalog:operations:1"
OPERATION_CATALOG_SCHEMA_ID = "urn:beadhive:wire-schema:operation-catalog:1"
JSON_SCHEMA_ARTIFACT = "json-schema"
CATALOG_DATA_ARTIFACT = "operation-catalog-data"


class Reader(Protocol):
    def read(self, path: Path) -> bytes: ...


@dataclass(frozen=True)
class FilesystemReader:
    root: Path

    def read(self, path: Path) -> bytes:
        return (self.root / path).read_bytes()


@dataclass(frozen=True)
class GitReader:
    root: Path
    commit: str

    def read(self, path: Path) -> bytes:
        result = subprocess.run(
            ["git", "show", f"{self.commit}:{path.as_posix()}"],
            cwd=self.root,
            check=True,
            capture_output=True,
        )
        return result.stdout


@dataclass(frozen=True)
class Artifact:
    artifact_id: str
    contract_version: int
    path: Path
    document: dict[str, Any]
    artifact_type: str

    @property
    def schema(self) -> dict[str, Any]:
        if self.artifact_type != JSON_SCHEMA_ARTIFACT:
            raise TypeError(f"{self.artifact_id} is {self.artifact_type}, not JSON Schema")
        return self.document


@dataclass(frozen=True)
class Release:
    version: str
    major: int
    manifest_path: Path
    files: tuple[Path, ...]
    artifacts: dict[str, Artifact]


@dataclass(frozen=True)
class RepositoryRelease:
    latest: Release
    releases: dict[str, Release]


def _load_json(reader: Reader, path: Path) -> Any:
    try:
        return json.loads(reader.read(path))
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot load {path}: {exc}") from exc


def _artifact_type(artifact_id: str) -> str:
    if artifact_id.startswith(SCHEMA_ARTIFACT_PREFIX):
        return JSON_SCHEMA_ARTIFACT
    if artifact_id == OPERATION_CATALOG_ARTIFACT_ID:
        return CATALOG_DATA_ARTIFACT
    raise ValueError(f"unsupported artifact id/type: {artifact_id!r}")


def load_repository(reader: Reader) -> RepositoryRelease:
    index = _load_json(reader, WIRE_INDEX)
    if index.get("format_version") != 1:
        raise ValueError("wire index format_version must be 1")
    entries = index.get("releases")
    if not isinstance(entries, list) or not entries:
        raise ValueError("wire index must contain at least one release")

    releases: dict[str, Release] = {}
    for entry in entries:
        version = str(entry.get("version", ""))
        match = SEMVER.fullmatch(version)
        if not match:
            raise ValueError(f"invalid release version: {version!r}")
        major = int(match.group(1))
        if entry.get("major") != major:
            raise ValueError(f"release {version}: major does not match semver")
        if version in releases:
            raise ValueError(f"duplicate release version: {version}")
        manifest_path = WIRE_INDEX.parent / str(entry.get("manifest", ""))
        manifest = _load_json(reader, manifest_path)
        if manifest.get("format_version") != 1:
            raise ValueError(f"{manifest_path}: format_version must be 1")
        if manifest.get("release_version") != version:
            raise ValueError(f"{manifest_path}: release_version must be {version}")

        release_dir = manifest_path.parent
        artifacts: dict[str, Artifact] = {}
        files = [manifest_path]
        manifest_artifacts = manifest.get("artifacts")
        if not isinstance(manifest_artifacts, list) or not manifest_artifacts:
            raise ValueError(f"{manifest_path}: artifacts must be a non-empty list")
        for item in manifest_artifacts:
            artifact_id = str(item.get("id", ""))
            artifact_path = release_dir / str(item.get("path", ""))
            document = _load_json(reader, artifact_path)
            artifact_type = _artifact_type(artifact_id)
            if artifact_type == JSON_SCHEMA_ARTIFACT:
                Draft202012Validator.check_schema(document)
            if document.get("$id") != artifact_id:
                raise ValueError(f"{artifact_path}: $id does not match manifest id")
            if artifact_id in artifacts:
                raise ValueError(f"{manifest_path}: duplicate artifact id {artifact_id}")
            contract_version = item.get("contract_version")
            if not isinstance(contract_version, int) or contract_version < 1:
                raise ValueError(f"{manifest_path}: invalid contract_version for {artifact_id}")
            if contract_version != major:
                raise ValueError(
                    f"{manifest_path}: {artifact_id} contract_version must match release major"
                )
            artifacts[artifact_id] = Artifact(
                artifact_id,
                contract_version,
                artifact_path,
                document,
                artifact_type,
            )
            files.append(artifact_path)

        for artifact in artifacts.values():
            if artifact.artifact_type != CATALOG_DATA_ARTIFACT:
                continue
            catalog_schema = artifacts.get(OPERATION_CATALOG_SCHEMA_ID)
            if catalog_schema is None or catalog_schema.artifact_type != JSON_SCHEMA_ARTIFACT:
                raise ValueError(
                    f"{artifact.path}: catalog data requires schema artifact "
                    f"{OPERATION_CATALOG_SCHEMA_ID}"
                )
            validation_errors = sorted(
                Draft202012Validator(catalog_schema.schema).iter_errors(artifact.document),
                key=lambda error: tuple(str(part) for part in error.absolute_path),
            )
            if validation_errors:
                error = validation_errors[0]
                location = "$" + "".join(
                    f"[{part}]" if isinstance(part, int) else f".{part}"
                    for part in error.absolute_path
                )
                raise ValueError(f"{artifact.path} {location}: {error.message}")

        fixture_path = release_dir / str(manifest.get("conformance_fixtures", ""))
        fixtures = _load_json(reader, fixture_path)
        files.append(fixture_path)
        _validate_fixtures(fixtures, version, artifacts, fixture_path)
        releases[version] = Release(version, major, manifest_path, tuple(files), artifacts)

    latest_version = str(index.get("latest", ""))
    if latest_version not in releases:
        raise ValueError("wire index latest must name a listed release")
    if _semver_tuple(latest_version) != max(map(_semver_tuple, releases)):
        raise ValueError("wire index latest must name the greatest listed semver")
    return RepositoryRelease(releases[latest_version], releases)


def _validate_fixtures(
    fixtures: dict[str, Any],
    release_version: str,
    artifacts: dict[str, Artifact],
    fixture_path: Path,
) -> None:
    if fixtures.get("format_version") != 1:
        raise ValueError(f"{fixture_path}: format_version must be 1")
    if fixtures.get("release_version") != release_version:
        raise ValueError(f"{fixture_path}: release_version mismatch")
    cases = fixtures.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError(f"{fixture_path}: cases must be a non-empty list")
    names: set[str] = set()
    for case in cases:
        name = str(case.get("name", ""))
        if not name or name in names:
            raise ValueError(f"{fixture_path}: fixture names must be unique and non-empty")
        names.add(name)
        artifact_id = str(case.get("artifact_id", ""))
        if artifact_id not in artifacts:
            raise ValueError(f"{fixture_path}: {name} references unknown artifact {artifact_id}")
        if artifacts[artifact_id].artifact_type != JSON_SCHEMA_ARTIFACT:
            raise ValueError(
                f"{fixture_path}: {name} references data artifact {artifact_id} as a schema"
            )
        errors = list(
            Draft202012Validator(artifacts[artifact_id].schema).iter_errors(case.get("input"))
        )
        expected_valid = case.get("schema_valid")
        if expected_valid != (not errors):
            raise ValueError(
                f"{fixture_path}: {name} expected schema_valid={expected_valid}, "
                f"observed {not errors}"
            )


def _resolve_ref(schema: dict[str, Any], root: dict[str, Any]) -> Any:
    ref = schema["$ref"]
    if not isinstance(ref, str) or not ref.startswith("#"):
        raise ValueError(f"compatibility checker only supports local refs, got {ref!r}")
    if ref == "#":
        return root
    if not ref.startswith("#/"):
        anchor = ref[1:]
        matches = _find_local_anchor(root, anchor)
        if len(matches) != 1:
            raise ValueError(
                f"local ref {ref!r} resolved to {len(matches)} anchors; expected exactly one"
            )
        return matches[0]
    value: Any = root
    for part in ref[2:].split("/"):
        value = value[part.replace("~1", "/").replace("~0", "~")]
    return value


def _find_local_anchor(root: dict[str, Any], anchor: str) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    pending: list[Any] = [root]
    visited: set[int] = set()
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            if id(value) in visited:
                continue
            visited.add(id(value))
            if value.get("$anchor") == anchor or value.get("$dynamicAnchor") == anchor:
                matches.append(value)
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    return matches


def compatibility_errors(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """Return same-major full-compatibility failures for the supported schema subset."""
    errors: list[str] = []
    _compare_node(old, new, old, new, "$", errors, set())
    return errors


def _compare_node(
    old_node: Any,
    new_node: Any,
    old_root: dict[str, Any],
    new_root: dict[str, Any],
    path: str,
    errors: list[str],
    seen: set[tuple[int, int]],
) -> None:
    if not isinstance(old_node, dict) or not isinstance(new_node, dict):
        if old_node != new_node:
            errors.append(f"{path}: boolean schema changed from {old_node!r} to {new_node!r}")
        return

    pair = (id(old_node), id(new_node))
    if pair in seen:
        return
    seen.add(pair)

    _compare_unimplemented_keywords(old_node, new_node, path, errors)
    old_ref = old_node.get("$ref", _MISSING)
    new_ref = new_node.get("$ref", _MISSING)
    if old_ref != new_ref:
        errors.append(f"{path}: $ref changed from {old_ref!r} to {new_ref!r}")

    # Draft 2020-12 applies siblings beside $ref. Compare those local constraints first rather
    # than resolving the ref and silently dropping them (the review finding on bh-j4gbx.2).
    old_local = {key: value for key, value in old_node.items() if key != "$ref"}
    new_local = {key: value for key, value in new_node.items() if key != "$ref"}
    _compare_constraints(old_local, new_local, old_root, new_root, path, errors, seen)
    _compare_schema_applicators(old_local, new_local, old_root, new_root, path, errors, seen)

    if old_ref is not _MISSING and new_ref is not _MISSING and old_ref == new_ref:
        _compare_node(
            _resolve_ref(old_node, old_root),
            _resolve_ref(new_node, new_root),
            old_root,
            new_root,
            f"{path}->$ref",
            errors,
            seen,
        )


def _compare_constraints(
    old: dict[str, Any],
    new: dict[str, Any],
    old_root: dict[str, Any],
    new_root: dict[str, Any],
    path: str,
    errors: list[str],
    seen: set[tuple[int, int]],
) -> None:
    for keyword in ("$schema", "$id"):
        if old.get(keyword, _MISSING) != new.get(keyword, _MISSING):
            errors.append(f"{path}: {keyword} changed")

    if _normalized(old.get("type")) != _normalized(new.get("type")):
        errors.append(f"{path}: type changed from {old.get('type')!r} to {new.get('type')!r}")
    if old.get("const", _MISSING) != new.get("const", _MISSING):
        errors.append(f"{path}: const changed")

    old_enum = old.get("enum", _MISSING)
    new_enum = new.get("enum", _MISSING)
    if old_enum is not _MISSING or new_enum is not _MISSING:
        if _enum_signature(old_enum) != _enum_signature(new_enum):
            errors.append(f"{path}: closed-union members changed from {old_enum!r} to {new_enum!r}")
        if bool(old.get("x-beadhive-closed-union")) != bool(new.get("x-beadhive-closed-union")):
            errors.append(f"{path}: closed-union marker changed")

    old_props = old.get("properties", {})
    new_props = new.get("properties", {})
    old_required = set(old.get("required", []))
    new_required = set(new.get("required", []))
    if old_required != new_required:
        errors.append(
            f"{path}: required properties changed from {sorted(old_required)!r} "
            f"to {sorted(new_required)!r}"
        )
    for name in sorted(set(old_props) - set(new_props)):
        errors.append(f"{path}.{name}: property was removed")
    for name in sorted(set(new_props) - set(old_props)):
        if name in new_required:
            errors.append(f"{path}.{name}: new property is required")
        elif not _optional_property_addition_is_safe(old, new):
            errors.append(
                f"{path}.{name}: optional property addition is not provably compatible "
                "with the old unknown-property policy"
            )
    for name in sorted(set(old_props) & set(new_props)):
        _compare_node(
            old_props[name],
            new_props[name],
            old_root,
            new_root,
            f"{path}.{name}",
            errors,
            seen,
        )

    old_additional = old.get("additionalProperties", True)
    new_additional = new.get("additionalProperties", True)
    if isinstance(old_additional, dict) and isinstance(new_additional, dict):
        _compare_node(
            old_additional,
            new_additional,
            old_root,
            new_root,
            f"{path}.additionalProperties",
            errors,
            seen,
        )
    elif old_additional != new_additional:
        errors.append(f"{path}: additionalProperties policy changed")
    if "items" in old or "items" in new:
        if "items" not in old or "items" not in new:
            errors.append(f"{path}: array item policy changed")
        else:
            _compare_node(old["items"], new["items"], old_root, new_root, f"{path}[]", errors, seen)

    for keyword in (
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minLength",
        "maxLength",
        "pattern",
        "format",
        "minItems",
        "maxItems",
        "uniqueItems",
        "minProperties",
        "maxProperties",
    ):
        if old.get(keyword, _MISSING) != new.get(keyword, _MISSING):
            errors.append(f"{path}: {keyword} changed")


# Draft 2020-12 applicators whose values contain schemas. They remain "unimplemented" for
# compatibility-policy purposes, so a changed container still produces the fail-closed raw-value
# diagnostic. Traversing common children additionally detects changes hidden behind an unchanged
# applicator, notably an unchanged local $ref whose target in $defs changed.
_SCHEMA_ARRAY_APPLICATORS = ("allOf", "anyOf", "oneOf", "prefixItems")
_SCHEMA_VALUE_APPLICATORS = (
    "not",
    "if",
    "then",
    "else",
    "contains",
    "propertyNames",
    "unevaluatedProperties",
    "unevaluatedItems",
    "contentSchema",
)
_SCHEMA_MAP_APPLICATORS = (
    "dependentSchemas",
    "patternProperties",
    "$defs",
    # Adjacent draft vocabulary: conservative traversal also closes unchanged $recursiveRef
    # paths for schemas that still publish the legacy definitions container.
    "definitions",
)


def _compare_schema_applicators(
    old: dict[str, Any],
    new: dict[str, Any],
    old_root: dict[str, Any],
    new_root: dict[str, Any],
    path: str,
    errors: list[str],
    seen: set[tuple[int, int]],
) -> None:
    for keyword in _SCHEMA_ARRAY_APPLICATORS:
        old_value = old.get(keyword)
        new_value = new.get(keyword)
        if not isinstance(old_value, list) or not isinstance(new_value, list):
            continue
        for index, (old_child, new_child) in enumerate(zip(old_value, new_value, strict=False)):
            _compare_node(
                old_child,
                new_child,
                old_root,
                new_root,
                f"{path}.{keyword}[{index}]",
                errors,
                seen,
            )

    for keyword in _SCHEMA_VALUE_APPLICATORS:
        if keyword not in old or keyword not in new:
            continue
        _compare_node(
            old[keyword],
            new[keyword],
            old_root,
            new_root,
            f"{path}.{keyword}",
            errors,
            seen,
        )

    for keyword in _SCHEMA_MAP_APPLICATORS:
        old_value = old.get(keyword)
        new_value = new.get(keyword)
        if not isinstance(old_value, dict) or not isinstance(new_value, dict):
            continue
        if keyword == "$defs" and set(old_value) != set(new_value):
            errors.append(
                f"{path}.$defs: definition names changed from "
                f"{sorted(old_value)!r} to {sorted(new_value)!r}"
            )
        for name in sorted(set(old_value) & set(new_value)):
            _compare_node(
                old_value[name],
                new_value[name],
                old_root,
                new_root,
                f"{path}.{keyword}.{name}",
                errors,
                seen,
            )


# Keywords whose compatibility effect the structured comparator above understands. Annotation
# changes are intentionally free; every `$defs` entry is compared conservatively so reference
# vocabulary with dynamic resolution cannot hide a changed assertion. Every other keyword is
# fail-closed: unchanged is safe, but adding, removing, or changing it is rejected rather than
# producing a false compatibility verdict.
_IMPLEMENTED_KEYWORDS = {
    "$schema",
    "$id",
    "$ref",
    "$defs",
    "type",
    "const",
    "enum",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "minLength",
    "maxLength",
    "pattern",
    "format",
    "minItems",
    "maxItems",
    "uniqueItems",
    "minProperties",
    "maxProperties",
    "x-beadhive-closed-union",
}
_ANNOTATION_KEYWORDS = {
    "$comment",
    "title",
    "description",
    "default",
    "deprecated",
    "examples",
    "readOnly",
    "writeOnly",
}


def _compare_unimplemented_keywords(
    old: dict[str, Any], new: dict[str, Any], path: str, errors: list[str]
) -> None:
    for keyword in sorted(_unimplemented_keywords(old, new)):
        old_value = old.get(keyword, _MISSING)
        new_value = new.get(keyword, _MISSING)
        if old_value != new_value:
            errors.append(
                f"{path}: unsupported compatibility keyword {keyword!r} changed "
                f"from {old_value!r} to {new_value!r}"
            )


def _unimplemented_keywords(old: dict[str, Any], new: dict[str, Any]) -> set[str]:
    ignored = _IMPLEMENTED_KEYWORDS | _ANNOTATION_KEYWORDS
    return (set(old) | set(new)) - ignored


def _optional_property_addition_is_safe(old: dict[str, Any], new: dict[str, Any]) -> bool:
    # The ADR's explicit optional-field exception depends on old consumers ignoring unknown
    # object members. A closed/additional schema or an assertion the adapter does not understand
    # makes that promise unprovable, so the exception must fail closed.
    return old.get("additionalProperties", True) is True and not _unimplemented_keywords(old, new)


_MISSING = object()


def _normalized(value: Any) -> Any:
    if isinstance(value, list):
        return tuple(sorted(value, key=lambda item: str(item)))
    return value


def _enum_signature(value: Any) -> Any:
    if value is _MISSING:
        return _MISSING
    return tuple(sorted(json.dumps(item, sort_keys=True) for item in value))


def _semver_tuple(value: str) -> tuple[int, int, int]:
    match = SEMVER.fullmatch(value)
    if match is None:
        raise ValueError(f"invalid semver: {value!r}")
    return tuple(int(part) for part in match.groups())


def _compare_catalog_value(old: Any, new: Any, path: str, errors: list[str]) -> None:
    """Fail closed on a published catalog value while retaining an exact JSON path."""
    if isinstance(old, dict) and isinstance(new, dict):
        for key in sorted(set(old) - set(new)):
            errors.append(f"{path}.{key}: field was removed")
        for key in sorted(set(new) - set(old)):
            errors.append(f"{path}.{key}: field was added to a published shape")
        for key in sorted(set(old) & set(new)):
            _compare_catalog_value(old[key], new[key], f"{path}.{key}", errors)
        return
    if isinstance(old, list) and isinstance(new, list):
        if len(old) != len(new):
            errors.append(f"{path}: list length changed from {len(old)} to {len(new)}")
        for index, (old_item, new_item) in enumerate(zip(old, new, strict=False)):
            _compare_catalog_value(old_item, new_item, f"{path}[{index}]", errors)
        return
    if type(old) is not type(new):
        errors.append(
            f"{path}: value type changed from {type(old).__name__} to {type(new).__name__}"
        )
    elif old != new:
        errors.append(f"{path}: value changed from {old!r} to {new!r}")


def _catalog_operations(
    document: dict[str, Any], side: str, errors: list[str]
) -> dict[str, dict[str, Any]]:
    operations = document.get("operations")
    if not isinstance(operations, list):
        errors.append("$.operations: must be an array")
        return {}
    indexed: dict[str, dict[str, Any]] = {}
    for index, operation in enumerate(operations):
        if not isinstance(operation, dict) or not isinstance(operation.get("name"), str):
            errors.append(f"$.operations[{index}]: operation must carry a string name")
            continue
        name = operation["name"]
        if name in indexed:
            errors.append(
                f"$.operations[{index}].name: duplicate canonical operation identity "
                f"{name!r} in {side} catalog"
            )
            continue
        indexed[name] = operation
    return indexed


def _catalog_projection_uniqueness(
    operations: dict[str, dict[str, Any]], errors: list[str]
) -> None:
    seen: dict[tuple[str, str], str] = {}

    def remember(kind: str, value: Any, path: str) -> None:
        if not isinstance(value, str):
            return
        key = (kind, value)
        if prior := seen.get(key):
            errors.append(
                f"{path}: duplicate {kind} projection {value!r}; first declared at {prior}"
            )
        else:
            seen[key] = path

    for name, operation in operations.items():
        surfaces = operation.get("surfaces", {})
        if not isinstance(surfaces, dict):
            continue
        cli = surfaces.get("cli")
        if isinstance(cli, dict):
            remember("CLI path", cli.get("path"), f"$.operations[name={name!r}].surfaces.cli.path")
            aliases = cli.get("aliases", [])
            if isinstance(aliases, list):
                for index, alias in enumerate(aliases):
                    if isinstance(alias, dict):
                        remember(
                            "CLI path",
                            alias.get("path"),
                            f"$.operations[name={name!r}].surfaces.cli.aliases[{index}].path",
                        )
        mcp = surfaces.get("mcp")
        if isinstance(mcp, dict):
            remember("MCP tool", mcp.get("tool"), f"$.operations[name={name!r}].surfaces.mcp.tool")
            remember(
                "MCP resource",
                mcp.get("resource"),
                f"$.operations[name={name!r}].surfaces.mcp.resource",
            )


def catalog_compatibility_errors(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """Return append-only, same-major compatibility failures for catalog data.

    Existing operation declarations and global projection policy are immutable within a major.
    A uniquely named, schema-valid operation may be appended because old readers ignore it.
    """
    errors: list[str] = []
    old_keys = set(old) - {"operations", "catalog_version"}
    new_keys = set(new) - {"operations", "catalog_version"}
    for key in sorted(old_keys - new_keys):
        errors.append(f"$.{key}: top-level field was removed")
    for key in sorted(new_keys - old_keys):
        errors.append(f"$.{key}: top-level field was added to the published catalog shape")
    for key in sorted(old_keys & new_keys):
        _compare_catalog_value(old[key], new[key], f"$.{key}", errors)

    old_version = str(old.get("catalog_version", ""))
    new_version = str(new.get("catalog_version", ""))
    try:
        old_semver = _semver_tuple(old_version)
        new_semver = _semver_tuple(new_version)
        if old_semver[0] != new_semver[0] or new_semver < old_semver:
            errors.append(
                "$.catalog_version: same-major catalog version must not change major or move "
                f"backwards ({old_version!r} -> {new_version!r})"
            )
    except ValueError:
        errors.append(
            f"$.catalog_version: invalid semantic version change {old_version!r} -> {new_version!r}"
        )

    old_operations = _catalog_operations(old, "baseline", errors)
    new_operations = _catalog_operations(new, "candidate", errors)
    _catalog_projection_uniqueness(new_operations, errors)
    for name in sorted(set(old_operations) - set(new_operations)):
        errors.append(f"$.operations[name={name!r}]: canonical operation was removed")
    for name in sorted(set(old_operations) & set(new_operations)):
        _compare_catalog_value(
            old_operations[name],
            new_operations[name],
            f"$.operations[name={name!r}]",
            errors,
        )
    return errors


def compare_releases(old: Release, new: Release) -> list[str]:
    if old.major != new.major:
        return []
    errors: list[str] = []
    for artifact_id, old_artifact in old.artifacts.items():
        new_artifact = new.artifacts.get(artifact_id)
        if new_artifact is None:
            errors.append(f"{artifact_id}: artifact removed within major {old.major}")
            continue
        if old_artifact.contract_version != new_artifact.contract_version:
            errors.append(f"{artifact_id}: contract_version changed within the same release major")
        if old_artifact.artifact_type != new_artifact.artifact_type:
            errors.append(
                f"{artifact_id}: artifact type changed from {old_artifact.artifact_type} "
                f"to {new_artifact.artifact_type}"
            )
            continue
        comparator = (
            catalog_compatibility_errors
            if old_artifact.artifact_type == CATALOG_DATA_ARTIFACT
            else compatibility_errors
        )
        errors.extend(
            f"{artifact_id} {error}"
            for error in comparator(old_artifact.document, new_artifact.document)
        )
    return errors


def _merge_base(root: Path, ref: str) -> str | None:
    result = subprocess.run(
        ["git", "merge-base", "HEAD", ref], cwd=root, check=False, capture_output=True, text=True
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _baseline_has_wire_index(root: Path, commit: str) -> bool:
    result = subprocess.run(
        ["git", "cat-file", "-e", f"{commit}:{WIRE_INDEX.as_posix()}"],
        cwd=root,
        check=False,
        capture_output=True,
    )
    return result.returncode == 0


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    try:
        candidate_reader = FilesystemReader(root)
        candidate = load_repository(candidate_reader)
        base_ref = os.environ.get("BH_WIRE_SCHEMA_BASE_REF", "main")
        base_commit = _merge_base(root, base_ref)
        if base_commit is None or not _baseline_has_wire_index(root, base_commit):
            print(f"wire-schema-compat: initial release validated; no baseline at {base_ref!r}")
            return 0

        baseline_reader = GitReader(root, base_commit)
        baseline = load_repository(baseline_reader)
        errors: list[str] = []
        for version, old_release in baseline.releases.items():
            new_release = candidate.releases.get(version)
            if new_release is None:
                errors.append(f"published release {version} was removed")
                continue
            if old_release.manifest_path != new_release.manifest_path:
                errors.append(f"published release {version} manifest path changed")
            for path in old_release.files:
                if baseline_reader.read(path) != candidate_reader.read(path):
                    errors.append(f"published release file was modified in place: {path}")
        errors.extend(compare_releases(baseline.latest, candidate.latest))
        if _semver_tuple(candidate.latest.version) < _semver_tuple(baseline.latest.version):
            errors.append("wire index latest moved backwards")
        if errors:
            print("wire-schema-compat: incompatible change:", file=sys.stderr)
            for error in errors:
                print(f"  - {error}", file=sys.stderr)
            return 1
        print(
            f"wire-schema-compat: {baseline.latest.version} -> "
            f"{candidate.latest.version} is fully compatible"
        )
        return 0
    except (ValueError, KeyError) as exc:
        print(f"wire-schema-compat: invalid release: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
