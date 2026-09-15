"""Official v1 contract bundle generation, integrity, and compatibility proofs."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
from collections import Counter
from copy import deepcopy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

import beadhive.contract_release as contract_release
from beadhive.contract_release import (
    OFFICIAL_V1_FAMILIES,
    RELEASE_VERSION,
    build_release,
    compatibility_errors,
    load_artifact,
    load_published_baseline,
    load_release,
    main,
    published_baseline_root,
    release_root,
    render_release,
    validate_release,
    write_release,
)

EXPECTED_FAMILIES = frozenset(
    {
        "config",
        "plugin-manifest",
        "plugin-fragment",
        "operation-catalog",
        "cli",
        "mcp",
        "openapi",
        "gateway",
        "lifecycle",
        "telemetry",
        "seat",
        "launch",
        "workspace",
        "prepared",
        "commit",
        "abort",
        "receipt",
    }
)


def _files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _artifact(release: dict, family: str) -> dict:
    return next(item for item in release["artifacts"] if item["family"] == family)


def _canonical_json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


def _write_json(path: Path, value: object) -> None:
    path.write_bytes(_canonical_json(value))


def _release_documents(root: Path) -> tuple[dict, dict]:
    return (
        json.loads((root / "inventory.json").read_bytes()),
        json.loads((root / "conformance.json").read_bytes()),
    )


def test_bundle_inventory_is_complete_canonical_and_source_owned() -> None:
    release = build_release()
    rendered = render_release(release)
    inventory = json.loads(rendered[Path("inventory.json")])
    fixtures = json.loads(rendered[Path("conformance.json")])

    assert RELEASE_VERSION == "1.0.0"
    assert OFFICIAL_V1_FAMILIES == EXPECTED_FAMILIES
    assert {row["family"] for row in inventory["artifacts"]} == EXPECTED_FAMILIES
    assert len({row["id"] for row in inventory["artifacts"]}) == len(inventory["artifacts"])
    assert [row["path"] for row in inventory["artifacts"]] == sorted(
        row["path"] for row in inventory["artifacts"]
    )

    cases = {case["name"] for case in fixtures["cases"]}
    assert all("input" in case and case["assertion"] for case in fixtures["cases"])
    for row in inventory["artifacts"]:
        path = Path(row["path"])
        payload = rendered[path]
        document = json.loads(payload)
        assert row["version"] == 1
        if row["kind"] == "json-schema":
            assert document["$id"] == row["id"]
            assert document["version"] == 1
        elif "$id" in document:
            assert document["$id"] == row["id"]
        assert row["source_owner"].startswith("beadhive.") or row["source_owner"].startswith(
            "docs/schemas/wire/"
        )
        assert row["compatibility_policy"] in {
            "append-only-catalog-v1",
            "json-schema-additive-v1",
            "openapi-additive-v1",
        }
        assert row["examples"] and set(row["examples"]) <= cases
        assert row["sha256"] == f"sha256:{hashlib.sha256(payload).hexdigest()}"
        assert (
            payload
            == (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
        )

    cases_by_name = {case["name"]: case for case in fixtures["cases"]}
    for row in inventory["artifacts"]:
        document = json.loads(rendered[Path(row["path"])])
        case = cases_by_name[row["examples"][0]]
        if row["kind"] == "json-schema":
            Draft202012Validator.check_schema(document)
            Draft202012Validator(document).validate(case["input"])
        elif case["assertion"] == "catalog-row-present":
            assert case["input"] in document["operations"]
        elif case["assertion"] == "projection-row-present":
            assert case["input"] in document["projections"]
        elif case["assertion"] == "gateway-contract-present":
            assert any(
                item["contractVersion"] == case["input"]["contractVersion"]
                for item in document["contracts"]
            )
        elif case["assertion"] == "lifecycle-event-present":
            assert case["input"] in document["events"]
        elif case["assertion"] == "seat-contract-present":
            assert case["input"] in document["contracts"]
        elif case["assertion"] == "openapi-path-present":
            assert case["input"]["path"] in document["paths"]

    mcp = next(row for row in inventory["artifacts"] if row["family"] == "mcp")
    mcp_document = json.loads(rendered[Path(mcp["path"])])
    assert {row["surface"] for row in mcp_document["projections"]} == {
        "mcp-resource",
        "mcp-tool",
    }


def test_generator_is_offline_and_byte_identical_across_two_clean_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*_args, **_kwargs):
        raise AssertionError("release generation attempted external I/O")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(os, "system", forbidden)

    first = tmp_path / "first"
    second = tmp_path / "second"
    write_release(first)
    write_release(second)

    assert _files(first) == _files(second)
    assert validate_release(first) == ()
    assert validate_release(second) == ()


def test_two_fresh_processes_generate_byte_identical_release(tmp_path: Path) -> None:
    first = tmp_path / "first-process"
    second = tmp_path / "second-process"
    probe = (
        "from pathlib import Path; import sys; "
        "from beadhive.contract_release import write_release; "
        "write_release(Path(sys.argv[1]))"
    )
    for target in (first, second):
        result = subprocess.run(
            [sys.executable, "-I", "-c", probe, str(target)],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
    assert _files(first) == _files(second)


def test_checked_release_is_current_and_install_lookup_is_path_safe() -> None:
    assert validate_release(release_root()) == ()
    loaded = load_release(release_root())
    assert loaded == build_release()
    artifact = loaded["artifacts"][0]
    assert load_artifact(artifact["id"], release_root()) == artifact["document"]


def test_published_baseline_is_distinct_complete_and_digest_pinned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline = published_baseline_root()
    assert baseline != release_root()
    published = load_published_baseline()
    candidate = build_release()
    assert published != candidate
    assert compatibility_errors(published, candidate) == []

    substituted = tmp_path / "baseline"
    shutil.copytree(baseline, substituted)
    inventory_path = substituted / "inventory.json"
    inventory = json.loads(inventory_path.read_bytes())
    row = inventory["artifacts"][0]
    artifact_path = substituted / row["path"]
    document = json.loads(artifact_path.read_bytes())
    document["description"] = "coherently substituted baseline"
    payload = _canonical_json(document)
    artifact_path.write_bytes(payload)
    row["sha256"] = f"sha256:{hashlib.sha256(payload).hexdigest()}"
    _write_json(inventory_path, inventory)
    monkeypatch.setattr(contract_release, "published_baseline_root", lambda: substituted)

    with pytest.raises(ValueError, match="published snapshot digest mismatch"):
        load_published_baseline()


def test_loader_has_stable_valid_control(tmp_path: Path) -> None:
    root = tmp_path / "release"
    write_release(root)

    first = load_release(root)
    second = load_release(root)

    assert first == second == build_release()


def test_installed_loader_does_not_regenerate_from_source_owners(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "release"
    write_release(root)
    expected = build_release()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("installed lookup attempted source-owner regeneration")

    monkeypatch.setattr(contract_release, "render_release", forbidden)

    assert load_release(root) == expected


@pytest.mark.parametrize("substituted_path", ["absolute", "traversal"])
def test_loader_uses_validated_inventory_snapshot_when_later_read_substitutes_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    substituted_path: str,
) -> None:
    root = tmp_path / "release"
    write_release(root)
    inventory_path = root / "inventory.json"
    outside_path = tmp_path / "outside.json"
    _write_json(outside_path, {"outside": True})
    original_read_bytes = Path.read_bytes
    inventory_reads = 0

    def substitute_on_second_read(path: Path) -> bytes:
        nonlocal inventory_reads
        payload = original_read_bytes(path)
        if path == inventory_path:
            inventory_reads += 1
            if inventory_reads == 2:
                inventory = json.loads(payload)
                inventory["artifacts"][0]["path"] = (
                    str(outside_path) if substituted_path == "absolute" else "../outside.json"
                )
                return _canonical_json(inventory)
        return payload

    monkeypatch.setattr(Path, "read_bytes", substitute_on_second_read)

    assert load_release(root) == build_release()


def test_loader_uses_validated_conformance_snapshot_when_later_read_reorders_cases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "release"
    write_release(root)
    conformance_path = root / "conformance.json"
    original_read_bytes = Path.read_bytes
    conformance_reads = 0

    def substitute_on_second_read(path: Path) -> bytes:
        nonlocal conformance_reads
        payload = original_read_bytes(path)
        if path == conformance_path:
            conformance_reads += 1
            if conformance_reads == 2:
                conformance = json.loads(payload)
                conformance["cases"].reverse()
                return _canonical_json(conformance)
        return payload

    monkeypatch.setattr(Path, "read_bytes", substitute_on_second_read)

    assert load_release(root) == build_release()


def test_loader_uses_validated_artifact_snapshot_when_later_read_substitutes_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "release"
    write_release(root)
    inventory, _ = _release_documents(root)
    artifact_path = root / inventory["artifacts"][0]["path"]
    original_read_bytes = Path.read_bytes
    artifact_reads = 0

    def substitute_on_third_read(path: Path) -> bytes:
        nonlocal artifact_reads
        payload = original_read_bytes(path)
        if path == artifact_path:
            artifact_reads += 1
            if artifact_reads == 3:
                return _canonical_json({"outside": True})
        return payload

    monkeypatch.setattr(Path, "read_bytes", substitute_on_third_read)

    assert load_release(root) == build_release()


@pytest.mark.parametrize("filename", ["inventory.json", "conformance.json"])
def test_loader_rejects_symlinked_release_metadata(tmp_path: Path, filename: str) -> None:
    root = tmp_path / "release"
    write_release(root)
    path = root / filename
    external = tmp_path / f"external-{filename}"
    external.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(external)

    with pytest.raises(ValueError, match="symlink"):
        load_release(root)


@pytest.mark.parametrize(
    "document_name, collection", [("inventory.json", "artifacts"), ("conformance.json", "cases")]
)
def test_loader_rejects_noncanonical_release_entry_order(
    tmp_path: Path, document_name: str, collection: str
) -> None:
    root = tmp_path / "release"
    write_release(root)
    path = root / document_name
    document = json.loads(path.read_bytes())
    document[collection].reverse()
    _write_json(path, document)

    with pytest.raises(ValueError, match="canonical .* order"):
        load_release(root)


def test_loader_rejects_self_consistent_missing_official_entry(tmp_path: Path) -> None:
    root = tmp_path / "release"
    write_release(root)
    inventory, conformance = _release_documents(root)
    family_counts = Counter(row["family"] for row in inventory["artifacts"])
    removed = next(row for row in inventory["artifacts"] if family_counts[row["family"]] > 1)
    inventory["artifacts"].remove(removed)
    conformance["cases"] = [
        case for case in conformance["cases"] if case["artifact_id"] != removed["id"]
    ]
    _write_json(root / "inventory.json", inventory)
    _write_json(root / "conformance.json", conformance)

    with pytest.raises(ValueError, match="canonical .* set"):
        load_release(root)


def test_loader_rejects_self_consistent_extra_official_entry(tmp_path: Path) -> None:
    root = tmp_path / "release"
    write_release(root)
    inventory, conformance = _release_documents(root)
    source = deepcopy(inventory["artifacts"][0])
    source_document = json.loads((root / source["path"]).read_bytes())
    source["id"] = "urn:beadhive:wire-schema:extra:1"
    source["path"] = "artifacts/zz-extra-v1.schema.json"
    source["examples"] = ["zz-extra-valid"]
    source_document["$id"] = source["id"]
    payload = _canonical_json(source_document)
    source["sha256"] = f"sha256:{hashlib.sha256(payload).hexdigest()}"
    (root / source["path"]).write_bytes(payload)
    inventory["artifacts"].append(source)
    inventory["artifacts"].sort(key=lambda row: row["path"])
    conformance["cases"].append(
        {
            "name": "zz-extra-valid",
            "artifact_id": source["id"],
            "assertion": "valid-json-schema-instance",
            "input": {},
        }
    )
    conformance["cases"].sort(key=lambda case: case["name"])
    _write_json(root / "inventory.json", inventory)
    _write_json(root / "conformance.json", conformance)

    with pytest.raises(ValueError, match="canonical .* set"):
        load_release(root)


@pytest.mark.parametrize(
    "duplicate", ["artifact-id", "artifact-path", "case-name", "case-artifact-id"]
)
def test_loader_rejects_duplicate_release_entries(tmp_path: Path, duplicate: str) -> None:
    root = tmp_path / "release"
    write_release(root)
    inventory, conformance = _release_documents(root)
    if duplicate == "artifact-id":
        inventory["artifacts"][1]["id"] = inventory["artifacts"][0]["id"]
    elif duplicate == "artifact-path":
        inventory["artifacts"][1]["path"] = inventory["artifacts"][0]["path"]
    elif duplicate == "case-name":
        conformance["cases"][1]["name"] = conformance["cases"][0]["name"]
    else:
        duplicate_case = deepcopy(conformance["cases"][0])
        duplicate_case["name"] = "zz-duplicate-artifact-case"
        conformance["cases"].append(duplicate_case)
        conformance["cases"].sort(key=lambda case: case["name"])
    _write_json(root / "inventory.json", inventory)
    _write_json(root / "conformance.json", conformance)

    with pytest.raises(ValueError, match="duplicate"):
        load_release(root)


@pytest.mark.parametrize(
    "document_name, collection, mutation",
    [
        ("inventory.json", None, "extra"),
        ("inventory.json", "artifacts", "missing"),
        ("inventory.json", "artifacts", "extra"),
        ("conformance.json", None, "extra"),
        ("conformance.json", "cases", "missing"),
        ("conformance.json", "cases", "extra"),
    ],
)
def test_loader_rejects_release_entry_shape_drift(
    tmp_path: Path, document_name: str, collection: str | None, mutation: str
) -> None:
    root = tmp_path / "release"
    write_release(root)
    path = root / document_name
    document = json.loads(path.read_bytes())
    target = document if collection is None else document[collection][0]
    if mutation == "extra":
        target["unexpected"] = True
    else:
        target.pop(next(reversed(target)))
    _write_json(path, document)

    with pytest.raises(ValueError, match="exact keys"):
        load_release(root)


def test_consumer_lookup_fails_closed_before_returning_tampered_bytes(tmp_path: Path) -> None:
    root = tmp_path / "release"
    write_release(root)
    inventory = json.loads((root / "inventory.json").read_text())
    row = inventory["artifacts"][0]
    artifact_path = root / row["path"]
    artifact_path.write_text(artifact_path.read_text() + " ")
    with pytest.raises(ValueError, match="checksum"):
        load_artifact(row["id"], root)


def test_integrity_gate_rejects_path_escape_symlink_and_metadata_drift(tmp_path: Path) -> None:
    root = tmp_path / "release"
    write_release(root)

    inventory_path = root / "inventory.json"
    inventory = json.loads(inventory_path.read_text())
    inventory["artifacts"][0]["path"] = "../escape.json"
    inventory_path.write_text(json.dumps(inventory, indent=2, sort_keys=True) + "\n")
    assert any("path escapes release root" in error for error in validate_release(root))

    write_release(root)
    artifact_path = root / json.loads(inventory_path.read_text())["artifacts"][0]["path"]
    artifact_path.unlink()
    artifact_path.symlink_to(root / "inventory.json")
    assert any("symlink" in error for error in validate_release(root))
    artifact_path.unlink()

    for field in ("source_owner", "compatibility_policy", "examples", "sha256"):
        write_release(root)
        inventory = json.loads(inventory_path.read_text())
        row = inventory["artifacts"][0]
        row[field] = [] if field == "examples" else "drift"
        inventory_path.write_text(json.dumps(inventory, indent=2, sort_keys=True) + "\n")
        assert any(field in error for error in validate_release(root))

    write_release(root)
    inventory = json.loads(inventory_path.read_text())
    artifact_path = root / inventory["artifacts"][0]["path"]
    artifact_path.write_text('{"version":1,"$id":"non-canonical"}\n')
    assert any("canonical JSON" in error for error in validate_release(root))


def test_writer_refuses_root_symlink_and_atomically_replaces_nested_symlink(
    tmp_path: Path,
) -> None:
    target = tmp_path / "linked"
    target.symlink_to(tmp_path / "elsewhere")
    with pytest.raises(ValueError, match="symlink"):
        write_release(target)

    release = tmp_path / "release"
    release.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    sentinel = elsewhere / "sentinel"
    sentinel.write_text("outside\n")
    (release / "artifacts").symlink_to(elsewhere, target_is_directory=True)

    assert any("symlink" in error for error in validate_release(release))
    write_release(release)

    assert validate_release(release) == ()
    assert (release / "artifacts").is_dir()
    assert not (release / "artifacts").is_symlink()
    assert sentinel.read_text() == "outside\n"


def test_compatibility_gate_rejects_removal_rename_and_requiredness() -> None:
    old = build_release()

    removed = deepcopy(old)
    removed["artifacts"].pop()
    assert any("artifact was removed" in error for error in compatibility_errors(old, removed))

    renamed = deepcopy(old)
    renamed["artifacts"][0]["document"]["$id"] += "-renamed"
    assert any(
        "stable artifact identity changed" in error for error in compatibility_errors(old, renamed)
    )

    required = deepcopy(old)
    config = _artifact(required, "config")["document"]
    config.setdefault("required", []).append("future_required")
    assert any(
        "required properties changed" in error for error in compatibility_errors(old, required)
    )

    family_removed = deepcopy(old)
    family_removed["artifacts"] = [
        row for row in family_removed["artifacts"] if row["family"] != "telemetry"
    ]
    assert any("family 'telemetry'" in error for error in compatibility_errors(old, family_removed))

    row_identity_renamed = deepcopy(old)
    _artifact(row_identity_renamed, "telemetry")["id"] += "-renamed"
    assert any(
        "artifact was removed" in error for error in compatibility_errors(old, row_identity_renamed)
    )


def test_compatibility_gate_rejects_closed_unions_operation_identity_and_privilege() -> None:
    old = build_release()

    union_drift = deepcopy(old)
    telemetry = _artifact(union_drift, "telemetry")["document"]
    telemetry["$defs"]["outcome"]["enum"].append("future-outcome")
    assert any(
        "closed-union members changed" in error for error in compatibility_errors(old, union_drift)
    )

    operation_drift = deepcopy(old)
    catalog = _artifact(operation_drift, "operation-catalog")["document"]
    catalog["operations"][0]["name"] += ".renamed"
    assert any(
        "operation identity" in error for error in compatibility_errors(old, operation_drift)
    )

    privilege_drift = deepcopy(old)
    mcp = _artifact(privilege_drift, "mcp")["document"]
    mcp["projections"][0]["privilege"] = "admin"
    assert any(
        "projection privilege" in error for error in compatibility_errors(old, privilege_drift)
    )


@pytest.mark.parametrize(
    "family,collection",
    [
        ("operation-catalog", "operations"),
        ("cli", "projections"),
        ("mcp", "projections"),
        ("gateway", "contracts"),
        ("lifecycle", "events"),
        ("seat", "contracts"),
    ],
)
def test_append_only_catalog_policy_rejects_member_removal_and_reorder(
    family: str, collection: str
) -> None:
    old = build_release()

    removed = deepcopy(old)
    _artifact(removed, family)["document"][collection].pop()
    assert any("append-only" in error for error in compatibility_errors(old, removed))

    reordered = deepcopy(old)
    members = _artifact(reordered, family)["document"][collection]
    members[0], members[1] = members[1], members[0]
    assert any("append-only" in error for error in compatibility_errors(old, reordered))


@pytest.mark.parametrize(
    "family,collection",
    [
        ("operation-catalog", "operations"),
        ("cli", "projections"),
        ("mcp", "projections"),
        ("gateway", "contracts"),
        ("lifecycle", "events"),
        ("seat", "contracts"),
    ],
)
def test_append_only_catalog_policy_allows_tail_member_addition(
    family: str, collection: str
) -> None:
    old = build_release()
    additive = deepcopy(old)
    members = _artifact(additive, family)["document"][collection]
    added = deepcopy(members[-1])
    identity_field = {
        "operation-catalog": "name",
        "cli": "identifier",
        "mcp": "identifier",
        "gateway": "contractVersion",
        "lifecycle": "id",
        "seat": "seat",
    }[family]
    added[identity_field] = f"{added[identity_field]}.future-additive"
    members.append(added)

    assert compatibility_errors(old, additive) == []


def test_openapi_policy_rejects_route_method_request_response_and_schema_breaks() -> None:
    old = build_release()

    route_removed = deepcopy(old)
    _artifact(route_removed, "openapi")["document"]["paths"].pop("/health")
    assert any("OpenAPI route" in error for error in compatibility_errors(old, route_removed))

    method_removed = deepcopy(old)
    health = _artifact(method_removed, "openapi")["document"]["paths"]["/health"]
    health.pop(next(method for method in health if method.lower() in {"get", "post"}))
    assert any("OpenAPI method" in error for error in compatibility_errors(old, method_removed))

    request_removed = deepcopy(old)
    activity = _artifact(request_removed, "openapi")["document"]["paths"][
        "/api/v1/runs/{run_id}/activity"
    ]["post"]
    activity.pop("requestBody")
    assert any("request body" in error for error in compatibility_errors(old, request_removed))

    request_schema_removed = deepcopy(old)
    activity = _artifact(request_schema_removed, "openapi")["document"]["paths"][
        "/api/v1/runs/{run_id}/activity"
    ]["post"]
    activity["requestBody"]["content"]["application/json"].pop("schema")
    assert any(
        "OpenAPI schema" in error for error in compatibility_errors(old, request_schema_removed)
    )

    response_removed = deepcopy(old)
    responses = _artifact(response_removed, "openapi")["document"]["paths"]["/health"]["get"][
        "responses"
    ]
    responses.pop(next(iter(responses)))
    assert any("response" in error for error in compatibility_errors(old, response_removed))

    response_schema_narrowed = deepcopy(old)
    response_schema = _artifact(response_schema_narrowed, "openapi")["document"]["paths"][
        "/health"
    ]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    response_schema["$ref"] = "#/components/schemas/VersionResponse"
    assert any(
        "schema reference changed" in error
        for error in compatibility_errors(old, response_schema_narrowed)
    )

    schema_removed = deepcopy(old)
    schemas = _artifact(schema_removed, "openapi")["document"]["components"]["schemas"]
    schemas.pop(next(iter(schemas)))
    assert any("OpenAPI schema" in error for error in compatibility_errors(old, schema_removed))


def _restrict_health_access(release: dict) -> None:
    health = _artifact(release, "openapi")["document"]["paths"]["/health"]["get"]
    health["security"] = [{"BearerAuth": ["operator:read"]}]
    health["x-beadhive-required-scope"] = "operator:read"


def test_openapi_policy_rejects_public_operation_access_narrowing() -> None:
    old = build_release()
    restricted = deepcopy(old)
    _restrict_health_access(restricted)

    assert any("access" in error for error in compatibility_errors(old, restricted))


def test_write_release_rejects_public_operation_access_narrowing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    restricted = build_release()
    _restrict_health_access(restricted)
    monkeypatch.setattr(contract_release, "build_release", lambda: deepcopy(restricted))

    with pytest.raises(ValueError, match="compatibility baseline.*access"):
        write_release(tmp_path / "restricted")


def test_validate_release_rejects_public_operation_access_narrowing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    restricted = build_release()
    _restrict_health_access(restricted)
    target = tmp_path / "restricted"
    for relative, payload in render_release(restricted).items():
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
    monkeypatch.setattr(contract_release, "build_release", lambda: deepcopy(restricted))

    assert any(
        "compatibility baseline" in error and "access" in error
        for error in validate_release(target)
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "document-security-scope",
        "document-required-privilege",
        "path-security",
        "path-required-scope",
        "operation-security",
        "operation-add-scheme",
        "operation-remove-alternative",
        "operation-required-scope",
        "operation-transport-privilege",
        "operation-canonical-privilege",
        "websocket-required-scope",
        "named-scheme-removal",
        "named-scheme-identity",
        "oauth-scope-removal",
    ],
)
def test_openapi_policy_rejects_access_narrowing_at_every_level(mutation: str) -> None:
    old = build_release()
    candidate = deepcopy(old)
    old_document = _artifact(old, "openapi")["document"]
    new_document = _artifact(candidate, "openapi")["document"]
    old_health_path = old_document["paths"]["/health"]
    new_health_path = new_document["paths"]["/health"]
    old_health = old_health_path["get"]
    new_health = new_health_path["get"]

    if mutation == "document-security-scope":
        new_document["security"] = [{"BearerAuth": ["operator:read"]}]
    elif mutation == "document-required-privilege":
        new_document["x-beadhive-required-privilege"] = "operator"
    elif mutation == "path-security":
        new_health_path["security"] = [{"BearerAuth": ["operator:read"]}]
    elif mutation == "path-required-scope":
        new_health_path["x-beadhive-required-scope"] = "operator:read"
    elif mutation == "operation-security":
        new_health["security"] = [{"BearerAuth": []}]
    elif mutation == "operation-add-scheme":
        old_health["security"] = [{"BearerAuth": []}]
        new_health["security"] = [{"BearerAuth": [], "SessionAuth": []}]
    elif mutation == "operation-remove-alternative":
        old_health["security"] = [{}, {"BearerAuth": []}]
        new_health["security"] = [{"BearerAuth": []}]
    elif mutation == "operation-required-scope":
        new_health["x-beadhive-required-scope"] = "operator:read"
    elif mutation == "operation-transport-privilege":
        new_health["x-beadhive-catalog-projection"]["transportPrivilege"] = "operator:read"
    elif mutation == "operation-canonical-privilege":
        projection = new_document["paths"]["/api/v1/factory"]["get"][
            "x-beadhive-catalog-projection"
        ]
        projection["canonicalContracts"][0]["privilege"] = "privileged"
    elif mutation == "websocket-required-scope":
        websocket = new_document["paths"]["/ws/terminal"]["x-beadhive-websocket"]
        websocket["x-beadhive-required-scope"] = "terminal:admin"
    elif mutation == "named-scheme-removal":
        new_document["components"]["securitySchemes"].pop("BearerAuth")
    elif mutation == "named-scheme-identity":
        new_document["components"]["securitySchemes"]["BearerAuth"]["scheme"] = "basic"
    else:
        oauth = {
            "type": "oauth2",
            "flows": {
                "clientCredentials": {
                    "tokenUrl": "/token",
                    "scopes": {"operator:read": "read", "operator:write": "write"},
                }
            },
        }
        old_document["components"]["securitySchemes"]["OAuth"] = deepcopy(oauth)
        new_document["components"]["securitySchemes"]["OAuth"] = deepcopy(oauth)
        new_document["components"]["securitySchemes"]["OAuth"]["flows"]["clientCredentials"][
            "scopes"
        ].pop("operator:write")

    errors = compatibility_errors(old, candidate)
    assert any("access" in error for error in errors), errors


@pytest.mark.parametrize(
    "relaxation",
    [
        "document-security-scope",
        "document-required-privilege",
        "path-security",
        "path-required-scope",
        "operation-security",
        "operation-add-alternative",
        "operation-required-scope",
        "operation-transport-privilege",
        "websocket-required-scope",
        "named-scheme-required-scope",
        "oauth-scope-addition",
    ],
)
def test_openapi_policy_allows_provable_access_loosenings(relaxation: str) -> None:
    old = build_release()
    candidate = deepcopy(old)
    old_document = _artifact(old, "openapi")["document"]
    new_document = _artifact(candidate, "openapi")["document"]
    old_health_path = old_document["paths"]["/health"]
    new_health_path = new_document["paths"]["/health"]
    old_health = old_health_path["get"]
    new_health = new_health_path["get"]

    if relaxation == "document-security-scope":
        old_document["security"] = [{"BearerAuth": ["operator:read"]}]
        new_document["security"] = [{"BearerAuth": []}]
    elif relaxation == "document-required-privilege":
        old_document["x-beadhive-required-privilege"] = "operator"
        new_document["x-beadhive-required-privilege"] = None
    elif relaxation == "path-security":
        old_health_path["security"] = [{"BearerAuth": []}]
        new_health_path["security"] = []
    elif relaxation == "path-required-scope":
        old_health_path["x-beadhive-required-scope"] = "operator:read"
        new_health_path["x-beadhive-required-scope"] = None
    elif relaxation == "operation-security":
        old_health["security"] = [{"BearerAuth": ["operator:read"]}]
        new_health["security"] = [{"BearerAuth": []}]
    elif relaxation == "operation-add-alternative":
        old_health["security"] = [{"BearerAuth": []}]
        new_health["security"] = [{"BearerAuth": []}, {}]
    elif relaxation == "operation-required-scope":
        old_health["x-beadhive-required-scope"] = "operator:read"
        new_health["x-beadhive-required-scope"] = None
    elif relaxation == "operation-transport-privilege":
        old_health["x-beadhive-catalog-projection"]["transportPrivilege"] = "operator:read"
        new_health["x-beadhive-catalog-projection"]["transportPrivilege"] = "public-liveness"
    elif relaxation == "websocket-required-scope":
        old_websocket = old_document["paths"]["/ws/terminal"]["x-beadhive-websocket"]
        new_websocket = new_document["paths"]["/ws/terminal"]["x-beadhive-websocket"]
        old_websocket["x-beadhive-required-scope"] = "terminal:admin"
        new_websocket["x-beadhive-required-scope"] = None
    elif relaxation == "named-scheme-required-scope":
        old_scheme = old_document["components"]["securitySchemes"]["BearerAuth"]
        new_scheme = new_document["components"]["securitySchemes"]["BearerAuth"]
        old_scheme["x-beadhive-required-scope"] = "operator:admin"
        new_scheme["x-beadhive-required-scope"] = None
    else:
        oauth = {
            "type": "oauth2",
            "flows": {
                "clientCredentials": {
                    "tokenUrl": "/token",
                    "scopes": {"operator:read": "read"},
                }
            },
        }
        old_document["components"]["securitySchemes"]["OAuth"] = deepcopy(oauth)
        new_document["components"]["securitySchemes"]["OAuth"] = deepcopy(oauth)
        new_document["components"]["securitySchemes"]["OAuth"]["flows"]["clientCredentials"][
            "scopes"
        ]["operator:write"] = "write"

    assert compatibility_errors(old, candidate) == []


@pytest.mark.parametrize(
    "family,collection",
    [("cli", "projections"), ("mcp", "projections"), ("gateway", "contracts")],
)
def test_append_only_projection_policy_rejects_new_access_requirement_on_existing_member(
    family: str, collection: str
) -> None:
    old = build_release()
    restricted = deepcopy(old)
    member = _artifact(restricted, family)["document"][collection][0]
    member["x-beadhive-required-scope"] = "operator:read"

    assert any(
        "required access metadata" in error for error in compatibility_errors(old, restricted)
    )


@pytest.mark.parametrize(
    "family,collection",
    [("cli", "projections"), ("mcp", "projections"), ("gateway", "contracts")],
)
def test_append_only_projection_policy_allows_unrestricted_access_annotation(
    family: str, collection: str
) -> None:
    old = build_release()
    annotated = deepcopy(old)
    member = _artifact(annotated, family)["document"][collection][0]
    member["x-beadhive-required-scope"] = None

    assert compatibility_errors(old, annotated) == []


def _existing_extension_target(release: dict, target: str) -> dict:
    if target.startswith("openapi-"):
        document = _artifact(release, "openapi")["document"]
        return {
            "openapi-document": document,
            "openapi-path": document["paths"]["/health"],
            "openapi-operation": document["paths"]["/health"]["get"],
            "openapi-security-scheme": document["components"]["securitySchemes"]["BearerAuth"],
        }[target]
    collection = {"cli": "projections", "mcp": "projections", "gateway": "contracts"}[target]
    return _artifact(release, target)["document"][collection][0]


@pytest.mark.parametrize(
    "target,extension",
    [
        (target, extension)
        for target in (
            "openapi-document",
            "openapi-path",
            "openapi-operation",
            "openapi-security-scheme",
            "cli",
            "mcp",
            "gateway",
        )
        for extension in ("x-required-role", "x-required-permission")
    ]
    + [("openapi-operation-change", "x-required-role")],
)
def test_existing_public_members_fail_closed_for_unknown_extension_metadata(
    target: str, extension: str
) -> None:
    old = build_release()
    candidate = deepcopy(old)
    actual_target = target.removesuffix("-change")
    if target.endswith("-change"):
        _existing_extension_target(old, actual_target)[extension] = "reader"
    _existing_extension_target(candidate, actual_target)[extension] = "administrator"

    assert any("extension metadata" in error for error in compatibility_errors(old, candidate))


def _add_unknown_health_access_extension(release: dict) -> None:
    health = _artifact(release, "openapi")["document"]["paths"]["/health"]["get"]
    health["x-required-role"] = "administrator"


def test_write_release_rejects_unknown_access_extension_on_existing_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    restricted = build_release()
    _add_unknown_health_access_extension(restricted)
    monkeypatch.setattr(contract_release, "build_release", lambda: deepcopy(restricted))

    with pytest.raises(ValueError, match="compatibility baseline.*extension metadata"):
        write_release(tmp_path / "restricted-extension")


def test_validate_release_rejects_unknown_access_extension_on_existing_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    restricted = build_release()
    _add_unknown_health_access_extension(restricted)
    target = tmp_path / "restricted-extension"
    for relative, payload in render_release(restricted).items():
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
    monkeypatch.setattr(contract_release, "build_release", lambda: deepcopy(restricted))

    assert any(
        "compatibility baseline" in error and "extension metadata" in error
        for error in validate_release(target)
    )


@pytest.mark.parametrize(
    "extension", ["x-required-auth", "x-required-scope", "x-required-privilege"]
)
def test_unknown_access_looking_extensions_are_exact_not_semantic(extension: str) -> None:
    old = build_release()
    candidate = deepcopy(old)
    _existing_extension_target(candidate, "openapi-operation")[extension] = "administrator"

    assert any("extension metadata" in error for error in compatibility_errors(old, candidate))


@pytest.mark.parametrize("mutation", ["add", "change", "remove"])
@pytest.mark.parametrize("target", ["openapi-operation", "cli", "mcp", "gateway"])
def test_extension_maps_fail_closed_for_add_change_and_remove(target: str, mutation: str) -> None:
    old = build_release()
    if mutation != "add":
        _existing_extension_target(old, target)["x-contract-policy"] = {"role": "reader"}
    candidate = deepcopy(old)
    candidate_target = _existing_extension_target(candidate, target)
    if mutation == "add":
        candidate_target["x-contract-policy"] = {"role": "reader"}
    elif mutation == "change":
        candidate_target["x-contract-policy"]["role"] = "administrator"
    else:
        candidate_target.pop("x-contract-policy")

    assert any(
        "extension metadata" in error or "append-only" in error
        for error in compatibility_errors(old, candidate)
    )


@pytest.mark.parametrize("target", ["cli", "mcp", "gateway"])
def test_catalog_extension_maps_apply_known_access_semantics(target: str) -> None:
    old = build_release()
    old_extension = {
        "x-beadhive-required-scope": None,
        "description": "stable extension metadata",
    }
    _existing_extension_target(old, target)["x-beadhive-catalog-projection"] = old_extension
    tightened = deepcopy(old)
    _existing_extension_target(tightened, target)["x-beadhive-catalog-projection"][
        "x-beadhive-required-scope"
    ] = "operator:admin"
    assert any(
        "access metadata tightened" in error for error in compatibility_errors(old, tightened)
    )

    old_restricted = deepcopy(old)
    _existing_extension_target(old_restricted, target)["x-beadhive-catalog-projection"][
        "x-beadhive-required-scope"
    ] = "operator:admin"
    loosened = deepcopy(old_restricted)
    _existing_extension_target(loosened, target)["x-beadhive-catalog-projection"][
        "x-beadhive-required-scope"
    ] = None
    assert compatibility_errors(old_restricted, loosened) == []


@pytest.mark.parametrize(
    "key,restricted,unrestricted",
    [
        ("x-beadhive-auth-required", True, False),
        ("x-beadhive-required-scope", "operator:admin", None),
        ("x-beadhive-required-privilege", "administrator", None),
    ],
)
def test_known_access_extensions_keep_semantic_tightening_and_loosening(
    key: str, restricted: object, unrestricted: object
) -> None:
    baseline = build_release()
    tightened = deepcopy(baseline)
    _existing_extension_target(tightened, "openapi-operation")[key] = restricted
    assert any("access metadata" in error for error in compatibility_errors(baseline, tightened))

    old_restricted = build_release()
    _existing_extension_target(old_restricted, "openapi-operation")[key] = restricted
    loosened = deepcopy(old_restricted)
    _existing_extension_target(loosened, "openapi-operation")[key] = unrestricted
    assert compatibility_errors(old_restricted, loosened) == []


@pytest.mark.parametrize(
    "target,field",
    [
        ("openapi-document", "info-description"),
        ("openapi-path", "description"),
        ("openapi-operation", "description"),
        ("openapi-security-scheme", "description"),
        ("cli", "description"),
        ("mcp", "description"),
        ("gateway", "description"),
    ],
)
def test_standard_descriptive_metadata_remains_additive(target: str, field: str) -> None:
    old = build_release()
    described = deepcopy(old)
    if field == "info-description":
        _artifact(described, "openapi")["document"]["info"]["description"] = "clarified"
    else:
        _existing_extension_target(described, target)[field] = "clarified"

    assert compatibility_errors(old, described) == []


@pytest.mark.parametrize("equivalence", ["alternative-order", "anonymous-spelling"])
def test_openapi_security_equivalence_is_order_independent(equivalence: str) -> None:
    old = build_release()
    candidate = deepcopy(old)
    old_health = _existing_extension_target(old, "openapi-operation")
    new_health = _existing_extension_target(candidate, "openapi-operation")
    if equivalence == "alternative-order":
        old_health["security"] = [{"BearerAuth": []}, {}]
        new_health["security"] = [{}, {"BearerAuth": []}]
    else:
        old_health["security"] = []
        new_health["security"] = [{}]

    assert compatibility_errors(old, candidate) == []


def test_extension_compatibility_errors_have_deterministic_key_order() -> None:
    old = build_release()
    left = deepcopy(old)
    right = deepcopy(old)
    left_target = _existing_extension_target(left, "openapi-operation")
    right_target = _existing_extension_target(right, "openapi-operation")
    left_target["x-z-policy"] = 1
    left_target["x-a-policy"] = 1
    right_target["x-a-policy"] = 1
    right_target["x-z-policy"] = 1

    left_errors = [
        error for error in compatibility_errors(old, left) if "extension metadata" in error
    ]
    right_errors = [
        error for error in compatibility_errors(old, right) if "extension metadata" in error
    ]
    assert left_errors == right_errors
    assert ".x-a-policy:" in left_errors[0]
    assert ".x-z-policy:" in left_errors[1]


@pytest.mark.parametrize(
    "security",
    [
        [{"BearerAuth": ["operator:admin"]}],
        {"BearerAuth": ["operator:admin"]},
    ],
    ids=("list", "object"),
)
def test_nested_security_in_catalog_projection_is_exact_extension_metadata(
    security: object,
) -> None:
    old = build_release()
    candidate = deepcopy(old)
    projection = _existing_extension_target(candidate, "openapi-operation")[
        "x-beadhive-catalog-projection"
    ]
    projection["security"] = security

    assert any(
        "x-beadhive-catalog-projection.security" in error and "extension metadata" in error
        for error in compatibility_errors(old, candidate)
    )


@pytest.mark.parametrize("target", ["openapi-operation", "cli", "mcp", "gateway"])
@pytest.mark.parametrize(
    "extension",
    [
        "x-required-role",
        "X-required-role",
        "ｘ-required-role",
        "x‐required-role",
    ],
    ids=("canonical", "uppercase-prefix", "fullwidth-prefix", "unicode-hyphen"),
)
def test_hidden_extension_like_keys_in_new_ordinary_containers_fail_closed(
    target: str, extension: str
) -> None:
    old = build_release()
    candidate = deepcopy(old)
    _existing_extension_target(candidate, target)["policy"] = {
        "rules": [{extension: "administrator"}]
    }

    assert any("extension metadata" in error for error in compatibility_errors(old, candidate))


@pytest.mark.parametrize("target", ["openapi-operation", "cli", "mcp", "gateway"])
@pytest.mark.parametrize("mutation", ["change", "remove"])
def test_hidden_extension_metadata_changes_and_removals_fail_closed(
    target: str, mutation: str
) -> None:
    old = build_release()
    _existing_extension_target(old, target)["policy"] = {"rules": [{"x-required-role": "reader"}]}
    candidate = deepcopy(old)
    candidate_target = _existing_extension_target(candidate, target)
    if mutation == "change":
        candidate_target["policy"]["rules"][0]["x-required-role"] = "administrator"
    else:
        candidate_target.pop("policy")

    assert compatibility_errors(old, candidate)


@pytest.mark.parametrize("extension", ["X-required-role", "ｘ-required-role", "x‐required-role"])
def test_noncanonical_extension_like_keys_are_rejected_without_normalizing(
    extension: str,
) -> None:
    old = build_release()
    candidate = deepcopy(old)
    _existing_extension_target(candidate, "openapi-operation")[extension] = "administrator"

    errors = compatibility_errors(old, candidate)
    assert any(extension in error and "non-canonical extension" in error for error in errors)


@pytest.mark.parametrize("target", ["openapi-operation", "cli", "mcp", "gateway"])
@pytest.mark.parametrize("placement", ["direct", "nested"])
@pytest.mark.parametrize(
    "extension",
    [
        "x−required-role",
        "𝐱-required-role",
        "ˣ-required-role",
        "х-required-role",
    ],
    ids=("minus-sign", "mathematical-x", "modifier-x", "cyrillic-ha"),
)
def test_additional_confusable_extension_prefixes_fail_closed(
    target: str, placement: str, extension: str
) -> None:
    old = build_release()
    candidate = deepcopy(old)
    candidate_target = _existing_extension_target(candidate, target)
    if placement == "direct":
        candidate_target[extension] = "administrator"
    else:
        candidate_target["policy"] = {"rules": [{extension: "administrator"}]}

    assert any(
        extension in error and "non-canonical extension" in error
        for error in compatibility_errors(old, candidate)
    )


def _add_direct_minus_sign_extension(release: dict) -> None:
    _existing_extension_target(release, "openapi-operation")["x−required-role"] = "administrator"


def _add_nested_minus_sign_extension(release: dict) -> None:
    _existing_extension_target(release, "openapi-operation")["policy"] = {
        "rules": [{"x−required-role": "administrator"}]
    }


@pytest.mark.parametrize(
    "mutate", [_add_direct_minus_sign_extension, _add_nested_minus_sign_extension]
)
def test_write_release_rejects_minus_sign_extension_bypasses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutate
) -> None:
    restricted = build_release()
    mutate(restricted)
    monkeypatch.setattr(contract_release, "build_release", lambda: deepcopy(restricted))

    with pytest.raises(ValueError, match="compatibility baseline.*non-canonical extension"):
        write_release(tmp_path / "minus-sign-extension")


@pytest.mark.parametrize(
    "mutate", [_add_direct_minus_sign_extension, _add_nested_minus_sign_extension]
)
def test_validate_release_rejects_minus_sign_extension_bypasses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutate
) -> None:
    restricted = build_release()
    mutate(restricted)
    target = tmp_path / "minus-sign-extension"
    for relative, payload in render_release(restricted).items():
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
    monkeypatch.setattr(contract_release, "build_release", lambda: deepcopy(restricted))

    assert any(
        "compatibility baseline" in error and "non-canonical extension" in error
        for error in validate_release(target)
    )


def test_standard_http_header_and_json_schema_property_names_are_not_extensions() -> None:
    old = build_release()
    candidate = deepcopy(old)
    openapi = _artifact(candidate, "openapi")["document"]
    response_headers = openapi["paths"]["/health"]["get"]["responses"]["200"].setdefault(
        "headers", {}
    )
    response_headers["X-RateLimit-Remaining"] = {"schema": {"type": "integer"}}

    gateway = _artifact(candidate, "gateway")["document"]["contracts"][0]
    gateway["schemas"]["eventsRequest"]["properties"]["x-coordinate"] = {"type": "number"}

    assert compatibility_errors(old, candidate) == []


def _add_nested_projection_security(release: dict) -> None:
    projection = _existing_extension_target(release, "openapi-operation")[
        "x-beadhive-catalog-projection"
    ]
    projection["security"] = [{"BearerAuth": ["operator:admin"]}]


def _add_hidden_projection_extension(release: dict) -> None:
    _existing_extension_target(release, "openapi-operation")["policy"] = {
        "rules": [{"x-required-role": "administrator"}]
    }


@pytest.mark.parametrize(
    "mutate", [_add_nested_projection_security, _add_hidden_projection_extension]
)
def test_write_release_rejects_nested_extension_bypasses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutate
) -> None:
    restricted = build_release()
    mutate(restricted)
    monkeypatch.setattr(contract_release, "build_release", lambda: deepcopy(restricted))

    with pytest.raises(ValueError, match="compatibility baseline.*extension metadata"):
        write_release(tmp_path / "nested-extension")


@pytest.mark.parametrize(
    "mutate", [_add_nested_projection_security, _add_hidden_projection_extension]
)
def test_validate_release_rejects_nested_extension_bypasses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutate
) -> None:
    restricted = build_release()
    mutate(restricted)
    target = tmp_path / "nested-extension"
    for relative, payload in render_release(restricted).items():
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
    monkeypatch.setattr(contract_release, "build_release", lambda: deepcopy(restricted))

    assert any(
        "compatibility baseline" in error and "extension metadata" in error
        for error in validate_release(target)
    )


@pytest.mark.parametrize("mutation", ["array-member", "type-change"])
def test_existing_extension_trees_are_exact_across_arrays_and_types(mutation: str) -> None:
    old = build_release()
    extension = {"rules": [{"role": "reader"}], "enabled": True}
    _existing_extension_target(old, "openapi-operation")["x-contract-policy"] = extension
    candidate = deepcopy(old)
    policy = _existing_extension_target(candidate, "openapi-operation")["x-contract-policy"]
    if mutation == "array-member":
        policy["rules"].append({"role": "administrator"})
    else:
        policy["enabled"] = {"value": True}

    assert any("extension metadata" in error for error in compatibility_errors(old, candidate))


def test_websocket_security_remains_semantic_at_its_actual_access_object() -> None:
    old = build_release()
    candidate = deepcopy(old)
    old_websocket = _artifact(old, "openapi")["document"]["paths"]["/ws/terminal"][
        "x-beadhive-websocket"
    ]
    new_websocket = _artifact(candidate, "openapi")["document"]["paths"]["/ws/terminal"][
        "x-beadhive-websocket"
    ]
    old_websocket["security"] = [{"BearerAuth": []}, {}]
    new_websocket["security"] = [{}, {"BearerAuth": []}]

    assert compatibility_errors(old, candidate) == []


def test_json_schema_policy_rejects_narrowing_and_closed_union_breaks() -> None:
    old = build_release()

    type_narrowed = deepcopy(old)
    config = _artifact(type_narrowed, "config")["document"]
    config["properties"]["schema_version"]["type"] = "null"
    assert any("type narrowed" in error for error in compatibility_errors(old, type_narrowed))

    closed_union = deepcopy(old)
    outcome = _artifact(closed_union, "telemetry")["document"]["$defs"]["outcome"]["enum"]
    outcome.pop()
    assert any("closed-union" in error for error in compatibility_errors(old, closed_union))

    union_removed = deepcopy(old)
    outcome_schema = _artifact(union_removed, "telemetry")["document"]["$defs"]["outcome"]
    outcome_schema.pop("enum")
    assert any("closed-union" in error for error in compatibility_errors(old, union_removed))

    newly_required = deepcopy(old)
    config = _artifact(newly_required, "config")["document"]
    optional = next(name for name in config["properties"] if name not in config.get("required", []))
    config.setdefault("required", []).append(optional)
    assert any("required" in error for error in compatibility_errors(old, newly_required))


@pytest.fixture(scope="module")
def compatibility_release_template() -> dict:
    return build_release()


def _synthetic_schema_compatibility_errors(
    template: dict, old_schema: dict, candidate_schema: dict
) -> list[str]:
    old = deepcopy(template)
    row = _artifact(old, "config")
    identity = row["id"]
    row["document"] = {"$id": identity, "version": 1} | deepcopy(old_schema)
    candidate = deepcopy(old)
    _artifact(candidate, "config")["document"] = {
        "$id": identity,
        "version": 1,
    } | deepcopy(candidate_schema)
    return compatibility_errors(old, candidate)


@pytest.mark.parametrize(
    "old_schema,candidate_schema,keyword",
    [
        ({"enum": ["a", "b"]}, {"enum": ["a"]}, "enum"),
        ({}, {"const": "a"}, "const"),
        ({"type": ["integer", "null"]}, {"type": "integer"}, "type"),
        (
            {"type": "object", "properties": {"x": {}}, "required": []},
            {"type": "object", "properties": {"x": {}}, "required": ["x"]},
            "required",
        ),
        (
            {"type": "object"},
            {"type": "object", "additionalProperties": False},
            "additionalProperties",
        ),
        (
            {"type": "object"},
            {"type": "object", "unevaluatedProperties": False},
            "unevaluatedProperties",
        ),
        ({"type": "number", "minimum": 0}, {"type": "number", "minimum": 1}, "minimum"),
        (
            {"type": "number", "exclusiveMinimum": 0},
            {"type": "number", "exclusiveMinimum": 1},
            "exclusiveMinimum",
        ),
        ({"type": "number", "maximum": 10}, {"type": "number", "maximum": 9}, "maximum"),
        (
            {"type": "number", "exclusiveMaximum": 10},
            {"type": "number", "exclusiveMaximum": 9},
            "exclusiveMaximum",
        ),
        ({"type": "string"}, {"type": "string", "minLength": 1}, "minLength"),
        ({"type": "string", "maxLength": 10}, {"type": "string", "maxLength": 9}, "maxLength"),
        ({"type": "string"}, {"type": "string", "pattern": "^a"}, "pattern"),
        ({"type": "string"}, {"type": "string", "format": "uuid"}, "format"),
        ({"type": "array"}, {"type": "array", "minItems": 1}, "minItems"),
        ({"type": "array", "maxItems": 10}, {"type": "array", "maxItems": 9}, "maxItems"),
        ({"type": "array"}, {"type": "array", "uniqueItems": True}, "uniqueItems"),
        ({"type": "array"}, {"type": "array", "items": {"type": "string"}}, "items"),
        (
            {"type": "array"},
            {"type": "array", "unevaluatedItems": False},
            "unevaluatedItems",
        ),
        ({"type": "array"}, {"type": "array", "contains": {"type": "string"}}, "contains"),
        (
            {"type": "array", "contains": True, "minContains": 0},
            {"type": "array", "contains": True, "minContains": 1},
            "minContains",
        ),
        (
            {"type": "array", "contains": True},
            {"type": "array", "contains": True, "maxContains": 2},
            "maxContains",
        ),
        ({"type": "object"}, {"type": "object", "minProperties": 1}, "minProperties"),
        (
            {"type": "object", "maxProperties": 10},
            {"type": "object", "maxProperties": 9},
            "maxProperties",
        ),
        (
            {"type": "object"},
            {"type": "object", "propertyNames": {"pattern": "^a"}},
            "propertyNames",
        ),
        (
            {"type": "object", "properties": {"x": {}, "y": {}}},
            {"type": "object", "properties": {"x": {}}},
            "properties",
        ),
        (
            {"type": "object"},
            {"type": "object", "patternProperties": {"^x": {"type": "string"}}},
            "patternProperties",
        ),
        (
            {"type": "object"},
            {"type": "object", "dependentRequired": {"x": ["y"]}},
            "dependentRequired",
        ),
        (
            {"type": "object"},
            {"type": "object", "dependentSchemas": {"x": {"required": ["y"]}}},
            "dependentSchemas",
        ),
        ({}, {"allOf": [{"type": "string"}]}, "allOf"),
        (
            {"anyOf": [{"type": "string"}, {"type": "integer"}]},
            {"anyOf": [{"type": "string"}]},
            "anyOf",
        ),
        ({"oneOf": [{"type": "string"}]}, {"oneOf": [{"type": "integer"}]}, "oneOf"),
        ({}, {"not": {"type": "null"}}, "not"),
        ({}, {"if": {"type": "string"}, "then": {"minLength": 1}}, "if/then/else"),
        ({"type": "array"}, {"type": "array", "prefixItems": [{"type": "string"}]}, "prefixItems"),
        ({"type": "number"}, {"type": "number", "multipleOf": 2}, "multipleOf"),
        ({"type": "number", "multipleOf": 2}, {"type": "number", "multipleOf": 4}, "multipleOf"),
        ({}, {"contentSchema": {"type": "string"}}, "contentSchema"),
        ({}, {"futureAssertion": 1}, "futureAssertion"),
    ],
)
def test_json_schema_policy_rejects_every_supported_narrowing_keyword(
    compatibility_release_template: dict,
    old_schema: dict,
    candidate_schema: dict,
    keyword: str,
) -> None:
    errors = _synthetic_schema_compatibility_errors(
        compatibility_release_template, old_schema, candidate_schema
    )
    assert any(keyword in error for error in errors), errors


@pytest.mark.parametrize(
    "old_schema,candidate_schema",
    [
        ({"const": "a"}, {}),
        ({"type": "integer"}, {"type": ["integer", "number"]}),
        (
            {"type": "object", "properties": {"x": {}}, "required": ["x"]},
            {"type": "object", "properties": {"x": {}}, "required": []},
        ),
        (
            {"type": "object", "additionalProperties": False},
            {"type": "object", "additionalProperties": True},
        ),
        (
            {"type": "object", "unevaluatedProperties": False},
            {"type": "object", "unevaluatedProperties": True},
        ),
        ({"type": "number", "minimum": 1}, {"type": "number", "minimum": 0}),
        ({"type": "number", "maximum": 9}, {"type": "number", "maximum": 10}),
        ({"type": "string", "minLength": 1}, {"type": "string", "minLength": 0}),
        ({"type": "string", "maxLength": 9}, {"type": "string", "maxLength": 10}),
        ({"type": "string", "pattern": "^a"}, {"type": "string"}),
        ({"type": "string", "format": "uuid"}, {"type": "string"}),
        ({"type": "array", "minItems": 1}, {"type": "array", "minItems": 0}),
        ({"type": "array", "maxItems": 9}, {"type": "array", "maxItems": 10}),
        ({"type": "array", "uniqueItems": True}, {"type": "array", "uniqueItems": False}),
        ({"type": "array", "items": {"type": "string"}}, {"type": "array", "items": True}),
        (
            {"type": "array", "contains": {"type": "string"}},
            {"type": "array", "contains": True},
        ),
        (
            {"type": "object", "propertyNames": {"pattern": "^a"}},
            {"type": "object"},
        ),
        (
            {"type": "object", "dependentRequired": {"x": ["y", "z"]}},
            {"type": "object", "dependentRequired": {"x": ["y"]}},
        ),
        (
            {"type": "object", "dependentSchemas": {"x": {"required": ["y"]}}},
            {"type": "object"},
        ),
        (
            {"allOf": [{"type": "string"}, {"minLength": 1}]},
            {"allOf": [{"type": "string"}]},
        ),
        (
            {"anyOf": [{"type": "string"}]},
            {"anyOf": [{"type": "string"}, {"type": "integer"}]},
        ),
        ({"not": {"type": "null"}}, {}),
        (
            {"if": {"type": "string"}, "then": {"minLength": 1}},
            {},
        ),
        ({"type": "number", "multipleOf": 4}, {"type": "number", "multipleOf": 2}),
        (
            {"type": "string", "title": "old"},
            {"type": "string", "title": "new", "description": "annotation"},
        ),
    ],
)
def test_json_schema_policy_allows_provable_relaxations(
    compatibility_release_template: dict, old_schema: dict, candidate_schema: dict
) -> None:
    assert (
        _synthetic_schema_compatibility_errors(
            compatibility_release_template, old_schema, candidate_schema
        )
        == []
    )


def test_release_compatibility_allows_documented_additive_changes() -> None:
    old = build_release()
    additive = deepcopy(old)

    config = _artifact(additive, "config")["document"]
    config["properties"]["future_optional"] = {"type": "string"}

    lifecycle = _artifact(additive, "lifecycle")["document"]
    lifecycle["events"].append(
        {
            "id": "future.event",
            "family": "future",
            "phase": "completed",
            "order": 999,
            "context": {"type": "FutureContext", "fields": []},
        }
    )

    openapi = _artifact(additive, "openapi")["document"]
    openapi["paths"]["/future"] = {
        "get": {"responses": {"200": {"description": "additive response"}}}
    }
    openapi["paths"]["/health"]["post"] = {"responses": {"204": {"description": "additive method"}}}
    openapi["paths"]["/health"]["get"]["responses"]["299"] = {"description": "additive response"}

    assert compatibility_errors(old, additive) == []


def test_release_write_and_check_paths_enforce_compatibility_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    old = build_release()
    breaking = deepcopy(old)
    _artifact(breaking, "openapi")["document"]["paths"].pop("/health")
    monkeypatch.setattr(contract_release, "build_release", lambda: deepcopy(breaking))

    target = tmp_path / "breaking"
    with pytest.raises(ValueError, match="compatibility baseline"):
        write_release(target)
    assert not target.exists()
    assert any("compatibility baseline" in error for error in validate_release(release_root()))
    assert main(["--check"]) == 1
    assert "compatibility baseline" in capsys.readouterr().out
    assert main(["--write"]) == 1
    assert "compatibility baseline" in capsys.readouterr().out


def test_release_write_and_check_paths_accept_additive_evolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    additive = build_release()
    config = _artifact(additive, "config")["document"]
    config["properties"]["future_optional"] = {"type": "string"}
    monkeypatch.setattr(contract_release, "build_release", lambda: deepcopy(additive))

    target = tmp_path / "additive"
    write_release(target)
    assert validate_release(target) == ()


@pytest.mark.parametrize(
    "definition,property_name,keyword,value",
    [
        ("AlertsConfig", "worktree_cap_mb", "multipleOf", 2),
        ("ConflictConfig", "union_globs", "uniqueItems", True),
    ],
)
def test_schema_policy_rejects_standard_assertion_narrowing_in_real_write_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    definition: str,
    property_name: str,
    keyword: str,
    value: object,
) -> None:
    old = build_release()
    narrowed = deepcopy(old)
    config = _artifact(narrowed, "config")["document"]
    config["$defs"][definition]["properties"][property_name][keyword] = value

    errors = compatibility_errors(old, narrowed)
    assert any(keyword in error for error in errors)

    monkeypatch.setattr(contract_release, "build_release", lambda: deepcopy(narrowed))
    target = tmp_path / "narrowed"
    with pytest.raises(ValueError, match="compatibility baseline"):
        write_release(target)
    assert not target.exists()


def test_writer_failure_leaves_existing_target_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "release"
    write_release(target)
    before = _files(target)
    old = build_release()
    candidate = deepcopy(old)
    candidate["artifacts"][0]["document"]["description"] = "new additive annotation"
    monkeypatch.setattr(contract_release, "build_release", lambda: deepcopy(candidate))
    original_write_bytes = Path.write_bytes
    writes = 0

    def fail_second_write(path: Path, payload: bytes) -> int:
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("injected second destination-write failure")
        return original_write_bytes(path, payload)

    monkeypatch.setattr(Path, "write_bytes", fail_second_write)
    with pytest.raises(OSError, match="injected second"):
        write_release(target)

    assert _files(target) == before
    monkeypatch.setattr(contract_release, "build_release", lambda: deepcopy(old))
    assert validate_release(target) == ()


def test_validate_uses_candidate_exact_tree_and_write_removes_stale_target_entries(
    tmp_path: Path,
) -> None:
    target = tmp_path / "release"
    write_release(target)
    stale = target / "artifacts" / "stale-unlisted.json"
    stale.write_text("{}\n")
    inventory = json.loads((target / "inventory.json").read_bytes())
    stale_row = deepcopy(inventory["artifacts"][0])
    stale_row["path"] = "artifacts/stale-unlisted.json"
    stale_row["sha256"] = f"sha256:{hashlib.sha256(stale.read_bytes()).hexdigest()}"
    inventory["artifacts"].append(stale_row)
    _write_json(target / "inventory.json", inventory)

    assert any("unlisted release entry" in error for error in validate_release(target))

    write_release(target)
    assert not stale.exists()
    assert validate_release(target) == ()


def _staging_siblings(target: Path) -> list[Path]:
    return list(target.parent.glob(f".{target.name}.staging-*"))


@pytest.mark.parametrize("failure_index", range(1, 26))
def test_every_staged_file_write_failure_preserves_existing_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_index: int
) -> None:
    target = tmp_path / "release"
    shutil.copytree(release_root(), target)
    before = _files(target)
    candidate = build_release()
    candidate["artifacts"][0]["document"]["description"] = "new additive annotation"
    monkeypatch.setattr(contract_release, "build_release", lambda: deepcopy(candidate))
    original_write_bytes = Path.write_bytes
    writes = 0

    def fail_selected_write(path: Path, payload: bytes) -> int:
        nonlocal writes
        writes += 1
        if writes == failure_index:
            raise OSError(f"injected staged write {failure_index}")
        return original_write_bytes(path, payload)

    monkeypatch.setattr(Path, "write_bytes", fail_selected_write)
    with pytest.raises(OSError, match=f"injected staged write {failure_index}"):
        write_release(target)

    assert writes == failure_index
    assert _files(target) == before
    assert _staging_siblings(target) == []


@pytest.mark.parametrize("target_exists", [False, True])
def test_staging_creation_or_validation_failure_preserves_prior_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_exists: bool,
) -> None:
    target = tmp_path / "release"
    if target_exists:
        shutil.copytree(release_root(), target)
    before = _files(target) if target_exists else None

    monkeypatch.setattr(
        contract_release,
        "_validate_release_candidate",
        lambda *_args, **_kwargs: ("injected staged validation failure",),
    )
    with pytest.raises(ValueError, match="injected staged validation"):
        write_release(target)

    assert (_files(target) if target.exists() else None) == before
    assert _staging_siblings(target) == []


@pytest.mark.parametrize("target_exists,failed_replace", [(False, 1), (True, 1), (True, 2)])
def test_every_publish_swap_failure_rolls_back_or_leaves_target_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_exists: bool,
    failed_replace: int,
) -> None:
    target = tmp_path / "release"
    if target_exists:
        shutil.copytree(release_root(), target)
    before = _files(target) if target_exists else None
    original_replace = contract_release._atomic_replace
    replaces = 0

    def fail_selected_replace(source: Path, destination: Path) -> None:
        nonlocal replaces
        replaces += 1
        if replaces == failed_replace:
            raise OSError(f"injected atomic replace {failed_replace}")
        original_replace(source, destination)

    monkeypatch.setattr(contract_release, "_atomic_replace", fail_selected_replace)
    with pytest.raises(OSError, match=f"injected atomic replace {failed_replace}"):
        write_release(target)

    assert (_files(target) if target.exists() else None) == before
    assert _staging_siblings(target) == []


def test_staging_directory_creation_failure_preserves_existing_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "release"
    shutil.copytree(release_root(), target)
    before = _files(target)

    def fail_mkdtemp(*_args, **_kwargs):
        raise OSError("injected staging directory failure")

    monkeypatch.setattr(contract_release.tempfile, "mkdtemp", fail_mkdtemp)
    with pytest.raises(OSError, match="injected staging directory"):
        write_release(target)

    assert _files(target) == before
    assert _staging_siblings(target) == []


@pytest.mark.parametrize(
    "stale_kind",
    [
        "root-file",
        "nested-file",
        "empty-directory",
        "file-symlink",
        "directory-symlink",
        "expected-directory-symlink",
    ],
)
def test_stale_file_directory_and_symlink_are_rejected_then_atomically_removed(
    tmp_path: Path, stale_kind: str
) -> None:
    target = tmp_path / "release"
    shutil.copytree(release_root(), target)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.json"
    sentinel.write_text('{"outside":true}\n')
    stale = target / "stale"

    if stale_kind == "root-file":
        stale.write_text("{}\n")
    elif stale_kind == "nested-file":
        stale = target / "artifacts" / "nested" / "stale.json"
        stale.parent.mkdir()
        stale.write_text("{}\n")
    elif stale_kind == "empty-directory":
        stale.mkdir()
    elif stale_kind == "file-symlink":
        stale.symlink_to(sentinel)
    elif stale_kind == "directory-symlink":
        stale.symlink_to(outside, target_is_directory=True)
    else:
        stale = target / "artifacts"
        shutil.rmtree(stale)
        stale.symlink_to(outside, target_is_directory=True)

    assert any(
        "unlisted release entry" in error or "symlink" in error
        for error in validate_release(target)
    )

    write_release(target)
    expected = {path.as_posix(): payload for path, payload in render_release().items()}
    assert _files(target) == expected
    assert validate_release(target) == ()
    assert sentinel.read_text() == '{"outside":true}\n'
    assert _staging_siblings(target) == []
