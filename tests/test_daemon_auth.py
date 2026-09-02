"""Attack, lifecycle, middleware, and redaction tests for daemon bearer authority."""

from __future__ import annotations

import asyncio
import json
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from beadhive import daemon_auth, host_daemon
from beadhive.daemon_contract import AuthScope
from harness import processes

AUDIENCE = "beadhive-host"
PRINCIPAL = "operator:alice"


def _process_add_credential(path: str, credential_id: str, start, results) -> None:
    """Spawn-safe process target for the cross-process lost-update regression."""

    start.wait()
    try:
        credential = daemon_auth.add_credential(
            Path(path),
            credential_id=credential_id,
            audience=AUDIENCE,
            principal=f"service:{credential_id}",
            scopes=(AuthScope.ACTIVITY_PUBLISH,),
            expires_at=int(time.time()) + 3_600,
        )
    except Exception as exc:
        results.put(("error", type(exc).__name__, str(exc)))
    else:
        results.put(("ok", credential_id, credential.bearer.reveal_for_authority()))


def _provision(
    tmp_path: Path,
    *,
    scopes: tuple[AuthScope, ...] = (AuthScope.OPERATOR_READ,),
    expires_at: int | None = None,
) -> tuple[Path, daemon_auth.ProvisionedCredential]:
    path = (tmp_path / "private" / "daemon-credentials.json").absolute()
    credential = daemon_auth.provision_credential_file(
        path,
        credential_id="operator-alice",
        audience=AUDIENCE,
        principal=PRINCIPAL,
        scopes=scopes,
        expires_at=expires_at or int(time.time()) + 3_600,
    )
    return path, credential


def _authority(
    path: Path, *, clock=lambda: time.time(), audience: str = AUDIENCE, interval: float = 30
) -> daemon_auth.CredentialAuthority:
    return daemon_auth.CredentialAuthority(
        path,
        audience=audience,
        session_revalidation_seconds=interval,
        clock=clock,
    )


def _bearer(credential: daemon_auth.ProvisionedCredential) -> str:
    return credential.bearer.reveal_for_authority()


def _failure(
    authority: daemon_auth.CredentialAuthority,
    token: str | daemon_auth.SecretBearer | None,
    **kwargs,
) -> daemon_auth.AuthenticationError:
    with pytest.raises(daemon_auth.AuthenticationError) as caught:
        authority.authenticate(token, **kwargs)
    return caught.value


def test_safe_provisioning_persists_only_a_private_verifier_and_redacts_output(tmp_path: Path):
    path, credential = _provision(
        tmp_path,
        scopes=(
            AuthScope.MCP_CONTROL,
            AuthScope.OPERATOR_READ,
            AuthScope.ACTIVITY_PUBLISH,
            AuthScope.TERMINAL_ATTACH,
        ),
    )
    token = _bearer(credential)
    persisted = path.read_text()

    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert path.stat().st_mode & 0o777 == 0o600
    assert token not in persisted
    assert token not in repr(credential)
    assert token not in str(credential.bearer)
    assert "secretDigest" in persisted
    assert json.loads(persisted)["schemaVersion"] == 1

    principal = _authority(path).authenticate(credential.bearer)
    assert principal.principal == PRINCIPAL
    assert principal.scopes == frozenset(AuthScope)


def test_safe_provisioning_refuses_to_overwrite_an_existing_authority(tmp_path: Path):
    path, original = _provision(tmp_path)
    with pytest.raises(daemon_auth.CredentialFileError, match="already exists"):
        daemon_auth.provision_credential_file(
            path,
            credential_id="replacement",
            audience=AUDIENCE,
            principal="operator:bob",
            scopes=(AuthScope.OPERATOR_READ,),
            expires_at=int(time.time()) + 3_600,
        )
    assert _authority(path).authenticate(original.bearer).credential_id == "operator-alice"


@pytest.mark.parametrize(
    "candidate",
    [
        "",
        "Bearer",
        "bh1",
        "bh1.id.short",
        "bh2.id.AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "bh1.bad.id.AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "bh1.id.AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA+",
    ],
)
def test_malformed_tokens_fail_with_one_redacted_code(tmp_path: Path, candidate: str):
    path, _credential = _provision(tmp_path)
    error = _failure(_authority(path), candidate)
    assert error.code is daemon_auth.AuthFailureCode.MALFORMED
    if candidate:
        assert candidate not in str(error)


def test_missing_unknown_expired_wrong_audience_principal_and_scope_are_deterministic(
    tmp_path: Path,
):
    path, credential = _provision(tmp_path, expires_at=2_000)
    authority = _authority(path, clock=lambda: 1_000)
    token = _bearer(credential)
    prefix, _credential_id, secret = token.split(".")
    unknown = f"{prefix}.unknown-id.{secret}"

    assert _failure(authority, None).code is daemon_auth.AuthFailureCode.MISSING
    assert _failure(authority, unknown).code is daemon_auth.AuthFailureCode.UNKNOWN
    assert (
        _failure(_authority(path, clock=lambda: 2_000), token).code
        is daemon_auth.AuthFailureCode.EXPIRED
    )
    assert (
        _failure(_authority(path, clock=lambda: 1_000, audience="another-service"), token).code
        is daemon_auth.AuthFailureCode.WRONG_AUDIENCE
    )
    assert (
        _failure(authority, token, expected_principal="operator:bob").code
        is daemon_auth.AuthFailureCode.WRONG_PRINCIPAL
    )
    scope_error = _failure(authority, token, required_scope=AuthScope.MCP_CONTROL)
    assert scope_error.code is daemon_auth.AuthFailureCode.WRONG_SCOPE
    assert scope_error.status_code == 403


def test_every_scope_is_independent_and_never_implies_another(tmp_path: Path):
    for allowed in AuthScope:
        directory = tmp_path / allowed.value.replace(":", "-")
        path, credential = _provision(directory, scopes=(allowed,))
        authority = _authority(path)
        assert authority.authenticate(credential.bearer, required_scope=allowed).permits(allowed)
        for denied in set(AuthScope) - {allowed}:
            assert (
                _failure(authority, credential.bearer, required_scope=denied).code
                is daemon_auth.AuthFailureCode.WRONG_SCOPE
            )


def test_secret_verification_uses_constant_time_comparison(tmp_path: Path, monkeypatch):
    path, credential = _provision(tmp_path)
    observed: list[tuple[str, str]] = []
    real_compare = daemon_auth.hmac.compare_digest

    def observe(left: str, right: str) -> bool:
        observed.append((left, right))
        return real_compare(left, right)

    monkeypatch.setattr(daemon_auth.hmac, "compare_digest", observe)
    _authority(path).authenticate(credential.bearer, expected_principal=PRINCIPAL)

    assert observed
    assert observed[0][0].startswith("sha256:")
    assert observed[0][1].startswith("sha256:")
    assert _bearer(credential) not in repr(observed)


def test_rotation_and_revocation_affect_the_very_next_request(tmp_path: Path):
    path, original = _provision(tmp_path)
    authority = _authority(path)
    assert authority.authenticate(original.bearer).generation == 1

    rotated = daemon_auth.rotate_credential(path, "operator-alice")
    assert _failure(authority, original.bearer).code is daemon_auth.AuthFailureCode.ROTATED
    assert authority.authenticate(rotated.bearer).generation == 2

    daemon_auth.revoke_credential(path, "operator-alice")
    assert _failure(authority, rotated.bearer).code is daemon_auth.AuthFailureCode.REVOKED


def test_multiple_principals_are_added_with_independent_scope_sets(tmp_path: Path):
    path, operator = _provision(tmp_path)
    publisher = daemon_auth.add_credential(
        path,
        credential_id="publisher-baml",
        audience=AUDIENCE,
        principal="service:baml",
        scopes=(AuthScope.ACTIVITY_PUBLISH,),
        expires_at=int(time.time()) + 3_600,
    )
    authority = _authority(path)

    assert authority.authenticate(operator.bearer).generation == 2
    assert (
        authority.authenticate(
            publisher.bearer, required_scope=AuthScope.ACTIVITY_PUBLISH
        ).principal
        == "service:baml"
    )
    assert (
        _failure(authority, publisher.bearer, required_scope=AuthScope.OPERATOR_READ).code
        is daemon_auth.AuthFailureCode.WRONG_SCOPE
    )


def test_threaded_add_rotate_and_revoke_transactions_never_lose_a_success(tmp_path: Path):
    path, original = _provision(tmp_path)
    revoked = daemon_auth.add_credential(
        path,
        credential_id="revoked",
        audience=AUDIENCE,
        principal="service:revoked",
        scopes=(AuthScope.ACTIVITY_PUBLISH,),
        expires_at=int(time.time()) + 3_600,
    )
    barrier = threading.Barrier(4)

    def add(credential_id: str):
        barrier.wait()
        return daemon_auth.add_credential(
            path,
            credential_id=credential_id,
            audience=AUDIENCE,
            principal=f"service:{credential_id}",
            scopes=(AuthScope.ACTIVITY_PUBLISH,),
            expires_at=int(time.time()) + 3_600,
        )

    def rotate():
        barrier.wait()
        return daemon_auth.rotate_credential(path, original.credential_id)

    def revoke():
        barrier.wait()
        daemon_auth.revoke_credential(path, revoked.credential_id)

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(add, "thread-a"), pool.submit(add, "thread-b")]
        rotated_future = pool.submit(rotate)
        revoked_future = pool.submit(revoke)
        added = [future.result(timeout=5) for future in futures]
        rotated = rotated_future.result(timeout=5)
        revoked_future.result(timeout=5)

    authority = _authority(path)
    for credential in (*added, rotated):
        authority.authenticate(credential.bearer)
    assert _failure(authority, revoked.bearer).code is daemon_auth.AuthFailureCode.REVOKED
    final = daemon_auth.load_credential_file(path)
    assert final.generation == 6
    assert {record.credential_id for record in final.credentials} == {
        "operator-alice",
        "revoked",
        "thread-a",
        "thread-b",
    }


def test_process_concurrent_adds_preserve_every_returned_bearer_and_generation(tmp_path: Path):
    path, original = _provision(tmp_path)
    context = processes.process_context()
    start = context.Event()
    results = context.Queue()
    workers = [
        context.Process(
            target=_process_add_credential,
            args=(str(path), f"process-{index}", start, results),
        )
        for index in range(4)
    ]
    for process in workers:
        process.start()
    start.set()
    returned = [results.get(timeout=10) for _process in workers]
    for process in workers:
        process.join(timeout=10)
        assert process.exitcode == 0

    assert all(result[0] == "ok" for result in returned), returned
    authority = _authority(path)
    authority.authenticate(original.bearer)
    for _status, credential_id, token in returned:
        assert authority.authenticate(token).credential_id == credential_id
    final = daemon_auth.load_credential_file(path)
    assert final.generation == 5
    assert len(final.credentials) == 5


def test_insecure_malformed_and_duplicate_credential_files_fail_closed(tmp_path: Path):
    path, _credential = _provision(tmp_path)
    path.chmod(0o640)
    with pytest.raises(daemon_auth.CredentialFileError, match="0600"):
        daemon_auth.load_credential_file(path)

    path.chmod(0o600)
    document = json.loads(path.read_text())
    document["credentials"].append(document["credentials"][0])
    path.write_text(json.dumps(document))
    path.chmod(0o600)
    with pytest.raises(daemon_auth.CredentialFileError, match="schema"):
        daemon_auth.load_credential_file(path)

    with pytest.raises(ValidationError):
        daemon_auth.CredentialRecord(
            credential_id="id",
            secret_digest="not-a-verifier",
            audience=AUDIENCE,
            principal=PRINCIPAL,
            scopes=(AuthScope.OPERATOR_READ,),
            expires_at=1,
        )

    target, _credential = _provision(tmp_path / "symlink-target")
    link = (tmp_path / "credential-link.json").absolute()
    link.symlink_to(target)
    with pytest.raises(daemon_auth.CredentialFileError, match="unavailable"):
        daemon_auth.load_credential_file(link)


def _test_app(authority: daemon_auth.CredentialAuthority) -> Starlette:
    async def health(_request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    async def factory(request: Request) -> JSONResponse:
        principal = request.scope["state"]["auth_principal"]
        return JSONResponse({"principal": principal.principal})

    async def mcp(_request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    return Starlette(
        routes=[
            Route("/health", health),
            Route("/api/v1/factory", factory),
            Route("/mcp", mcp, methods=["POST"]),
        ],
        middleware=[Middleware(daemon_auth.BearerAuthMiddleware, authority=authority)],
    )


def test_middleware_returns_standard_redacted_401_and_403_and_keeps_health_public(tmp_path: Path):
    path, credential = _provision(tmp_path)
    token = _bearer(credential)
    app = _test_app(_authority(path))

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://daemon"
        ) as client:
            assert (await client.get("/health")).status_code == 200
            missing = await client.get("/api/v1/factory")
            assert missing.status_code == 401
            assert missing.headers["www-authenticate"] == 'Bearer realm="beadhive-host"'
            assert missing.json()["error"]["code"] == "unauthorized"
            allowed = await client.get(
                "/api/v1/factory", headers={"Authorization": f"Bearer {token}"}
            )
            assert allowed.json() == {"principal": PRINCIPAL}
            forbidden = await client.post("/mcp", headers={"Authorization": f"Bearer {token}"})
            assert forbidden.status_code == 403
            assert forbidden.json()["error"]["code"] == "forbidden"
            assert token not in missing.text + forbidden.text + repr(missing.headers)

    asyncio.run(exercise())


def test_middleware_rejects_credentials_in_urls_and_duplicate_or_malformed_headers(tmp_path: Path):
    path, credential = _provision(tmp_path)
    token = _bearer(credential)
    app = _test_app(_authority(path))

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://daemon"
        ) as client:
            in_url = await client.get(
                "/api/v1/factory",
                params={"access_token": token},
                headers={"Authorization": f"Bearer {token}"},
            )
            value_under_benign_name = await client.get(
                "/api/v1/factory",
                params={"next": token},
                headers={"Authorization": f"Bearer {token}"},
            )
            token_as_name = await client.get(
                f"/api/v1/factory?{token}=value",
                headers={"Authorization": f"Bearer {token}"},
            )
            encoded = token.replace(".", "%2E")
            encoded_in_path = await client.get(
                f"/api/v1/{encoded}", headers={"Authorization": f"Bearer {token}"}
            )
            double_encoded = token.replace(".", "%252E")
            double_in_query = await client.get(
                f"/api/v1/factory?next={double_encoded}",
                headers={"Authorization": f"Bearer {token}"},
            )
            duplicate_token = await client.get(
                f"/api/v1/factory?next={token}&next={token}",
                headers={"Authorization": f"Bearer {token}"},
            )
            malformed_encoding = await client.get(
                "/api/v1/factory?next=%GG",
                headers={"Authorization": f"Bearer {token}"},
            )
            attach_in_url = await client.get(
                "/api/v1/factory",
                params={"attachToken": token},
                headers={"Authorization": f"Bearer {token}"},
            )
            duplicate = await client.get(
                "/api/v1/factory",
                headers=[
                    ("Authorization", f"Bearer {token}"),
                    ("Authorization", f"Bearer {token}"),
                ],
            )
            malformed = await client.get(
                "/api/v1/factory", headers={"Authorization": f"Basic {token}"}
            )
            assert [
                in_url.status_code,
                attach_in_url.status_code,
                value_under_benign_name.status_code,
                token_as_name.status_code,
                encoded_in_path.status_code,
                double_in_query.status_code,
                duplicate_token.status_code,
                malformed_encoding.status_code,
                duplicate.status_code,
                malformed.status_code,
            ] == [
                401,
                401,
                401,
                401,
                401,
                401,
                401,
                401,
                401,
                401,
            ]
            rendered = "".join(
                response.text
                for response in (
                    in_url,
                    attach_in_url,
                    value_under_benign_name,
                    token_as_name,
                    encoded_in_path,
                    double_in_query,
                    duplicate_token,
                    malformed_encoding,
                    duplicate,
                    malformed,
                )
            )
            assert token not in rendered

    asyncio.run(exercise())


def test_websocket_auth_uses_redacted_4401_and_4403_closes(tmp_path: Path):
    path, credential = _provision(tmp_path)
    app = _test_app(_authority(path))

    async def attempt(headers=()):
        sent = []

        async def receive():
            return {"type": "websocket.connect"}

        async def send(message):
            sent.append(message)

        await app(
            {
                "type": "websocket",
                "asgi": {"version": "3.0"},
                "scheme": "ws",
                "path": "/ws/terminal",
                "raw_path": b"/ws/terminal",
                "query_string": b"",
                "headers": headers,
                "client": ("127.0.0.1", 1),
                "server": ("127.0.0.1", 2),
                "subprotocols": [],
                "state": {},
            },
            receive,
            send,
        )
        return sent

    missing = asyncio.run(attempt())
    wrong_scope = asyncio.run(
        attempt(((b"authorization", f"Bearer {_bearer(credential)}".encode()),))
    )
    assert missing == [{"type": "websocket.close", "code": 4401, "reason": "Unauthorized"}]
    assert wrong_scope == [{"type": "websocket.close", "code": 4403, "reason": "Forbidden"}]


def test_live_session_closes_on_rotation_at_its_next_bounded_revalidation(tmp_path: Path):
    path, credential = _provision(tmp_path)
    authority = _authority(path, interval=0.02)
    closed: list[daemon_auth.AuthFailureCode] = []

    async def exercise():
        registry = daemon_auth.CredentialSessionRegistry(authority)
        async with registry.lifespan():
            session = registry.open(
                credential.bearer,
                required_scope=AuthScope.OPERATOR_READ,
                close=closed.append,
            )
            assert not session.closed
            daemon_auth.rotate_credential(path, "operator-alice")
            deadline = asyncio.get_running_loop().time() + 0.5
            while not closed and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.005)
            assert session.closed
            assert session.close_reason is daemon_auth.AuthFailureCode.ROTATED

    asyncio.run(exercise())
    assert closed == [daemon_auth.AuthFailureCode.ROTATED]


def test_live_session_expiry_and_revocation_close_with_exact_reason(tmp_path: Path):
    path, credential = _provision(tmp_path, expires_at=200)
    now = [100.0]
    authority = _authority(path, clock=lambda: now[0], interval=30)
    closed: list[daemon_auth.AuthFailureCode] = []
    registry = daemon_auth.CredentialSessionRegistry(authority)
    session = registry.open(
        credential.bearer,
        required_scope=AuthScope.OPERATOR_READ,
        close=closed.append,
        now=now[0],
    )

    daemon_auth.revoke_credential(path, "operator-alice")
    now[0] = session.next_revalidation_at

    async def revalidate_and_drain():
        await registry.revalidate_due(now=now[0])
        await registry.drain_close_callbacks()

    asyncio.run(revalidate_and_drain())
    assert closed == [daemon_auth.AuthFailureCode.REVOKED]


def test_hung_and_cancelled_callbacks_are_bounded_isolated_and_recorded(tmp_path: Path):
    path, credential = _provision(tmp_path, expires_at=1_000)
    now = [100.0]
    authority = _authority(path, clock=lambda: now[0], interval=0.05)
    registry = daemon_auth.CredentialSessionRegistry(authority, close_timeout_seconds=0.02)
    completed: list[daemon_auth.AuthFailureCode] = []
    release_hung = threading.Event()

    def hung(_reason):
        release_hung.wait()

    async def cancelled(_reason):
        raise asyncio.CancelledError

    for callback in (completed.append, hung, cancelled):
        registry.open(
            credential.bearer,
            required_scope=AuthScope.OPERATOR_READ,
            close=callback,
            now=now[0],
        )
    daemon_auth.revoke_credential(path, "operator-alice")
    now[0] += 0.05

    async def exercise():
        started = time.monotonic()
        await asyncio.gather(
            registry.revalidate_due(now=now[0]),
            registry.revalidate_due(now=now[0]),
        )
        assert registry.active_session_count == 0
        assert len(registry.closure_records) == 3
        await registry.drain_close_callbacks()
        assert time.monotonic() - started < 0.2

    asyncio.run(exercise())
    release_hung.set()
    assert completed == [daemon_auth.AuthFailureCode.REVOKED]
    assert {record.callback_status for record in registry.closure_records} == {
        daemon_auth.CloseCallbackStatus.COMPLETED,
        daemon_auth.CloseCallbackStatus.CANCELLED,
        daemon_auth.CloseCallbackStatus.TIMED_OUT,
    }
    assert all(
        record.reason is daemon_auth.AuthFailureCode.REVOKED for record in registry.closure_records
    )


def test_a_hung_callback_cannot_delay_a_later_session_invalidation(tmp_path: Path):
    path, credential = _provision(tmp_path, expires_at=1_000)
    now = [100.0]
    authority = _authority(path, clock=lambda: now[0], interval=0.1)
    registry = daemon_auth.CredentialSessionRegistry(authority, close_timeout_seconds=0.05)
    later_closed: list[daemon_auth.AuthFailureCode] = []

    async def hung(_reason):
        await asyncio.Event().wait()

    first = registry.open(
        credential.bearer,
        required_scope=AuthScope.OPERATOR_READ,
        close=hung,
        now=now[0],
    )
    later = registry.open(
        credential.bearer,
        required_scope=AuthScope.OPERATOR_READ,
        close=later_closed.append,
        now=now[0],
    )
    first.next_revalidation_at = 100.01
    later.next_revalidation_at = 100.02
    daemon_auth.revoke_credential(path, "operator-alice")

    async def exercise():
        started = time.monotonic()
        await registry.revalidate_due(now=100.01)
        assert first.closed and not later.closed
        await registry.revalidate_due(now=100.02)
        assert later.closed
        await asyncio.sleep(0.01)
        assert later_closed == [daemon_auth.AuthFailureCode.REVOKED]
        assert time.monotonic() - started < authority.session_revalidation_seconds
        await registry.drain_close_callbacks()

    asyncio.run(exercise())


def test_secret_values_never_reach_errors_rendered_artifacts_or_authority_repr(tmp_path: Path):
    path, credential = _provision(tmp_path)
    token = _bearer(credential)
    authority = _authority(path)
    rotated = daemon_auth.rotate_credential(path, "operator-alice")
    error = _failure(authority, token)
    artifacts = [repr(credential), repr(rotated), repr(error), str(error), path.read_text()]

    for artifact in artifacts:
        daemon_auth.assert_redacted(artifact, [token, _bearer(rotated)])
    with pytest.raises(ValueError, match="sensitive credential"):
        daemon_auth.assert_redacted({"authorization": token}, [token])


def test_tokens_never_reach_logs_checked_fixtures_or_daemon_control_records(tmp_path: Path, caplog):
    path, credential = _provision(tmp_path)
    token = _bearer(credential)
    authority = _authority(path)
    authority.authenticate(credential.bearer)
    _failure(authority, token + "corrupt")

    fixture_root = Path(__file__).parent / "fixtures" / "host_daemon"
    fixtures = "".join(path.read_text() for path in fixture_root.rglob("*") if path.is_file())
    key = host_daemon.DaemonKey("uid:1", "/private/home", "host-1")
    control = asdict(
        host_daemon.ControlRecord.create(
            key,
            listener_host="127.0.0.1",
            listener_port=8737,
            verification_key=b"c" * 32,
        )
    )
    rendered = caplog.text + fixtures + json.dumps(control, sort_keys=True)

    assert token not in rendered
    assert "Bearer " not in fixtures
    assert not ({"token", "credential", "authorization", "attach_token"} & set(control))


def test_token_factory_must_supply_the_documented_entropy_shape(tmp_path: Path):
    with pytest.raises(daemon_auth.CredentialFileError, match="high-entropy"):
        daemon_auth.provision_credential_file(
            (tmp_path / "credentials.json").absolute(),
            credential_id="weak",
            audience=AUDIENCE,
            principal=PRINCIPAL,
            scopes=(AuthScope.OPERATOR_READ,),
            expires_at=int(time.time()) + 3_600,
            token_factory=lambda _size: secrets.token_urlsafe(8),
        )
