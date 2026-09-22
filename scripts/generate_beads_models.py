#!/usr/bin/env python3
"""Generate strict Pydantic canonical-record models from the vendored Beads schema."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "src/beadhive/schemas/beads/v1.3.0/schema.json"
PROVENANCE = SCHEMA.with_name("provenance.json")
TARGET = ROOT / "src/beadhive/beads_models.py"
HEAVY_ISSUE_PROPERTIES = frozenset(
    {"description", "design", "acceptance_criteria", "notes", "payload", "waiters"}
)


def _literal(values: list[Any]) -> str:
    return "Literal[" + ", ".join(json.dumps(value) for value in values) + "]"


def _annotation(schema: Any, *, property_name: str) -> str:
    if schema is True:
        return "Any"
    if not isinstance(schema, dict):
        raise ValueError(f"property {property_name!r} is not an object schema")
    if enum := schema.get("enum"):
        return _literal(enum)
    type_ = schema.get("type")
    if isinstance(type_, list):
        concrete = [candidate for candidate in type_ if candidate != "null"]
        if not concrete:
            return "None"
        return " | ".join(
            _annotation({**schema, "type": candidate}, property_name=property_name)
            for candidate in concrete
        )
    if type_ == "string":
        return "_JsonDateTime" if schema.get("format") == "date-time" else "StrictStr"
    if type_ == "integer":
        return "StrictInt"
    if type_ == "boolean":
        return "StrictBool"
    if type_ == "array":
        if property_name == "dependencies":
            return "list[BdDependencyRecord]"
        if property_name == "comments":
            return "list[BdIssueComment]"
        if property_name == "bonded_from":
            return "list[BdIssueBond]"
        return f"list[{_annotation(schema.get('items'), property_name=property_name)}]"
    raise ValueError(f"unsupported schema for property {property_name!r}: {schema!r}")


def _allows_null(schema: Any) -> bool:
    if schema is True:
        return True
    if not isinstance(schema, dict):
        return False
    if None in schema.get("enum", ()):
        return True
    type_ = schema.get("type")
    return type_ == "null" or isinstance(type_, list) and "null" in type_


def _literal_lines(annotation: str, *, indent: str) -> list[str]:
    values = annotation.removeprefix("Literal[").removesuffix("]").split(", ")
    return [f"{indent}Literal[", *(f"{indent}    {value}," for value in values), f"{indent}]"]


def _field_lines(
    property_name: str, annotation: str, *, required: bool, nullable: bool
) -> list[str]:
    union_members = [annotation]
    if nullable and annotation != "Any":
        union_members.append("None")
    if not required:
        union_members.append("MISSING")
    field_annotation = " | ".join(union_members)
    default = "" if required else " = MISSING"
    direct = f"    {property_name}: {field_annotation}{default}"
    if len(direct) <= 100:
        return [direct]
    if not annotation.startswith("Literal["):
        raise ValueError(f"cannot format long annotation for {property_name!r}: {field_annotation}")
    if len(union_members) == 1:
        literal = _literal_lines(annotation, indent="    ")
        literal[0] = f"    {property_name}: {literal[0].lstrip()}"
        return literal
    return [
        f"    {property_name}: (",
        *_literal_lines(annotation, indent="        "),
        *(f"        | {member}" for member in union_members[1:]),
        f"    ){default}",
    ]


def _class_source(name: str, schema: dict[str, Any]) -> str:
    if schema.get("type") != "object" or schema.get("additionalProperties") is not False:
        raise ValueError(f"{name} must be a closed object schema")
    properties = schema.get("properties")
    required = schema.get("required")
    if not isinstance(properties, dict) or not isinstance(required, list):
        raise ValueError(f"{name} must declare properties and required")
    required_names = set(required)
    unknown_required = required_names - properties.keys()
    if unknown_required:
        raise ValueError(f"{name} requires unknown properties: {sorted(unknown_required)}")

    lines = [f"class {name}(_StrictRecord):"]
    for property_name, property_schema in properties.items():
        annotation = _annotation(property_schema, property_name=property_name)
        lines.extend(
            _field_lines(
                property_name,
                annotation,
                required=property_name in required_names,
                nullable=_allows_null(property_schema),
            )
        )
    if len(lines) == 1:
        lines.append("    pass")
    return "\n".join(lines)


def _nested_schema(issue: dict[str, Any], property_name: str) -> dict[str, Any]:
    return issue["properties"][property_name]["items"]


def _brief_schema(issue: dict[str, Any]) -> dict[str, Any]:
    properties = issue["properties"]
    missing = HEAVY_ISSUE_PROPERTIES - properties.keys()
    if missing:
        raise ValueError(f"issue schema is missing heavy properties: {sorted(missing)}")
    return {
        **issue,
        "properties": {
            name: schema
            for name, schema in properties.items()
            if name not in HEAVY_ISSUE_PROPERTIES
        },
    }


def render(schema_bytes: bytes, provenance: dict[str, Any]) -> str:
    document = json.loads(schema_bytes)
    issue = document["types"]["issue"]
    dependency = document["types"]["dependency"]
    digest = hashlib.sha256(schema_bytes).hexdigest()
    if provenance.get("document_sha256") != digest:
        raise ValueError("vendored schema does not match provenance document_sha256")

    classes = [
        _class_source("BdDependencyRecord", dependency),
        _class_source("BdIssueComment", _nested_schema(issue, "comments")),
        _class_source("BdIssueBond", _nested_schema(issue, "bonded_from")),
        _class_source("BdIssueRecord", issue),
        _class_source("BdBriefIssue", _brief_schema(issue)),
    ]
    return (
        '''"""Generated canonical Beads record models. Do not edit by hand.

Regenerate with ``uv run python scripts/generate_beads_models.py`` after intentionally capturing
a new pinned ``bd schema`` artifact.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    StrictBool,
    StrictInt,
    StrictStr,
)
from pydantic.experimental.missing_sentinel import MISSING

'''
        + f"SOURCE_BEADS_VERSION = {json.dumps(provenance['version'])}\n"
        + f"SOURCE_BEADS_COMMIT = {json.dumps(provenance['commit'])}\n"
        + f"SOURCE_SCHEMA_SHA256 = {json.dumps(digest)}\n\n\n"
        + """def _require_datetime_string(value: Any) -> Any:
    if not isinstance(value, str):
        raise ValueError("date-time value must be a JSON string")
    return value


_JsonDateTime = Annotated[datetime, BeforeValidator(_require_datetime_string)]


class _StrictRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")


"""
        + "\n\n\n".join(classes)
        + "\n"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if the checked-in source drifts")
    parser.add_argument("--schema", type=Path, default=SCHEMA)
    parser.add_argument("--provenance", type=Path, default=PROVENANCE)
    parser.add_argument("--output", type=Path, default=TARGET)
    args = parser.parse_args(argv)

    generated = render(
        args.schema.read_bytes(), json.loads(args.provenance.read_text(encoding="utf-8"))
    )
    if args.check:
        if not args.output.is_file() or args.output.read_text(encoding="utf-8") != generated:
            parser.error(f"{args.output} is stale; regenerate it without --check")
        return 0
    args.output.write_text(generated, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
