"""Deterministic catalog projection contract for the live Development gateway."""

from __future__ import annotations

import asyncio
import json
from hashlib import sha256
from pathlib import Path

import httpx
import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from starlette.routing import Route

from beadhive import gateway_contract, gateway_read, remote_gateway


def _canonical_digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return f"sha256:{sha256(encoded).hexdigest()}"


def _resolve_wire_schema(document: dict[str, object], reference: str) -> dict[str, object]:
    contract_id, fragment = reference.split("#", 1)
    contracts = {
        contract["$id"]: contract
        for contract in document["wireContracts"]  # type: ignore[index,union-attr]
    }
    assert fragment.startswith("/schemas/")
    schema_name = fragment.removeprefix("/schemas/")
    return contracts[contract_id]["schemas"][schema_name]  # type: ignore[index,return-value]


def _gateway_app():
    return remote_gateway.build_development_gateway_application(
        config=remote_gateway.DevelopmentGatewayConfig(
            issuer=remote_gateway.DEVELOPMENT_ISSUER,
            audience="beadhive-gateway-dev",
            app_origin="https://app-dev.beadhive.cloud",
            gateway_origin="https://gateway-dev.beadhive.cloud",
        ),
        verifier=object(),
        registry=remote_gateway.DevelopmentInstanceRegistry(instances={}),
    )


def _success_gateway_app():
    subject = "review-subject"

    class Verifier:
        def verify(self, encoded: str) -> str:
            assert encoded == "review-token"
            return subject

    async def snapshot():
        return {
            "schemaVersion": 1,
            "revision": "sha256:" + "a" * 64,
            "generatedAt": 1724716800000,
            "workItems": [],
            "agents": [],
        }

    async def online() -> bool:
        return True

    async def refresh(expected_revision: str, correlation_id: str):
        assert expected_revision == "sha256:" + "a" * 64
        assert correlation_id == "123e4567-e89b-42d3-a456-426614174000"
        return {"status": "completed", "revision": "sha256:" + "b" * 64}

    config = remote_gateway.DevelopmentGatewayConfig(
        issuer=remote_gateway.DEVELOPMENT_ISSUER,
        audience="beadhive-gateway-dev",
        app_origin="https://app-dev.beadhive.cloud",
        gateway_origin="https://gateway-dev.beadhive.cloud",
    )
    registry = remote_gateway.DevelopmentInstanceRegistry(
        instances={
            remote_gateway.DEVELOPMENT_INSTANCE_ID: remote_gateway.RemoteInstance(
                display_name="Development demo",
                authorized_subjects=frozenset({subject}),
                snapshot=snapshot,
                online=online,
                refresh=refresh,
            )
        }
    )
    return remote_gateway.build_development_gateway_application(
        config=config,
        verifier=Verifier(),
        registry=registry,
        read_source=gateway_read.load_packaged_development_source(
            authorized_subjects=frozenset({subject})
        ),
    )


def _live_routes() -> set[str]:
    app = _gateway_app()
    return {
        f"{method} {route.path}"
        for route in app.routes
        if isinstance(route, Route)
        for method in route.methods or ()
        if method != "HEAD"
    }


def test_gateway_contract_is_deterministic_checked_and_schema_valid() -> None:
    first = gateway_contract.generate_document()
    assert first == gateway_contract.generate_document()
    assert json.loads(gateway_contract.contract_path().read_text()) == first

    schema = json.loads(gateway_contract.schema_path().read_text())
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(first)

    assert first["$id"] == "urn:beadhive:gateway-projection-contract:1"
    assert first["contractVersion"] == "1.3.0"
    assert first["profile"] == "Development"
    assert first["policy"]["wireAuthority"] == (
        "gateway.v1 and gateway.read.v1 remain gateway-owned"
    )
    assert first["policy"]["localCompatibility"] == (
        "CLI and MCP stdio do not require the gateway or host daemon"
    )


def test_gateway_contract_matches_the_independently_composed_live_surface() -> None:
    checked = gateway_contract.checked_document()
    declared = {row["identifier"] for row in checked["operations"]}
    assert declared == _live_routes()
    assert [row["identifier"] for row in checked["operations"]] == sorted(declared)


def test_preflight_contracts_match_runtime_method_headers_auth_and_results() -> None:
    document = gateway_contract.generate_document()
    operations = {operation["identifier"]: operation for operation in document["operations"]}
    preflights = [
        operation for operation in document["operations"] if operation["method"] == "OPTIONS"
    ]
    assert all(operation["transportPrivilege"] == "network-admission" for operation in preflights)

    command = operations["OPTIONS /v1/instances/{stage}/{slug}/commands/refresh"]
    assert command["wireRequestSchema"].endswith("#/schemas/commandPreflightRequest")
    command_request_schema = _resolve_wire_schema(document, command["wireRequestSchema"])
    runtime_request = {
        "origin": "https://app-dev.beadhive.cloud",
        "method": "POST",
        "headers": ["authorization", "content-type"],
    }
    Draft202012Validator(command_request_schema).validate(runtime_request)
    with pytest.raises(ValidationError):
        Draft202012Validator(command_request_schema).validate(
            runtime_request | {"method": "GET", "headers": ["authorization"]}
        )

    read = operations["OPTIONS /v1/instances"]
    assert read["wireRequestSchema"].endswith("#/schemas/readPreflightRequest")
    Draft202012Validator(_resolve_wire_schema(document, read["wireRequestSchema"])).validate(
        runtime_request | {"method": "GET", "headers": ["authorization"]}
    )

    async def exercise():
        transport = httpx.ASGITransport(app=_gateway_app(), client=("127.0.0.1", 5000))
        async with httpx.AsyncClient(
            transport=transport, base_url="https://gateway-dev.beadhive.cloud"
        ) as client:
            allowed = await client.options(
                "/v1/instances/dev/demo/commands/refresh",
                headers={
                    "Origin": runtime_request["origin"],
                    "Access-Control-Request-Method": runtime_request["method"],
                    "Access-Control-Request-Headers": "Authorization, Content-Type",
                },
            )
            schema_shaped = await client.options(
                "/v1/instances/dev/demo/commands/refresh",
                headers={
                    "Origin": runtime_request["origin"],
                    "Access-Control-Request-Method": "GET",
                    "Access-Control-Request-Headers": "Authorization",
                },
            )
            fallback = await client.options("/not-declared")
        return allowed, schema_shaped, fallback

    allowed, schema_shaped, fallback = asyncio.run(exercise())
    assert allowed.status_code == 204
    Draft202012Validator(_resolve_wire_schema(document, command["wireResultSchema"])).validate(
        allowed.text
    )
    assert schema_shaped.status_code == 403

    catch_all = operations["OPTIONS /{path:path}"]
    assert catch_all["wireResultSchema"].endswith("#/schemas/errorResponse")
    assert fallback.status_code == 403
    Draft202012Validator(_resolve_wire_schema(document, catch_all["wireResultSchema"])).validate(
        fallback.json()
    )


def test_every_live_non_streaming_success_response_validates_resolved_wire_schema() -> None:
    document = gateway_contract.generate_document()
    operations = {operation["identifier"]: operation for operation in document["operations"]}
    auth = {
        "Authorization": "Bearer review-token",
        "Origin": "https://app-dev.beadhive.cloud",
    }

    async def exercise():
        transport = httpx.ASGITransport(app=_success_gateway_app(), client=("127.0.0.1", 5000))
        async with httpx.AsyncClient(
            transport=transport, base_url="https://gateway-dev.beadhive.cloud"
        ) as client:
            return {
                "GET /healthz": await client.get("/healthz"),
                "GET /v1/instances": await client.get(
                    "/v1/instances", params={"limit": "50"}, headers=auth
                ),
                "GET /v1/instances/{stage}/{slug}/hives": await client.get(
                    "/v1/instances/dev/demo/hives", params={"limit": "50"}, headers=auth
                ),
                "GET /v1/instances/{stage}/{slug}/hives/{hive_id:path}/snapshot": (
                    await client.get(
                        "/v1/instances/dev/demo/hives/github%2Fbeadhive%2Fbaml-harness/snapshot",
                        headers=auth,
                    )
                ),
                "GET /v1/instances/{stage}/{slug}/snapshot": await client.get(
                    "/v1/instances/dev/demo/snapshot", headers=auth
                ),
                "POST /v1/instances/{stage}/{slug}/commands/refresh": await client.post(
                    "/v1/instances/dev/demo/commands/refresh",
                    headers=auth,
                    json={
                        "schemaVersion": 1,
                        "correlationId": "123e4567-e89b-42d3-a456-426614174000",
                        "expectedRevision": "sha256:" + "a" * 64,
                    },
                ),
            }

    responses = asyncio.run(exercise())
    for identifier, response in responses.items():
        assert response.status_code == 200, (identifier, response.text)
        result_schema = _resolve_wire_schema(document, operations[identifier]["wireResultSchema"])
        Draft202012Validator(result_schema).validate(response.json())


def test_gateway_wire_references_resolve_to_versioned_digested_owned_contracts() -> None:
    document = gateway_contract.generate_document()
    contracts = document["wireContracts"]
    assert [contract["contractVersion"] for contract in contracts] == [
        "gateway.read.v1",
        "gateway.v1",
    ]
    for contract in contracts:
        digest_payload = {
            "contractVersion": contract["contractVersion"],
            "schemaVersion": contract["schemaVersion"],
            "schemas": contract["schemas"],
        }
        assert contract["digest"] == _canonical_digest(digest_payload)
        assert contract["$id"] == (
            "urn:beadhive:gateway-wire-contract:"
            f"{contract['contractVersion']}:{contract['schemaVersion']}:{contract['digest']}"
        )

    for operation in document["operations"]:
        request_schema = _resolve_wire_schema(document, operation["wireRequestSchema"])
        result_schema = _resolve_wire_schema(document, operation["wireResultSchema"])
        Draft202012Validator.check_schema(request_schema)
        Draft202012Validator.check_schema(result_schema)

    legacy_snapshot = _resolve_wire_schema(
        document,
        next(
            operation["wireResultSchema"]
            for operation in document["operations"]
            if operation["identifier"] == "GET /v1/instances/{stage}/{slug}/snapshot"
        ),
    )
    assert legacy_snapshot["properties"]["snapshot"]["required"] == sorted(
        remote_gateway._SNAPSHOT_KEYS
    )

    rich_snapshot = _resolve_wire_schema(
        document,
        next(
            operation["wireResultSchema"]
            for operation in document["operations"]
            if operation["identifier"]
            == "GET /v1/instances/{stage}/{slug}/hives/{hive_id:path}/snapshot"
        ),
    )
    assert rich_snapshot["properties"]["snapshot"]["required"] == sorted(
        gateway_read._SNAPSHOT_REQUIRED
    )


def test_gateway_owned_wire_version_and_required_shape_drift_changes_artifact(
    monkeypatch,
) -> None:
    baseline = gateway_contract.render_document()
    baseline_document = json.loads(baseline)
    baseline_operations = {
        operation["identifier"]: operation for operation in baseline_document["operations"]
    }
    legacy_snapshot = "GET /v1/instances/{stage}/{slug}/snapshot"
    rich_snapshot = "GET /v1/instances/{stage}/{slug}/hives/{hive_id:path}/snapshot"

    mutations = (
        (remote_gateway, "CONTRACT_VERSION", "gateway.v1-review-drift", legacy_snapshot),
        (gateway_read, "CONTRACT_VERSION", "gateway.read.v1-review-drift", rich_snapshot),
        (
            remote_gateway,
            "_SNAPSHOT_KEYS",
            remote_gateway._SNAPSHOT_KEYS | {"reviewRequired"},
            legacy_snapshot,
        ),
        (
            gateway_read,
            "_SNAPSHOT_REQUIRED",
            gateway_read._SNAPSHOT_REQUIRED | {"reviewRequired"},
            rich_snapshot,
        ),
    )
    for module, attribute, value, identifier in mutations:
        with monkeypatch.context() as context:
            context.setattr(module, attribute, value)
            mutated = gateway_contract.generate_document()
            assert gateway_contract.render_document(mutated) != baseline
            mutated_operations = {
                operation["identifier"]: operation for operation in mutated["operations"]
            }
            for reference in ("wireRequestSchema", "wireResultSchema"):
                assert (
                    mutated_operations[identifier][reference]
                    != baseline_operations[identifier][reference]
                )
            assert gateway_contract.check_artifact() is False


def test_gateway_contract_carries_canonical_and_transport_contracts() -> None:
    operations = {
        row["identifier"]: row for row in gateway_contract.generate_document()["operations"]
    }

    work_items = operations["GET /v1/instances/{stage}/{slug}/hives/{hive_id:path}/snapshot"]
    assert work_items["classification"] == "composite"
    assert work_items["shape"] == "coarser"
    assert [row["operation"] for row in work_items["canonicalContracts"]] == [
        "work.list",
        "work.schedule",
    ]
    assert all(
        row["requestSchema"].startswith("catalog:") for row in work_items["canonicalContracts"]
    )
    assert all(
        row["resultSchema"].startswith("urn:beadhive:") for row in work_items["canonicalContracts"]
    )
    assert work_items["wireRequestSchema"].startswith(
        "urn:beadhive:gateway-wire-contract:gateway.read.v1:1:sha256:"
    )
    assert work_items["wireResultSchema"].startswith(
        "urn:beadhive:gateway-wire-contract:gateway.read.v1:1:sha256:"
    )
    assert work_items["transportPrivilege"] == "authenticated-development-subject"
    assert work_items["sideEffects"] == "none"

    refresh = operations["POST /v1/instances/{stage}/{slug}/commands/refresh"]
    assert refresh["classification"] == "explicit-exclusion"
    assert refresh["canonicalContracts"] == []
    assert refresh["sideEffects"] == "bounded-runtime-refresh"
    assert "not canonical sync" in refresh["reason"]


def test_generation_never_consumes_the_checked_artifact(monkeypatch, tmp_path: Path) -> None:
    first = gateway_contract.generate_document()

    def circular_read_is_a_failure() -> Path:
        raise AssertionError("generation must not read the artifact it checks")

    monkeypatch.setattr(gateway_contract, "contract_path", circular_read_is_a_failure)
    assert gateway_contract.generate_document() == first

    drifted = tmp_path / "gateway.json"
    drifted.write_text(json.dumps(first | {"contractVersion": "wrong"}))
    monkeypatch.setattr(gateway_contract, "contract_path", lambda: drifted)
    assert gateway_contract.check_artifact() is False
