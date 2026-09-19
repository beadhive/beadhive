"""Conformance for the additive gateway.read.v1 generated Development bridge."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from importlib import resources
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from joserfc import jwt
from joserfc.jwk import RSAKey
from joserfc.jws import JWSRegistry
from jsonschema import Draft202012Validator

from beadhive import frame_bridge, frame_bridge_runtime, gateway_read

ISSUER = "https://rapid-snail-6758.clerk.accounts.dev"
AUDIENCE = "beadhive-gateway-dev"
APP_ORIGIN = "https://app-dev.beadhive.cloud"
GATEWAY_ORIGIN = "https://gateway-dev.beadhive.cloud"
SUBJECT = "user_dev_demo"
EXPERIENCE_CORPUS = Path(__file__).parent / "fixtures" / "gateway_experience_v1"


def _keys() -> tuple[RSAKey, RSAKey]:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return RSAKey.import_key(private), RSAKey.import_key(private.public_key())


def _token(private_key: RSAKey, *, subject: str = SUBJECT, expires_at: int | None = None) -> str:
    return jwt.encode(
        {"alg": "RS256", "kid": "development-test"},
        {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": subject,
            "exp": expires_at if expires_at is not None else int(time.time()) + 300,
        },
        private_key,
        registry=JWSRegistry(algorithms=["RS256"], strict_check_header=False),
    )


def _application(
    public_key: RSAKey,
    source: gateway_read.GatewayReadSource | None,
    *,
    experience_source: gateway_read.ExperienceReadSource | None = None,
    fail_legacy_access: bool = False,
    authorized_subjects: frozenset[str] = frozenset({SUBJECT}),
    verifier_now=lambda: time.time(),
    subject_is_revoked=lambda _subject: False,
    runtime_calls: frame_bridge.RuntimeCallPolicy | None = None,
):
    config = frame_bridge.DevelopmentFrameBridgeConfig(
        issuer=ISSUER,
        audience=AUDIENCE,
        app_origin=APP_ORIGIN,
        gateway_origin=GATEWAY_ORIGIN,
    )

    async def snapshot():
        if fail_legacy_access:
            raise AssertionError("rich request touched the legacy runtime")
        return {
            "schemaVersion": 1,
            "revision": "sha256:" + "a" * 64,
            "generatedAt": 1,
            "workItems": [],
            "agents": [],
        }

    async def online():
        if fail_legacy_access:
            raise AssertionError("rich request touched the legacy runtime")
        return True

    return frame_bridge.build_development_frame_bridge_application(
        config=config,
        verifier=frame_bridge.ClerkTokenVerifier(
            config=config,
            key=public_key,
            now=verifier_now,
            subject_is_revoked=subject_is_revoked,
        ),
        registry=frame_bridge.DevelopmentInstanceRegistry(
            instances={
                "dev/demo": frame_bridge.RemoteInstance(
                    display_name="Development demo",
                    authorized_subjects=authorized_subjects,
                    snapshot=snapshot,
                    online=online,
                )
            }
        ),
        runtime_calls=runtime_calls,
        read_source=source,
        experience_source=experience_source,
    )


def _catalog_bytes() -> tuple[bytes, bytes]:
    package = resources.files("beadhive").joinpath("catalog")
    return (
        package.joinpath(gateway_read.CATALOG_FILE).read_bytes(),
        package.joinpath(gateway_read.MANIFEST_FILE).read_bytes(),
    )


def _experience_bytes() -> tuple[bytes, bytes]:
    package = resources.files("beadhive").joinpath("catalog")
    return (
        package.joinpath(gateway_read.DEMO_EXPERIENCE_FILE).read_bytes(),
        package.joinpath(gateway_read.DEMO_EXPERIENCE_MANIFEST_FILE).read_bytes(),
    )


def _manifest_select(manifest: bytes, scenario_id: str) -> tuple[bytes, str]:
    value = json.loads(manifest)
    value["selectedScenarioId"] = scenario_id
    selected = json.dumps(value, indent=2, sort_keys=True).encode() + b"\n"
    return selected, hashlib.sha256(selected).hexdigest()


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Origin": APP_ORIGIN}


def _hive_ids(value: object) -> list[str]:
    if isinstance(value, list):
        return [hive_id for item in value for hive_id in _hive_ids(item)]
    if isinstance(value, dict):
        return [
            *([value["hiveId"]] if isinstance(value.get("hiveId"), str) else []),
            *(hive_id for item in value.values() for hive_id in _hive_ids(item)),
        ]
    return []


def test_packaged_catalog_validates_every_scenario_and_canonical_hive_identity() -> None:
    source = gateway_read.load_packaged_development_source(
        authorized_subjects=frozenset({"user_dev_demo"})
    )

    assert source.scenario_hive_ids == {
        "small": ("github/beadhive/beadhive-ui",),
        "dense": ("github/beadhive/beadhive-ui",),
        "multi-hive": (
            "github/beadhive/baml-harness",
            "github/beadhive/beadhive-app",
            "github/beadhive/beadhive-ui",
        ),
        "blocked-path": ("github/beadhive/beadhive-ui",),
        "gate-pending": ("github/beadhive/beadhive-ui",),
        "ready-kickoff": ("github/beadhive/beadhive-ui",),
    }
    assert source.selected_scenario_id == "multi-hive"


def test_packaged_tour_overview_is_validated_and_read_from_memory() -> None:
    source = gateway_read.load_packaged_development_experience_source(
        authorized_subjects=frozenset({SUBJECT})
    )

    async def exercise():
        return await source.experience(SUBJECT, instance_id="dev/demo")

    envelope = asyncio.run(exercise())
    assert envelope["contractVersion"] == "gateway.read.v1"
    assert envelope["instanceId"] == "dev/demo"
    assert envelope["provider"]["authority"] == "generated"
    assert envelope["provider"]["scenario"] == {
        "id": "tour-overview",
        "artifactVersion": 1,
        "revision": envelope["experience"]["lanes"]["operator"]["snapshot"]["revision"],
        "selectedBy": "gateway",
    }
    assert envelope["provenance"]["package"] == "@beadhive/demo-scenarios"
    assert envelope["provenance"]["packageVersion"] == "0.1.0"

    def forbidden_resource_access(*_args, **_kwargs):
        raise AssertionError("experience read attempted package or filesystem discovery")

    original = gateway_read.resources.files
    gateway_read.resources.files = forbidden_resource_access
    try:
        assert asyncio.run(exercise()) == envelope
    finally:
        gateway_read.resources.files = original


def test_authenticated_experience_route_serves_every_lane_from_one_source() -> None:
    private_key, public_key = _keys()
    source = gateway_read.load_packaged_development_experience_source(
        authorized_subjects=frozenset({SUBJECT})
    )
    app = _application(
        public_key,
        None,
        experience_source=source,
        fail_legacy_access=True,
    )

    async def exercise():
        expected = await source.experience(SUBJECT, instance_id="dev/demo")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=GATEWAY_ORIGIN
        ) as client:
            response = await client.get(
                "/v1/instances/dev/demo/experience",
                headers=_headers(_token(private_key)),
            )
        return expected, response

    expected, response = asyncio.run(exercise())
    assert response.status_code == 200
    assert response.json() == expected
    assert set(response.json()["experience"]["lanes"]) == {
        "operator",
        "planning",
        "activity",
        "assistant",
    }
    assert response.json()["provider"]["capabilities"] == list(
        gateway_read.DEMO_EXPERIENCE_CAPABILITIES
    )
    assert response.json()["experience"]["lanes"]["operator"]["snapshot"]["advertisedActions"] == []


def test_experience_route_has_specific_authentication_and_authorization_failures() -> None:
    private_key, public_key = _keys()
    source = gateway_read.load_packaged_development_experience_source(
        authorized_subjects=frozenset({SUBJECT})
    )
    app = _application(
        public_key,
        None,
        experience_source=source,
        authorized_subjects=frozenset({SUBJECT, "user_denied"}),
    )

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=GATEWAY_ORIGIN
        ) as client:
            missing = await client.get(
                "/v1/instances/dev/demo/experience",
                headers={"Origin": APP_ORIGIN},
            )
            denied = await client.get(
                "/v1/instances/dev/demo/experience",
                headers=_headers(_token(private_key, subject="user_denied")),
            )
        return missing, denied

    missing, denied = asyncio.run(exercise())
    assert (missing.status_code, missing.json()["error"]["code"]) == (
        401,
        "authentication_failed",
    )
    assert (denied.status_code, denied.json()["error"]["code"]) == (
        403,
        "authorization_failed",
    )
    assert missing.headers["cache-control"] == denied.headers["cache-control"] == "no-store"


def test_experience_validator_is_stable_until_gateway_restart_changes_its_epoch() -> None:
    private_key, public_key = _keys()
    first_source = gateway_read.load_packaged_development_experience_source(
        authorized_subjects=frozenset({SUBJECT})
    )
    second_source = gateway_read.load_packaged_development_experience_source(
        authorized_subjects=frozenset({SUBJECT})
    )

    async def exercise():
        headers = _headers(_token(private_key))
        first_app = _application(public_key, None, experience_source=first_source)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=first_app), base_url=GATEWAY_ORIGIN
        ) as client:
            first = await client.get("/v1/instances/dev/demo/experience", headers=headers)
            unchanged = await client.get(
                "/v1/instances/dev/demo/experience",
                headers={**headers, "If-None-Match": first.headers["etag"]},
            )
        restarted_app = _application(public_key, None, experience_source=second_source)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=restarted_app), base_url=GATEWAY_ORIGIN
        ) as client:
            restarted = await client.get(
                "/v1/instances/dev/demo/experience",
                headers={**headers, "If-None-Match": first.headers["etag"]},
            )
        return first, unchanged, restarted

    first, unchanged, restarted = asyncio.run(exercise())
    assert first.status_code == restarted.status_code == 200
    assert unchanged.status_code == 304
    assert unchanged.content == b""
    assert unchanged.headers["etag"] == first.headers["etag"]
    assert restarted.headers["etag"] != first.headers["etag"]
    first_epoch = first.json()["experience"]["lanes"]["operator"]["snapshot"]["cursor"][
        "producerEpoch"
    ]
    restarted_epoch = restarted.json()["experience"]["lanes"]["operator"]["snapshot"]["cursor"][
        "producerEpoch"
    ]
    assert first_epoch == first_source.cache_boundary
    assert restarted_epoch == second_source.cache_boundary
    assert restarted_epoch != first_epoch


def test_experience_reauthentication_never_reuses_an_authorized_cached_response() -> None:
    private_key, public_key = _keys()
    access = {"revoked": False}
    source = gateway_read.load_packaged_development_experience_source(
        authorized_subjects=frozenset({SUBJECT})
    )
    app = _application(
        public_key,
        None,
        experience_source=source,
        subject_is_revoked=lambda _subject: access["revoked"],
    )

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=GATEWAY_ORIGIN
        ) as client:
            authorized = await client.get(
                "/v1/instances/dev/demo/experience",
                headers=_headers(_token(private_key)),
            )
            access["revoked"] = True
            denied = await client.get(
                "/v1/instances/dev/demo/experience",
                headers={
                    **_headers(_token(private_key)),
                    "If-None-Match": authorized.headers["etag"],
                },
            )
        return authorized, denied

    authorized, denied = asyncio.run(exercise())
    assert authorized.status_code == 200
    assert (denied.status_code, denied.json()["error"]["code"]) == (
        401,
        "authentication_failed",
    )
    assert denied.headers["cache-control"] == "no-store"
    assert "etag" not in denied.headers
    assert b'"experience"' not in denied.content


def test_experience_request_cancellation_and_shutdown_cancel_the_source_read() -> None:
    private_key, public_key = _keys()

    class BlockingExperienceSource:
        source_mode = "generated"

        def __init__(self) -> None:
            self.cache_boundary = "blocking-experience"
            self.entered = asyncio.Event()
            self.cancelled = asyncio.Event()

        async def experience(self, subject: str, *, instance_id: str):
            assert subject == SUBJECT
            assert instance_id == "dev/demo"
            self.entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise

    async def cancel_request() -> None:
        source = BlockingExperienceSource()
        app = _application(public_key, None, experience_source=source)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=GATEWAY_ORIGIN
        ) as client:
            request = asyncio.create_task(
                client.get(
                    "/v1/instances/dev/demo/experience",
                    headers=_headers(_token(private_key)),
                )
            )
            await source.entered.wait()
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
            await asyncio.wait_for(source.cancelled.wait(), timeout=0.5)

    async def stop_application() -> None:
        source = BlockingExperienceSource()
        app = _application(public_key, None, experience_source=source)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=GATEWAY_ORIGIN
        ) as client:
            async with app.router.lifespan_context(app):
                request = asyncio.create_task(
                    client.get(
                        "/v1/instances/dev/demo/experience",
                        headers=_headers(_token(private_key)),
                    )
                )
                await source.entered.wait()
            await asyncio.wait_for(source.cancelled.wait(), timeout=0.5)
            result = await asyncio.gather(request, return_exceptions=True)
            assert isinstance(result[0], asyncio.CancelledError | RuntimeError)
            if isinstance(result[0], RuntimeError):
                assert str(result[0]) == "No response returned."

    asyncio.run(cancel_request())
    asyncio.run(stop_application())


def test_pinned_gateway_experience_corpus_validates_the_authenticated_response() -> None:
    lock = json.loads((EXPERIENCE_CORPUS / "corpus-lock.json").read_bytes())
    assert lock["revision"] == "4ed2425b9860006f692fddfaa19cef4ec2a656f4"
    for name, expected in lock["files"].items():
        assert hashlib.sha256((EXPERIENCE_CORPUS / name).read_bytes()).hexdigest() == expected

    cases = json.loads((EXPERIENCE_CORPUS / "demo-experience-cases.json").read_bytes())
    schema = json.loads((EXPERIENCE_CORPUS / "demo-experience-v1.schema.json").read_bytes())
    Draft202012Validator.check_schema(schema)
    private_key, public_key = _keys()
    source = gateway_read.load_packaged_development_experience_source(
        authorized_subjects=frozenset({SUBJECT})
    )
    app = _application(public_key, None, experience_source=source)

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=GATEWAY_ORIGIN
        ) as client:
            return await client.get(
                "/v1/instances/dev/demo/experience",
                headers=_headers(_token(private_key)),
            )

    response = asyncio.run(exercise())
    assert response.status_code == 200
    body = response.json()
    Draft202012Validator(schema).validate(body)
    assert body["provider"]["authority"] == "generated"
    assert body["provider"]["scenario"]["selectedBy"] == "gateway"
    assert body["experience"]["lanes"]["operator"]["snapshot"]["advertisedActions"] == []
    for case in cases["positive"]:
        if capability := case.get("capability"):
            assert capability in body["provider"]["capabilities"]
        if case.get("field") == "experience.coverage":
            assert (
                body["experience"]["coverage"]["required"]
                == body["experience"]["coverage"]["exercised"]
            )


def test_pinned_failure_policy_covers_generated_and_live_startup_modes() -> None:
    failure_policy = json.loads(
        (EXPERIENCE_CORPUS / "demo-provider-failure-cases.json").read_bytes()
    )
    error_schema = json.loads(
        (EXPERIENCE_CORPUS / "demo-experience-errors-v1.schema.json").read_bytes()
    )
    Draft202012Validator.check_schema(error_schema)
    cases = {case["id"]: case for case in failure_policy["cases"]}
    private_key, public_key = _keys()
    generated = gateway_read.load_packaged_development_experience_source(
        authorized_subjects=frozenset({SUBJECT})
    )
    generated_app = _application(
        public_key,
        None,
        experience_source=generated,
        authorized_subjects=frozenset({SUBJECT, "user_denied"}),
    )
    live_app = _application(public_key, None, experience_source=None)

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=generated_app), base_url=GATEWAY_ORIGIN
        ) as client:
            unauthenticated = await client.get(
                "/v1/instances/dev/demo/experience", headers={"Origin": APP_ORIGIN}
            )
            unauthorized = await client.get(
                "/v1/instances/dev/demo/experience",
                headers=_headers(_token(private_key, subject="user_denied")),
            )
            selected = await client.get(
                "/v1/instances/dev/demo/experience?scenario=dense",
                headers=_headers(_token(private_key)),
            )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=live_app), base_url=GATEWAY_ORIGIN
        ) as client:
            unavailable = await client.get(
                "/v1/instances/dev/demo/experience",
                headers=_headers(_token(private_key)),
            )
        return unauthenticated, unauthorized, selected, unavailable

    unauthenticated, unauthorized, selected, unavailable = asyncio.run(exercise())
    for response, case_id in (
        (unauthenticated, "authentication-failed"),
        (unauthorized, "authorization-failed"),
        (unavailable, "source-unavailable"),
    ):
        case = cases[case_id]
        assert response.status_code == case["expectedStatus"]
        assert response.json()["error"]["code"] == case["errorCode"]
        assert response.json()["error"]["retryable"] is case["retryable"]
        assert response.headers["cache-control"] == "no-store"
        Draft202012Validator(error_schema).validate(response.json())
    assert unavailable.headers["retry-after"] == "1"
    assert failure_policy["sourcePolicy"]["modes"]["live"]["fallback"] == "forbidden"
    assert (selected.status_code, selected.json()["error"]["code"]) == (
        400,
        "invalid_request",
    )


def test_generated_source_rejects_digest_valid_mixed_lane_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_bytes, manifest_bytes = _experience_bytes()
    artifact = json.loads(artifact_bytes)
    artifact["experience"]["lanes"]["planning"]["authority"] = "live"
    digest_input = {
        key: artifact[key]
        for key in ("schemaVersion", "artifactVersion", "generatedBy", "descriptor", "experience")
    }
    artifact["digest"] = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                digest_input, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode()
            + b"\n"
        ).hexdigest()
    )
    artifact["byteCount"] = 0
    for _ in range(3):
        encoded_artifact = (
            json.dumps(artifact, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
            + b"\n"
        )
        artifact["byteCount"] = len(encoded_artifact)
    encoded_artifact = (
        json.dumps(artifact, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        + b"\n"
    )
    manifest = json.loads(manifest_bytes)
    manifest["digest"] = artifact["digest"]
    manifest["byteCount"] = artifact["byteCount"]
    encoded_manifest = json.dumps(manifest, indent=2, sort_keys=True).encode() + b"\n"
    monkeypatch.setattr(
        gateway_read,
        "PINNED_DEMO_EXPERIENCE_SHA256",
        hashlib.sha256(encoded_artifact).hexdigest(),
    )
    monkeypatch.setattr(
        gateway_read,
        "PINNED_DEMO_EXPERIENCE_MANIFEST_SHA256",
        hashlib.sha256(encoded_manifest).hexdigest(),
    )
    monkeypatch.setattr(gateway_read, "PINNED_DEMO_EXPERIENCE_DIGEST", artifact["digest"])

    with pytest.raises(gateway_read.CatalogValidationError, match="provenance"):
        gateway_read.GeneratedExperienceReadSource(
            encoded_artifact,
            encoded_manifest,
            authorized_subjects=frozenset({SUBJECT}),
        )


def test_packaged_catalog_refuses_tampered_artifact_or_manifest_before_use() -> None:
    artifact, manifest = _catalog_bytes()

    with pytest.raises(gateway_read.CatalogValidationError, match="artifact does not match"):
        gateway_read.GeneratedCatalogReadSource(
            artifact + b" ", manifest, authorized_subjects=frozenset({SUBJECT})
        )
    with pytest.raises(gateway_read.CatalogValidationError, match="manifest digest"):
        gateway_read.GeneratedCatalogReadSource(
            artifact, manifest + b" ", authorized_subjects=frozenset({SUBJECT})
        )


def test_release_selection_is_read_from_the_digest_pinned_manifest() -> None:
    artifact, packaged_manifest = _catalog_bytes()
    selected_manifest, selected_digest = _manifest_select(packaged_manifest, "small")

    source = gateway_read.GeneratedCatalogReadSource(
        artifact,
        selected_manifest,
        expected_manifest_sha256=selected_digest,
        authorized_subjects=frozenset({SUBJECT}),
    )

    assert source.selected_scenario_id == "small"
    with pytest.raises(gateway_read.CatalogValidationError, match="manifest digest"):
        gateway_read.GeneratedCatalogReadSource(
            artifact,
            selected_manifest.replace(b'"small"', b'"dense"', 1),
            expected_manifest_sha256=selected_digest,
            authorized_subjects=frozenset({SUBJECT}),
        )


def test_authenticated_bridge_lists_selected_hives_and_returns_rich_generated_snapshot() -> None:
    private_key, public_key = _keys()
    source = gateway_read.load_packaged_development_source(authorized_subjects=frozenset({SUBJECT}))
    app = _application(public_key, source)

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=GATEWAY_ORIGIN
        ) as client:
            headers = _headers(_token(private_key))
            directory = await client.get("/v1/instances/dev/demo/hives", headers=headers)
            snapshot = await client.get(
                "/v1/instances/dev/demo/hives/github%2Fbeadhive%2Fbaml-harness/snapshot",
                params={"detail": "live"},
                headers=headers,
            )
            return directory, snapshot

    directory, response = asyncio.run(exercise())
    assert directory.status_code == response.status_code == 200
    assert [item["hiveId"] for item in directory.json()["items"]] == [
        "github/beadhive/baml-harness",
        "github/beadhive/beadhive-app",
        "github/beadhive/beadhive-ui",
    ]
    assert directory.json()["detailLevel"] == "summary"
    assert directory.json()["nextCursor"] is None
    assert all(item["sourceMode"] == "generated" for item in directory.json()["items"])
    assert all(item["scenarioId"] == "multi-hive" for item in directory.json()["items"])
    assert all(item["capabilities"] == ["snapshot"] for item in directory.json()["items"])
    assert directory.headers["cache-control"] == "private, max-age=0, must-revalidate"
    assert directory.headers["vary"] == "Authorization, Origin, Accept"
    assert directory.headers["etag"].startswith('"sha256:')

    envelope = response.json()
    assert envelope["contractVersion"] == "gateway.read.v1"
    assert envelope["factoryId"] == "development"
    assert envelope["hiveId"] == "github/beadhive/baml-harness"
    assert envelope["detailLevel"] == "live"
    assert envelope["source"]["mode"] == "generated"
    assert envelope["source"]["provenance"] == {
        "system": "@beadhive/operator-testkit",
        "version": "0.1.0",
        "scenario": "multi-hive",
    }
    assert envelope["source"]["revision"] == envelope["snapshot"]["revision"]
    assert envelope["snapshot"]["hive"]["prefix"] == envelope["hiveId"]
    assert not envelope["snapshot"]["advertisedActions"]
    assert set(_hive_ids(envelope["snapshot"])) <= {envelope["hiveId"]}


def test_canonical_factory_routes_alias_generated_bridge_payloads_without_instance_identity() -> (
    None
):
    private_key, public_key = _keys()
    source = gateway_read.load_packaged_development_source(authorized_subjects=frozenset({SUBJECT}))
    app = _application(public_key, source)

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=GATEWAY_ORIGIN
        ) as client:
            headers = _headers(_token(private_key))
            bridge_directory = await client.get(
                "/v1/instances/dev/demo/hives", params={"limit": "1"}, headers=headers
            )
            canonical_directory = await client.get(
                "/v1/factories/development/hives", params={"limit": "1"}, headers=headers
            )
            hive = "github%2Fbeadhive%2Fbaml-harness"
            bridge_snapshot = await client.get(
                f"/v1/instances/dev/demo/hives/{hive}/snapshot", headers=headers
            )
            canonical_snapshot = await client.get(
                f"/v1/factories/development/hives/{hive}/snapshot", headers=headers
            )
            return bridge_directory, canonical_directory, bridge_snapshot, canonical_snapshot

    bridge_directory, canonical_directory, bridge_snapshot, canonical_snapshot = asyncio.run(
        exercise()
    )
    assert canonical_directory.status_code == canonical_snapshot.status_code == 200
    expected_directory = bridge_directory.json()
    expected_directory.pop("instanceId")
    assert canonical_directory.json() == expected_directory
    expected_snapshot = bridge_snapshot.json()
    expected_snapshot.pop("instanceId")
    assert canonical_snapshot.json() == expected_snapshot
    assert bridge_directory.json()["instanceId"] == "dev/demo"
    assert bridge_snapshot.json()["instanceId"] == "dev/demo"


def test_factory_overview_discloses_group_freshness_and_non_atomic_aggregate_coverage() -> None:
    private_key, public_key = _keys()
    source = gateway_read.load_packaged_development_source(authorized_subjects=frozenset({SUBJECT}))
    app = _application(public_key, source)

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=GATEWAY_ORIGIN
        ) as client:
            headers = _headers(_token(private_key))
            overview = await client.get("/v1/factories/development", headers=headers)
            missing = await client.get("/v1/factories/production", headers=headers)
            unauthorized = await client.get(
                "/v1/factories/development", headers={"Origin": APP_ORIGIN}
            )
            return overview, missing, unauthorized

    overview, missing, unauthorized = asyncio.run(exercise())
    assert overview.status_code == 200
    assert overview.json() == {
        "schemaVersion": 1,
        "contractVersion": "gateway.read.v1",
        "factoryId": "development",
        "detailLevel": "overview",
        "groups": {
            "hives": {
                "freshness": {
                    "state": "fresh",
                    "asOf": 1777000000000,
                    "expiresAt": None,
                    "detail": (
                        "Each hive is observed independently; timestamps do not imply atomic "
                        "cross-hive ordering."
                    ),
                },
                "coverage": {"state": "complete", "requested": 3, "returned": 3},
            }
        },
        "coverage": {"state": "complete", "groupsRequested": 1, "groupsReturned": 1},
        "consistency": {"atomicAcrossHives": False, "ordering": "per-hive"},
    }
    assert missing.status_code == 404
    assert unauthorized.status_code == 401


def test_canonical_directory_cursor_is_bound_to_exact_factory_selector() -> None:
    private_key, public_key = _keys()
    source = gateway_read.load_packaged_development_source(authorized_subjects=frozenset({SUBJECT}))
    app = _application(public_key, source)

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=GATEWAY_ORIGIN
        ) as client:
            headers = _headers(_token(private_key))
            first = await client.get(
                "/v1/factories/development/hives", params={"limit": "1"}, headers=headers
            )
            cursor = first.json()["nextCursor"]
            resumed = await client.get(
                "/v1/factories/development/hives",
                params={"limit": "1", "after": cursor},
                headers=headers,
            )
            wrong_factory = await client.get(
                "/v1/factories/production/hives",
                params={"limit": "1", "after": cursor},
                headers=headers,
            )
            return first, resumed, wrong_factory

    first, resumed, wrong_factory = asyncio.run(exercise())
    assert first.status_code == resumed.status_code == 200
    assert first.json()["items"] != resumed.json()["items"]
    assert wrong_factory.status_code == 404


def test_directory_advertises_events_only_when_the_selected_hive_has_replayable_events() -> None:
    artifact, packaged_manifest = _catalog_bytes()

    async def capabilities_for(scenario_id: str) -> list[str]:
        manifest, manifest_digest = _manifest_select(packaged_manifest, scenario_id)
        source = gateway_read.GeneratedCatalogReadSource(
            artifact,
            manifest,
            authorized_subjects=frozenset({SUBJECT}),
            expected_manifest_sha256=manifest_digest,
        )
        directory = await source.list_hives(SUBJECT, limit=200, after=None)
        return directory["items"][0]["capabilities"]  # type: ignore[index,return-value]

    assert asyncio.run(capabilities_for("small")) == ["snapshot", "events"]
    assert asyncio.run(capabilities_for("dense")) == ["snapshot"]


@pytest.mark.parametrize(
    ("route_prefix", "has_legacy_instance"),
    [
        ("/v1/instances/dev/demo", True),
        ("/v1/factories/development", False),
    ],
)
def test_generated_sse_replays_only_catalog_events_and_requires_exact_snapshot_scope(
    route_prefix: str, has_legacy_instance: bool
) -> None:
    private_key, public_key = _keys()
    artifact, packaged_manifest = _catalog_bytes()
    manifest, manifest_digest = _manifest_select(packaged_manifest, "small")
    source = gateway_read.GeneratedCatalogReadSource(
        artifact,
        manifest,
        authorized_subjects=frozenset({SUBJECT}),
        expected_manifest_sha256=manifest_digest,
    )
    revoked = False
    app = _application(
        public_key,
        source,
        subject_is_revoked=lambda _subject: revoked,
        runtime_calls=frame_bridge.RuntimeCallPolicy(stream_reauthorize_seconds=0.05),
    )

    async def exercise():
        nonlocal revoked
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=GATEWAY_ORIGIN
        ) as client:
            headers = _headers(_token(private_key))
            snapshot = await client.get(
                f"{route_prefix}/hives/github%2Fbeadhive%2Fbeadhive-ui/snapshot",
                headers=headers,
            )
            cursor = snapshot.json()["snapshot"]["cursor"]
            wrong_scope = await client.get(
                f"{route_prefix}/hives/github%2Fbeadhive%2Fbeadhive-ui/events",
                params={"subscription": "wrong", "after": f"{cursor['producerEpoch']}:10"},
                headers=headers,
            )
            conflicting = await client.get(
                f"{route_prefix}/hives/github%2Fbeadhive%2Fbeadhive-ui/events",
                params={
                    "subscription": cursor["subscriptionId"],
                    "after": f"{cursor['producerEpoch']}:10",
                },
                headers={**headers, "Last-Event-ID": f"{cursor['producerEpoch']}:9"},
            )
            stream_task = asyncio.create_task(
                client.get(
                    f"{route_prefix}/hives/github%2Fbeadhive%2Fbeadhive-ui/events",
                    params={
                        "subscription": cursor["subscriptionId"],
                        "after": f"{cursor['producerEpoch']}:{cursor['sequence']}",
                    },
                    headers=headers,
                )
            )
            await asyncio.sleep(0.12)
            assert not stream_task.done()
            revoked = True
            stream = await asyncio.wait_for(stream_task, timeout=0.5)
            return stream, wrong_scope, conflicting

    stream, wrong_scope, conflicting = asyncio.run(exercise())
    assert stream.status_code == 200
    assert stream.headers["cache-control"] == "no-store, no-transform"
    assert stream.headers["x-accel-buffering"] == "no"
    assert stream.text.count("event: operator-event") == 1
    assert '"contractVersion":"gateway.read.v1"' in stream.text
    assert '"factoryId":"development"' in stream.text
    assert '"hiveId":"github/beadhive/beadhive-ui"' in stream.text
    assert ('"instanceId":"dev/demo"' in stream.text) is has_legacy_instance
    assert '"kind":"entity-upsert"' in stream.text
    assert "fixture-subscription" not in stream.text
    assert "fixture-epoch-1" not in stream.text
    assert wrong_scope.status_code == 409
    assert wrong_scope.json()["error"]["code"] == "resnapshot_required"
    assert conflicting.status_code == 400
    assert conflicting.json()["error"]["code"] == "invalid_request"


@pytest.mark.parametrize("authorization_change", ["expiry", "revocation"])
def test_zero_event_generated_sse_stays_open_and_continuously_reauthorizes(
    authorization_change: str,
) -> None:
    private_key, public_key = _keys()
    source = gateway_read.load_packaged_development_source(authorized_subjects=frozenset({SUBJECT}))
    auth = {"now": 100.0, "revoked": False}
    app = _application(
        public_key,
        source,
        verifier_now=lambda: auth["now"],
        subject_is_revoked=lambda _subject: auth["revoked"],
        runtime_calls=frame_bridge.RuntimeCallPolicy(stream_reauthorize_seconds=0.05),
    )

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=GATEWAY_ORIGIN
        ) as client:
            token = _token(private_key, expires_at=101)
            headers = _headers(token)
            snapshot = await client.get(
                "/v1/instances/dev/demo/hives/github%2Fbeadhive%2Fbaml-harness/snapshot",
                headers=headers,
            )
            cursor = snapshot.json()["snapshot"]["cursor"]
            request = asyncio.create_task(
                client.get(
                    "/v1/instances/dev/demo/hives/github%2Fbeadhive%2Fbaml-harness/events",
                    params={
                        "subscription": cursor["subscriptionId"],
                        "after": f"{cursor['producerEpoch']}:{cursor['sequence']}",
                    },
                    headers=headers,
                )
            )
            await asyncio.sleep(0.12)
            assert not request.done(), "a zero-event generated subscription must remain live"
            if authorization_change == "expiry":
                auth["now"] = 102.0
            else:
                auth["revoked"] = True
            return await asyncio.wait_for(request, timeout=0.5)

    response = asyncio.run(exercise())
    assert response.status_code == 200
    assert ": keep-alive\n\n" in response.text
    assert "event: operator-event" not in response.text


def test_zero_event_generated_iterator_is_cancellation_aware() -> None:
    source = gateway_read.load_packaged_development_source(authorized_subjects=frozenset({SUBJECT}))

    async def exercise() -> None:
        snapshot = await source.snapshot(
            SUBJECT,
            factory_id="development",
            hive_id="github/beadhive/baml-harness",
            detail="live",
        )
        cursor = snapshot["snapshot"]["cursor"]
        assert isinstance(cursor, dict)
        stream = await source.events(
            SUBJECT,
            factory_id="development",
            hive_id="github/beadhive/baml-harness",
            subscription=str(cursor["subscriptionId"]),
            after=f"{cursor['producerEpoch']}:{cursor['sequence']}",
        )
        pending = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        assert not pending.done()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending

    asyncio.run(exercise())


def test_rich_read_admission_is_partitioned_by_authenticated_subject() -> None:
    private_key, public_key = _keys()
    other = "other_development_user"
    base = gateway_read.load_packaged_development_source(
        authorized_subjects=frozenset({SUBJECT, other})
    )
    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingSource:
        cache_boundary = base.cache_boundary

        async def list_hives(self, subject, *, limit, after):
            if subject == SUBJECT:
                entered.set()
                await release.wait()
            return await base.list_hives(subject, limit=limit, after=after)

        async def snapshot(self, subject, *, factory_id, hive_id, detail):
            return await base.snapshot(
                subject, factory_id=factory_id, hive_id=hive_id, detail=detail
            )

        async def events(self, subject, *, factory_id, hive_id, subscription, after):
            return await base.events(
                subject,
                factory_id=factory_id,
                hive_id=hive_id,
                subscription=subscription,
                after=after,
            )

    app = _application(
        public_key,
        BlockingSource(),
        authorized_subjects=frozenset({SUBJECT, other}),
        runtime_calls=frame_bridge.RuntimeCallPolicy(
            deadline_seconds=1,
            rich_read_concurrency=2,
            rich_read_concurrency_per_subject=1,
        ),
    )

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=GATEWAY_ORIGIN
        ) as client:
            first = asyncio.create_task(
                client.get(
                    "/v1/instances/dev/demo/hives",
                    headers=_headers(_token(private_key)),
                )
            )
            await asyncio.wait_for(entered.wait(), timeout=0.5)
            same_subject = await client.get(
                "/v1/instances/dev/demo/hives",
                headers=_headers(_token(private_key)),
            )
            other_subject = await client.get(
                "/v1/instances/dev/demo/hives",
                headers=_headers(_token(private_key, subject=other)),
            )
            release.set()
            return await first, same_subject, other_subject

    first, same_subject, other_subject = asyncio.run(exercise())
    assert first.status_code == other_subject.status_code == 200
    assert same_subject.status_code == 429
    assert same_subject.json()["error"]["code"] == "rate_limited"


def test_sse_admission_is_partitioned_by_authenticated_subject() -> None:
    private_key, public_key = _keys()
    other = "other_development_user"
    third = "third_development_user"
    base = gateway_read.load_packaged_development_source(
        authorized_subjects=frozenset({SUBJECT, other, third})
    )
    entered = {subject: asyncio.Event() for subject in (SUBJECT, other, third)}
    revoked: set[str] = set()

    class ObservedSource:
        cache_boundary = base.cache_boundary

        async def list_hives(self, subject, *, limit, after):
            return await base.list_hives(subject, limit=limit, after=after)

        async def snapshot(self, subject, *, factory_id, hive_id, detail):
            return await base.snapshot(
                subject, factory_id=factory_id, hive_id=hive_id, detail=detail
            )

        async def events(self, subject, *, factory_id, hive_id, subscription, after):
            entered[subject].set()
            return await base.events(
                subject,
                factory_id=factory_id,
                hive_id=hive_id,
                subscription=subscription,
                after=after,
            )

    app = _application(
        public_key,
        ObservedSource(),
        authorized_subjects=frozenset({SUBJECT, other, third}),
        subject_is_revoked=lambda subject: subject in revoked,
        runtime_calls=frame_bridge.RuntimeCallPolicy(
            stream_concurrency=2,
            stream_concurrency_per_subject=1,
            stream_reauthorize_seconds=0.05,
        ),
    )

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=GATEWAY_ORIGIN
        ) as client:
            streams = []
            for subject in (SUBJECT, other, third):
                headers = _headers(_token(private_key, subject=subject))
                snapshot = await client.get(
                    "/v1/instances/dev/demo/hives/github%2Fbeadhive%2Fbaml-harness/snapshot",
                    headers=headers,
                )
                cursor = snapshot.json()["snapshot"]["cursor"]
                streams.append(
                    (
                        headers,
                        {
                            "subscription": cursor["subscriptionId"],
                            "after": f"{cursor['producerEpoch']}:{cursor['sequence']}",
                        },
                    )
                )
            path = "/v1/instances/dev/demo/hives/github%2Fbeadhive%2Fbaml-harness/events"
            first = asyncio.create_task(
                client.get(path, headers=streams[0][0], params=streams[0][1])
            )
            await asyncio.wait_for(entered[SUBJECT].wait(), timeout=0.5)
            same_subject = await client.get(path, headers=streams[0][0], params=streams[0][1])
            second = asyncio.create_task(
                client.get(path, headers=streams[1][0], params=streams[1][1])
            )
            await asyncio.wait_for(entered[other].wait(), timeout=0.5)
            assert not first.done() and not second.done()
            process_ceiling = await client.get(path, headers=streams[2][0], params=streams[2][1])
            revoked.update({SUBJECT, other})
            return (
                await asyncio.wait_for(first, timeout=0.5),
                same_subject,
                await asyncio.wait_for(second, timeout=0.5),
                process_ceiling,
            )

    first, same_subject, second, process_ceiling = asyncio.run(exercise())
    assert first.status_code == second.status_code == 200
    assert same_subject.status_code == process_ceiling.status_code == 429
    assert same_subject.json()["error"]["code"] == "rate_limited"
    assert process_ceiling.json()["error"]["code"] == "rate_limited"


@pytest.mark.parametrize("failure", ["malformed", "gapped"])
def test_generated_sse_converts_malformed_or_gapped_events_to_reset(failure: str) -> None:
    private_key, public_key = _keys()
    base = gateway_read.load_packaged_development_source(authorized_subjects=frozenset({SUBJECT}))

    class BrokenEventSource:
        cache_boundary = base.cache_boundary

        async def list_hives(self, subject, *, limit, after):
            return await base.list_hives(subject, limit=limit, after=after)

        async def snapshot(self, subject, *, factory_id, hive_id, detail):
            return await base.snapshot(
                subject, factory_id=factory_id, hive_id=hive_id, detail=detail
            )

        async def events(self, subject, *, factory_id, hive_id, subscription, after):
            assert after is not None
            epoch, raw_sequence = after.rsplit(":", 1)
            base_sequence = int(raw_sequence)

            async def stream():
                event: object
                if failure == "malformed":
                    event = []
                else:
                    event = {
                        "subscriptionId": subscription,
                        "producerEpoch": epoch,
                        "hiveId": hive_id,
                        "sequence": base_sequence + 2,
                        "baseSequence": base_sequence,
                    }
                yield {
                    "schemaVersion": 1,
                    "contractVersion": "gateway.read.v1",
                    "instanceId": "dev/demo",
                    "factoryId": factory_id,
                    "hiveId": hive_id,
                    "detailLevel": "live",
                    "event": event,
                }

            return stream()

    app = _application(public_key, BrokenEventSource())

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=GATEWAY_ORIGIN
        ) as client:
            headers = _headers(_token(private_key))
            path = "/v1/instances/dev/demo/hives/github%2Fbeadhive%2Fbaml-harness"
            snapshot = await client.get(f"{path}/snapshot", headers=headers)
            cursor = snapshot.json()["snapshot"]["cursor"]
            return await client.get(
                f"{path}/events",
                params={
                    "subscription": cursor["subscriptionId"],
                    "after": f"{cursor['producerEpoch']}:{cursor['sequence']}",
                },
                headers=headers,
            )

    response = asyncio.run(exercise())
    assert response.status_code == 200
    assert response.text == 'event: resnapshot-required\ndata: {"schemaVersion":1}\n\n'


def test_directory_pagination_etag_and_errors_are_scope_bound_and_redacted() -> None:
    private_key, public_key = _keys()
    source = gateway_read.load_packaged_development_source(authorized_subjects=frozenset({SUBJECT}))
    app = _application(public_key, source)

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=GATEWAY_ORIGIN
        ) as client:
            headers = _headers(_token(private_key))
            first = await client.get(
                "/v1/instances/dev/demo/hives", params={"limit": "1"}, headers=headers
            )
            second = await client.get(
                "/v1/instances/dev/demo/hives",
                params={"limit": "1", "after": first.json()["nextCursor"]},
                headers=headers,
            )
            third = await client.get(
                "/v1/instances/dev/demo/hives",
                params={"limit": "1", "after": second.json()["nextCursor"]},
                headers=headers,
            )
            conditional = await client.get(
                "/v1/instances/dev/demo/hives",
                params={"limit": "1"},
                headers={**headers, "If-None-Match": first.headers["etag"]},
            )
            rebound = await client.get(
                "/v1/instances/dev/demo/hives",
                params={"limit": "2", "after": first.json()["nextCursor"]},
                headers=headers,
            )
            selected_by_caller = await client.get(
                "/v1/instances/dev/demo/hives",
                params={"scenario": "small"},
                headers=headers,
            )
            malformed_hive = await client.get(
                "/v1/instances/dev/demo/hives/github%2fbeadhive%2fbeadhive-ui/snapshot",
                headers=headers,
            )
            hidden_hive = await client.get(
                "/v1/instances/dev/demo/hives/github%2Fbeadhive%2Fmissing/snapshot",
                headers=headers,
            )
            unauthenticated = await client.get(
                "/v1/instances/dev/demo/hives", headers={"Origin": APP_ORIGIN}
            )
            unauthorized = await client.get(
                "/v1/instances/dev/demo/hives",
                headers=_headers(_token(private_key, subject="other_development_user")),
            )
            return (
                first,
                second,
                third,
                conditional,
                rebound,
                selected_by_caller,
                malformed_hive,
                hidden_hive,
                unauthenticated,
                unauthorized,
            )

    (
        first,
        second,
        third,
        conditional,
        rebound,
        selected_by_caller,
        malformed_hive,
        hidden_hive,
        unauthenticated,
        unauthorized,
    ) = asyncio.run(exercise())
    assert [
        first.json()["items"][0]["hiveId"],
        second.json()["items"][0]["hiveId"],
        third.json()["items"][0]["hiveId"],
    ] == [
        "github/beadhive/baml-harness",
        "github/beadhive/beadhive-app",
        "github/beadhive/beadhive-ui",
    ]
    assert first.json()["nextCursor"] and second.json()["nextCursor"]
    assert third.json()["nextCursor"] is None
    assert conditional.status_code == 304 and not conditional.content
    assert rebound.status_code == 409
    assert selected_by_caller.status_code == malformed_hive.status_code == 400
    assert hidden_hive.status_code == unauthorized.status_code == 404
    assert unauthenticated.status_code == 401
    for response in (
        rebound,
        selected_by_caller,
        malformed_hive,
        hidden_hive,
        unauthenticated,
        unauthorized,
    ):
        assert set(response.json()["error"]) == {
            "code",
            "message",
            "retryable",
            "requestId",
            "details",
        }
        assert response.json()["error"]["details"] == {}
        assert "Bearer" not in response.text
        assert "fixture" not in response.text


def test_runtime_factory_installs_validated_catalog_before_serving(tmp_path, monkeypatch) -> None:
    private_key, public_key = _keys()
    jwk = public_key.as_dict()
    jwk.update({"kid": "development-test", "use": "sig", "alg": "RS256"})
    jwks = tmp_path / "clerk-jwks.json"
    subjects = tmp_path / "authorized-subjects.json"
    daemon_bearer = tmp_path / "daemon-bearer"
    jwks.write_text(json.dumps({"keys": [jwk]}), encoding="utf-8")
    subjects.write_text(json.dumps([SUBJECT]), encoding="utf-8")
    daemon_bearer.write_text("bh1.frame-bridge." + "d" * 43, encoding="ascii")
    jwks.chmod(0o600)
    subjects.chmod(0o600)
    daemon_bearer.chmod(0o600)
    monkeypatch.setenv("BEADHIVE_FRAME_BRIDGE_JWKS_FILE", str(jwks))
    monkeypatch.setenv("BEADHIVE_FRAME_BRIDGE_SUBJECTS_FILE", str(subjects))
    monkeypatch.setenv("BEADHIVE_FRAME_BRIDGE_DAEMON_CREDENTIAL_FILE", str(daemon_bearer))
    monkeypatch.setenv("BEADHIVE_FRAME_BRIDGE_SOURCE_MODE", "generated")

    app = frame_bridge_runtime.create_application()

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=GATEWAY_ORIGIN
        ) as client:
            headers = _headers(_token(private_key))
            return (
                await client.get("/v1/instances/dev/demo/hives", headers=headers),
                await client.get("/v1/instances/dev/demo/experience", headers=headers),
            )

    directory, experience = asyncio.run(exercise())
    assert directory.status_code == experience.status_code == 200
    assert directory.json()["contractVersion"] == "gateway.read.v1"
    assert len(directory.json()["items"]) == 3
    assert experience.json()["provider"]["authority"] == "generated"


def test_runtime_live_mode_never_loads_generated_artifacts(tmp_path, monkeypatch) -> None:
    private_key, public_key = _keys()
    jwk = public_key.as_dict()
    jwk.update({"kid": "development-test", "use": "sig", "alg": "RS256"})
    jwks = tmp_path / "clerk-jwks.json"
    subjects = tmp_path / "authorized-subjects.json"
    daemon_bearer = tmp_path / "daemon-bearer"
    jwks.write_text(json.dumps({"keys": [jwk]}), encoding="utf-8")
    subjects.write_text(json.dumps([SUBJECT]), encoding="utf-8")
    daemon_bearer.write_text("bh1.frame-bridge." + "d" * 43, encoding="ascii")
    jwks.chmod(0o600)
    subjects.chmod(0o600)
    daemon_bearer.chmod(0o600)
    monkeypatch.setenv("BEADHIVE_FRAME_BRIDGE_JWKS_FILE", str(jwks))
    monkeypatch.setenv("BEADHIVE_FRAME_BRIDGE_SUBJECTS_FILE", str(subjects))
    monkeypatch.setenv("BEADHIVE_FRAME_BRIDGE_DAEMON_CREDENTIAL_FILE", str(daemon_bearer))
    monkeypatch.setenv("BEADHIVE_FRAME_BRIDGE_SOURCE_MODE", "live")

    def forbidden_generated_load(*_args, **_kwargs):
        raise AssertionError("live startup attempted to load a generated artifact")

    monkeypatch.setattr(gateway_read, "load_packaged_development_source", forbidden_generated_load)
    monkeypatch.setattr(
        gateway_read,
        "load_packaged_development_experience_source",
        forbidden_generated_load,
    )

    app = frame_bridge_runtime.create_application()

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=GATEWAY_ORIGIN
        ) as client:
            return (
                await client.get("/healthz"),
                await client.get(
                    "/v1/instances/dev/demo/experience",
                    headers=_headers(_token(private_key)),
                ),
            )

    health, experience = asyncio.run(exercise())
    assert health.status_code == 200
    assert (experience.status_code, experience.json()["error"]["code"]) == (
        503,
        "source_unavailable",
    )


def test_rich_requests_use_only_prevalidated_memory(monkeypatch) -> None:
    private_key, public_key = _keys()
    source = gateway_read.load_packaged_development_source(authorized_subjects=frozenset({SUBJECT}))

    def forbidden_resource_access(*_args, **_kwargs):
        raise AssertionError("request attempted package or filesystem discovery")

    monkeypatch.setattr(gateway_read.resources, "files", forbidden_resource_access)
    app = _application(public_key, source, fail_legacy_access=True)

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=GATEWAY_ORIGIN
        ) as client:
            headers = _headers(_token(private_key))
            directory = await client.get("/v1/instances/dev/demo/hives", headers=headers)
            snapshot = await client.get(
                "/v1/instances/dev/demo/hives/github%2Fbeadhive%2Fbeadhive-ui/snapshot",
                headers=headers,
            )
            return directory, snapshot

    directory, snapshot = asyncio.run(exercise())
    assert directory.status_code == snapshot.status_code == 200


def test_legacy_gateway_v1_routes_remain_byte_compatible_with_bridge_installed() -> None:
    private_key, public_key = _keys()
    source = gateway_read.load_packaged_development_source(authorized_subjects=frozenset({SUBJECT}))
    config = frame_bridge.DevelopmentFrameBridgeConfig(
        issuer=ISSUER,
        audience=AUDIENCE,
        app_origin=APP_ORIGIN,
        gateway_origin=GATEWAY_ORIGIN,
    )
    event_epoch = "123e4567-e89b-42d3-a456-426614174000"

    async def snapshot():
        return {
            "schemaVersion": 1,
            "revision": "sha256:" + "a" * 64,
            "generatedAt": 1,
            "workItems": [],
            "agents": [],
            "eventCursor": f"{event_epoch}:1",
        }

    async def online():
        return True

    async def refresh(_expected_revision, _correlation_id):
        return {"status": "completed", "revision": "sha256:" + "b" * 64}

    async def events(cursor):
        sequence = int(cursor.rsplit(":", 1)[1])

        async def stream():
            yield {
                "cursor": f"{event_epoch}:{sequence + 1}",
                "revision": "sha256:" + "b" * 64,
            }

        return stream()

    def build(read_source):
        return frame_bridge.build_development_frame_bridge_application(
            config=config,
            verifier=frame_bridge.ClerkTokenVerifier(config=config, key=public_key),
            registry=frame_bridge.DevelopmentInstanceRegistry(
                instances={
                    "dev/demo": frame_bridge.RemoteInstance(
                        display_name="Development demo",
                        authorized_subjects=frozenset({SUBJECT}),
                        snapshot=snapshot,
                        online=online,
                        refresh=refresh,
                        events=events,
                    )
                }
            ),
            read_source=read_source,
        )

    legacy_app = build(None)
    bridge_app = build(source)
    token = _token(private_key)

    async def read_legacy_routes(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=GATEWAY_ORIGIN
        ) as client:
            auth = _headers(token)
            responses = [
                await client.get("/healthz"),
                await client.get("/v1/instances", params={"limit": "50"}, headers=auth),
                await client.get("/v1/instances/dev/demo/snapshot", headers=auth),
                await client.post(
                    "/v1/instances/dev/demo/commands/refresh",
                    headers=auth,
                    json={
                        "schemaVersion": 1,
                        "correlationId": "123e4567-e89b-42d3-a456-426614174000",
                        "expectedRevision": "sha256:" + "a" * 64,
                    },
                ),
                await client.get(
                    "/v1/instances/dev/demo/events",
                    params={"cursor": f"{event_epoch}:1"},
                    headers=auth,
                ),
                await client.get("/v1/instances", headers={"Origin": APP_ORIGIN}),
                await client.get("/v1/instances/dev/missing/snapshot", headers=auth),
                await client.get("/v1/instances", params={"limit": "1"}, headers=auth),
                await client.options(
                    "/v1/instances",
                    headers={
                        "Origin": APP_ORIGIN,
                        "Access-Control-Request-Method": "GET",
                        "Access-Control-Request-Headers": "Authorization",
                    },
                ),
                await client.options(
                    "/v1/instances/dev/demo/commands/refresh",
                    headers={
                        "Origin": APP_ORIGIN,
                        "Access-Control-Request-Method": "POST",
                        "Access-Control-Request-Headers": "Authorization, Content-Type",
                    },
                ),
            ]
            return tuple(
                (response.status_code, response.content, tuple(response.headers.multi_items()))
                for response in responses
            )

    bridge_responses = asyncio.run(read_legacy_routes(bridge_app))
    legacy_responses = asyncio.run(read_legacy_routes(legacy_app))
    assert bridge_responses == legacy_responses
