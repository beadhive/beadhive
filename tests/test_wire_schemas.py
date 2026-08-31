"""Published wire release validity and compatibility-policy proofs."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
WIRE = ROOT / "docs" / "schemas" / "wire" / "v1.1.0"
_SPEC = importlib.util.spec_from_file_location(
    "check_wire_schema_compat", ROOT / "scripts" / "check_wire_schema_compat.py"
)
assert _SPEC is not None and _SPEC.loader is not None
_COMPAT = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _COMPAT
_SPEC.loader.exec_module(_COMPAT)
FilesystemReader = _COMPAT.FilesystemReader
compatibility_errors = _COMPAT.compatibility_errors
compare_releases = _COMPAT.compare_releases
load_repository = _COMPAT.load_repository


def _schema(name: str) -> dict:
    return json.loads((WIRE / name).read_text())


def test_release_manifest_schemas_and_conformance_fixtures_are_valid() -> None:
    release = load_repository(FilesystemReader(ROOT)).latest
    fixtures = json.loads((WIRE / "conformance.json").read_text())
    cases = {case["name"]: case for case in fixtures["cases"]}

    assert release.version == "1.1.0"
    assert set(release.artifacts) == {
        "urn:beadhive:wire-schema:bh.hive-onboard:1",
        "urn:beadhive:wire-schema:bh.hive-ready:1",
        "urn:beadhive:wire-schema:bh.hive-status:1",
        "urn:beadhive:wire-schema:bh.hive-survey:1",
        "urn:beadhive:wire-schema:factory.bead-node:1",
        "urn:beadhive:wire-schema:factory.hive-info:1",
        "urn:beadhive:wire-schema:factory.snapshot:1",
    }
    mismatch = cases["factory-snapshot-version-mismatch-wins"]
    assert mismatch["input"]["schemaVersion"] == 2
    assert mismatch["input"]["hives"] == "hostile"
    assert mismatch["decoder_result"] == {
        "ok": False,
        "reason": "schema-mismatch",
        "expected": 1,
        "received": 2,
    }


def test_command_schemas_accept_the_existing_emitted_shapes() -> None:
    status = {
        "candidates": ["github/acme/new"],
        "collisions": [{"prefix": "ac", "hives": ["acme/one", "acme/two"]}],
        "violations": ["acme/repo: bad != ac-*"],
        "hives": [
            {
                "provider": "github",
                "org": "acme",
                "repo": "repo",
                "prefix": "ac-repo",
                "kind": "fork",
                "upstream": "upstream/repo",
            }
        ],
    }
    survey = [
        {
            "repo": "github/acme/repo",
            "registered": True,
            "classification": "fork upstream=upstream/repo",
            "commits": 3,
            "last_commit": "2026-08-30",
            "age_days": 0.5,
            "ahead": None,
            "behind": None,
            "dirty_branches": 0,
            "disk": "12.0 KiB",
            "disk_bytes": 12288,
            "difficulty": "easy",
        }
    ]

    Draft202012Validator(_schema("bh-hive-status-v1.schema.json")).validate(status)
    Draft202012Validator(_schema("bh-hive-survey-v1.schema.json")).validate(survey)


def test_hive_info_preserves_triplet_identity_without_a_derived_id() -> None:
    hive_info = _schema("factory-hive-info-v1.schema.json")
    embedded = _schema("factory-snapshot-v1.schema.json")["$defs"]["HiveInfo"]

    assert set(hive_info["properties"]) == {"prefix", "provider", "org", "repo", "kind"}
    assert "id" not in hive_info["properties"]
    assert embedded["properties"] == hive_info["properties"]
    assert embedded["required"] == hive_info["required"]


def test_compatibility_gate_allows_an_optional_result_property() -> None:
    old = {
        "type": "object",
        "required": ["id"],
        "additionalProperties": True,
        "properties": {"id": {"type": "string"}},
    }
    new = deepcopy(old)
    new["properties"]["detail"] = {"type": ["string", "null"]}

    assert compatibility_errors(old, new) == []


@pytest.mark.parametrize(
    "old_policy",
    [
        {"additionalProperties": False},
        {"unevaluatedProperties": False},
        {"not": {"required": ["forbidden"]}},
    ],
)
def test_optional_property_addition_requires_a_provably_open_old_object(
    old_policy: dict[str, object],
) -> None:
    old = {"type": "object", "properties": {"id": {"type": "string"}}, **old_policy}
    candidate = deepcopy(old)
    candidate["properties"]["detail"] = {"type": "string"}

    assert any(
        "optional property addition is not provably compatible" in error
        for error in compatibility_errors(old, candidate)
    )


@pytest.mark.parametrize(
    "members",
    [
        ["complete", "partial", "unavailable", "stale"],
        ["complete", "partial"],
    ],
)
def test_compatibility_gate_rejects_closed_union_membership_changes(members: list[str]) -> None:
    old = _schema("factory-snapshot-v1.schema.json")
    candidate = deepcopy(old)
    candidate["$defs"]["SourceCoverage"]["properties"]["state"]["enum"] = members

    errors = compatibility_errors(old, candidate)

    assert errors
    assert "closed-union members changed" in errors[0]


def test_compatibility_gate_rejects_removal_retyping_and_requiredness_changes() -> None:
    old = {
        "type": "object",
        "required": ["id"],
        "additionalProperties": True,
        "properties": {"id": {"type": "string"}, "detail": {"type": "string"}},
    }
    removed = deepcopy(old)
    del removed["properties"]["detail"]
    retyped = deepcopy(old)
    retyped["properties"]["detail"] = {"type": "integer"}
    required = deepcopy(old)
    required["required"].append("detail")

    assert any("property was removed" in error for error in compatibility_errors(old, removed))
    assert any("type changed" in error for error in compatibility_errors(old, retyped))
    assert any(
        "required properties changed" in error for error in compatibility_errors(old, required)
    )


def test_same_major_release_gate_rejects_not_constraint_mutation() -> None:
    """Review regression: valid JSON Schema keywords must never be silently ignored."""
    release = load_repository(FilesystemReader(ROOT)).latest
    artifact_id = "urn:beadhive:wire-schema:factory.snapshot:1"
    old_artifact = release.artifacts[artifact_id]
    candidate_schema = deepcopy(old_artifact.schema)
    candidate_schema["not"] = {}
    fixtures = json.loads((WIRE / "conformance.json").read_text())
    valid_payload = next(
        case["input"] for case in fixtures["cases"] if case["name"] == "factory-snapshot-valid"
    )
    Draft202012Validator(old_artifact.schema).validate(valid_payload)
    assert list(Draft202012Validator(candidate_schema).iter_errors(valid_payload))
    candidate_artifact = replace(old_artifact, schema=candidate_schema)
    candidate_artifacts = dict(release.artifacts)
    candidate_artifacts[artifact_id] = candidate_artifact
    candidate_release = replace(release, version="1.0.1", artifacts=candidate_artifacts)

    errors = compare_releases(release, candidate_release)

    assert any("unsupported compatibility keyword 'not' changed" in error for error in errors)


def test_compatibility_gate_rejects_unsupported_ref_sibling_constraint() -> None:
    old = _schema("factory-snapshot-v1.schema.json")
    candidate = deepcopy(old)
    # Draft 2020-12 evaluates this sibling together with the referenced FactoryCoverage schema.
    # The old checker resolved `$ref` first and silently dropped `not`, returning a false green.
    candidate["properties"]["coverage"]["not"] = {}

    errors = compatibility_errors(old, candidate)

    assert any("$.coverage: unsupported compatibility keyword 'not'" in error for error in errors)


def test_compatibility_gate_follows_unchanged_allof_ref_to_changed_definition() -> None:
    """Review regression: unchanged applicators must not hide changed referenced assertions."""
    old = {
        "$defs": {"guard": {"type": "object"}},
        "allOf": [{"$ref": "#/$defs/guard"}],
    }
    candidate = deepcopy(old)
    candidate["$defs"]["guard"] = {"not": {}}
    Draft202012Validator(old).validate({})
    assert list(Draft202012Validator(candidate).iter_errors({}))

    errors = compatibility_errors(old, candidate)

    assert any(
        "$.allOf[0]->$ref: unsupported compatibility keyword 'not' changed" in error
        for error in errors
    )


def test_compatibility_gate_follows_unchanged_dynamic_ref_to_changed_definition() -> None:
    """Review regression: dynamic resolution must not hide a changed definition assertion."""
    old = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$defs": {"guard": {"$dynamicAnchor": "guard", "type": "string"}},
        "$dynamicRef": "#guard",
    }
    candidate = deepcopy(old)
    candidate["$defs"]["guard"] = {"$dynamicAnchor": "guard", "type": "integer"}
    Draft202012Validator(old).validate("x")
    assert list(Draft202012Validator(candidate).iter_errors("x"))

    errors = compatibility_errors(old, candidate)

    assert any("$.$defs.guard: type changed" in error for error in errors)


@pytest.mark.parametrize("reference_keyword", ["$ref", "$dynamicRef", "$recursiveRef"])
def test_compatibility_gate_conservatively_compares_recursive_definition_targets(
    reference_keyword: str,
) -> None:
    old = {
        "$defs": {
            "guard": {
                "$anchor": "guard",
                "type": "string",
                "allOf": [{"$ref": "#/$defs/guard"}],
            }
        },
        reference_keyword: "#guard",
    }
    candidate = deepcopy(old)
    candidate["$defs"]["guard"]["type"] = "integer"

    assert any(
        "$.$defs.guard: type changed" in error for error in compatibility_errors(old, candidate)
    )


@pytest.mark.parametrize(
    ("keyword", "container"),
    [
        ("allOf", [{"$ref": "#/$defs/guard"}]),
        ("anyOf", [{"$ref": "#/$defs/guard"}]),
        ("oneOf", [{"$ref": "#/$defs/guard"}]),
        ("not", {"$ref": "#/$defs/guard"}),
        ("if", {"$ref": "#/$defs/guard"}),
        ("then", {"$ref": "#/$defs/guard"}),
        ("else", {"$ref": "#/$defs/guard"}),
        ("contains", {"$ref": "#/$defs/guard"}),
        ("dependentSchemas", {"flag": {"$ref": "#/$defs/guard"}}),
        ("additionalProperties", {"$ref": "#/$defs/guard"}),
    ],
)
def test_compatibility_gate_follows_refs_from_schema_applicators(
    keyword: str, container: object
) -> None:
    old = {"$defs": {"guard": {"type": "string"}}, keyword: container}
    candidate = deepcopy(old)
    candidate["$defs"]["guard"] = {"type": "integer"}

    assert any("type changed" in error for error in compatibility_errors(old, candidate))


@pytest.mark.parametrize(
    ("keyword", "constraint"),
    [
        ("allOf", [{"type": "object"}]),
        ("if", {"required": ["id"]}),
        ("unevaluatedProperties", False),
    ],
)
def test_compatibility_gate_fails_closed_for_other_unimplemented_keywords(
    keyword: str, constraint: object
) -> None:
    old = {"type": "object"}
    candidate = {"type": "object", keyword: constraint}

    assert any(
        f"unsupported compatibility keyword {keyword!r}" in error
        for error in compatibility_errors(old, candidate)
    )


@pytest.mark.parametrize(
    ("mutation", "diagnostic_path"),
    [
        ("root-not", "$"),
        ("ref-sibling-not", "$.coverage"),
        ("allof-changed-def", "$.allOf[0]->$ref"),
        ("dynamicref-changed-def", "$.$defs.CompatGuard"),
    ],
)
def test_actual_gate_cli_rejects_same_major_not_mutations(
    tmp_path: Path, mutation: str, diagnostic_path: str
) -> None:
    """Exercise main(), Git baseline loading, release validation, and the exit status together."""
    repo = tmp_path / "wire-gate-repo"
    shutil.copytree(ROOT / "docs" / "schemas" / "wire", repo / "docs" / "schemas" / "wire")
    (repo / "scripts").mkdir()
    shutil.copy2(
        ROOT / "scripts" / "check_wire_schema_compat.py",
        repo / "scripts" / "check_wire_schema_compat.py",
    )
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Wire Gate Test")
    _git(repo, "config", "user.email", "wire-gate@example.invalid")
    if mutation in {"allof-changed-def", "dynamicref-changed-def"}:
        baseline_schema_path = (
            repo / "docs" / "schemas" / "wire" / "v1.1.0" / "factory-snapshot-v1.schema.json"
        )
        baseline_schema = json.loads(baseline_schema_path.read_text())
        baseline_schema["$defs"]["CompatGuard"] = {
            "$dynamicAnchor": "compat-guard",
            "type": "object",
        }
        if mutation == "allof-changed-def":
            baseline_schema["allOf"] = [{"$ref": "#/$defs/CompatGuard"}]
        else:
            baseline_schema["$dynamicRef"] = "#compat-guard"
        baseline_schema_path.write_text(json.dumps(baseline_schema, indent=2) + "\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "baseline release")
    _git(repo, "branch", "baseline")
    _git(repo, "switch", "-qc", "candidate")

    wire = repo / "docs" / "schemas" / "wire"
    candidate_release = wire / "v1.1.1"
    shutil.copytree(wire / "v1.1.0", candidate_release)
    _rewrite_json(candidate_release / "release.json", release_version="1.1.1")
    fixtures = _rewrite_json(candidate_release / "conformance.json", release_version="1.1.1")
    artifact_id = "urn:beadhive:wire-schema:factory.snapshot:1"
    for case in fixtures["cases"]:
        if case["artifact_id"] == artifact_id:
            case["schema_valid"] = False
    (candidate_release / "conformance.json").write_text(json.dumps(fixtures, indent=2) + "\n")
    index = json.loads((wire / "index.json").read_text())
    index["latest"] = "1.1.1"
    index["releases"].append({"version": "1.1.1", "major": 1, "manifest": "v1.1.1/release.json"})
    (wire / "index.json").write_text(json.dumps(index, indent=2) + "\n")

    schema_path = candidate_release / "factory-snapshot-v1.schema.json"
    schema = json.loads(schema_path.read_text())
    if mutation == "root-not":
        schema["not"] = {}
    elif mutation == "ref-sibling-not":
        schema["properties"]["coverage"]["not"] = {}
    elif mutation == "allof-changed-def":
        schema["$defs"]["CompatGuard"] = {"not": {}}
    else:
        schema["$defs"]["CompatGuard"] = {
            "$dynamicAnchor": "compat-guard",
            "not": {},
        }
    schema_path.write_text(json.dumps(schema, indent=2) + "\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", f"candidate {mutation}")

    env = os.environ.copy()
    env["BH_WIRE_SCHEMA_BASE_REF"] = "baseline"
    result = subprocess.run(
        [sys.executable, "scripts/check_wire_schema_compat.py"],
        cwd=repo,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "wire-schema-compat: incompatible change:" in result.stderr
    assert (
        f"{artifact_id} {diagnostic_path}: unsupported compatibility keyword 'not' changed"
        in result.stderr
    )
    assert "initial release validated" not in result.stdout
    assert _git(repo, "rev-parse", "baseline").stdout != _git(repo, "rev-parse", "HEAD").stdout


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _rewrite_json(path: Path, **updates: object) -> dict:
    value = json.loads(path.read_text())
    value.update(updates)
    path.write_text(json.dumps(value, indent=2) + "\n")
    return value
