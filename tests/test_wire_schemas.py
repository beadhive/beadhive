"""Published wire release validity and compatibility-policy proofs."""

from __future__ import annotations

import hashlib
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
WIRE = ROOT / "docs" / "schemas" / "wire" / "v1.5.0"
_SPEC = importlib.util.spec_from_file_location(
    "check_wire_schema_compat", ROOT / "scripts" / "check_wire_schema_compat.py"
)
assert _SPEC is not None and _SPEC.loader is not None
_COMPAT = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _COMPAT
_SPEC.loader.exec_module(_COMPAT)
FilesystemReader = _COMPAT.FilesystemReader
compatibility_errors = _COMPAT.compatibility_errors
catalog_compatibility_errors = _COMPAT.catalog_compatibility_errors
compare_releases = _COMPAT.compare_releases
load_repository = _COMPAT.load_repository


def _schema(name: str) -> dict:
    return json.loads((WIRE / name).read_text())


def test_release_manifest_schemas_and_conformance_fixtures_are_valid() -> None:
    release = load_repository(FilesystemReader(ROOT)).latest
    fixtures = json.loads((WIRE / "conformance.json").read_text())
    cases = {case["name"]: case for case in fixtures["cases"]}

    assert release.version == "1.5.0"
    assert set(release.artifacts) == {
        "urn:beadhive:wire-schema:bh.hive-onboard:1",
        "urn:beadhive:wire-schema:bh.hive-ready:1",
        "urn:beadhive:wire-schema:bh.hive-status:1",
        "urn:beadhive:wire-schema:bh.hive-survey:1",
        "urn:beadhive:wire-schema:config:1",
        "urn:beadhive:wire-schema:factory.bead-node:1",
        "urn:beadhive:wire-schema:factory.hive-info:1",
        "urn:beadhive:wire-schema:factory.snapshot:1",
        "urn:beadhive:wire-schema:json-value:1",
        "urn:beadhive:wire-schema:operation-catalog:1",
        "urn:beadhive:wire-schema:plugin-config:herdr:1",
        "urn:beadhive:wire-schema:plugin-config:hitch:1",
        "urn:beadhive:wire-schema:plugin-config:observaloop:1",
        "urn:beadhive:wire-schema:plugin-config:orca:1",
        "urn:beadhive:wire-schema:plugin-config:repowise:1",
        "urn:beadhive:wire-schema:plugin-manifest:1",
        "urn:beadhive:wire-catalog:operations:1",
    }
    assert release.artifacts["urn:beadhive:wire-schema:operation-catalog:1"].artifact_type == (
        "json-schema"
    )
    assert release.artifacts["urn:beadhive:wire-catalog:operations:1"].artifact_type == (
        "operation-catalog-data"
    )
    with pytest.raises(TypeError, match="not JSON Schema"):
        _ = release.artifacts["urn:beadhive:wire-catalog:operations:1"].schema
    mismatch = cases["factory-snapshot-version-mismatch-wins"]
    assert mismatch["input"]["schemaVersion"] == 2
    assert mismatch["input"]["hives"] == "hostile"
    assert mismatch["decoder_result"] == {
        "ok": False,
        "reason": "schema-mismatch",
        "expected": 1,
        "received": 2,
    }


def test_plugin_manifest_schema_has_deterministic_bytes_and_forbids_secret_values() -> None:
    path = WIRE / "plugin-manifest-v1.schema.json"
    raw = path.read_text()
    schema = json.loads(raw)
    fixtures = json.loads((WIRE / "conformance.json").read_text())
    cases = {case["name"]: case for case in fixtures["cases"]}

    assert hashlib.sha256(raw.encode()).hexdigest() == (
        "18d2401fc261b47619c96d0ee058f2c5edb228e92af9b959e462d4b844f4aefc"
    )
    validator = Draft202012Validator(schema)
    validator.validate(cases["plugin-manifest-valid"]["input"])
    assert list(validator.iter_errors(cases["plugin-manifest-secret-value-invalid"]["input"]))


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


def _catalog() -> dict:
    return _schema("operation-catalog-v1.json")


def _operation(catalog: dict, name: str) -> dict:
    return next(operation for operation in catalog["operations"] if operation["name"] == name)


def test_catalog_compatibility_allows_a_unique_additive_operation() -> None:
    old = _catalog()
    candidate = deepcopy(old)
    candidate["catalog_version"] = "1.1.0"
    added = deepcopy(_operation(candidate, "probe.health"))
    added["name"] = "probe.version"
    added["surfaces"]["mcp"]["resource"] = "beadhive://probe/version"
    candidate["operations"].append(added)

    assert catalog_compatibility_errors(old, candidate) == []


def test_catalog_compatibility_allows_additive_cli_parent_but_rejects_drift() -> None:
    old = _catalog()
    additive = deepcopy(old)
    added = deepcopy(additive["cli_parents"][0])
    added["path"] = "future parent"
    additive["cli_parents"].append(added)
    assert catalog_compatibility_errors(old, additive) == []

    removed = deepcopy(old)
    removed["cli_parents"].pop(0)
    assert any(
        "canonical CLI parent was removed" in error
        for error in catalog_compatibility_errors(old, removed)
    )

    changed = deepcopy(old)
    changed["cli_parents"][0]["effective_hidden"] = True
    assert any(
        "effective_hidden: value changed" in error
        for error in catalog_compatibility_errors(old, changed)
    )


def test_catalog_compatibility_allows_only_the_audited_doctor_metadata_correction() -> None:
    old = json.loads((ROOT / "docs/schemas/wire/v1.4.0/operation-catalog-v1.json").read_text())
    candidate = _catalog()
    assert catalog_compatibility_errors(old, candidate) == []

    changed_reason = deepcopy(candidate)
    _operation(changed_reason, "doctor")["surfaces"]["cli"]["interactivity"]["reason"] = (
        "different reason"
    )
    assert any(
        "interactivity.reason" in error
        for error in catalog_compatibility_errors(old, changed_reason)
    )

    removed_guard = deepcopy(candidate)
    _operation(removed_guard, "doctor")["surfaces"]["cli"]["interactivity"]["guard_conditions"] = [
        "stdin-not-tty"
    ]
    assert any(
        "interactivity.guard_conditions" in error
        for error in catalog_compatibility_errors(old, removed_guard)
    )

    unrelated = deepcopy(candidate)
    _operation(unrelated, "config.set")["surfaces"]["cli"]["interactivity"]["mode"] = (
        "guarded-prompt"
    )
    assert any(
        "config.set" in error and "interactivity.mode: value changed" in error
        for error in catalog_compatibility_errors(old, unrelated)
    )


def test_catalog_compatibility_rejects_duplicate_operation_and_projection_identities() -> None:
    old = _catalog()
    duplicate_operation = deepcopy(old)
    duplicate_operation["operations"].append(deepcopy(_operation(old, "probe.health")))
    assert any(
        "duplicate canonical operation identity 'probe.health'" in error
        for error in catalog_compatibility_errors(old, duplicate_operation)
    )

    duplicate_projection = deepcopy(old)
    added = deepcopy(_operation(duplicate_projection, "probe.health"))
    added["name"] = "probe.version"
    duplicate_projection["operations"].append(added)
    assert any(
        "duplicate MCP resource projection 'beadhive://probe/health'" in error
        for error in catalog_compatibility_errors(old, duplicate_projection)
    )


@pytest.mark.parametrize(
    ("mutation", "diagnostic"),
    [
        ("remove", "$.operations[name='probe.health']: canonical operation was removed"),
        ("identity", "$.operations[name='probe.health']: canonical operation was removed"),
        (
            "signature",
            "$.operations[name='config.set'].parameters[0].schema.type: value changed",
        ),
        (
            "result",
            "$.operations[name='probe.health'].result_schema: value changed",
        ),
        (
            "projection",
            "$.operations[name='probe.health'].surfaces.mcp.resource: value changed",
        ),
        ("policy", "$.policy.catalog_role: value changed"),
    ],
)
def test_catalog_compatibility_rejects_existing_contract_changes(
    mutation: str, diagnostic: str
) -> None:
    old = _catalog()
    candidate = deepcopy(old)
    probe = _operation(candidate, "probe.health")
    if mutation == "remove":
        candidate["operations"].remove(probe)
    elif mutation == "identity":
        probe["name"] = "probe.status"
    elif mutation == "signature":
        _operation(candidate, "config.set")["parameters"][0]["schema"]["type"] = "integer"
    elif mutation == "result":
        probe["result_schema"] = "urn:beadhive:wire-schema:bh.hive-status:1"
    elif mutation == "projection":
        probe["surfaces"]["mcp"]["resource"] = "beadhive://probe/status"
    else:
        candidate["policy"]["catalog_role"] = "runtime dispatcher"

    assert any(diagnostic in error for error in catalog_compatibility_errors(old, candidate))


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
    candidate_artifact = replace(old_artifact, document=candidate_schema)
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
    wire = repo / "docs" / "schemas" / "wire"
    shutil.copytree(ROOT / "docs" / "schemas" / "wire", wire)
    # This fixture intentionally snapshots the historic v1.4 baseline before
    # creating a v1.4.1 candidate; later repository releases are not published
    # in the synthetic baseline.
    shutil.rmtree(wire / "v1.5.0")
    index = json.loads((wire / "index.json").read_text())
    index["releases"] = [release for release in index["releases"] if release["version"] <= "1.4.0"]
    index["latest"] = "1.4.0"
    (wire / "index.json").write_text(json.dumps(index, indent=2) + "\n")
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
            repo / "docs" / "schemas" / "wire" / "v1.4.0" / "factory-snapshot-v1.schema.json"
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

    candidate_release = wire / "v1.4.1"
    shutil.copytree(wire / "v1.4.0", candidate_release)
    _rewrite_json(candidate_release / "release.json", release_version="1.4.1")
    fixtures = _rewrite_json(candidate_release / "conformance.json", release_version="1.4.1")
    artifact_id = "urn:beadhive:wire-schema:factory.snapshot:1"
    for case in fixtures["cases"]:
        if case["artifact_id"] == artifact_id:
            case["schema_valid"] = False
    (candidate_release / "conformance.json").write_text(json.dumps(fixtures, indent=2) + "\n")
    index = json.loads((wire / "index.json").read_text())
    # This fixture deliberately exercises the historical v1.4 -> v1.4.1 path.
    # Keep later repository releases out of its isolated index.
    index["releases"] = [release for release in index["releases"] if release["version"] <= "1.4.0"]
    index["latest"] = "1.4.1"
    index["releases"].append({"version": "1.4.1", "major": 1, "manifest": "v1.4.1/release.json"})
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


def _catalog_gate_candidate(tmp_path: Path, mutation: str) -> tuple[Path, str, str]:
    repo = tmp_path / f"catalog-gate-{mutation}"
    wire = repo / "docs" / "schemas" / "wire"
    shutil.copytree(ROOT / "docs" / "schemas" / "wire", wire)
    # This fixture intentionally snapshots the historic v1.4 baseline before
    # creating a v1.4.1 candidate; later repository releases are not published
    # in the synthetic baseline.
    shutil.rmtree(wire / "v1.5.0")
    index = json.loads((wire / "index.json").read_text())
    index["releases"] = [release for release in index["releases"] if release["version"] <= "1.4.0"]
    index["latest"] = "1.4.0"
    (wire / "index.json").write_text(json.dumps(index, indent=2) + "\n")
    (repo / "scripts").mkdir()
    shutil.copy2(
        ROOT / "scripts" / "check_wire_schema_compat.py",
        repo / "scripts" / "check_wire_schema_compat.py",
    )
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Catalog Gate Test")
    _git(repo, "config", "user.email", "catalog-gate@example.invalid")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "immutable v1.4 baseline")
    _git(repo, "branch", "baseline")

    candidate_release = wire / "v1.4.1"
    shutil.copytree(wire / "v1.4.0", candidate_release)
    _rewrite_json(candidate_release / "release.json", release_version="1.4.1")
    _rewrite_json(candidate_release / "conformance.json", release_version="1.4.1")
    index = json.loads((wire / "index.json").read_text())
    # This fixture deliberately exercises the historical v1.4 -> v1.4.1 path.
    # Keep later repository releases out of its isolated index.
    index["releases"] = [release for release in index["releases"] if release["version"] <= "1.4.0"]
    index["latest"] = "1.4.1"
    index["releases"].append({"version": "1.4.1", "major": 1, "manifest": "v1.4.1/release.json"})
    (wire / "index.json").write_text(json.dumps(index, indent=2) + "\n")

    catalog_path = candidate_release / "operation-catalog-v1.json"
    catalog = json.loads(catalog_path.read_text())
    catalog["catalog_version"] = "1.1.0"
    probe = _operation(catalog, "probe.health")
    if mutation == "add":
        added = deepcopy(probe)
        added["name"] = "probe.version"
        added["surfaces"]["mcp"]["resource"] = "beadhive://probe/version"
        catalog["operations"].append(added)
        diagnostic = ""
    elif mutation == "remove":
        catalog["operations"].remove(probe)
        diagnostic = "$.operations[name='probe.health']: canonical operation was removed"
    elif mutation == "identity":
        probe["name"] = "probe.status"
        diagnostic = "$.operations[name='probe.health']: canonical operation was removed"
    elif mutation == "signature":
        _operation(catalog, "config.set")["parameters"][0]["schema"]["type"] = "integer"
        diagnostic = "$.operations[name='config.set'].parameters[0].schema.type: value changed"
    elif mutation == "result":
        probe["result_schema"] = "urn:beadhive:wire-schema:bh.hive-status:1"
        diagnostic = "$.operations[name='probe.health'].result_schema: value changed"
    elif mutation == "projection":
        probe["surfaces"]["mcp"]["resource"] = "beadhive://probe/status"
        diagnostic = "$.operations[name='probe.health'].surfaces.mcp.resource: value changed"
    else:
        catalog["policy"]["catalog_role"] = "runtime dispatcher"
        diagnostic = "$.policy.catalog_role: value changed"
    catalog_path.write_text(json.dumps(catalog, indent=2) + "\n")

    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", f"candidate catalog {mutation}")
    return repo, _git(repo, "rev-parse", "baseline").stdout.strip(), diagnostic


def test_actual_gate_cli_allows_additive_catalog_operation(tmp_path: Path) -> None:
    repo, baseline, _diagnostic = _catalog_gate_candidate(tmp_path, "add")
    result = subprocess.run(
        [sys.executable, "scripts/check_wire_schema_compat.py"],
        cwd=repo,
        env={**os.environ, "BH_WIRE_SCHEMA_BASE_REF": "baseline"},
        check=False,
        capture_output=True,
        text=True,
    )

    assert baseline != _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "wire-schema-compat: 1.4.0 -> 1.4.1 is fully compatible" in result.stdout


@pytest.mark.parametrize(
    "mutation", ["remove", "identity", "signature", "result", "projection", "policy"]
)
def test_actual_gate_cli_rejects_breaking_catalog_changes(tmp_path: Path, mutation: str) -> None:
    repo, baseline, diagnostic = _catalog_gate_candidate(tmp_path, mutation)
    result = subprocess.run(
        [sys.executable, "scripts/check_wire_schema_compat.py"],
        cwd=repo,
        env={**os.environ, "BH_WIRE_SCHEMA_BASE_REF": "baseline"},
        check=False,
        capture_output=True,
        text=True,
    )

    assert baseline != _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert result.returncode == 1, result.stdout + result.stderr
    assert "urn:beadhive:wire-catalog:operations:1" in result.stderr
    assert diagnostic in result.stderr


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _rewrite_json(path: Path, **updates: object) -> dict:
    value = json.loads(path.read_text())
    value.update(updates)
    path.write_text(json.dumps(value, indent=2) + "\n")
    return value
