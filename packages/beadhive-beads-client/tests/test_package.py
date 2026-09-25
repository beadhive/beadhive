"""Artifact and import boundary checks for the stand-alone workspace wheel."""

from __future__ import annotations

import ast
import hashlib
import json
from importlib.resources import files
from pathlib import Path

from beads_v1_3.models import ContextResponse, IssuesPage, Problem, ReadyPage

PACKAGE = Path(__file__).resolve().parents[1]


def test_pinned_wire_models_and_artifact() -> None:
    expected = (PACKAGE / "spec/SHA256").read_text().split()[0]
    actual = hashlib.sha256((PACKAGE / "spec/openapi.v0.yaml").read_bytes()).hexdigest()
    assert actual == expected == "9a33a349e5266bdba916246594a4fc457c63e39d7affb5acec9bd5b75acde4dd"
    assert (
        ContextResponse.from_dict(
            {
                "api_version": "v0",
                "bd_version": "1.3.0",
                "schema_version": 1,
                "backend": "dolt",
                "dolt_mode": "embedded",
                "database": "scratch",
                "project_id": "project",
                "capabilities": ["project.enforce"],
            }
        ).project_id
        == "project"
    )
    assert IssuesPage.from_dict({"items": [], "has_more": False}).items == []
    assert ReadyPage.from_dict({"items": [], "has_more": False}).items == []
    assert (
        Problem.from_dict(
            {"status": 404, "title": "Not Found", "code": "not_found", "request_id": "r1"}
        ).code
        == "not_found"
    )


def test_package_imports_do_not_reach_root_or_legacy_implementations() -> None:
    forbidden = {"beadhive", "beadhive_pants", "bd", "dolt", "tests"}
    for file in (PACKAGE / "src").rglob("*.py"):
        tree = ast.parse(file.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(alias.name.split(".")[0] not in forbidden for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                assert node.module.split(".")[0] not in forbidden


def test_versioned_operation_matrix_is_shipped_without_destructive_exports() -> None:
    matrix = json.loads(
        files("beadhive_beads_client").joinpath("operation_matrix_v1.json").read_text()
    )
    assert matrix["version"] == 1
    assert matrix["beads_release"] == "1.3.0"
    categories = {row["name"]: row["classification"] for row in matrix["operations"]}
    assert categories["work.claim-next"] == "api-ready"
    assert categories["plan.batch-apply"] == "cli-compatibility"
    assert categories["issues.delete"] == categories["issues.sweep"] == "denied"
