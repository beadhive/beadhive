"""Deterministic OpenAPI 3.1 generation for the daemon-owned non-MCP surface.

FastMCP discovery remains the schema authority for ``/mcp``.  The checked JSON document is an
output only: operations come from :data:`daemon_contract.NON_MCP_ROUTES`, and JSON schemas come
from the product wire models those routes name.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from importlib import resources
from pathlib import Path
from typing import Any

from . import daemon_contract
from .daemon_contract import NON_MCP_ROUTES, TERMINAL_PROTOCOL, RouteSpec, WireModel

OPENAPI_CONTRACT = "beadhive-host-openapi-v1.json"
OPENAPI_COMPONENTS_SHA256 = "2faaddede747d89b03d91029e78d98d9f62f8d628e1bafd26080cb7824417231"

_ERROR_RESPONSES = {
    400: "BadRequest",
    401: "Unauthorized",
    403: "Forbidden",
    404: "NotFound",
    408: "RequestTimeout",
    409: "Conflict",
    410: "Gone",
    413: "PayloadTooLarge",
    429: "RateLimited",
    503: "Unavailable",
}
_COMPONENT_NAMES = {
    daemon_contract.HiveSnapshotResponse: "HiveOperatorSnapshot",
    daemon_contract.ErrorResponse: "Error",
}
_HIVE_ID_SCHEMA = {
    "type": "string",
    "pattern": r"^[A-Za-z0-9._~-]+%2F[A-Za-z0-9._~-]+%2F[A-Za-z0-9._~-]+$",
}
_RUN_ID_SCHEMA = {
    "type": "string",
    "pattern": r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$",
}
_EVENT_CURSOR_SCHEMA = {
    "type": "string",
    "maxLength": 85,
    "pattern": r"^[A-Za-z0-9._~-]{1,64}:(0|[1-9][0-9]{0,19})$",
}


def contract_path() -> Path:
    return Path(resources.files("beadhive").joinpath("schemas", OPENAPI_CONTRACT))


def checked_openapi_document() -> dict[str, Any]:
    value = json.loads(contract_path().read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("daemon OpenAPI root must be an object")
    return value


def _without_titles(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _without_titles(item)
            for key, item in value.items()
            if key != "title" or not isinstance(item, str)
        }
    if isinstance(value, list):
        return [_without_titles(item) for item in value]
    return value


def _require_serialized_fields(value: Any) -> None:
    """Mark fields always emitted by ``WireModel.to_wire`` as response-required."""

    if isinstance(value, dict):
        properties = value.get("properties")
        if value.get("type") == "object" and isinstance(properties, dict):
            value["required"] = list(properties)
        for item in value.values():
            _require_serialized_fields(item)
    elif isinstance(value, list):
        for item in value:
            _require_serialized_fields(item)


def _install_model_schema(
    components: dict[str, Any],
    model: type[WireModel],
    *,
    component_name: str | None = None,
    response: bool,
) -> None:
    schema = model.model_json_schema(
        by_alias=True,
        ref_template="#/components/schemas/{model}",
    )
    definitions = schema.pop("$defs", {})
    schema = _without_titles(schema)
    definitions = _without_titles(definitions)
    if response:
        _require_serialized_fields(schema)
        _require_serialized_fields(definitions)
    for name, definition in definitions.items():
        existing = components.get(name)
        if existing is not None and existing != definition:
            raise RuntimeError(f"conflicting generated OpenAPI schema for {name}")
        components[name] = definition
    name = component_name or model.__name__
    existing = components.get(name)
    if existing is not None and existing != schema:
        raise RuntimeError(f"conflicting generated OpenAPI schema for {name}")
    components[name] = schema


def _error_schema() -> dict[str, Any]:
    """Shared runtime error envelope; handlers own an intentionally extensible code vocabulary."""

    return {
        "type": "object",
        "required": ["schemaVersion", "error"],
        "additionalProperties": False,
        "properties": {
            "schemaVersion": {"const": daemon_contract.WIRE_SCHEMA_VERSION},
            "error": {
                "type": "object",
                "required": ["code", "message", "retryable", "action", "requestId"],
                "additionalProperties": False,
                "properties": {
                    "code": {"type": "string"},
                    "message": {"type": "string"},
                    "retryable": {"type": "boolean"},
                    "action": {
                        "type": ["string", "null"],
                        "enum": ["retry", "resnapshot", "reauthenticate", None],
                    },
                    "requestId": {"type": ["string", "null"]},
                },
            },
        },
    }


def _component_schemas() -> dict[str, Any]:
    schemas: dict[str, Any] = {}
    response_models = {
        route.response_model for route in NON_MCP_ROUTES if route.response_model is not None
    }
    for model in sorted(response_models, key=lambda item: item.__name__):
        _install_model_schema(
            schemas,
            model,
            component_name=_COMPONENT_NAMES.get(model),
            response=True,
        )
    request_models = {
        route.request_model for route in NON_MCP_ROUTES if route.request_model is not None
    }
    for model in sorted(request_models, key=lambda item: item.__name__):
        _install_model_schema(schemas, model, response=False)
    _install_model_schema(
        schemas,
        daemon_contract.EventResnapshotResponse,
        component_name="Resnapshot",
        response=True,
    )
    schemas["Error"] = _error_schema()
    schemas["EventCursorString"] = dict(_EVENT_CURSOR_SCHEMA)
    schemas["WorkItemQueueName"] = {
        "type": "string",
        "enum": ["ready", "active", "blocked", "recent"],
    }
    return dict(sorted(schemas.items()))


def _response_components() -> dict[str, Any]:
    descriptions = {
        "BadRequest": "Unsafe identity, encoding, body, or cursor",
        "Unauthorized": "A bearer credential is required",
        "Forbidden": "The credential does not grant the operation",
        "NotFound": "The exact resource does not exist",
        "RequestTimeout": "The bounded request body read timed out",
        "Conflict": "Authoritative identity or revision conflict",
        "Gone": "The supplied resumable cursor has expired",
        "PayloadTooLarge": "The request body exceeded its configured bound",
        "RateLimited": "The normalized bearer exceeded its configured request rate",
        "Unavailable": "An authoritative source is unavailable or the daemon is draining",
    }
    return {
        name: {
            "description": description,
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}},
        }
        for name, description in descriptions.items()
    } | {
        "Resnapshot": {
            "description": "Fetch a replacement hive snapshot before reconnecting",
            "content": {
                "application/json": {"schema": {"$ref": "#/components/schemas/Resnapshot"}}
            },
        }
    }


def _parameter(
    name: str,
    location: str,
    schema: dict[str, Any],
    *,
    required: bool = False,
    description: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "name": name,
        "in": location,
        "required": required,
        "schema": schema,
    }
    if description is not None:
        value["description"] = description
    value.update(extra)
    return value


def _hive_parameter() -> dict[str, Any]:
    return _parameter(
        "hive_id",
        "path",
        dict(_HIVE_ID_SCHEMA),
        required=True,
        description=(
            "Full canonical provider/organization/repository identity, with separators "
            "encoded as uppercase %2F."
        ),
    )


def _route_non_header_parameters(route: RouteSpec) -> list[dict[str, Any]]:
    key = (route.method, route.path)
    if key == ("GET", "/api/v1/factory/hives"):
        return [
            _parameter(
                "limit",
                "query",
                {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
            ),
            _parameter(
                "cursor",
                "query",
                {"type": "string", "minLength": 1},
                description="Opaque snapshot-scoped cursor returned by nextCursor.",
            ),
            _parameter("availability", "query", {"enum": ["available", "unavailable"]}),
        ]
    if key == ("GET", "/api/v1/hives/{hive_id}/snapshot"):
        return [_hive_parameter()]
    if key == ("GET", "/api/v1/hives/{hive_id}/work-items"):
        return [
            _hive_parameter(),
            _parameter(
                "queue",
                "query",
                {"$ref": "#/components/schemas/WorkItemQueueName"},
                required=True,
            ),
            _parameter(
                "limit",
                "query",
                {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
            ),
            _parameter(
                "cursor",
                "query",
                {"type": "string", "minLength": 1},
                description="Opaque cursor returned by the preceding page.",
            ),
            _parameter(
                "priority",
                "query",
                {"type": "array", "items": {"type": "string", "pattern": "^P[0-4]$"}},
                description="Repeat to select any supplied priority.",
                style="form",
                explode=True,
            ),
            _parameter(
                "label",
                "query",
                {"type": "array", "items": {"type": "string", "minLength": 1}},
                description="Repeat to require every supplied label.",
                style="form",
                explode=True,
            ),
            _parameter("assignee", "query", {"type": "string"}),
            _parameter("type", "query", {"type": "string"}),
            _parameter("parent", "query", {"type": "string"}),
        ]
    if key == ("GET", "/api/v1/hives/{hive_id}/work-items/{bead_id}"):
        return [
            _hive_parameter(),
            _parameter(
                "bead_id",
                "path",
                {"type": "string", "pattern": r"^[A-Za-z0-9._~-]+$"},
                required=True,
            ),
        ]
    if key == ("GET", "/api/v1/hives/{hive_id}/events"):
        cursor_description = "Resumable producerEpoch:sequence cursor, with an 85-byte maximum."
        return [
            _hive_parameter(),
            _parameter(
                "subscription",
                "query",
                {"type": "string", "minLength": 1, "maxLength": 512},
                required=True,
            ),
            _parameter(
                "after",
                "query",
                {"$ref": "#/components/schemas/EventCursorString"},
                description=cursor_description,
            ),
            _parameter(
                "cursor",
                "query",
                {"$ref": "#/components/schemas/EventCursorString"},
                description=f"Temporary compatibility alias. {cursor_description}",
                deprecated=True,
            ),
        ]
    if route.path == "/api/v1/runs/{run_id}/activity":
        run_schema = (
            dict(_RUN_ID_SCHEMA)
            if route.method == "POST"
            else {"type": "string", "minLength": 1, "maxLength": 256}
        )
        values = [_parameter("run_id", "path", run_schema, required=True)]
        if route.method == "GET":
            values.append(
                _parameter(
                    "after",
                    "query",
                    dict(_EVENT_CURSOR_SCHEMA),
                    description="Return a delta strictly after producerEpoch:sequence.",
                )
            )
        return values
    return []


def _request_header_parameter(name: str) -> dict[str, Any]:
    if name == "If-None-Match":
        return _parameter(name, "header", {"type": "string"})
    if name == "Last-Event-ID":
        return _parameter(
            name,
            "header",
            {"$ref": "#/components/schemas/EventCursorString"},
            description="Resumable producerEpoch:sequence cursor, with an 85-byte maximum.",
        )
    raise RuntimeError(f"unsupported generated request header: {name}")


def _route_parameters(route: RouteSpec) -> list[dict[str, Any]]:
    return _route_non_header_parameters(route) + [
        _request_header_parameter(name) for name in route.request_headers
    ]


def _component_name(model: type[WireModel]) -> str:
    return _COMPONENT_NAMES.get(model, model.__name__)


def _success_response(route: RouteSpec, status: int) -> dict[str, Any] | None:
    if status == 304:
        return {
            "description": "The current representation matches If-None-Match",
            "headers": {
                "ETag": {"schema": {"type": "string"}},
                "Cache-Control": {"schema": {"const": "no-cache"}},
            },
        }
    if route.path == "/openapi.json" and status == 200:
        return {
            "description": "OpenAPI 3.1 document",
            "content": {"application/json": {"schema": {"type": "object"}}},
        }
    if route.response_model is None or status not in {200, 201}:
        return None
    media_type = (
        "text/event-stream"
        if route.path == "/api/v1/hives/{hive_id}/events"
        else "application/json"
    )
    response: dict[str, Any] = {
        "description": "Product wire response",
        "content": {
            media_type: {
                "schema": {"$ref": f"#/components/schemas/{_component_name(route.response_model)}"}
            }
        },
    }
    if route.path in {
        "/api/v1/factory/hives",
        "/api/v1/hives/{hive_id}/work-items",
        "/api/v1/hives/{hive_id}/work-items/{bead_id}",
    }:
        response["headers"] = {
            "ETag": {"schema": {"type": "string"}},
            "Cache-Control": {"schema": {"const": "no-cache"}},
        }
    elif route.path == "/api/v1/hives/{hive_id}/events":
        response["headers"] = {
            "Cache-Control": {"schema": {"const": "no-cache, no-transform"}},
            "X-Accel-Buffering": {"schema": {"const": "no"}},
        }
    return response


def _terminal_unavailable_response() -> dict[str, Any]:
    return {
        "description": "Terminal attachment is explicitly unavailable",
        "content": {
            "application/json": {
                "schema": {
                    "oneOf": [
                        {"$ref": "#/components/schemas/TerminalUnavailable"},
                        {"$ref": "#/components/schemas/Error"},
                    ]
                }
            }
        },
    }


def _operation(route: RouteSpec) -> dict[str, Any]:
    operation_id = {
        ("GET", "/health"): "health",
        ("GET", "/api/v1/factory"): "operatorFactory",
        ("GET", "/api/v1/factory/hives"): "operatorFactoryHives",
        ("GET", "/api/v1/hives/{hive_id}/snapshot"): "operatorHiveSnapshot",
        ("GET", "/api/v1/hives/{hive_id}/work-items"): "operatorWorkItems",
        ("GET", "/api/v1/hives/{hive_id}/work-items/{bead_id}"): "operatorWorkItemDetail",
        ("GET", "/api/v1/hives/{hive_id}/events"): "operatorHiveEvents",
        ("GET", "/api/v1/runs/{run_id}/activity"): "operatorRunActivity",
        ("POST", "/api/v1/runs/{run_id}/activity"): "publishRunActivity",
        ("POST", "/api/v1/terminal/attach-token"): "terminalAttachTokenUnavailable",
        ("GET", "/openapi.json"): "operatorOpenapi",
    }[(route.method, route.path)]
    operation: dict[str, Any] = {
        "operationId": operation_id,
        "summary": operation_id,
        "security": [] if route.scope is None else [{"BearerAuth": []}],
        "x-beadhive-required-scope": None if route.scope is None else route.scope.value,
        "responses": {},
    }
    parameters = _route_parameters(route)
    if parameters:
        operation["parameters"] = parameters
    if route.request_model is not None:
        operation["requestBody"] = {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "$ref": f"#/components/schemas/{_component_name(route.request_model)}"
                    }
                }
            },
        }
    for status in route.statuses:
        success = _success_response(route, status)
        if success is not None:
            response = success
        elif status == 503 and route.path == "/api/v1/terminal/attach-token":
            response = _terminal_unavailable_response()
        elif status == 409 and route.path == "/api/v1/hives/{hive_id}/events":
            response = {"$ref": "#/components/responses/Resnapshot"}
        else:
            response = {"$ref": f"#/components/responses/{_ERROR_RESPONSES[status]}"}
        operation["responses"][str(status)] = response
    if route.path == "/api/v1/hives/{hive_id}/events":
        operation["x-beadhive-streaming"] = "server-sent-events"
    if route.path.startswith("/api/v1/terminal"):
        operation["x-beadhive-availability"] = "unavailable-pending-bh-lx6e-replan"
    return operation


def _websocket_operation(route: RouteSpec) -> dict[str, Any]:
    assert route.method == "WEBSOCKET"
    return {
        "operationId": "terminalWebsocketUnavailable",
        "summary": "Terminal attachment is unavailable pending the bh-lx6e replan",
        "security": [{"BearerAuth": []}],
        "x-beadhive-required-scope": route.scope.value if route.scope is not None else None,
        "x-beadhive-availability": "unavailable-pending-bh-lx6e-replan",
        "x-beadhive-websocket-subprotocol": TERMINAL_PROTOCOL,
        "responses": {
            "503": {
                "description": "Terminal attachment is explicitly unavailable",
                "content": {
                    "application/json": {
                        "schema": {"$ref": "#/components/schemas/TerminalUnavailable"}
                    }
                },
            }
        },
        "x-beadhive-denial-handshake": {
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
        },
        "x-beadhive-close-events": {
            "unauthenticated": {"code": 4401, "reason": "Unauthorized"},
            "forbidden": {"code": 4403, "reason": "Forbidden"},
            "terminalUnavailable": {
                "code": 1013,
                "reason": "terminal.unavailable:pty_verdict_pending",
            },
        },
    }


def component_digest(components: dict[str, Any]) -> str:
    """Digest every independently generated reusable OpenAPI component."""

    encoded = json.dumps(components, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _components() -> dict[str, Any]:
    return {
        "securitySchemes": {
            "BearerAuth": {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "bh1.<credential-id>.<secret>",
                "x-beadhive-required-scope": "operator:read",
            }
        },
        "schemas": _component_schemas(),
        "responses": _response_components(),
    }


def _checked_components() -> dict[str, Any]:
    components = _components()
    actual = component_digest(components)
    if actual != OPENAPI_COMPONENTS_SHA256:
        raise RuntimeError(
            "generated OpenAPI component digest drifted: "
            f"expected {OPENAPI_COMPONENTS_SHA256}, got {actual}"
        )
    return components


def generate_openapi_document() -> dict[str, Any]:
    """Generate the complete contract without consulting the checked output artifact."""

    paths: dict[str, dict[str, Any]] = {}
    for route in NON_MCP_ROUTES:
        path_item = paths.setdefault(route.path, {})
        if route.method == "WEBSOCKET":
            path_item["x-beadhive-websocket"] = _websocket_operation(route)
        else:
            path_item[route.method.lower()] = _operation(route)
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Beadhive Host Daemon API",
            "version": "1.3.0",
            "description": (
                "Generated contract for daemon-owned REST, SSE, health, and reserved terminal "
                "routes. MCP is intentionally discovered through FastMCP instead."
            ),
        },
        "security": [{"BearerAuth": []}],
        "x-beadhive-mcp": {
            "path": "/mcp",
            "schemaAuthority": "FastMCP protocol discovery",
            "describedByOpenAPI": False,
        },
        "x-beadhive-secure-network-preflight": {
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
        },
        "paths": paths,
        "components": _checked_components(),
    }


def render_openapi_document(document: dict[str, Any] | None = None) -> str:
    return json.dumps(document or generate_openapi_document(), indent=2, ensure_ascii=False) + "\n"


def check_openapi_artifact() -> bool:
    return render_openapi_document() == contract_path().read_text(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="fail if the checked artifact drifted")
    mode.add_argument("--write", action="store_true", help="rewrite the checked artifact")
    args = parser.parse_args(argv)
    if args.check:
        if check_openapi_artifact():
            return 0
        print(f"OpenAPI drift: regenerate {contract_path()} with --write")
        return 1
    contract_path().write_text(render_openapi_document(), encoding="utf-8")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a CI command
    raise SystemExit(main())
