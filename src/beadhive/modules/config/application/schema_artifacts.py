"""Deterministic, side-effect-free projections of the canonical config contracts."""

from __future__ import annotations

import json
from typing import Any

from ..contracts import SCHEMA_VERSION, BeadhiveConfig, iter_schema_fields

CONFIG_SCHEMA_ARTIFACT_ID = "urn:beadhive:wire-schema:config:1"
CONFIG_SCHEMA_ARTIFACT_VERSION = SCHEMA_VERSION
LEGACY_SCHEMA_ROWS_PROJECTION_VERSION = 1
JSON_SCHEMA_DRAFT = "https://json-schema.org/draft/2020-12/schema"


def generate_config_json_schema() -> dict[str, Any]:
    """Return the official schema without reading configuration or touching external state."""

    schema = BeadhiveConfig.model_json_schema(by_alias=True, mode="validation")
    schema["$id"] = CONFIG_SCHEMA_ARTIFACT_ID
    schema["$schema"] = JSON_SCHEMA_DRAFT
    schema["version"] = CONFIG_SCHEMA_ARTIFACT_VERSION
    return schema


def generate_config_json_schema_bytes() -> bytes:
    """Return stable checked-artifact bytes."""

    return (
        json.dumps(
            generate_config_json_schema(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode()


def legacy_schema_rows() -> list[dict[str, str]]:
    """Version 1 compatibility projection used by ``bh config schema --json``."""

    return [
        {
            "path": field.path,
            "type": field.type,
            "default": field.default,
            "description": field.description,
        }
        for field in iter_schema_fields()
    ]


__all__ = (
    "CONFIG_SCHEMA_ARTIFACT_ID",
    "CONFIG_SCHEMA_ARTIFACT_VERSION",
    "JSON_SCHEMA_DRAFT",
    "LEGACY_SCHEMA_ROWS_PROJECTION_VERSION",
    "generate_config_json_schema",
    "generate_config_json_schema_bytes",
    "legacy_schema_rows",
)
