"""Deterministic JSON Schema source for semantic telemetry event-envelope v1."""

from __future__ import annotations

from typing import Any

from .contracts import (
    _FINITE_ATTRIBUTE_VALUES,
    EVENT_ENVELOPE_SCHEMA_ID,
    EVENT_ENVELOPE_SCHEMA_VERSION,
    MAX_ATTRIBUTES,
    AttributeKey,
    ErrorClassification,
    EventPhase,
    Outcome,
    Seat,
)

_DOTTED_PATTERN = r"^[a-z][a-z0-9-]*(?:\.[a-z][a-z0-9-]*){1,7}$"
_CODE_PATTERN = r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+){0,7}$"
_OPAQUE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
_NONZERO_HEX_PATTERN = r"^[0-9a-f]*[1-9a-f][0-9a-f]*$"
_NONZERO_YEAR_PATTERN = r"(?:[0-9]{3}[1-9]|[0-9]{2}[1-9][0-9]|[0-9][1-9][0-9]{2}|[1-9][0-9]{3})"
_LEAP_YEAR_PATTERN = (
    r"(?:[0-9]{2}(?:0[48]|[2468][048]|[13579][26])|"
    r"(?:0[48]|[2468][048]|[13579][26])00)"
)
_UTC_DATE_PATTERN = (
    rf"(?:{_NONZERO_YEAR_PATTERN}-(?:"
    r"(?:01|03|05|07|08|10|12)-(?:0[1-9]|[12][0-9]|3[01])|"
    r"(?:04|06|09|11)-(?:0[1-9]|[12][0-9]|30)|"
    r"02-(?:0[1-9]|1[0-9]|2[0-8]))|"
    rf"{_LEAP_YEAR_PATTERN}-02-29)"
)
_UTC_TIMESTAMP_PATTERN = (
    rf"^{_UTC_DATE_PATTERN}"
    r"T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]"
    r"(?:\.[0-9]{1,6})?Z$"
)


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    return {"oneOf": [schema, {"type": "null"}]}


def _attribute_variant(key: AttributeKey) -> dict[str, Any]:
    if key is AttributeKey.RETRY_COUNT:
        value: dict[str, Any] = {"type": "integer", "minimum": 0, "maximum": 10}
    elif key in _FINITE_ATTRIBUTE_VALUES:
        value = {
            "type": "string",
            "enum": sorted(_FINITE_ATTRIBUTE_VALUES[key]),
            "x-beadhive-closed-union": True,
        }
    else:
        value = {
            "type": "string",
            "pattern": (
                _DOTTED_PATTERN
                if key in {AttributeKey.OPERATION_NAME, AttributeKey.LIFECYCLE_EVENT}
                else _CODE_PATTERN
            ),
            "maxLength": 128,
        }
    return value


def event_envelope_schema() -> dict[str, Any]:
    """Return a new deterministic Draft 2020-12 schema document."""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": EVENT_ENVELOPE_SCHEMA_ID,
        "title": "Beadhive semantic telemetry event envelope v1",
        "description": (
            "Transport-neutral, allowlisted semantic observation. Adapter and collector wire "
            "formats are projections of this contract, not part of it."
        ),
        "type": "object",
        "required": [
            "schema_version",
            "event_name",
            "event_version",
            "event_id",
            "occurred_at",
            "phase",
            "correlation_id",
            "causation_id",
            "trace_id",
            "span_id",
            "identity",
            "outcome",
            "duration_ms",
            "error",
            "attributes",
        ],
        "properties": {
            "schema_version": {"const": EVENT_ENVELOPE_SCHEMA_VERSION},
            "event_name": {
                "type": "string",
                "pattern": r"^beadhive(?:\.[a-z][a-z0-9-]*){2,7}$",
                "maxLength": 128,
            },
            "event_version": {"type": "integer", "minimum": 1},
            "event_id": {"type": "string", "pattern": _OPAQUE_PATTERN},
            "occurred_at": {
                "type": "string",
                "pattern": _UTC_TIMESTAMP_PATTERN,
            },
            "phase": {"$ref": "#/$defs/phase"},
            "correlation_id": {"type": "string", "pattern": _OPAQUE_PATTERN},
            "causation_id": _nullable({"type": "string", "pattern": _OPAQUE_PATTERN}),
            "trace_id": _nullable(
                {
                    "type": "string",
                    "pattern": _NONZERO_HEX_PATTERN,
                    "minLength": 32,
                    "maxLength": 32,
                }
            ),
            "span_id": _nullable(
                {
                    "type": "string",
                    "pattern": _NONZERO_HEX_PATTERN,
                    "minLength": 16,
                    "maxLength": 16,
                }
            ),
            "identity": {"$ref": "#/$defs/identity"},
            "outcome": _nullable({"$ref": "#/$defs/outcome"}),
            "duration_ms": _nullable({"type": "integer", "minimum": 0}),
            "error": _nullable({"$ref": "#/$defs/error"}),
            "attributes": {
                "type": "object",
                "maxProperties": MAX_ATTRIBUTES,
                "properties": {key.value: _attribute_variant(key) for key in AttributeKey},
                "additionalProperties": False,
            },
        },
        "additionalProperties": False,
        "allOf": [
            {
                "if": {"properties": {"span_id": {"type": "string"}}},
                "then": {"properties": {"trace_id": {"type": "string"}}},
            },
            {
                "if": {"properties": {"phase": {"const": EventPhase.COMPLETED.value}}},
                "then": {
                    "properties": {
                        "outcome": {"$ref": "#/$defs/outcome"},
                        "duration_ms": {
                            "type": "integer",
                            "minimum": 0,
                        },
                    }
                },
            },
            {
                "if": {
                    "properties": {
                        "phase": {
                            "enum": [EventPhase.STARTED.value, EventPhase.OBSERVED.value],
                            "x-beadhive-closed-union": True,
                        }
                    }
                },
                "then": {
                    "properties": {
                        "outcome": {"type": "null"},
                        "duration_ms": {"type": "null"},
                        "error": {"type": "null"},
                    }
                },
            },
            {
                "if": {
                    "properties": {
                        "outcome": {
                            "enum": [Outcome.SUCCEEDED.value, Outcome.NO_OP.value],
                            "x-beadhive-closed-union": True,
                        }
                    }
                },
                "then": {"properties": {"error": {"type": "null"}}},
            },
            {
                "if": {
                    "properties": {
                        "outcome": {
                            "enum": [
                                Outcome.FAILED.value,
                                Outcome.TIMED_OUT.value,
                                Outcome.CANCELLED.value,
                            ],
                            "x-beadhive-closed-union": True,
                        }
                    }
                },
                "then": {"properties": {"error": {"$ref": "#/$defs/error"}}},
            },
            {
                "if": {"properties": {"outcome": {"const": Outcome.TIMED_OUT.value}}},
                "then": {
                    "properties": {
                        "error": {
                            "allOf": [
                                {"$ref": "#/$defs/error"},
                                {
                                    "properties": {
                                        "classification": {
                                            "const": ErrorClassification.TIMEOUT.value
                                        }
                                    }
                                },
                            ]
                        }
                    }
                },
            },
            {
                "if": {"properties": {"outcome": {"const": Outcome.CANCELLED.value}}},
                "then": {
                    "properties": {
                        "error": {
                            "allOf": [
                                {"$ref": "#/$defs/error"},
                                {
                                    "properties": {
                                        "classification": {
                                            "const": ErrorClassification.CANCELLATION.value
                                        }
                                    }
                                },
                            ]
                        }
                    }
                },
            },
        ],
        "$defs": {
            "phase": {
                "type": "string",
                "enum": [item.value for item in EventPhase],
                "x-beadhive-closed-union": True,
            },
            "outcome": {
                "type": "string",
                "enum": [item.value for item in Outcome],
                "x-beadhive-closed-union": True,
            },
            "error": {
                "type": "object",
                "required": ["classification", "code", "retryable"],
                "properties": {
                    "classification": {
                        "type": "string",
                        "enum": [item.value for item in ErrorClassification],
                        "x-beadhive-closed-union": True,
                    },
                    "code": {"type": "string", "pattern": _CODE_PATTERN, "maxLength": 128},
                    "retryable": {"type": "boolean"},
                },
                "additionalProperties": False,
            },
            "identity": {
                "type": "object",
                "required": [
                    "service",
                    "instance_id",
                    "host_id",
                    "hive_id",
                    "bead_id",
                    "run_id",
                    "actor",
                    "seat",
                    "plugin_id",
                    "plugin_version",
                ],
                "properties": {
                    "service": {
                        "type": "string",
                        "pattern": "^[a-z][a-z0-9-]{0,63}$",
                    },
                    "instance_id": {"type": "string", "pattern": _OPAQUE_PATTERN},
                    "host_id": _nullable({"type": "string", "pattern": _OPAQUE_PATTERN}),
                    "hive_id": _nullable(
                        {
                            "type": "string",
                            "pattern": (
                                "^[a-z0-9][a-z0-9._-]{0,63}/"
                                "[A-Za-z0-9][A-Za-z0-9._-]{0,127}/"
                                "[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
                            ),
                        }
                    ),
                    "bead_id": _nullable(
                        {
                            "type": "string",
                            "pattern": "^[A-Za-z][A-Za-z0-9]*-[A-Za-z0-9]+(?:\\.[0-9]+)?$",
                        }
                    ),
                    "run_id": _nullable({"type": "string", "pattern": _OPAQUE_PATTERN}),
                    "actor": _nullable(
                        {
                            "type": "string",
                            "pattern": ("^[a-z][a-z0-9-]*/[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"),
                        }
                    ),
                    "seat": _nullable(
                        {
                            "type": "string",
                            "enum": [item.value for item in Seat],
                            "x-beadhive-closed-union": True,
                        }
                    ),
                    "plugin_id": _nullable({"type": "string", "pattern": "^[a-z][a-z0-9-]{0,63}$"}),
                    "plugin_version": _nullable(
                        {
                            "type": "string",
                            "pattern": ("^(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)$"),
                        }
                    ),
                },
                "additionalProperties": False,
                "dependentRequired": {"plugin_version": ["plugin_id"]},
                "allOf": [
                    {
                        "if": {"properties": {"plugin_version": {"type": "string"}}},
                        "then": {"properties": {"plugin_id": {"type": "string"}}},
                    }
                ],
            },
        },
    }
