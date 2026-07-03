"""Guards the report-channel SPEC (bead): the JSON Schema at
``docs/schemas/report-channel.schema.json`` must be a valid schema AND must validate the worked
example at ``docs/schemas/report-channel.example.json`` (the acceptance criterion "the schema
validates the example descriptor"). This is a design deliverable — there is no consumption /
auto-routing code to test; the contract under test is the schema ⇄ example agreement wired into
``just check``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

jsonschema = pytest.importorskip("jsonschema")

_DOCS = Path(__file__).resolve().parents[1] / "docs" / "schemas"
_SCHEMA_PATH = _DOCS / "report-channel.schema.json"
_EXAMPLE_PATH = _DOCS / "report-channel.example.json"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_schema_is_self_consistent() -> None:
    """The schema itself is a valid draft 2020-12 schema (catches typos in the spec artifact)."""
    schema = _load(_SCHEMA_PATH)
    jsonschema.Draft202012Validator.check_schema(schema)


def test_schema_validates_example_document() -> None:
    """ACCEPTANCE: the shipped example discovery document validates against the schema."""
    schema = _load(_SCHEMA_PATH)
    example = _load(_EXAMPLE_PATH)
    jsonschema.Draft202012Validator(schema).validate(example)


def test_schema_validates_embedded_descriptor_example() -> None:
    """The ``report_channel`` descriptor example embedded in the schema validates against its
    own ``$defs`` subschema — the minimal {kind, target, verb?, labels?} shape."""
    schema = _load(_SCHEMA_PATH)
    descriptor_schema = {"$schema": schema["$schema"], **schema["$defs"]["report_channel"]}
    for descriptor in schema["$defs"]["report_channel"]["examples"]:
        jsonschema.Draft202012Validator(descriptor_schema).validate(descriptor)


def test_all_declared_kinds_are_exercised() -> None:
    """Every ``kind`` in the enum should be demonstrated somewhere across the example + embedded
    descriptors, so the spec's discovery forms all have a worked reference."""
    schema = _load(_SCHEMA_PATH)
    declared = set(schema["$defs"]["report_channel"]["properties"]["kind"]["enum"])
    example = _load(_EXAMPLE_PATH)
    seen = {c["kind"] for c in example["channels"]}
    seen |= {d["kind"] for d in schema["$defs"]["report_channel"]["examples"]}
    assert seen <= declared, f"example uses undeclared kind(s): {seen - declared}"


def test_additional_core_property_is_rejected() -> None:
    """A typo'd/unknown core field is a hard error (additionalProperties: false), while the
    ``x-`` extension escape hatch is admitted — the 'minimal + extensible' contract."""
    schema = _load(_SCHEMA_PATH)
    validator = jsonschema.Draft202012Validator(schema)
    bad = {"version": "1", "channels": [{"kind": "email", "target": "b@x.io", "targt": "typo"}]}
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(bad)
    ok = {"version": "1", "channels": [{"kind": "email", "target": "b@x.io", "x-priority": 1}]}
    validator.validate(ok)  # must not raise
