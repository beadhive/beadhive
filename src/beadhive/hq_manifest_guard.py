"""Server snapshot validation of the complete pinned HostManifest JSON Schema.

Only the schema vocabulary emitted by HostManifest is supported. Unknown assertions
are refused instead of ignored. YAML uses the pinned pure-Python safe constructor.
"""

from __future__ import annotations

import math


def validate(value, schema, root=None):
    root = schema if root is None else root
    allowed = {
        "$defs",
        "$ref",
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "anyOf",
        "enum",
        "minimum",
        "title",
        "description",
        "default",
    }
    if set(schema) - allowed:
        raise ValueError("unsupported pinned manifest schema assertion")
    if "$ref" in schema:
        reference = schema["$ref"]
        if not reference.startswith("#/$defs/"):
            raise ValueError("nonlocal manifest schema reference")
        validate(value, root["$defs"][reference.removeprefix("#/$defs/")], root)
    if "anyOf" in schema:
        for alternative in schema["anyOf"]:
            try:
                validate(value, alternative, root)
                break
            except ValueError:
                continue
        else:
            raise ValueError("manifest value matches no schema alternative")
    kind = schema.get("type")
    valid = {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "null": value is None,
        "integer": type(value) in {int, float} and math.isfinite(value) and value == int(value),
    }
    if kind is not None and (kind not in valid or not valid[kind]):
        raise ValueError("manifest schema type mismatch")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError("manifest schema enum mismatch")
    if "minimum" in schema and value < schema["minimum"]:
        raise ValueError("manifest schema minimum violation")
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("manifest object keys must be strings")
        if set(schema.get("required", ())) - set(value):
            raise ValueError("manifest required field missing")
        properties = schema.get("properties", {})
        for key, item in value.items():
            if key in properties:
                validate(item, properties[key], root)
            elif schema.get("additionalProperties") is False:
                raise ValueError("manifest unknown field")
    if isinstance(value, list) and "items" in schema:
        for item in value:
            validate(item, schema["items"], root)


def parse_manifest(text, schema):
    from ruamel.yaml import YAML

    yaml = YAML(typ="safe", pure=True)
    yaml.allow_duplicate_keys = False
    value = yaml.load(text)
    validate(value, schema)
    return value
