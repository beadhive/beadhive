"""Deterministic catalog projection contract for the live Development gateway."""

from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator
from starlette.routing import Route

from beadhive import gateway_contract, remote_gateway


def _live_routes() -> set[str]:
    app = remote_gateway.build_development_gateway_application(
        config=remote_gateway.DevelopmentGatewayConfig(
            issuer=remote_gateway.DEVELOPMENT_ISSUER,
            audience="beadhive-gateway-dev",
            app_origin="https://app-dev.beadhive.cloud",
            gateway_origin="https://gateway-dev.beadhive.cloud",
        ),
        verifier=object(),
        registry=remote_gateway.DevelopmentInstanceRegistry(instances={}),
    )
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
    assert first["contractVersion"] == "1.0.0"
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
    assert work_items["wireRequestSchema"].startswith("python:beadhive.remote_gateway#")
    assert work_items["wireResultSchema"].startswith("python:beadhive.remote_gateway#")
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
