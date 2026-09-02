"""Deterministic inventory of every public transport projection.

The operation catalog remains the source of application-operation identity.  This module adds
the transport audit that the catalog intentionally cannot infer: HTTP methods and paths,
transport mechanics, composites, and explicit exclusions.  It contains declarations only; it
does not register routes or dispatch application behavior.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

from .operation_catalog import OperationSpec, operations

Classification = Literal["catalog-entry", "composite", "transport-mechanic", "explicit-exclusion"]


@dataclass(frozen=True)
class ProjectionSpec:
    surface: str
    identifier: str
    classification: Classification
    operation: str | None
    composes: tuple[str, ...]
    request_schema: str
    result_schema: str
    privilege: str
    side_effects: str
    prompts: bool
    streaming: bool
    availability: str
    compatibility: str
    reason: str


def _effect(operation: OperationSpec) -> str:
    return "none" if operation.kind == "read-resource" else "application-mutation"


def _catalog_projections() -> list[ProjectionSpec]:
    result: list[ProjectionSpec] = []
    for operation in operations():
        cli = operation.surfaces.get("cli")
        if cli:
            result.append(
                ProjectionSpec(
                    surface="cli",
                    identifier=cli["path"],
                    classification="catalog-entry",
                    operation=operation.name,
                    composes=(),
                    request_schema=f"catalog:{operation.name}#parameters",
                    result_schema=operation.result_schema,
                    privilege=operation.privilege,
                    side_effects=_effect(operation),
                    prompts=cli["interactivity"]["mode"] != "none",
                    streaming=False,
                    availability="local process; no daemon required",
                    compatibility="Typer path, options, help, output, and exit behavior",
                    reason="canonical CLI projection",
                )
            )
            for alias in cli["aliases"]:
                result.append(
                    ProjectionSpec(
                        surface="cli",
                        identifier=alias["path"],
                        classification="catalog-entry",
                        operation=operation.name,
                        composes=(),
                        request_schema=f"catalog:{operation.name}#parameters",
                        result_schema=operation.result_schema,
                        privilege=operation.privilege,
                        side_effects=_effect(operation),
                        prompts=alias["interactivity"]["mode"] != "none",
                        streaming=False,
                        availability="local process; no daemon required",
                        compatibility="declared CLI compatibility alias",
                        reason=alias["divergence"],
                    )
                )

        mcp = operation.surfaces.get("mcp")
        if mcp and (tool := mcp.get("tool")):
            composes = tuple(mcp.get("composes", ()))
            result.append(
                ProjectionSpec(
                    surface="mcp-tool",
                    identifier=tool,
                    classification="composite" if composes else "catalog-entry",
                    operation=operation.name,
                    composes=composes,
                    request_schema=f"fastmcp:tool/{tool}#inputSchema",
                    result_schema=operation.result_schema,
                    privilege=operation.privilege,
                    side_effects=_effect(operation),
                    prompts=False,
                    streaming=False,
                    availability="stdio; HTTP parity is owned by bh-q0lol.6",
                    compatibility="FastMCP discovery is authoritative",
                    reason=mcp.get("divergence", "positive catalog allowlist"),
                )
            )
        if mcp and (resource := mcp.get("resource")):
            result.append(
                ProjectionSpec(
                    surface="mcp-resource",
                    identifier=resource,
                    classification="catalog-entry",
                    operation=operation.name,
                    composes=(),
                    request_schema=f"fastmcp:resource/{resource}#parameters",
                    result_schema=operation.result_schema,
                    privilege=operation.privilege,
                    side_effects="none",
                    prompts=False,
                    streaming=False,
                    availability="stdio; HTTP parity is owned by bh-q0lol.6",
                    compatibility="FastMCP discovery is authoritative",
                    reason=mcp.get("divergence", "positive catalog allowlist"),
                )
            )
    return result


def _http(
    surface: str,
    method: str,
    path: str,
    classification: Classification,
    *,
    operation: str | None = None,
    composes: tuple[str, ...] = (),
    request_schema: str,
    result_schema: str,
    privilege: str,
    side_effects: str = "none",
    streaming: bool = False,
    availability: str,
    compatibility: str,
    reason: str,
) -> ProjectionSpec:
    return ProjectionSpec(
        surface=surface,
        identifier=f"{method} {path}",
        classification=classification,
        operation=operation,
        composes=composes,
        request_schema=request_schema,
        result_schema=result_schema,
        privilege=privilege,
        side_effects=side_effects,
        prompts=False,
        streaming=streaming,
        availability=availability,
        compatibility=compatibility,
        reason=reason,
    )


_OPERATOR_OPENAPI = "openapi:beadhive-host-openapi-v1.json"
_OPERATOR_AVAILABILITY = "loopback host daemon; CLI and stdio remain daemon-independent"
_OPERATOR_COMPATIBILITY = "checked OpenAPI 3.1 operation and runtime route must remain identical"

_OPERATOR_PROJECTIONS = (
    _http(
        "operator-api",
        "GET",
        "/health",
        "transport-mechanic",
        request_schema=f"{_OPERATOR_OPENAPI}#/paths/~1health/get",
        result_schema=f"{_OPERATOR_OPENAPI}#/paths/~1health/get/responses",
        privilege="public-liveness",
        availability=_OPERATOR_AVAILABILITY,
        compatibility=_OPERATOR_COMPATIBILITY,
        reason="daemon liveness/readiness mechanic",
    ),
    _http(
        "operator-api",
        "GET",
        "/api/v1/factory",
        "composite",
        composes=("hive.list", "host.list"),
        request_schema=f"{_OPERATOR_OPENAPI}#/paths/~1api~1v1~1factory/get",
        result_schema=f"{_OPERATOR_OPENAPI}#/paths/~1api~1v1~1factory/get/responses",
        privilege="operator-read",
        availability=_OPERATOR_AVAILABILITY,
        compatibility=_OPERATOR_COMPATIBILITY,
        reason="read-only directory composite over canonical hive and host inventories",
    ),
    _http(
        "operator-api",
        "GET",
        "/api/v1/factory/hives",
        "composite",
        composes=("hive.list", "hive.status"),
        request_schema=f"{_OPERATOR_OPENAPI}#/paths/~1api~1v1~1factory~1hives/get",
        result_schema=f"{_OPERATOR_OPENAPI}#/paths/~1api~1v1~1factory~1hives/get/responses",
        privilege="operator-read",
        availability=_OPERATOR_AVAILABILITY,
        compatibility=_OPERATOR_COMPATIBILITY,
        reason="bounded hive summary composite over canonical hive reads",
    ),
    _http(
        "operator-api",
        "GET",
        "/api/v1/hives/{hive_id:path}/snapshot",
        "composite",
        composes=("work.list", "work.schedule"),
        request_schema=f"{_OPERATOR_OPENAPI}#/paths/~1api~1v1~1hives~1{{hive_id}}~1snapshot/get",
        result_schema=f"{_OPERATOR_OPENAPI}#/paths/~1api~1v1~1hives~1{{hive_id}}~1snapshot/get/responses",
        privilege="operator-read",
        availability=_OPERATOR_AVAILABILITY,
        compatibility=_OPERATOR_COMPATIBILITY,
        reason="read-only state composite; source projection policy remains authoritative",
    ),
    _http(
        "operator-api",
        "GET",
        "/api/v1/hives/{hive_id:path}/work-items",
        "catalog-entry",
        operation="work.list",
        request_schema=f"{_OPERATOR_OPENAPI}#/paths/~1api~1v1~1hives~1{{hive_id}}~1work-items/get",
        result_schema=f"{_OPERATOR_OPENAPI}#/paths/~1api~1v1~1hives~1{{hive_id}}~1work-items/get/responses",
        privilege="operator-read",
        availability=_OPERATOR_AVAILABILITY,
        compatibility=_OPERATOR_COMPATIBILITY,
        reason="richer bounded projection of the canonical work list read",
    ),
    _http(
        "operator-api",
        "GET",
        "/api/v1/hives/{hive_id:path}/work-items/{bead_id}",
        "catalog-entry",
        operation="work.issue",
        request_schema=f"{_OPERATOR_OPENAPI}#/paths/~1api~1v1~1hives~1{{hive_id}}~1work-items~1{{bead_id}}/get",
        result_schema=f"{_OPERATOR_OPENAPI}#/paths/~1api~1v1~1hives~1{{hive_id}}~1work-items~1{{bead_id}}/get/responses",
        privilege="operator-read",
        availability=_OPERATOR_AVAILABILITY,
        compatibility=_OPERATOR_COMPATIBILITY,
        reason="exact-identity projection of the canonical work issue read",
    ),
    _http(
        "operator-api",
        "GET",
        "/api/v1/runs/{run_id}/activity",
        "explicit-exclusion",
        request_schema=f"{_OPERATOR_OPENAPI}#/paths/~1api~1v1~1runs~1{{run_id}}~1activity/get",
        result_schema=f"{_OPERATOR_OPENAPI}#/paths/~1api~1v1~1runs~1{{run_id}}~1activity/get/responses",
        privilege="operator-read",
        availability=_OPERATOR_AVAILABILITY,
        compatibility=_OPERATOR_COMPATIBILITY,
        reason=(
            "run-journal read has no canonical application operation yet; "
            "bh-3qkmk.4 owns projection"
        ),
    ),
    _http(
        "operator-api",
        "GET",
        "/api/v1/hives/{hive_id:path}/events",
        "transport-mechanic",
        request_schema=f"{_OPERATOR_OPENAPI}#/paths/~1api~1v1~1hives~1{{hive_id}}~1events/get",
        result_schema=f"{_OPERATOR_OPENAPI}#/paths/~1api~1v1~1hives~1{{hive_id}}~1events/get/responses",
        privilege="operator-read",
        streaming=True,
        availability="optional host-daemon SSE route",
        compatibility=_OPERATOR_COMPATIBILITY,
        reason="SSE cursor/replay/backpressure is transport policy owned by bh-q0lol",
    ),
    _http(
        "operator-api",
        "OPTIONS",
        "/api/v1/hives/{hive_id:path}/events",
        "transport-mechanic",
        request_schema=f"{_OPERATOR_OPENAPI}#/paths/~1api~1v1~1hives~1{{hive_id}}~1events/options",
        result_schema=(
            f"{_OPERATOR_OPENAPI}#/paths/~1api~1v1~1hives~1{{hive_id}}~1events/options/responses"
        ),
        privilege="operator-read",
        availability="optional host-daemon SSE route",
        compatibility=_OPERATOR_COMPATIBILITY,
        reason="exact local SSE CORS preflight mechanic",
    ),
    _http(
        "operator-api",
        "GET",
        "/openapi.json",
        "transport-mechanic",
        request_schema="none",
        result_schema="openapi:beadhive-host-openapi-v1.json",
        privilege="operator-read",
        availability=_OPERATOR_AVAILABILITY,
        compatibility="must byte-match the checked OpenAPI artifact",
        reason="transport contract discovery, not an application operation",
    ),
)


_GATEWAY_AVAILABILITY = "Development gateway profile only; local CLI/stdio remain independent"
_GATEWAY_COMPATIBILITY = "gateway.read.v1 and recursive disclosure allowlists"


def _gateway(
    method: str,
    path: str,
    classification: Classification,
    *,
    operation: str | None = None,
    composes: tuple[str, ...] = (),
    streaming: bool = False,
    side_effects: str = "none",
    reason: str,
) -> ProjectionSpec:
    symbol = path.replace("/", "~1")
    return _http(
        "gateway",
        method,
        path,
        classification,
        operation=operation,
        composes=composes,
        request_schema=f"python:beadhive.remote_gateway#{method.lower()}:{symbol}:request",
        result_schema=f"python:beadhive.remote_gateway#{method.lower()}:{symbol}:result",
        privilege="authenticated-development-subject" if path != "/healthz" else "public-liveness",
        side_effects=side_effects,
        streaming=streaming,
        availability=_GATEWAY_AVAILABILITY,
        compatibility=_GATEWAY_COMPATIBILITY,
        reason=reason,
    )


_GATEWAY_FUNCTIONAL = (
    _gateway("GET", "/healthz", "transport-mechanic", reason="gateway liveness mechanic"),
    _gateway(
        "GET",
        "/v1/instances",
        "composite",
        composes=("hive.list", "host.list"),
        reason="authorized remote directory composite",
    ),
    _gateway(
        "GET",
        "/v1/instances/{stage}/{slug}/hives",
        "catalog-entry",
        operation="hive.list",
        reason="richer remote projection of the canonical hive list",
    ),
    _gateway(
        "GET",
        "/v1/instances/{stage}/{slug}/hives/{hive_id:path}/snapshot",
        "composite",
        composes=("work.list", "work.schedule"),
        reason="bounded remote state composite",
    ),
    _gateway(
        "GET",
        "/v1/instances/{stage}/{slug}/hives/{hive_id:path}/events",
        "transport-mechanic",
        streaming=True,
        reason="remote SSE cursor/replay mechanic",
    ),
    _gateway(
        "GET",
        "/v1/instances/{stage}/{slug}/snapshot",
        "explicit-exclusion",
        reason=(
            "legacy coarse snapshot has no single application operation; "
            "bh-3qkmk.4 owns replacement mapping"
        ),
    ),
    _gateway(
        "GET",
        "/v1/instances/{stage}/{slug}/events",
        "transport-mechanic",
        streaming=True,
        reason="legacy coarse SSE compatibility mechanic",
    ),
    _gateway(
        "POST",
        "/v1/instances/{stage}/{slug}/commands/refresh",
        "explicit-exclusion",
        side_effects="bounded-runtime-refresh",
        reason="runtime refresh is not canonical sync; bh-3qkmk.4 owns operation projection",
    ),
    _gateway(
        "POST",
        "/v1/instances/{stage}/{slug}/commands/{command}",
        "transport-mechanic",
        reason="stable unavailable-command response prevents undeclared mutation exposure",
    ),
)

_GATEWAY_OPTIONS_PATHS = tuple(
    spec.identifier.removeprefix("GET ").removeprefix("POST ") for spec in _GATEWAY_FUNCTIONAL[1:8]
)
_GATEWAY_MECHANICS = tuple(
    _gateway(
        "OPTIONS", path, "transport-mechanic", reason="exact credentialed CORS preflight allowlist"
    )
    for path in _GATEWAY_OPTIONS_PATHS
) + (
    _gateway("GET", "/{path:path}", "transport-mechanic", reason="closed-world not-found fallback"),
    _gateway(
        "OPTIONS", "/{path:path}", "transport-mechanic", reason="closed-world preflight fallback"
    ),
)


def projections() -> tuple[ProjectionSpec, ...]:
    """Return the complete inventory in stable surface/identifier order."""
    rows = [
        *_catalog_projections(),
        *_OPERATOR_PROJECTIONS,
        *_GATEWAY_FUNCTIONAL,
        *_GATEWAY_MECHANICS,
    ]
    return tuple(sorted(rows, key=lambda row: (row.surface, row.identifier)))


def document() -> dict[str, Any]:
    """Return the checked, language-neutral transport inventory document."""
    return {
        "format_version": 1,
        "inventory_version": "1.0.0",
        "policy": {
            "classification": (
                "every public projection is a catalog entry, composite, transport mechanic, "
                "or explicit exclusion"
            ),
            "composites": (
                "constituents are canonical operations and must preserve application privilege "
                "and side-effect policy"
            ),
            "mcp": "positive allowlist only; absence is denial",
            "ownership": (
                "bh-q0lol retains daemon auth, streaming, sessions, OpenAPI runtime, "
                "and supervision"
            ),
        },
        "projections": [asdict(row) | {"composes": list(row.composes)} for row in projections()],
    }
