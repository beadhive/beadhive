"""Conformance coverage for the private aggregate-Gateway upstream."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import socket
import tempfile
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest
import uvicorn
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from joserfc import jws
from joserfc.jwk import OKPKey
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from beadhive import daemon_auth, frame_bridge_factory
from beadhive import frame_bridge_upstream as upstream

NOW = 1_800_000_000
REQUEST_ID = "req_" + "A" * 22
EPOCH = "11111111-1111-4111-8111-111111111111"
REVISION = "sha256:" + "a" * 64
BASE = "/frame-bridge/upstream/v1"
GATEWAY_REVISION = "e482bb44ad9f89dd9a751ce42d4332bec0b311e6"
GATEWAY_CONTRACT_CASES_SHA256 = "22c102911d8c346814db24dcd0ad70d6f9a6691c12a512072b2ae1116d1e52e9"


def _retrieval(revision: str) -> dict[str, object]:
    return {
        "contract": "beadhive.work-items/v1",
        "revision": revision,
        "views": ["ready", "active", "blocked", "recent"],
        "maxPageItems": 200,
        "maxPageBytes": 917_504,
        "maxDetailBytes": 917_504,
    }


@dataclass
class Source:
    online_value: bool = True
    calls: list[str] = field(default_factory=list)
    refreshes: int = 0
    page_too_large: bool = False
    detail_too_large: bool = False

    async def online(self) -> bool:
        self.calls.append("online")
        return self.online_value

    async def directory(self, *, limit: int, cursor: str | None) -> dict[str, object]:
        self.calls.append(f"directory:{limit}:{cursor}")
        return {
            "generatedAt": 1_787_000_000_000,
            "revision": REVISION,
            "items": []
            if cursor is not None
            else [
                {
                    "hiveId": "github/beadhive/beadhive",
                    "displayName": "Beadhive",
                    "availability": "available",
                    "coverage": "complete",
                    "asOf": 1_787_000_000_000,
                }
            ],
            "nextCursor": None,
        }

    async def snapshot(self) -> dict[str, object]:
        self.calls.append("snapshot")
        return {
            "schemaVersion": 1,
            "hive": {
                "prefix": "github/beadhive/beadhive",
                "provider": "github",
                "org": "beadhive",
                "repo": "beadhive",
            },
            "revision": REVISION,
            "generatedAt": 1_787_000_000_000,
            "cursor": {
                "subscriptionId": "source-subscription",
                "producerEpoch": EPOCH,
                "sequence": 1,
                "observedAt": 1_787_000_000_000,
            },
            "projectionPolicy": "beadhive.snapshot-summary/v1",
            "limits": {"maxBytes": 917_504, "maxWorkItems": 4_096},
            "coverage": {
                "state": "complete",
                "generatedAt": 1_787_000_000_000,
                "eligible": 0,
                "returned": 0,
                "reason": None,
                "policy": "beadhive.snapshot-summary/v1",
                "sourceRevision": REVISION,
                "limits": {"maxBytes": 917_504, "maxWorkItems": 4_096},
                "workItemRetrieval": _retrieval(REVISION),
                "sources": {},
            },
            "workItems": [],
        }

    async def work_items(
        self,
        *,
        view: str,
        limit: int,
        cursor: str | None,
        priorities: tuple[str, ...],
        labels: tuple[str, ...],
        assignee: str | None,
        issue_type: str | None,
        parent: str | None,
    ) -> dict[str, object]:
        self.calls.append(f"work-items:{view}:{limit}:{cursor}")
        if self.page_too_large:
            raise upstream.WorkItemPageTooLarge
        return {
            "schemaVersion": 1,
            "projectionPolicy": "beadhive.snapshot-summary/v1",
            "hiveId": "github/beadhive/beadhive",
            "queue": view,
            "revision": REVISION,
            "generatedAt": 1_787_000_000_000,
            "limits": {"maxBytes": 917_504, "maxItems": 200},
            "filters": {
                "priorities": list(priorities),
                "labels": list(labels),
                "assignee": assignee,
                "type": issue_type,
                "parent": parent,
                "ordering": "beadhive.work-items/v1",
            },
            "coverage": {
                "state": "complete",
                "sources": {},
                "eligible": 0,
                "returned": 0,
                "truncated": False,
                "nextCursor": None,
            },
            "limit": limit,
            "returned": 0,
            "truncated": False,
            "nextCursor": None,
            "items": [],
            "warnings": [],
        }

    async def work_item_detail(self, *, bead_id: str) -> dict[str, object]:
        self.calls.append(f"work-item-detail:{bead_id}")
        if self.detail_too_large:
            raise upstream.WorkItemDetailTooLarge
        raise upstream.SourceNotFound

    async def events(self, *, subscription: str, after: str | None):
        self.calls.append(f"events:{subscription}:{after}")

        async def stream():
            payload = {
                "schemaVersion": 1,
                "hiveId": "github/beadhive/beadhive",
                "subscriptionId": subscription,
                "producerEpoch": EPOCH,
                "sequence": 2,
                "baseSequence": 1,
                "observedAt": 1_787_000_000_001,
                "generatedAt": 1_787_000_000_001,
                "source": "beads",
                "revision": REVISION,
                "entity": None,
                "payload": {"kind": "heartbeat"},
            }
            yield (
                f"id: {EPOCH}:2\nevent: operator-event\ndata: "
                + json.dumps(payload, separators=(",", ":"))
                + "\n\n"
            ).encode()

        return stream()

    async def refresh(self, *, expected_revision: str, correlation_id: str) -> dict[str, object]:
        self.calls.append(f"refresh:{correlation_id}")
        self.refreshes += 1
        if expected_revision != REVISION:
            raise upstream.ResnapshotRequired
        return await self.snapshot()

    async def close(self) -> None:
        self.calls.append("close")


@dataclass
class Fixture:
    app: object
    private_key: Ed25519PrivateKey
    source: Source
    verifier_store: upstream.GatewayVerifierStore


def _fixture(*, admission_limit: int = 32) -> Fixture:
    private_key = Ed25519PrivateKey.generate()
    public = (
        base64.urlsafe_b64encode(private_key.public_key().public_bytes_raw()).decode().rstrip("=")
    )
    verifier_set = upstream.GatewayVerifierSet.from_document(
        {
            "verifierConfigVersion": 1,
            "issuers": [
                {
                    "issuer": upstream.GATEWAY_ISSUER,
                    "acceptedKeys": [
                        {
                            "kid": "development-gateway-key",
                            "alg": "EdDSA",
                            "jwk": {"kty": "OKP", "crv": "Ed25519", "x": public},
                            "notBefore": NOW - 60,
                            "notAfter": NOW + 60,
                        }
                    ],
                    "revokedKeyIds": [],
                }
            ],
        }
    )
    source = Source()
    verifier_store = upstream.GatewayVerifierStore(verifier_set)
    config = upstream.PrivateFrameBridgeConfig(
        verifier_store=verifier_store,
        host_epoch=EPOCH,
        admission_limit=admission_limit,
    )
    return Fixture(
        app=upstream.build_private_frame_bridge_application(
            config=config, source=source, now=lambda: NOW
        ),
        private_key=private_key,
        source=source,
        verifier_store=verifier_store,
    )


def _token(
    fixture: Fixture,
    *,
    target: str,
    scope: str,
    method: str = "GET",
    jti: bytes = b"j" * 16,
    header: dict[str, object] | None = None,
    **claims_overrides: object,
) -> str:
    claims: dict[str, object] = {
        "iss": upstream.GATEWAY_ISSUER,
        "sub": upstream.GATEWAY_ISSUER,
        "aud": upstream.UPSTREAM_CONTRACT,
        "iat": NOW,
        "nbf": NOW - 1,
        "exp": NOW + 9,
        "jti": base64.urlsafe_b64encode(jti).decode().rstrip("="),
        "request_id": REQUEST_ID,
        "method": method,
        "target_sha256": hashlib.sha256(target.encode("ascii")).hexdigest(),
        "host_id": "factory",
        "instance_id": "dev/demo",
        "factory_id": "development",
        "principal_sub": "system:gateway-health",
        "scope": scope,
    }
    claims.update(claims_overrides)
    protected = {"alg": "EdDSA", "kid": "development-gateway-key", "typ": "JWT"}
    protected.update(header or {})
    return jws.serialize_compact(
        protected,
        json.dumps(claims, separators=(",", ":")),
        OKPKey.import_key(fixture.private_key),
        algorithms=["EdDSA"],
    )


def _headers(
    fixture: Fixture,
    *,
    target: str,
    scope: str,
    method: str = "GET",
    jti: bytes = b"j" * 16,
    **claims_overrides: object,
) -> dict[str, str]:
    return {
        "Authorization": "Bearer "
        + _token(
            fixture,
            target=target,
            scope=scope,
            method=method,
            jti=jti,
            **claims_overrides,
        ),
        "X-Beadhive-Upstream-Contract": upstream.UPSTREAM_CONTRACT,
        "X-Beadhive-Request-Id": REQUEST_ID,
        "X-Beadhive-Deadline-Ms": "1000",
    }


def _pinned_gateway_registration_admission(value: object) -> tuple[int, str] | None:
    """Apply e482's fixed Factory registration admission result locally."""

    expected = {
        "schemaVersion": 1,
        "contractVersion": upstream.UPSTREAM_CONTRACT,
        "hostId": "factory",
        "hostEpoch": EPOCH,
        "instances": [upstream.RegisteredInstance().to_wire()],
    }
    if value == expected:
        return None
    return (409, "registration_mismatch")


@asynccontextmanager
async def _private_unix_client(app: object, socket_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Exercise the production listener shape without opening a TCP socket."""

    # Linux AF_UNIX paths are capped at 108 bytes.  The clean-checkout gate's
    # pytest tmpdir is deliberately nested enough to exceed that limit, so use
    # a still-private, unique short directory when necessary.
    socket_dir: tempfile.TemporaryDirectory[str] | None = None
    if len(os.fsencode(str(socket_path))) >= 100:
        socket_dir = tempfile.TemporaryDirectory(prefix="bh-frame-bridge-")
        socket_path = Path(socket_dir.name) / socket_path.name
    socket_path.parent.mkdir(mode=0o750, exist_ok=True)
    socket_path.parent.chmod(0o750)
    monkeypatch.setattr(frame_bridge_factory, "_FACTORY_SOCKET", socket_path)
    listener = frame_bridge_factory._bind_factory_socket(socket_path)
    server = uvicorn.Server(
        uvicorn.Config(app, access_log=False, lifespan="off", log_level="critical")
    )
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        for _ in range(200):
            if server.started:
                break
            await asyncio.sleep(0.01)
        assert server.started
        assert listener.family == socket.AF_UNIX
        transport = httpx.AsyncHTTPTransport(uds=str(socket_path), retries=0)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://frame-bridge.invalid"
        ) as client:
            yield client
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=3)
        if listener.fileno() != -1:
            listener.close()
        frame_bridge_factory._remove_stale_socket(socket_path)
        if socket_dir is not None:
            socket_dir.cleanup()


def test_private_registration_readiness_and_source_operations_are_request_bound() -> None:
    fixture = _fixture()

    async def exercise():
        transport = httpx.ASGITransport(app=fixture.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://frame-bridge.invalid"
        ) as client:
            registration_target = f"{BASE}/registration"
            registration = await client.get(
                registration_target,
                headers=_headers(
                    fixture,
                    target=registration_target,
                    scope="upstream:registration",
                    jti=b"1" * 16,
                ),
            )
            ready_target = f"{BASE}/readyz"
            ready = await client.get(
                ready_target,
                headers=_headers(
                    fixture, target=ready_target, scope="upstream:health", jti=b"2" * 16
                ),
            )
            directory_target = f"{BASE}/instances/dev%2Fdemo/hives?limit=50"
            directory = await client.get(
                directory_target,
                headers=_headers(
                    fixture, target=directory_target, scope="upstream:read", jti=b"3" * 16
                ),
            )
            snapshot_target = (
                f"{BASE}/instances/dev%2Fdemo/hives/github%2Fbeadhive%2Fbeadhive/snapshot"
            )
            snapshot = await client.get(
                snapshot_target,
                headers=_headers(
                    fixture, target=snapshot_target, scope="upstream:read", jti=b"4" * 16
                ),
            )
            return registration, ready, directory, snapshot

    registration, ready, directory, snapshot = asyncio.run(exercise())

    assert registration.status_code == 200
    assert registration.json()["instances"][0]["primaryHiveId"] == "github/beadhive/beadhive"
    assert ready.status_code == 200
    assert ready.json()["registrationDigest"].startswith("sha256:")
    assert directory.status_code == 200
    assert directory.json()["items"][0]["hiveId"] == "github/beadhive/beadhive"
    assert snapshot.status_code == 200
    assert snapshot.json()["hive"]["prefix"] == "github/beadhive/beadhive"
    assert fixture.source.calls == ["online", "directory:50:None", "snapshot"]


def test_private_work_item_page_is_normalized_and_detail_overflow_is_stable() -> None:
    fixture = _fixture()
    fixture.source.detail_too_large = True

    async def exercise():
        transport = httpx.ASGITransport(app=fixture.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://frame-bridge.invalid"
        ) as client:
            page_target = (
                f"{BASE}/instances/dev%2Fdemo/hives/github%2Fbeadhive%2Fbeadhive/"
                "work-items?limit=2&priority=P0&view=ready"
            )
            page = await client.get(
                page_target,
                headers=_headers(fixture, target=page_target, scope="upstream:read", jti=b"w" * 16),
            )
            detail_target = (
                f"{BASE}/instances/dev%2Fdemo/hives/github%2Fbeadhive%2Fbeadhive/work-items/bh-1"
            )
            detail = await client.get(
                detail_target,
                headers=_headers(
                    fixture, target=detail_target, scope="upstream:read", jti=b"d" * 16
                ),
            )
            return page, detail

    page, detail = asyncio.run(exercise())
    assert page.status_code == 200
    assert page.json()["view"] == "ready"
    assert "queue" not in page.json()
    assert page.json()["filters"]["priorities"] == ["P0"]
    assert "work-items:ready:2:None" in fixture.source.calls
    assert detail.status_code == 413
    assert detail.json()["error"]["code"] == "work_item_detail_too_large"


def test_private_work_item_page_overflow_preserves_stable_413() -> None:
    fixture = _fixture()
    fixture.source.page_too_large = True

    async def exercise():
        transport = httpx.ASGITransport(app=fixture.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://frame-bridge.invalid"
        ) as client:
            target = (
                f"{BASE}/instances/dev%2Fdemo/hives/github%2Fbeadhive%2Fbeadhive/"
                "work-items?limit=200&view=ready"
            )
            return await client.get(
                target,
                headers=_headers(fixture, target=target, scope="upstream:read", jti=b"p" * 16),
            )

    response = asyncio.run(exercise())
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "work_items_page_too_large"


def test_private_authority_failures_are_fail_closed_before_daemon_access() -> None:
    fixture = _fixture()
    target = f"{BASE}/instances/dev%2Fdemo/hives?limit=50"

    async def exercise():
        transport = httpx.ASGITransport(app=fixture.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://frame-bridge.invalid"
        ) as client:
            missing = await client.get(target)
            forwarded = await client.get(
                target,
                headers={
                    **_headers(fixture, target=target, scope="upstream:read", jti=b"5" * 16),
                    "Forwarded": "for=browser.invalid",
                },
            )
            wrong_scope = await client.get(
                target,
                headers=_headers(fixture, target=target, scope="upstream:refresh", jti=b"6" * 16),
            )
            valid_headers = _headers(fixture, target=target, scope="upstream:read", jti=b"7" * 16)
            accepted = await client.get(target, headers=valid_headers)
            replayed = await client.get(target, headers=valid_headers)
            wrong_method = await client.put(target)
            return missing, forwarded, wrong_scope, accepted, replayed, wrong_method

    missing, forwarded, wrong_scope, accepted, replayed, wrong_method = asyncio.run(exercise())

    assert (missing.status_code, missing.json()["error"]["code"]) == (
        401,
        "upstream_authentication_failed",
    )
    assert (forwarded.status_code, forwarded.json()["error"]["code"]) == (400, "invalid_request")
    assert (wrong_scope.status_code, wrong_scope.json()["error"]["code"]) == (
        403,
        "upstream_authorization_failed",
    )
    assert accepted.status_code == 200
    assert (replayed.status_code, replayed.json()["error"]["code"]) == (
        401,
        "upstream_authentication_failed",
    )
    assert (wrong_method.status_code, wrong_method.json()["error"]["code"]) == (
        400,
        "invalid_request",
    )
    assert fixture.source.calls == ["directory:50:None"]


def test_private_refresh_is_revision_guarded_and_idempotency_bound() -> None:
    fixture = _fixture()
    target = f"{BASE}/instances/dev%2Fdemo/hives/github%2Fbeadhive%2Fbeadhive/refresh"
    body = {"schemaVersion": 1, "expectedRevision": REVISION, "correlationId": "correlation-1"}

    async def exercise():
        transport = httpx.ASGITransport(app=fixture.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://frame-bridge.invalid"
        ) as client:
            headers = {
                **_headers(
                    fixture, target=target, scope="upstream:refresh", method="POST", jti=b"8" * 16
                ),
                "Content-Type": "application/json",
                "Idempotency-Key": "correlation-1",
            }
            first = await client.post(target, headers=headers, json=body)
            replay = await client.post(
                target,
                headers={
                    **_headers(
                        fixture,
                        target=target,
                        scope="upstream:refresh",
                        method="POST",
                        jti=b"9" * 16,
                    ),
                    "Content-Type": "application/json",
                    "Idempotency-Key": "correlation-1",
                },
                json=body,
            )
            conflict_body = {**body, "expectedRevision": "sha256:" + "b" * 64}
            conflict = await client.post(
                target,
                headers={
                    **_headers(
                        fixture,
                        target=target,
                        scope="upstream:refresh",
                        method="POST",
                        jti=b"a" * 16,
                    ),
                    "Content-Type": "application/json",
                    "Idempotency-Key": "correlation-1",
                },
                json=conflict_body,
            )
            stale_body = {
                **body,
                "correlationId": "correlation-2",
                "expectedRevision": "sha256:" + "c" * 64,
            }
            stale = await client.post(
                target,
                headers={
                    **_headers(
                        fixture,
                        target=target,
                        scope="upstream:refresh",
                        method="POST",
                        jti=b"b" * 16,
                    ),
                    "Content-Type": "application/json",
                    "Idempotency-Key": "correlation-2",
                },
                json=stale_body,
            )
            return first, replay, conflict, stale

    first, replay, conflict, stale = asyncio.run(exercise())

    assert first.status_code == replay.status_code == 200
    assert first.json()["snapshot"]["revision"] == REVISION
    assert fixture.source.refreshes == 2
    assert (conflict.status_code, conflict.json()["error"]["code"]) == (409, "idempotency_conflict")
    assert (stale.status_code, stale.json()["error"]["code"]) == (409, "resnapshot_required")


def test_private_events_and_liveness_never_need_browser_authority() -> None:
    fixture = _fixture()
    events_target = (
        f"{BASE}/instances/dev%2Fdemo/hives/github%2Fbeadhive%2Fbeadhive/events"
        "?subscription=source-subscription"
    )

    async def exercise():
        transport = httpx.ASGITransport(app=fixture.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://frame-bridge.invalid"
        ) as client:
            live = await client.get(f"{BASE}/livez")
            events = await client.get(
                events_target,
                headers=_headers(
                    fixture, target=events_target, scope="upstream:events", jti=b"c" * 16
                ),
            )
            browser_live = await client.get(
                f"{BASE}/livez", headers={"Origin": "https://app-dev.beadhive.cloud"}
            )
            return live, events, browser_live

    live, events, browser_live = asyncio.run(exercise())

    assert live.json() == {"status": "live"}
    assert events.headers["content-type"].startswith("text/event-stream")
    assert "operator-event" in events.text
    assert fixture.source.calls == ["events:source-subscription:None"]
    assert (browser_live.status_code, browser_live.json()["error"]["code"]) == (
        400,
        "invalid_request",
    )


def test_host_daemon_adapter_is_loopback_only_and_rejects_malformed_snapshots() -> None:
    valid_snapshot = {
        "schemaVersion": 1,
        "hive": {
            "prefix": "github/beadhive/beadhive",
            "provider": "github",
            "org": "beadhive",
            "repo": "beadhive",
            "kind": "org-native",
        },
        "revision": REVISION,
        "generatedAt": 1_787_000_000_000,
        "cursor": {
            "subscriptionId": "source-subscription",
            "producerEpoch": EPOCH,
            "sequence": 1,
            "observedAt": 1_787_000_000_000,
        },
        "projectionPolicy": "beadhive.snapshot-summary/v1",
        "limits": {"maxBytes": 917_504, "maxWorkItems": 4_096},
        "coverage": {
            "state": "complete",
            "generatedAt": 1_787_000_000_000,
            "eligible": 0,
            "returned": 0,
            "reason": None,
            "policy": "beadhive.snapshot-summary/v1",
            "sourceRevision": REVISION,
            "limits": {"maxBytes": 917_504, "maxWorkItems": 4_096},
            "workItemRetrieval": _retrieval(REVISION),
            "sources": {},
        },
        "workItems": [],
    }
    requests: list[httpx.Request] = []
    responses = [
        httpx.Response(200, json=valid_snapshot),
        httpx.Response(200, json={"schemaVersion": 1}),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return responses.pop(0)

    async def exercise() -> dict[str, object]:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://127.0.0.1:8420"
        ) as client:
            source = upstream.HostDaemonFrameBridgeSource(
                daemon_bearer=daemon_auth.SecretBearer("bh1.frame-bridge." + "d" * 43),
                instance=upstream.RegisteredInstance(),
                client=client,
            )
            snapshot = await source.snapshot()
            with pytest.raises(upstream.SourceUnavailable):
                await source.snapshot()
            return dict(snapshot)

    snapshot = asyncio.run(exercise())

    assert snapshot == valid_snapshot
    assert [request.url.raw_path.decode("ascii") for request in requests] == [
        "/api/v1/hives/github%2Fbeadhive%2Fbeadhive/snapshot-with-work-items",
        "/api/v1/hives/github%2Fbeadhive%2Fbeadhive/snapshot-with-work-items",
    ]
    assert all(
        request.headers["authorization"] == "Bearer bh1.frame-bridge." + "d" * 43
        for request in requests
    )


def test_host_daemon_adapter_preserves_queue_overflow_as_stable_413_signal() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            413,
            json={
                "schemaVersion": 1,
                "error": {
                    "code": "work_items_page_too_large",
                    "message": "The work-items page exceeds its disclosure limit.",
                    "retryable": False,
                },
            },
        )

    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:8420"
        ) as client:
            source = upstream.HostDaemonFrameBridgeSource(
                daemon_bearer=daemon_auth.SecretBearer("bh1.frame-bridge." + "d" * 43),
                instance=upstream.RegisteredInstance(),
                client=client,
            )
            with pytest.raises(upstream.WorkItemPageTooLarge):
                await source.work_items(
                    view="ready",
                    limit=200,
                    cursor=None,
                    priorities=(),
                    labels=(),
                    assignee=None,
                    issue_type=None,
                    parent=None,
                )

    asyncio.run(exercise())


def test_invalid_verifier_reload_retains_active_set_but_marks_readiness_unready() -> None:
    fixture = _fixture()
    store = fixture.verifier_store

    active = store.active
    assert store.reload(lambda: (_ for _ in ()).throw(upstream.VerifierConfigError("bad"))) is False
    assert store.active is active
    assert store.ready is False


def test_verifier_configuration_rejects_unknown_fields_and_unsafe_file_modes(tmp_path) -> None:
    path = tmp_path / "gateway-verifiers.json"
    path.write_text("{}", encoding="utf-8")
    path.chmod(0o600)

    with pytest.raises(upstream.VerifierConfigError):
        upstream.load_gateway_verifier_set(path, require_root=False)


def test_pinned_aggregate_gateway_reads_over_factory_unix_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture()
    registration_target = f"{BASE}/registration"
    directory_target = f"{BASE}/instances/dev%2Fdemo/hives?limit=50"
    snapshot_target = f"{BASE}/instances/dev%2Fdemo/hives/github%2Fbeadhive%2Fbeadhive/snapshot"

    async def exercise():
        async with _private_unix_client(
            fixture.app, tmp_path / "frame-bridge" / "factory.sock", monkeypatch
        ) as client:
            registration = await client.get(
                registration_target,
                headers=_headers(
                    fixture,
                    target=registration_target,
                    scope="upstream:registration",
                    jti=b"d" * 16,
                ),
            )
            ready_target = f"{BASE}/readyz"
            ready = await client.get(
                ready_target,
                headers=_headers(
                    fixture,
                    target=ready_target,
                    scope="upstream:health",
                    jti=b"e" * 16,
                ),
            )
            directory = await client.get(
                directory_target,
                headers=_headers(
                    fixture,
                    target=directory_target,
                    scope="upstream:read",
                    jti=b"f" * 16,
                ),
            )
            snapshot = await client.get(
                snapshot_target,
                headers=_headers(
                    fixture,
                    target=snapshot_target,
                    scope="upstream:read",
                    jti=b"g" * 16,
                ),
            )
            return registration, ready, directory, snapshot

    registration, ready, directory, snapshot = asyncio.run(exercise())

    assert registration.status_code == 200
    registration_body = registration.json()
    assert _pinned_gateway_registration_admission(registration_body) is None
    assert _pinned_gateway_registration_admission(
        {**registration_body, "hostId": "replacement"}
    ) == (409, "registration_mismatch")
    assert registration_body["schemaVersion"] == 1
    assert registration_body["contractVersion"] == upstream.UPSTREAM_CONTRACT
    assert registration_body["hostId"] == "factory"
    assert registration_body["hostEpoch"] == EPOCH
    assert registration_body["instances"] == [upstream.RegisteredInstance().to_wire()]
    assert ready.status_code == 200
    expected_registration_digest = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(registration_body, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    assert ready.json()["registrationDigest"] == expected_registration_digest
    assert directory.status_code == 200
    assert directory.json()["items"][0]["hiveId"] == "github/beadhive/beadhive"
    assert snapshot.status_code == 200
    assert snapshot.json()["hive"]["prefix"] == "github/beadhive/beadhive"
    assert fixture.source.calls == ["online", "directory:50:None", "snapshot"]


def test_pinned_aggregate_gateway_rejects_bad_authority_and_source_failures() -> None:
    fixture = _fixture()
    target = f"{BASE}/instances/dev%2Fdemo/hives?limit=50"
    malformed_target = f"{BASE}/instances/dev%2Fdemo/hives?limit=050"
    ready_target = f"{BASE}/readyz"

    async def exercise():
        transport = httpx.ASGITransport(app=fixture.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://frame-bridge.invalid"
        ) as client:
            invalid_attestation = await client.get(
                target,
                headers={
                    "Authorization": "Bearer malformed",
                    "X-Beadhive-Upstream-Contract": upstream.UPSTREAM_CONTRACT,
                    "X-Beadhive-Request-Id": REQUEST_ID,
                    "X-Beadhive-Deadline-Ms": "1000",
                },
            )
            wrong_host = await client.get(
                target,
                headers=_headers(
                    fixture,
                    target=target,
                    scope="upstream:read",
                    jti=b"h" * 16,
                    host_id="replacement",
                ),
            )
            wrong_instance = await client.get(
                target,
                headers=_headers(
                    fixture,
                    target=target,
                    scope="upstream:read",
                    jti=b"i" * 16,
                    instance_id="dev/replacement",
                ),
            )
            clerk_header = await client.get(
                target,
                headers={
                    **_headers(fixture, target=target, scope="upstream:read", jti=b"j" * 16),
                    "X-Beadhive-Clerk-Token": "never-forward-this",
                },
            )
            malformed = await client.get(
                malformed_target,
                headers=_headers(
                    fixture,
                    target=malformed_target,
                    scope="upstream:read",
                    jti=b"k" * 16,
                ),
            )
            fixture.source.online_value = False
            unavailable = await client.get(
                ready_target,
                headers=_headers(
                    fixture,
                    target=ready_target,
                    scope="upstream:health",
                    jti=b"l" * 16,
                ),
            )
            return (
                invalid_attestation,
                wrong_host,
                wrong_instance,
                clerk_header,
                malformed,
                unavailable,
            )

    invalid_attestation, wrong_host, wrong_instance, clerk_header, malformed, unavailable = (
        asyncio.run(exercise())
    )

    assert (invalid_attestation.status_code, invalid_attestation.json()["error"]["code"]) == (
        401,
        "upstream_authentication_failed",
    )
    for response in (wrong_host, wrong_instance):
        assert (response.status_code, response.json()["error"]["code"]) == (
            403,
            "upstream_authorization_failed",
        )
    assert (clerk_header.status_code, clerk_header.json()["error"]["code"]) == (
        400,
        "invalid_request",
    )
    assert (malformed.status_code, malformed.json()["error"]["code"]) == (400, "invalid_request")
    assert (unavailable.status_code, unavailable.json()["error"]["code"]) == (
        503,
        "source_unavailable",
    )
    assert fixture.source.calls == ["online"]


def test_gateway_and_daemon_authority_do_not_cross_the_private_seam() -> None:
    fixture = _fixture()
    daemon_bearer = "bh1.frame-bridge." + "d" * 43
    daemon_authorizations: list[str | None] = []
    daemon_snapshot = {
        "schemaVersion": 1,
        "hive": {
            "prefix": "github/beadhive/beadhive",
            "provider": "github",
            "org": "beadhive",
            "repo": "beadhive",
            "kind": "org-native",
        },
        "revision": REVISION,
        "generatedAt": 1_787_000_000_000,
        "cursor": {
            "subscriptionId": "source-subscription",
            "producerEpoch": EPOCH,
            "sequence": 1,
            "observedAt": 1_787_000_000_000,
        },
        "projectionPolicy": "beadhive.snapshot-summary/v1",
        "limits": {"maxBytes": 917_504, "maxWorkItems": 4_096},
        "coverage": {
            "state": "complete",
            "generatedAt": 1_787_000_000_000,
            "eligible": 0,
            "returned": 0,
            "reason": None,
            "policy": "beadhive.snapshot-summary/v1",
            "sourceRevision": REVISION,
            "limits": {"maxBytes": 917_504, "maxWorkItems": 4_096},
            "workItemRetrieval": _retrieval(REVISION),
            "sources": {},
        },
        "workItems": [],
    }

    async def daemon_snapshot_handler(request):
        daemon_authorizations.append(request.headers.get("authorization"))
        return JSONResponse(daemon_snapshot)

    daemon_app = Starlette(
        routes=[
            Route("/api/v1/hives/{hive:path}/snapshot-with-work-items", daemon_snapshot_handler)
        ]
    )
    daemon_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=daemon_app), base_url="http://127.0.0.1:8420"
    )
    source = upstream.HostDaemonFrameBridgeSource(
        daemon_bearer=daemon_auth.SecretBearer(daemon_bearer),
        instance=upstream.RegisteredInstance(),
        client=daemon_client,
    )
    fixture.app = upstream.build_private_frame_bridge_application(
        config=upstream.PrivateFrameBridgeConfig(
            verifier_store=fixture.verifier_store,
            host_epoch=EPOCH,
        ),
        source=source,
        now=lambda: NOW,
    )
    target = f"{BASE}/instances/dev%2Fdemo/hives/github%2Fbeadhive%2Fbeadhive/snapshot"

    async def exercise():
        try:
            transport = httpx.ASGITransport(app=fixture.app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://frame-bridge.invalid"
            ) as client:
                rejected = await client.get(
                    target,
                    headers={
                        **_headers(fixture, target=target, scope="upstream:read", jti=b"m" * 16),
                        "X-Beadhive-Clerk-Token": "never-forward-this",
                    },
                )
                headers = _headers(fixture, target=target, scope="upstream:read", jti=b"n" * 16)
                accepted = await client.get(target, headers=headers)
                return rejected, accepted, headers["Authorization"]
        finally:
            await source.close()

    rejected, accepted, gateway_attestation = asyncio.run(exercise())

    assert (rejected.status_code, rejected.json()["error"]["code"]) == (400, "invalid_request")
    assert accepted.status_code == 200
    assert daemon_authorizations == [f"Bearer {daemon_bearer}"]
    assert gateway_attestation not in daemon_authorizations[0]
    assert daemon_bearer not in accepted.text
    assert gateway_attestation not in accepted.text


def test_pinned_gateway_case_matrix_is_immutable_and_covered() -> None:
    fixture = (
        Path(__file__).parent
        / "fixtures"
        / "frame_bridge_gateway_e482"
        / "frame-bridge-upstream-v1-cases.json"
    )
    raw = fixture.read_bytes()
    matrix = json.loads(raw)
    proof = json.loads(
        (Path(__file__).parents[1] / "docs/proof/bh-bh6w3.3-gateway-conformance.json").read_text(
            encoding="utf-8"
        )
    )

    assert hashlib.sha256(raw).hexdigest() == GATEWAY_CONTRACT_CASES_SHA256
    assert matrix["schemaVersion"] == 1
    assert matrix["contractVersion"] == upstream.UPSTREAM_CONTRACT
    assert matrix["transport"] == {
        "protocol": "http1-unix",
        "socketRoot": "/run/beadhive/frame-bridge",
        "tcpListener": "forbidden",
        "publicExposure": "forbidden",
    }
    assert matrix["identity"]["forwardClerkToken"] is False
    assert {item["id"]: (item["method"], item["scope"]) for item in matrix["operations"]} == {
        "liveness": ("GET", None),
        "readiness": ("GET", "upstream:health"),
        "registration": ("GET", "upstream:registration"),
        "directory": ("GET", "upstream:read"),
        "snapshot": ("GET", "upstream:read"),
        "events": ("GET", "upstream:events"),
        "refresh": ("POST", "upstream:refresh"),
    }
    registration = next(item for item in matrix["operations"] if item["id"] == "registration")
    assert registration["errorResponses"]["409"] == ["registration_mismatch"]
    upstream_test = "tests/test_frame_bridge_upstream.py::"
    operation_coverage = {
        "liveness": upstream_test + "test_private_events_and_liveness_never_need_browser_authority",
        "readiness": upstream_test + "test_pinned_aggregate_gateway_reads_over_factory_unix_socket",
        "registration": upstream_test
        + "test_pinned_aggregate_gateway_reads_over_factory_unix_socket",
        "directory": upstream_test + "test_pinned_aggregate_gateway_reads_over_factory_unix_socket",
        "snapshot": upstream_test + "test_pinned_aggregate_gateway_reads_over_factory_unix_socket",
        "events": upstream_test + "test_private_events_and_liveness_never_need_browser_authority",
        "refresh": upstream_test + "test_private_refresh_is_revision_guarded_and_idempotency_bound",
    }
    assert set(operation_coverage) == {item["id"] for item in matrix["operations"]}

    case_ids = {item["id"] for item in matrix["negativeCases"]}
    factory_socket_test = (
        "tests/test_frame_bridge_factory.py::"
        "test_factory_systemd_profile_keeps_socket_private_and_has_no_tcp_listener"
    )
    rejection_test = (
        "tests/test_frame_bridge_upstream.py::"
        "test_pinned_aggregate_gateway_rejects_bad_authority_and_source_failures"
    )
    authority_test = (
        upstream_test + "test_private_authority_failures_are_fail_closed_before_daemon_access"
    )
    operations_test = (
        upstream_test
        + "test_private_registration_readiness_and_source_operations_are_request_bound"
    )
    registration_test = (
        upstream_test + "test_pinned_aggregate_gateway_reads_over_factory_unix_socket"
    )
    events_test = upstream_test + "test_private_events_and_liveness_never_need_browser_authority"
    refresh_test = upstream_test + "test_private_refresh_is_revision_guarded_and_idempotency_bound"
    source_test = (
        upstream_test + "test_host_daemon_adapter_is_loopback_only_and_rejects_malformed_snapshots"
    )
    verifier_test = (
        upstream_test + "test_verifier_configuration_rejects_unknown_fields_and_unsafe_file_modes"
    )
    reload_test = (
        upstream_test
        + "test_invalid_verifier_reload_retains_active_set_but_marks_readiness_unready"
    )
    # A complete test-reference index; execution status is classified below.
    case_test_index = {
        "client-socket-selector": factory_socket_test,
        "client-host-selector": authority_test,
        "duplicate-instance-route": authority_test,
        "socket-path-symlink": factory_socket_test,
        "direct-tcp-access": factory_socket_test,
        "forwarded-header": authority_test,
        "spoofed-private-identity-header": rejection_test,
        "forwarded-clerk-token": upstream_test
        + "test_gateway_and_daemon_authority_do_not_cross_the_private_seam",
        "missing-attestation": rejection_test,
        "expired-attestation": authority_test,
        "replayed-attestation": authority_test,
        "wrong-key": rejection_test,
        "signer-config-wrong-mode": factory_socket_test,
        "jku-header": rejection_test,
        "unknown-kid": rejection_test,
        "revoked-kid": verifier_test,
        "verifier-config-symlink": verifier_test,
        "verifier-config-wrong-mode": verifier_test,
        "verifier-invalid-reload": reload_test,
        "verifier-overlap-rotation": verifier_test,
        "verifier-emergency-revocation": verifier_test,
        "verifier-digest-mismatch": verifier_test,
        "wrong-issuer": authority_test,
        "wrong-audience": authority_test,
        "wrong-scope": authority_test,
        "wrong-host": rejection_test,
        "wrong-instance": rejection_test,
        "wrong-factory": authority_test,
        "wrong-method": authority_test,
        "wrong-target-hash": authority_test,
        "registration-drift": registration_test,
        "daemon-unavailable": rejection_test,
        "deadline-expired": rejection_test,
        "admission-exhausted": rejection_test,
        "malformed-json": source_test,
        "oversized-response": source_test,
        "wrong-contract-version": source_test,
        "mixed-host-identity": source_test,
        "mixed-hive-identity": source_test,
        "stale-cursor": refresh_test,
        "refresh-idempotency-conflict": refresh_test,
        "partial-response-reset": source_test,
        "event-disconnect": events_test,
        "refresh-disconnect": refresh_test,
        "public-cancellation": events_test,
        "generated-fallback-on-live-failure": operations_test,
        "empty-directory-on-source-failure": operations_test,
        "frame-bridge-as-tunnel-origin": factory_socket_test,
    }
    local_executed = {
        case_id: case_test_index[case_id]
        for case_id in {
            "direct-tcp-access",
            "forwarded-header",
            "spoofed-private-identity-header",
            "forwarded-clerk-token",
            "missing-attestation",
            "replayed-attestation",
            "verifier-config-wrong-mode",
            "verifier-invalid-reload",
            "wrong-scope",
            "wrong-host",
            "wrong-instance",
            "wrong-method",
            "registration-drift",
            "daemon-unavailable",
            "malformed-json",
            "refresh-idempotency-conflict",
            "frame-bridge-as-tunnel-origin",
        }
    }
    gateway_owned_unexecuted = {
        "client-socket-selector",
        "client-host-selector",
        "duplicate-instance-route",
        "socket-path-symlink",
        "expired-attestation",
        "wrong-key",
        "signer-config-wrong-mode",
        "jku-header",
        "unknown-kid",
        "revoked-kid",
        "verifier-config-symlink",
        "verifier-overlap-rotation",
        "verifier-emergency-revocation",
        "verifier-digest-mismatch",
        "wrong-issuer",
        "wrong-audience",
        "wrong-factory",
        "wrong-target-hash",
        "deadline-expired",
        "admission-exhausted",
        "oversized-response",
        "wrong-contract-version",
        "mixed-host-identity",
        "mixed-hive-identity",
        "stale-cursor",
        "partial-response-reset",
        "event-disconnect",
        "refresh-disconnect",
        "public-cancellation",
        "generated-fallback-on-live-failure",
        "empty-directory-on-source-failure",
    }
    assert set(local_executed).isdisjoint(gateway_owned_unexecuted)
    assert set(local_executed) | gateway_owned_unexecuted == case_ids
    assert all(test.startswith("tests/") for test in local_executed.values())

    case_coverage = {
        **{case_id: ("local-executed", test) for case_id, test in local_executed.items()},
        **{
            case_id: ("gateway-owned/unexecuted", str(fixture))
            for case_id in gateway_owned_unexecuted
        },
    }
    assert set(case_coverage) == case_ids
    assert {
        case_id for case_id, (kind, _) in case_coverage.items() if kind == "local-executed"
    } == set(local_executed)
    assert {
        case_id
        for case_id, (kind, evidence) in case_coverage.items()
        if kind == "gateway-owned/unexecuted" and evidence == str(fixture)
    } == gateway_owned_unexecuted

    assert set(case_test_index) == case_ids
    assert all(test.startswith("tests/") for test in operation_coverage.values())
    assert all(test.startswith("tests/") for test in case_test_index.values())
    assert case_test_index["registration-drift"] == registration_test
    assert proof["aggregateGateway"]["commit"] == GATEWAY_REVISION
    assert proof["aggregateGateway"]["contractCasesSha256"] == GATEWAY_CONTRACT_CASES_SHA256
    assert proof["aggregateGateway"]["matrixCoverage"] == {
        "operations": {"localExecuted": len(operation_coverage), "gatewayOwnedUnexecuted": 0},
        "negativeCases": {
            "localExecuted": len(local_executed),
            "gatewayOwnedUnexecuted": len(gateway_owned_unexecuted),
        },
        "registrationDrift": (
            "local pinned e482 admission in "
            "test_pinned_aggregate_gateway_reads_over_factory_unix_socket"
        ),
    }
    assert proof["aggregateGateway"]["gatewayRuntime"] == {
        "status": "unavailable",
        "reason": (
            "the Go executable is absent; Gateway-owned cases are verified only by the immutable "
            "e482 fixture"
        ),
        "gatewayOwnedCasesClaimedExecuted": False,
    }
    assert "counterpartRegistrationMismatchTest" not in proof["aggregateGateway"]
    assert "counterpartTestExecution" not in proof["aggregateGateway"]
