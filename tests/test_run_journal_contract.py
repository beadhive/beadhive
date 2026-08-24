"""Machine gate for the run-journal v1 design contract (bh-e8s3i.1).

The implementation lands in later beads. This test keeps the schema, worked JSONL example, and
acceptance-critical identity/redaction properties executable in the meantime.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

jsonschema = pytest.importorskip("jsonschema")

_SCHEMAS = Path(__file__).resolve().parents[1] / "docs" / "schemas"
_SCHEMA_PATH = _SCHEMAS / "run-journal-v1.schema.json"
_EXAMPLE_PATH = _SCHEMAS / "run-journal-v1.example.jsonl"
_IDENTITY = ("run_id", "hive", "bead", "driver", "provider", "manifest_digest")


def _schema() -> dict:
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


def _records() -> list[dict]:
    return [json.loads(line) for line in _EXAMPLE_PATH.read_text(encoding="utf-8").splitlines()]


def test_schema_is_valid_and_validates_every_complete_example_line() -> None:
    schema = _schema()
    jsonschema.Draft202012Validator.check_schema(schema)
    validator = jsonschema.Draft202012Validator(schema)
    records = _records()
    assert records
    for record in records:
        validator.validate(record)


def test_example_has_one_immutable_outer_identity_and_opaque_revisions() -> None:
    records = _records()
    expected = tuple(records[0][key] for key in _IDENTITY)
    assert all(tuple(record[key] for key in _IDENTITY) == expected for record in records)
    revisions = [record["source_revision"] for record in records]
    assert len(revisions) == len(set(revisions))
    assert all(isinstance(record["timestamp_ms"], int) for record in records)


def test_provider_continuation_is_required_but_separate_from_outer_run_id() -> None:
    records = _records()
    assert all("provider_continuation" in record for record in records)
    observed = [
        record["provider_continuation"] for record in records if record["provider_continuation"]
    ]
    assert observed
    assert all(value != records[0]["run_id"] for value in observed)


@pytest.mark.parametrize(
    "required",
    [
        "version",
        "source_revision",
        "timestamp_ms",
        "run_id",
        "hive",
        "bead",
        "driver",
        "provider",
        "manifest_digest",
        "provider_continuation",
        "writer",
        "activity",
    ],
)
def test_every_correlation_field_is_required(required: str) -> None:
    record = copy.deepcopy(_records()[0])
    del record[required]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(_schema()).validate(record)


def test_optional_bead_means_exact_id_or_null_not_empty_string() -> None:
    validator = jsonschema.Draft202012Validator(_schema())
    record = copy.deepcopy(_records()[0])
    record["bead"] = None
    validator.validate(record)
    record["bead"] = ""
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(record)


@pytest.mark.parametrize("forbidden", ["credentials", "task", "prompt", "stdout", "argv", "env"])
def test_activity_is_allowlisted_not_a_content_dump(forbidden: str) -> None:
    record = copy.deepcopy(_records()[0])
    record["activity"][forbidden] = "must never be serialized"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(_schema()).validate(record)


def test_legacy_session_id_cannot_replace_either_identity() -> None:
    record = copy.deepcopy(_records()[0])
    record["session_id"] = record["run_id"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(_schema()).validate(record)
