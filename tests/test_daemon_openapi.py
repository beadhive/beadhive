"""Generated OpenAPI parity for the daemon's complete non-MCP product surface."""

from __future__ import annotations

import asyncio
import copy
import json
import time
from pathlib import Path

import httpx
from starlette.routing import Route, WebSocketRoute

from beadhive import daemon_auth, daemon_contract, operator_api, operator_sources
from test_daemon_activity_api import _app as _authenticated_app
from test_operator_api import _app as _read_app


def _route_key(route) -> set[tuple[str, str]]:
    path = route.path.replace("{hive_id:path}", "{hive_id}")
    if isinstance(route, WebSocketRoute):
        return {("WEBSOCKET", path)}
    if isinstance(route, Route):
        return {
            (method, path)
            for method in (route.methods or ())
            if method != "HEAD" and not path.startswith("/mcp")
        }
    return set()


def test_generated_document_is_deterministic_and_matches_checked_artifact() -> None:
    from beadhive import daemon_openapi

    first = daemon_openapi.generate_openapi_document()
    second = daemon_openapi.generate_openapi_document()
    artifact = Path(operator_api.__file__).parent / "schemas" / operator_api.OPENAPI_CONTRACT

    assert first == second == json.loads(artifact.read_text())
    assert json.dumps(first, sort_keys=True, separators=(",", ":")) == json.dumps(
        second, sort_keys=True, separators=(",", ":")
    )


def test_compact_snapshot_label_bounds_are_published_in_openapi() -> None:
    from beadhive import daemon_openapi

    labels = daemon_openapi.generate_openapi_document()["components"]["schemas"][
        "SnapshotWorkItemSummary"
    ]["properties"]["labels"]
    assert labels["maxItems"] == 12
    assert labels["items"] == {"type": "string", "minLength": 1, "maxLength": 256}


def test_generation_does_not_consume_the_artifact_it_checks(monkeypatch) -> None:
    from beadhive import daemon_openapi

    def circular_read_is_a_failure():
        raise AssertionError("the generated artifact cannot be its own schema authority")

    monkeypatch.setattr(daemon_openapi, "checked_openapi_document", circular_read_is_a_failure)
    generated = daemon_openapi.generate_openapi_document()

    request = generated["paths"]["/api/v1/runs/{run_id}/activity"]["post"]["requestBody"]
    assert request["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ActivityAppendRequest"
    }


def test_checked_artifact_route_or_schema_drift_fails_the_ci_check(
    tmp_path: Path, monkeypatch
) -> None:
    from beadhive import daemon_openapi

    drifted = daemon_openapi.generate_openapi_document()
    drifted["paths"]["/openapi.json"]["get"]["security"] = []
    artifact = tmp_path / "openapi.json"
    artifact.write_text(json.dumps(drifted, indent=2) + "\n")
    monkeypatch.setattr(daemon_openapi, "contract_path", lambda: artifact)
    assert daemon_openapi.check_openapi_artifact() is False

    drifted = daemon_openapi.generate_openapi_document()
    drifted["paths"]["/api/v1/runs/{run_id}/activity"]["post"]["requestBody"] = {
        "required": True,
        "content": {"application/json": {"schema": {"type": "string"}}},
    }
    artifact.write_text(json.dumps(drifted, indent=2) + "\n")
    assert daemon_openapi.check_openapi_artifact() is False

    drifted = daemon_openapi.generate_openapi_document()
    drifted["components"]["schemas"]["ActivityAppendRequest"]["properties"]["source"] = {
        "type": "string"
    }
    artifact.write_text(json.dumps(drifted, indent=2) + "\n")
    assert daemon_openapi.check_openapi_artifact() is False

    drifted = daemon_openapi.generate_openapi_document()
    drifted["components"]["schemas"]["HealthResponse"]["properties"].pop("ready")
    artifact.write_text(json.dumps(drifted, indent=2) + "\n")
    assert daemon_openapi.check_openapi_artifact() is False


def test_generated_component_digest_covers_every_independent_component(monkeypatch) -> None:
    from beadhive import daemon_openapi

    document = daemon_openapi.generate_openapi_document()
    assert (
        daemon_openapi.component_digest(document["components"])
        == daemon_openapi.OPENAPI_COMPONENTS_SHA256
    )
    for category, members in document["components"].items():
        for name in members:
            tampered = copy.deepcopy(document["components"])
            tampered[category][name]["x-test-tamper"] = True
            assert (
                daemon_openapi.component_digest(tampered)
                != daemon_openapi.OPENAPI_COMPONENTS_SHA256
            )

    original = daemon_openapi._component_schemas

    def tampered_components():
        schemas = original()
        schemas["ActivityAppendRequest"]["properties"]["source"] = {"type": "integer"}
        return schemas

    monkeypatch.setattr(daemon_openapi, "_component_schemas", tampered_components)
    try:
        daemon_openapi.generate_openapi_document()
    except RuntimeError as exc:
        assert "component digest" in str(exc)
    else:
        raise AssertionError("independent component tampering must fail generation")


def test_generated_operations_match_live_routes_manifest_scopes_and_statuses(
    tmp_path: Path,
) -> None:
    app, _tokens, _record = _authenticated_app(tmp_path)
    running = set().union(*(_route_key(route) for route in app.routes))
    manifest = {(route.method, route.path): route for route in daemon_contract.NON_MCP_ROUTES}
    document = operator_api.openapi_document()
    documented = {
        (("WEBSOCKET" if method == "x-beadhive-websocket" else method.upper()), path)
        for path, path_item in document["paths"].items()
        for method in path_item
        if method in {"get", "post", "put", "patch", "delete", "x-beadhive-websocket"}
    }

    assert running == set(manifest) == documented
    assert all("/mcp" not in path for _method, path in documented)
    assert document["x-beadhive-mcp"] == {
        "path": "/mcp",
        "schemaAuthority": "FastMCP protocol discovery",
        "describedByOpenAPI": False,
    }

    for route in manifest.values():
        method = "x-beadhive-websocket" if route.method == "WEBSOCKET" else route.method.lower()
        operation = document["paths"][route.path][method]
        assert set(map(int, operation["responses"])) == set(route.statuses)
        assert operation["x-beadhive-required-scope"] == (
            route.scope.value if route.scope is not None else None
        )
        assert operation["security"] == ([] if route.scope is None else [{"BearerAuth": []}])


def test_terminal_routes_return_the_typed_pending_replan_contract(tmp_path: Path) -> None:
    app, _tokens, _record = _authenticated_app(tmp_path)
    terminal = daemon_auth.add_credential(
        (tmp_path / "credentials.json").absolute(),
        credential_id="terminal",
        audience="beadhive-host",
        principal="operator:terminal",
        scopes=(daemon_contract.AuthScope.TERMINAL_ATTACH,),
        expires_at=int(time.time()) + 3_600,
    )
    token = terminal.bearer.reveal_for_authority()

    async def exercise():
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 5000))
            async with httpx.AsyncClient(
                transport=transport, base_url="http://127.0.0.1:8420"
            ) as client:
                return await client.post(
                    "/api/v1/terminal/attach-token",
                    headers={"Authorization": f"Bearer {token}"},
                )

    response = asyncio.run(exercise())
    assert response.status_code == 503
    assert response.json() == daemon_contract.TerminalUnavailable().to_wire()

    document = operator_api.openapi_document()
    attach = document["paths"]["/api/v1/terminal/attach-token"]["post"]
    websocket = document["paths"]["/ws/terminal"]["x-beadhive-websocket"]
    unavailable = {"$ref": "#/components/schemas/TerminalUnavailable"}
    error = {"$ref": "#/components/schemas/Error"}
    assert attach["responses"]["503"]["content"]["application/json"]["schema"] == {
        "oneOf": [unavailable, error]
    }
    assert websocket["responses"]["503"]["content"]["application/json"]["schema"] == unavailable
    assert websocket["x-beadhive-websocket-subprotocol"] == daemon_contract.TERMINAL_PROTOCOL
    assert websocket["x-beadhive-availability"] == "unavailable-pending-bh-lx6e-replan"


def test_parameters_bodies_responses_and_streaming_are_checked_exactly() -> None:
    document = operator_api.openapi_document()
    expected_non_header_parameters = {
        ("GET", "/health"): [],
        ("GET", "/api/v1/factory"): [],
        ("GET", "/api/v1/factory/hives"): [
            "limit@query",
            "cursor@query",
            "availability@query",
        ],
        ("GET", "/api/v1/hives/{hive_id}/snapshot"): ["hive_id@path"],
        ("GET", "/api/v1/hives/{hive_id}/work-items"): [
            "hive_id@path",
            "queue@query",
            "limit@query",
            "cursor@query",
            "priority@query",
            "label@query",
            "assignee@query",
            "type@query",
            "parent@query",
        ],
        ("GET", "/api/v1/hives/{hive_id}/work-items/{bead_id}"): [
            "hive_id@path",
            "bead_id@path",
        ],
        ("GET", "/api/v1/hives/{hive_id}/events"): [
            "hive_id@path",
            "subscription@query",
            "after@query",
            "cursor@query",
        ],
        ("GET", "/api/v1/runs/{run_id}/activity"): ["run_id@path", "after@query"],
        ("POST", "/api/v1/runs/{run_id}/activity"): ["run_id@path"],
        ("POST", "/api/v1/terminal/attach-token"): [],
        ("GET", "/openapi.json"): [],
        ("WEBSOCKET", "/ws/terminal"): [],
    }
    expected_success = {
        ("GET", "/health"): "HealthResponse",
        ("GET", "/api/v1/factory"): "FactoryResponse",
        ("GET", "/api/v1/factory/hives"): "FactoryHivePage",
        ("GET", "/api/v1/hives/{hive_id}/snapshot"): "HiveOperatorSnapshot",
        ("GET", "/api/v1/hives/{hive_id}/work-items"): "WorkItemQueue",
        ("GET", "/api/v1/hives/{hive_id}/work-items/{bead_id}"): "WorkItemDetail",
        ("GET", "/api/v1/hives/{hive_id}/events"): "OperatorEvent",
        ("GET", "/api/v1/runs/{run_id}/activity"): "RunActivityFrame",
        ("POST", "/api/v1/runs/{run_id}/activity"): "ActivityAppendResponse",
    }

    for (method, path), expected in expected_non_header_parameters.items():
        operation_key = "x-beadhive-websocket" if method == "WEBSOCKET" else method.lower()
        operation = document["paths"][path][operation_key]
        assert [
            f"{parameter['name']}@{parameter['in']}"
            for parameter in operation.get("parameters", [])
            if parameter["in"] != "header"
        ] == expected

    for route in daemon_contract.NON_MCP_ROUTES:
        operation_key = (
            "x-beadhive-websocket" if route.method == "WEBSOCKET" else route.method.lower()
        )
        operation = document["paths"][route.path][operation_key]
        documented_headers = tuple(
            parameter["name"]
            for parameter in operation.get("parameters", [])
            if parameter["in"] == "header"
        )
        assert documented_headers == route.request_headers
        if 304 in route.statuses:
            assert "If-None-Match" in route.request_headers

    for (method, path), schema in expected_success.items():
        operation = document["paths"][path][method.lower()]
        status = "201" if method == "POST" else "200"
        media_type = "text/event-stream" if path.endswith("/events") else "application/json"
        assert operation["responses"][status]["content"][media_type]["schema"] == {
            "$ref": f"#/components/schemas/{schema}"
        }

    activity_post = document["paths"]["/api/v1/runs/{run_id}/activity"]["post"]
    assert activity_post["requestBody"] == {
        "required": True,
        "content": {
            "application/json": {"schema": {"$ref": "#/components/schemas/ActivityAppendRequest"}}
        },
    }
    events = document["paths"]["/api/v1/hives/{hive_id}/events"]["get"]
    assert events["x-beadhive-streaming"] == "server-sent-events"
    assert events["responses"]["200"]["headers"] == {
        "Cache-Control": {"schema": {"const": "no-cache, no-transform"}},
        "X-Accel-Buffering": {"schema": {"const": "no"}},
    }
    for path in (
        "/api/v1/factory/hives",
        "/api/v1/hives/{hive_id}/work-items",
        "/api/v1/hives/{hive_id}/work-items/{bead_id}",
    ):
        responses = document["paths"][path]["get"]["responses"]
        for status in ("200", "304"):
            assert responses[status]["headers"]["Cache-Control"] == {
                "schema": {"const": "no-cache"}
            }


def test_terminal_websocket_denies_upgrade_with_the_same_typed_contract(tmp_path: Path) -> None:
    app, _tokens, _record = _authenticated_app(tmp_path)
    websocket_origin = "http://127.0.0.1:3000"
    app.state.network_admission._allowed_origins = frozenset({websocket_origin})
    terminal = daemon_auth.add_credential(
        (tmp_path / "credentials.json").absolute(),
        credential_id="terminal-websocket",
        audience="beadhive-host",
        principal="operator:terminal",
        scopes=(daemon_contract.AuthScope.TERMINAL_ATTACH,),
        expires_at=int(time.time()) + 3_600,
    )
    token = terminal.bearer.reveal_for_authority()

    async def exercise(headers, *, denial_extension: bool):
        sent = []

        async def receive():
            return {"type": "websocket.connect"}

        async def send(message):
            sent.append(message)

        scope = {
            "type": "websocket",
            "asgi": {"version": "3.0", "spec_version": "2.5"},
            "http_version": "1.1",
            "scheme": "ws",
            "path": "/ws/terminal",
            "raw_path": b"/ws/terminal",
            "query_string": b"",
            "root_path": "",
            "headers": headers,
            "client": ("127.0.0.1", 5000),
            "server": ("127.0.0.1", 8420),
            "subprotocols": [daemon_contract.TERMINAL_PROTOCOL],
            "state": {},
            "extensions": {"websocket.http.response": {}} if denial_extension else {},
        }
        await app(scope, receive, send)
        return sent

    async def exercise_both():
        async with app.router.lifespan_context(app):
            missing = await exercise(
                [(b"host", b"127.0.0.1"), (b"origin", websocket_origin.encode("ascii"))],
                denial_extension=True,
            )
            extension = await exercise(
                [
                    (b"host", b"127.0.0.1"),
                    (b"origin", websocket_origin.encode("ascii")),
                    (b"authorization", f"Bearer {token}".encode("ascii")),
                ],
                denial_extension=True,
            )
            fallback = await exercise(
                [
                    (b"host", b"127.0.0.1"),
                    (b"origin", websocket_origin.encode("ascii")),
                    (b"authorization", f"Bearer {token}".encode("ascii")),
                ],
                denial_extension=False,
            )
            return missing, extension, fallback

    missing, extension, fallback = asyncio.run(exercise_both())
    assert missing == [{"type": "websocket.close", "code": 4401, "reason": "Unauthorized"}]
    assert extension[0] == {
        "type": "websocket.http.response.start",
        "status": 503,
        "headers": [(b"content-length", b"185"), (b"content-type", b"application/json")],
    }
    assert extension[1]["type"] == "websocket.http.response.body"
    assert extension[1].get("more_body", False) is False
    assert json.loads(extension[1]["body"]) == daemon_contract.TerminalUnavailable().to_wire()
    assert fallback == [
        {
            "type": "websocket.close",
            "code": 1013,
            "reason": "terminal.unavailable:pty_verdict_pending",
        }
    ]

    websocket = operator_api.openapi_document()["paths"]["/ws/terminal"]["x-beadhive-websocket"]
    assert websocket["x-beadhive-denial-handshake"] == {
        "extension": "websocket.http.response",
        "whenAdvertised": {
            "status": 503,
            "contentType": "application/json",
            "schema": {"$ref": "#/components/schemas/TerminalUnavailable"},
        },
        "fallback": {
            "closeCode": 1013,
            "reason": "terminal.unavailable:pty_verdict_pending",
        },
    }
    assert websocket["x-beadhive-close-events"] == {
        "unauthenticated": {"code": 4401, "reason": "Unauthorized"},
        "forbidden": {"code": 4403, "reason": "Forbidden"},
        "terminalUnavailable": {
            "code": 1013,
            "reason": "terminal.unavailable:pty_verdict_pending",
        },
    }


def test_activity_cursor_expiry_is_live_and_documented(tmp_path: Path) -> None:
    app, tokens, _record = _authenticated_app(tmp_path)

    async def expired(_run_id, _after):
        raise operator_sources.OperatorSourceError(
            "activity_cursor_expired",
            "The activity cursor has expired.",
            status_code=410,
            retryable=False,
        )

    app.state.operator_api.activity_reader = expired

    async def exercise():
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 5000))
            async with httpx.AsyncClient(
                transport=transport, base_url="http://127.0.0.1:8420"
            ) as client:
                return await client.get(
                    "/api/v1/runs/run-expired/activity?after=epoch:1",
                    headers={"Authorization": f"Bearer {tokens['read']}"},
                )

    response = asyncio.run(exercise())
    assert (response.status_code, response.json()["error"]["code"]) == (
        410,
        "activity_cursor_expired",
    )
    route = next(
        route
        for route in daemon_contract.NON_MCP_ROUTES
        if (route.method, route.path) == ("GET", "/api/v1/runs/{run_id}/activity")
    )
    assert 410 in route.statuses
    documented = operator_api.openapi_document()["paths"][route.path]["get"]["responses"]
    assert documented["410"] == {"$ref": "#/components/responses/Gone"}


def test_secure_network_preflight_is_one_documented_wildcard_route() -> None:
    document = operator_api.openapi_document()
    preflight = document["x-beadhive-secure-network-preflight"]

    assert preflight == {
        "method": "OPTIONS",
        "pathScope": "all request paths, including paths absent from this document",
        "successStatus": 204,
        "absentPathStatus": 204,
        "allowedMethods": ["DELETE", "GET", "POST"],
        "allowedHeaders": [
            "accept",
            "authorization",
            "content-type",
            "last-event-id",
            "mcp-protocol-version",
            "mcp-session-id",
        ],
        "responseHeaderSpelling": "lower-case, lexical, comma-space separated",
    }
    assert all("options" not in path_item for path_item in document["paths"].values())


def test_secure_network_preflight_wildcard_matches_known_and_absent_live_paths(
    tmp_path: Path,
) -> None:
    app, _tokens, _record = _authenticated_app(tmp_path)
    origin = "https://operator.example"
    app.state.network_admission._allowed_origins = frozenset({origin})

    async def exercise():
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 5000))
            async with httpx.AsyncClient(
                transport=transport, base_url="http://127.0.0.1:8420"
            ) as client:
                headers = {
                    "Origin": origin,
                    "Access-Control-Request-Method": "GET",
                    "Access-Control-Request-Headers": "Authorization",
                }
                return (
                    await client.options("/api/v1/factory", headers=headers),
                    await client.options("/not-a-route", headers=headers),
                )

    known, absent = asyncio.run(exercise())
    live_inventory = (
        {("OPTIONS", "*")} if (known.status_code, absent.status_code) == (204, 204) else set()
    )
    documented_inventory = (
        {("OPTIONS", "*")}
        if operator_api.openapi_document()["x-beadhive-secure-network-preflight"]["method"]
        == "OPTIONS"
        else set()
    )
    assert live_inventory == documented_inventory == {("OPTIONS", "*")}


def test_compatibility_profile_cannot_publish_openapi_without_operator_scope(
    tmp_path: Path,
) -> None:
    app = _read_app(tmp_path)

    async def exercise():
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 5000))
            async with httpx.AsyncClient(
                transport=transport, base_url="http://127.0.0.1:8420"
            ) as client:
                return await client.get("/openapi.json")

    response = asyncio.run(exercise())
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"

    async def policy_status(principal):
        sent = []

        async def downstream(scope, receive, send):
            await operator_api.JSONResponse({"openapi": "3.1.0"})(scope, receive, send)

        policy = operator_api.LocalReadPolicyMiddleware(
            downstream,
            listener_host="127.0.0.1",
            listener_port=8420,
        )
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.5"},
            "http_version": "1.1",
            "scheme": "http",
            "method": "GET",
            "path": "/openapi.json",
            "raw_path": b"/openapi.json",
            "query_string": b"",
            "root_path": "",
            "headers": [(b"host", b"127.0.0.1:8420")],
            "client": ("127.0.0.1", 5000),
            "server": ("127.0.0.1", 8420),
            "state": {"auth_principal": principal},
        }

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        await policy(scope, receive, send)
        return next(
            message["status"] for message in sent if message["type"] == "http.response.start"
        )

    def principal(scopes):
        return daemon_auth.AuthenticatedPrincipal(
            credential_id="compatibility-test",
            principal="operator:test",
            audience="beadhive-host",
            scopes=frozenset(scopes),
            expires_at=int(time.time()) + 3_600,
            generation=1,
        )

    async def exercise_policies():
        return await asyncio.gather(
            policy_status(principal({daemon_contract.AuthScope.ACTIVITY_PUBLISH})),
            policy_status(principal({daemon_contract.AuthScope.OPERATOR_READ})),
        )

    wrong, allowed = asyncio.run(exercise_policies())
    assert (wrong, allowed) == (403, 200)


def test_openapi_operation_is_itself_protected_by_operator_read(tmp_path: Path) -> None:
    document = operator_api.openapi_document()
    operation = document["paths"]["/openapi.json"]["get"]

    assert operation["security"] == [{"BearerAuth": []}]
    assert operation["x-beadhive-required-scope"] == "operator:read"
    assert (
        daemon_auth.request_scope({"type": "http", "method": "GET", "path": "/openapi.json"})
        is daemon_contract.AuthScope.OPERATOR_READ
    )

    app, tokens, _record = _authenticated_app(tmp_path)

    async def exercise():
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 5000))
            async with httpx.AsyncClient(
                transport=transport, base_url="http://127.0.0.1:8420"
            ) as client:
                missing = await client.get("/openapi.json")
                wrong_scope = await client.get(
                    "/openapi.json",
                    headers={"Authorization": f"Bearer {tokens['publish']}"},
                )
                allowed = await client.get(
                    "/openapi.json",
                    headers={"Authorization": f"Bearer {tokens['read']}"},
                )
                return missing, wrong_scope, allowed

    missing, wrong_scope, allowed = asyncio.run(exercise())
    assert [missing.status_code, wrong_scope.status_code, allowed.status_code] == [401, 403, 200]
    assert allowed.json() == document
