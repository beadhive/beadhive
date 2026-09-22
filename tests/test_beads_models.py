from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from beadhive.beads_models import BdBriefIssue, BdDependencyRecord, BdIssueRecord

ROOT = Path(__file__).parents[1]
CONTRACT = ROOT / "src/beadhive/schemas/beads/v1.3.0/schema.json"
FIXTURES = ROOT / "tests/fixtures/beads/v1.3.0"
HEAVY_PROPERTIES = {
    "description",
    "design",
    "acceptance_criteria",
    "notes",
    "payload",
    "waiters",
}


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def test_checked_in_models_are_current_for_the_vendored_artifact():
    result = subprocess.run(
        [sys.executable, "scripts/generate_beads_models.py", "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_models_are_closed_and_require_exactly_the_upstream_required_fields():
    document = json.loads(CONTRACT.read_text())
    expected = {
        BdIssueRecord: set(document["types"]["issue"]["required"]),
        BdDependencyRecord: set(document["types"]["dependency"]["required"]),
    }
    for model, required in expected.items():
        assert model.model_config["extra"] == "forbid"
        actual = {name for name, field in model.model_fields.items() if field.is_required()}
        assert actual == required


@pytest.mark.parametrize("fixture_name", ["canonical-export.json", "canonical-import.json"])
def test_representative_canonical_import_and_export_records_validate(fixture_name):
    fixture = _fixture(fixture_name)
    issue = BdIssueRecord.model_validate(fixture["issue"])
    dependency = BdDependencyRecord.model_validate(fixture["dependency"])
    assert issue.id == fixture["issue"]["id"]
    assert dependency.issue_id == fixture["dependency"]["issue_id"]


@pytest.mark.parametrize(
    ("model", "fixture_key"),
    [(BdIssueRecord, "issue"), (BdDependencyRecord, "dependency")],
)
def test_unknown_property_is_rejected_and_named(model, fixture_key):
    record = _fixture("canonical-import.json")[fixture_key]
    record["upstream_added_property"] = True
    with pytest.raises(ValidationError, match="upstream_added_property"):
        model.model_validate(record)


@pytest.mark.parametrize(
    ("field", "value"),
    [("status", "review"), ("issue_type", "initiative")],
)
def test_out_of_set_issue_enums_fail(field, value):
    record = _fixture("canonical-import.json")["issue"]
    record[field] = value
    with pytest.raises(ValidationError, match=field):
        BdIssueRecord.model_validate(record)


def test_out_of_set_dependency_type_fails():
    record = _fixture("canonical-import.json")["dependency"]
    record["type"] = "requires"
    with pytest.raises(ValidationError, match="type"):
        BdDependencyRecord.model_validate(record)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("description", None),
        ("description", 1),
        ("priority", "1"),
        ("priority", True),
        ("is_blocked", 1),
    ],
)
def test_issue_model_rejects_the_same_optional_nulls_and_primitive_coercions_as_schema(
    field, value
):
    record = _fixture("canonical-import.json")["issue"]
    record[field] = value
    schema = json.loads(CONTRACT.read_text())["types"]["issue"]
    assert list(Draft202012Validator(schema).iter_errors(record))
    with pytest.raises(ValidationError, match=field):
        BdIssueRecord.model_validate(record)


def test_omitted_non_nullable_property_stays_omitted_from_model_and_generated_schema():
    record = _fixture("canonical-import.json")["issue"]
    issue = BdIssueRecord.model_validate(record)
    assert "description" not in issue.model_dump()
    description_schema = BdIssueRecord.model_json_schema()["properties"]["description"]
    assert description_schema["type"] == "string"
    assert "anyOf" not in description_schema


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("issue_id", None),
        ("issue_id", 1),
        ("created_at", True),
        ("metadata", None),
        ("metadata", {"source": "not-a-string"}),
    ],
)
def test_dependency_model_rejects_the_same_nulls_and_primitive_coercions_as_schema(field, value):
    record = _fixture("canonical-export.json")["dependency"]
    record[field] = value
    schema = json.loads(CONTRACT.read_text())["types"]["dependency"]
    assert list(Draft202012Validator(schema).iter_errors(record))
    with pytest.raises(ValidationError, match=field):
        BdDependencyRecord.model_validate(record)


def test_brief_issue_is_issue_schema_minus_exactly_the_six_heavy_properties():
    assert BdBriefIssue.model_config["extra"] == "forbid"
    assert set(BdBriefIssue.model_fields) == set(BdIssueRecord.model_fields) - HEAVY_PROPERTIES
    assert not (HEAVY_PROPERTIES & BdBriefIssue.model_fields.keys())
    assert "partial" not in BdBriefIssue.model_fields
    assert "partialness" not in BdBriefIssue.model_fields


def test_measured_list_row_correctly_fails_the_canonical_issue_contract():
    with pytest.raises(ValidationError) as raised:
        BdIssueRecord.model_validate(_fixture("measured-list-row.json"))
    message = str(raised.value)
    for command_property in ("parent", "dependency_count", "dependent_count", "comment_count"):
        assert command_property in message
