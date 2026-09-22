from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from beadhive.beads_command_models import (
    BdBriefDependency,
    BdBriefDepsShowResult,
    BdBriefListResult,
    BdBriefReadyResult,
    BdCommandError,
    BdListResult,
    BdReadyResult,
    BdShowDependency,
    BdShowResult,
)
from beadhive.beads_models import BdDependencyRecord, BdIssueRecord

FIXTURES = Path(__file__).parents[1] / "tests/fixtures/beads/v1.3.0"
BRIEF_DEPENDENCY_FIELDS = {
    "id",
    "title",
    "status",
    "priority",
    "issue_type",
    "created_at",
    "updated_at",
    "dependency_type",
}


def _fixture(name: str):
    return json.loads((FIXTURES / name).read_text())


def test_measured_list_and_ready_rows_validate_against_command_contracts():
    list_row = _fixture("measured-list-row.json")
    ready = _fixture("measured-ready.json")

    assert BdListResult.model_validate([list_row]).root[0].parent == "bh-rfp5z"
    assert BdReadyResult.model_validate(ready).root[0].id == "bh-merge-slot"


def test_list_composes_command_fields_without_relaxing_the_canonical_model():
    list_row = _fixture("measured-list-row.json")

    composed = BdListResult.model_validate([list_row]).root[0]
    assert composed.dependency_count == 2
    assert composed.dependent_count == 0
    assert composed.comment_count == 0
    assert not issubclass(type(composed), BdIssueRecord)

    with pytest.raises(ValidationError) as raised:
        BdIssueRecord.model_validate(list_row)
    for command_field in ("parent", "dependency_count", "dependent_count", "comment_count"):
        assert command_field in str(raised.value)


def test_measured_brief_ready_row_validates_and_heavy_fields_stay_forbidden():
    ready_fixture = _fixture("measured-ready-brief.json")
    list_fixture = _fixture("measured-list-brief.json")
    assert BdBriefReadyResult.model_validate(ready_fixture).root[0].id == "bh-merge-slot"
    assert BdBriefListResult.model_validate(list_fixture).root[0].id == "bh-rfp5z.1"

    ready_fixture[0]["description"] = "brief output must not grow this field"
    with pytest.raises(ValidationError, match="description"):
        BdBriefReadyResult.model_validate(ready_fixture)


def test_measured_show_row_validates_with_command_decorations():
    result = BdShowResult.model_validate(_fixture("measured-show.json"))
    row = result.root[0]
    assert row.revision == "-4644301661091750736"
    assert row.dependency_count == row.dependent_count == row.comment_count == 0

    dependency = BdShowDependency.model_validate(_fixture("measured-show-dependency.json"))
    assert dependency.id == "bh-1altw"
    assert dependency.dependency_type == "blocks"


def test_brief_deps_are_exact_shallow_issue_rows_not_dependency_records():
    result = BdBriefDepsShowResult.model_validate(_fixture("measured-show-brief-deps.json"))
    dependencies = result.root[0].dependencies
    assert dependencies
    assert set(BdBriefDependency.model_fields) == BRIEF_DEPENDENCY_FIELDS
    assert not issubclass(BdBriefDependency, BdDependencyRecord)
    assert not issubclass(BdShowDependency, BdDependencyRecord)
    assert dependencies[0].dependency_type == "blocks"

    raw = _fixture("measured-show-brief-deps.json")[0]["dependencies"][0]
    with pytest.raises(ValidationError):
        BdDependencyRecord.model_validate(raw)


@pytest.mark.parametrize("extra", ["description", "labels", "metadata", "lease_expires_at"])
def test_brief_dependency_rejects_every_non_identity_decoration(extra):
    raw = _fixture("measured-show-brief-deps.json")[0]["dependencies"][0]
    raw[extra] = "not retained"
    with pytest.raises(ValidationError, match=extra):
        BdBriefDependency.model_validate(raw)


def test_array_commands_are_bare_while_typed_errors_are_version_enveloped():
    for fixture_name in (
        "measured-ready.json",
        "measured-list-brief.json",
        "measured-ready-brief.json",
        "measured-show.json",
        "measured-show-brief-deps.json",
    ):
        payload = _fixture(fixture_name)
        assert isinstance(payload, list)
        assert all("schema_version" not in row for row in payload)

    with pytest.raises(ValidationError):
        BdListResult.model_validate(
            {"schema_version": 1, "issues": [_fixture("measured-list-row.json")]}
        )

    error_fixture = _fixture("measured-command-error.json")
    error = BdCommandError.model_validate(error_fixture)
    assert error.schema_version == 1
    assert error.error == "no issues found matching the provided IDs"


def test_typed_error_contract_is_closed_and_version_specific():
    fixture = _fixture("measured-command-error.json")
    fixture["schema_version"] = 2
    with pytest.raises(ValidationError, match="schema_version"):
        BdCommandError.model_validate(fixture)

    fixture = _fixture("measured-command-error.json")
    fixture["exit_code"] = 1
    with pytest.raises(ValidationError, match="exit_code"):
        BdCommandError.model_validate(fixture)
