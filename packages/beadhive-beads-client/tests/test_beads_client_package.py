"""Artifact and import boundary checks for the stand-alone workspace wheel."""

from __future__ import annotations

import ast
import hashlib
import json
from importlib.resources import files
from pathlib import Path

from beadhive_beads_client import (
    BeadsSession,
    cli_compatibility_operations,
    load_operation_matrix,
)
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


DOWNSTREAM_OPERATIONS = {
    "context.verify",
    "work.issue.get",
    "work.issue.list",
    "work.ready.list",
    "work.dependencies.list",
    "work.dependencies.add",
    "work.dependencies.remove",
    "work.issue.update",
    "work.claim.acquire",
    "work.claim-next",
    "work.claim.release",
    "work.issue.close",
    "work.issue.reopen",
    "work.feedback.comment.add",
    "work.gate.lookup",
    "work.gate.create",
    "work.gate.resolve",
    "work.review.submit",
    "work.review.inspect",
    "work.review.approve",
    "work.review.bounce",
    "work.validation.check",
    "work.state.get",
    "work.state.update",
    "work.lease.acquire",
    "work.lease.heartbeat",
    "work.lease.reclaim",
    "work.lease.release",
    "work.merge-slot.check",
    "work.merge-slot.create",
    "work.merge-slot.acquire",
    "work.merge-slot.release",
    "work.merge",
    "work.molecule.progress",
    "work.swarm.inspect",
    "work.dispatch.poll",
    "work.local-loop.state",
    "plan.issue.get",
    "plan.issue.list",
    "plan.issue.create",
    "plan.issue.update",
    "plan.dependencies.list",
    "plan.dependencies.add",
    "plan.dependencies.remove",
    "plan.labels.update",
    "plan.metadata.compare-and-set",
    "plan.feedback.comment.add",
    "plan.gate.lookup",
    "plan.gate.create",
    "plan.gate.resolve",
    "plan.kickoff.get",
    "plan.kickoff.update",
    "plan.molecule.file",
    "plan.molecule.verify",
    "plan.molecule.repair",
    "plan.batch-create.atomic",
    "plan.batch-apply.atomic",
    "plan.batch-close.partial",
    "plan.partial-failure.reconcile",
}


def test_versioned_operation_matrix_covers_filed_downstream_contract() -> None:
    installed = json.loads(
        files("beadhive_beads_client").joinpath("operation_matrix_v1.json").read_text()
    )
    matrix = load_operation_matrix()
    assert matrix == installed
    assert matrix["version"] == 1
    assert matrix["beads_release"] == "1.3.0"
    rows = matrix["operations"]
    names = [row["name"] for row in rows]
    assert len(names) == len(set(names))
    assert DOWNSTREAM_OPERATIONS <= set(names)
    assert {row["classification"] for row in rows} == {
        "api-ready",
        "cli-compatibility",
        "administrative",
        "denied",
    }
    categories = {row["name"]: row["classification"] for row in rows}
    assert categories["work.claim-next"] == "api-ready"
    assert categories["work.gate.lookup"] == "cli-compatibility"
    assert categories["work.gate.create"] == "cli-compatibility"
    assert categories["work.gate.resolve"] == "cli-compatibility"
    assert categories["plan.batch-apply.atomic"] == "cli-compatibility"
    assert categories["issues.delete"] == categories["issues.sweep"] == "denied"


def test_matrix_api_and_cli_routes_match_the_supported_session_surface() -> None:
    rows = load_operation_matrix()["operations"]
    for row in rows:
        if row["classification"] == "api-ready":
            assert "real-service" in row["evidence"]
            assert callable(getattr(BeadsSession, row["session_method"]))
            assert "reason" not in row
        else:
            assert "session_method" not in row
            assert row.get("reason")
    assert cli_compatibility_operations() == {
        row["name"]
        for row in rows
        if row["classification"] in {"cli-compatibility", "administrative"}
    }
