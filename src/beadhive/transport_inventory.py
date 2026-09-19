"""Deterministic inventory of every public transport projection.

The operation catalog remains the source of application-operation identity.  This module adds
the transport audit that the catalog intentionally cannot infer: HTTP methods and paths,
transport mechanics, composites, and explicit exclusions.  It contains declarations only; it
does not register routes or dispatch application behavior.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from functools import cache
from typing import Any, Literal

from . import daemon_contract
from .gateway_wire_contracts import WireFamily, schema_ref
from .kernel.operations import OperationSpec, operations

Classification = Literal["catalog-entry", "composite", "transport-mechanic", "explicit-exclusion"]
ProjectionShape = Literal["exact", "richer", "coarser", "transport-only", "excluded"]


@dataclass(frozen=True)
class CanonicalOperationContract:
    """Catalog-owned request/result and policy metadata referenced by one projection."""

    operation: str
    request_schema: str
    result_schema: str
    privilege: str
    side_effects: str


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
    canonical_contracts: tuple[CanonicalOperationContract, ...]
    transport_owner: str
    shape: ProjectionShape


@dataclass(frozen=True)
class CompositionRootSpec:
    """One installed process boundary and the executable proof that keeps it thin."""

    surface: str
    console_script: str
    module: str
    callable: str
    runtime_factory: str
    responsibility: str
    registration_drift_test: str
    test_closure: tuple[str, ...]


def composition_roots() -> tuple[CompositionRootSpec, ...]:
    """Return every installed transport entrypoint in stable surface order."""

    return (
        CompositionRootSpec(
            surface="cli",
            console_script="bh",
            module="beadhive.bootstrap.cli",
            callable="main",
            runtime_factory="beadhive.cli:app",
            responsibility="composition-and-process-lifecycle-only",
            registration_drift_test=(
                "tests/test_cli_projection.py::test_complete_cli_tree_is_catalog_derived"
            ),
            test_closure=(
                "tests/test_transport_composition_roots.py",
                "tests/test_operation_catalog.py",
                "tests/test_cli_projection.py",
                "tests/test_cli.py",
            ),
        ),
        CompositionRootSpec(
            surface="gateway",
            console_script="beadhive-frame-bridge",
            module="beadhive.bootstrap.frame_bridge",
            callable="main",
            runtime_factory="beadhive.frame_bridge_runtime:create_application",
            responsibility="composition-and-process-lifecycle-only",
            registration_drift_test=(
                "tests/test_transport_inventory.py::"
                "test_gateway_inventory_matches_every_registered_runtime_route"
            ),
            test_closure=(
                "tests/test_transport_composition_roots.py",
                "tests/test_transport_inventory.py",
                "tests/test_gateway_contract.py",
                "tests/test_frame_bridge.py",
                "tests/test_frame_bridge_runtime.py",
            ),
        ),
        CompositionRootSpec(
            surface="mcp",
            console_script="bh-mcp",
            module="beadhive.bootstrap.mcp",
            callable="main",
            runtime_factory="beadhive.mcp:build_server",
            responsibility="composition-and-process-lifecycle-only",
            registration_drift_test=(
                "tests/test_mcp_catalog_projection.py::"
                "test_registration_plan_carries_the_explicit_composite_declaration"
            ),
            test_closure=(
                "tests/test_transport_composition_roots.py",
                "tests/test_mcp_catalog_projection.py",
                "tests/test_mcp.py",
                "tests/test_daemon_mcp_http.py",
            ),
        ),
        CompositionRootSpec(
            surface="operator-api",
            console_script="bh-host-daemon",
            module="beadhive.bootstrap.host",
            callable="main",
            runtime_factory="beadhive.host_daemon:build_product_application",
            responsibility="composition-and-process-lifecycle-only",
            registration_drift_test=(
                "tests/test_transport_inventory.py::"
                "test_operator_inventory_matches_runtime_routes_and_checked_openapi"
            ),
            test_closure=(
                "tests/test_transport_composition_roots.py",
                "tests/test_transport_inventory.py",
                "tests/test_daemon_openapi.py",
                "tests/test_host_daemon.py",
                "tests/test_daemon_state_broker.py",
            ),
        ),
    )


def registration_drift(
    surface: str,
    declared: frozenset[str],
    observed: frozenset[str],
) -> dict[str, Any]:
    """Describe both directions of drift between declarations and a live runtime surface."""

    additions = sorted(observed - declared)
    removals = sorted(declared - observed)
    return {
        "surface": surface,
        "declared": sorted(declared),
        "observed": sorted(observed),
        "declared_count": len(declared),
        "observed_count": len(observed),
        "additions": additions,
        "removals": removals,
        "delta": len(additions) + len(removals),
        "matches": not additions and not removals,
    }


def validate_registration_drift(evidence: dict[str, Any]) -> None:
    """Fail closed on either an undeclared runtime registration or a missing declaration."""

    if not evidence["matches"]:
        raise RuntimeError(
            f"{evidence['surface']} registration drift: "
            f"additions={evidence['additions']!r}, removals={evidence['removals']!r}"
        )


def _effect(operation: OperationSpec) -> str:
    return "none" if operation.kind == "read-resource" else "application-mutation"


@cache
def _catalog_index() -> dict[str, OperationSpec]:
    return {row.name: row for row in operations()}


def _canonical_contracts(
    operation: str | None, composes: tuple[str, ...]
) -> tuple[CanonicalOperationContract, ...]:
    names = (operation,) if operation is not None else composes
    catalog = _catalog_index()
    return tuple(
        CanonicalOperationContract(
            operation=name,
            request_schema=f"catalog:{name}#parameters",
            result_schema=catalog[name].result_schema,
            privilege=catalog[name].privilege,
            side_effects=_effect(catalog[name]),
        )
        for name in names
    )


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
                    canonical_contracts=(),
                    transport_owner="cli-projection",
                    shape="exact",
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
                        canonical_contracts=(),
                        transport_owner="cli-projection",
                        shape="exact",
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
                    canonical_contracts=(),
                    transport_owner="mcp-projection",
                    shape="coarser" if composes else "exact",
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
                    canonical_contracts=(),
                    transport_owner="mcp-projection",
                    shape="exact",
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
    transport_owner: str,
    shape: ProjectionShape,
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
        canonical_contracts=_canonical_contracts(operation, composes),
        transport_owner=transport_owner,
        shape=shape,
    )


_OPERATOR_OPENAPI = "openapi:beadhive-host-openapi-v1.json"
_OPERATOR_AVAILABILITY = "loopback host daemon; CLI and stdio remain daemon-independent"
_OPERATOR_COMPATIBILITY = "checked OpenAPI 3.1 operation and runtime route must remain identical"

# q0lol's RouteSpec manifest remains authoritative for methods, paths, auth scopes, and wire
# models.  This overlay owns only the catalog relationship and projection shape.  Requiring an
# exact key match makes a daemon route addition fail closed until Transport classifies it.
_OPERATOR_APPLICATION_PROJECTIONS: dict[tuple[str, str], dict[str, Any]] = {
    ("GET", "/health"): {
        "classification": "transport-mechanic",
        "shape": "transport-only",
        "reason": "daemon liveness/readiness mechanic",
    },
    ("GET", "/api/v1/factory"): {
        "classification": "composite",
        "composes": ("hive.list", "host.list"),
        "shape": "coarser",
        "reason": "read-only directory composite over canonical hive and host inventories",
    },
    ("GET", "/api/v1/factory/hives"): {
        "classification": "composite",
        "composes": ("hive.list", "hive.status"),
        "shape": "coarser",
        "reason": "bounded hive summary composite over canonical hive reads",
    },
    ("GET", "/api/v1/hives/{hive_id}/snapshot"): {
        "classification": "composite",
        "composes": ("work.list", "work.schedule"),
        "shape": "coarser",
        "reason": "read-only state composite; source projection policy remains authoritative",
    },
    ("GET", "/api/v1/hives/{hive_id}/work-items"): {
        "classification": "catalog-entry",
        "operation": "work.list",
        "shape": "richer",
        "reason": "richer bounded projection of the canonical work list read",
    },
    ("GET", "/api/v1/hives/{hive_id}/work-items/{bead_id}"): {
        "classification": "catalog-entry",
        "operation": "work.issue",
        "shape": "richer",
        "reason": "richer exact-identity projection of the canonical work issue read",
    },
    ("GET", "/api/v1/hives/{hive_id}/events"): {
        "classification": "transport-mechanic",
        "shape": "transport-only",
        "streaming": True,
        "availability": "optional host-daemon SSE route",
        "reason": "SSE cursor/replay/backpressure is transport policy owned by bh-q0lol",
    },
    ("GET", "/api/v1/runs/{run_id}/activity"): {
        "classification": "explicit-exclusion",
        "shape": "excluded",
        "reason": "run-journal read has no canonical application operation",
    },
    ("POST", "/api/v1/runs/{run_id}/activity"): {
        "classification": "explicit-exclusion",
        "shape": "excluded",
        "side_effects": "durable-activity-append",
        "reason": "authenticated activity publication has no canonical application operation",
    },
    ("POST", "/api/v1/terminal/attach-token"): {
        "classification": "transport-mechanic",
        "shape": "transport-only",
        "availability": "typed unavailable response pending the terminal implementation replan",
        "reason": "terminal session and availability policy is owned by bh-q0lol",
    },
    ("WEBSOCKET", "/ws/terminal"): {
        "classification": "transport-mechanic",
        "shape": "transport-only",
        "streaming": True,
        "availability": "typed unavailable response pending the terminal implementation replan",
        "reason": "terminal WebSocket/session policy is owned by bh-q0lol",
    },
    ("GET", "/openapi.json"): {
        "classification": "transport-mechanic",
        "shape": "transport-only",
        "reason": "transport contract discovery, not an application operation",
    },
}


def _openapi_operation_reference(method: str, path: str) -> str:
    pointer_path = path.replace("~", "~0").replace("/", "~1")
    operation_key = "x-beadhive-websocket" if method == "WEBSOCKET" else method.lower()
    return f"{_OPERATOR_OPENAPI}#/paths/{pointer_path}/{operation_key}"


def _operator_runtime_path(path: str) -> str:
    return path.replace("{hive_id}", "{hive_id:path}")


def _operator_projections() -> tuple[ProjectionSpec, ...]:
    manifest = {(route.method, route.path): route for route in daemon_contract.NON_MCP_ROUTES}
    if set(_OPERATOR_APPLICATION_PROJECTIONS) != set(manifest):
        missing = sorted(set(manifest) - set(_OPERATOR_APPLICATION_PROJECTIONS))
        extra = sorted(set(_OPERATOR_APPLICATION_PROJECTIONS) - set(manifest))
        raise ValueError(f"operator projection overlay drift (missing={missing}, extra={extra})")

    rows: list[ProjectionSpec] = []
    for key, route in manifest.items():
        semantics = _OPERATOR_APPLICATION_PROJECTIONS[key]
        reference = _openapi_operation_reference(*key)
        rows.append(
            _http(
                "operator-api",
                route.method,
                _operator_runtime_path(route.path),
                semantics["classification"],
                operation=semantics.get("operation"),
                composes=semantics.get("composes", ()),
                request_schema="none" if route.path == "/openapi.json" else reference,
                result_schema=(
                    _OPERATOR_OPENAPI if route.path == "/openapi.json" else f"{reference}/responses"
                ),
                privilege=route.scope.value if route.scope is not None else "public-liveness",
                side_effects=semantics.get("side_effects", "none"),
                streaming=semantics.get("streaming", False),
                availability=semantics.get("availability", _OPERATOR_AVAILABILITY),
                compatibility=_OPERATOR_COMPATIBILITY,
                reason=semantics["reason"],
                transport_owner="bh-q0lol",
                shape=semantics["shape"],
            )
        )
    rows.append(
        _http(
            "operator-api",
            "OPTIONS",
            "*",
            "transport-mechanic",
            request_schema=f"{_OPERATOR_OPENAPI}#/x-beadhive-secure-network-preflight",
            result_schema=f"{_OPERATOR_OPENAPI}#/x-beadhive-secure-network-preflight",
            privilege="network-admission",
            availability=_OPERATOR_AVAILABILITY,
            compatibility=_OPERATOR_COMPATIBILITY,
            reason="wildcard CORS preflight/auth admission is owned by bh-q0lol",
            transport_owner="bh-q0lol",
            shape="transport-only",
        )
    )
    return tuple(rows)


def operator_projection(method: str, path: str) -> ProjectionSpec:
    """Return catalog semantics for one q0lol-owned manifest route."""

    identifier = f"{method} {_operator_runtime_path(path)}"
    try:
        return next(row for row in _operator_projections() if row.identifier == identifier)
    except StopIteration as exc:  # pragma: no cover - guarded by the exact overlay gate
        raise ValueError(f"unclassified operator route: {method} {path}") from exc


def catalog_projection_extension(row: ProjectionSpec) -> dict[str, Any]:
    """Render the transport-neutral catalog metadata embedded in wire contracts."""

    return {
        "classification": row.classification,
        "shape": row.shape,
        "canonicalContracts": [
            {
                "operation": contract.operation,
                "requestSchema": contract.request_schema,
                "resultSchema": contract.result_schema,
                "privilege": contract.privilege,
                "sideEffects": contract.side_effects,
            }
            for contract in row.canonical_contracts
        ],
        "transportOwner": row.transport_owner,
        "transportPrivilege": row.privilege,
        "sideEffects": row.side_effects,
        "reason": row.reason,
    }


_GATEWAY_AVAILABILITY = "Development Frame Bridge only; local CLI/stdio remain independent"
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
    privilege: str | None = None,
    wire_family: WireFamily,
    wire_request_schema: str,
    wire_result_schema: str,
    reason: str,
    shape: ProjectionShape,
) -> ProjectionSpec:
    return _http(
        "gateway",
        method,
        path,
        classification,
        operation=operation,
        composes=composes,
        request_schema=schema_ref(wire_family, wire_request_schema),
        result_schema=schema_ref(wire_family, wire_result_schema),
        privilege=(
            privilege
            or ("authenticated-development-subject" if path != "/healthz" else "public-liveness")
        ),
        side_effects=side_effects,
        streaming=streaming,
        availability=_GATEWAY_AVAILABILITY,
        compatibility=_GATEWAY_COMPATIBILITY,
        reason=reason,
        transport_owner="gateway-contract",
        shape=shape,
    )


def _gateway_projections() -> tuple[ProjectionSpec, ...]:
    """Build fresh refs so gateway-owned version/schema drift changes the inventory."""

    functional = (
        _gateway(
            "GET",
            "/healthz",
            "transport-mechanic",
            wire_family="gateway.v1",
            wire_request_schema="emptyRequest",
            wire_result_schema="healthResponse",
            reason="gateway liveness mechanic",
            shape="transport-only",
        ),
        _gateway(
            "GET",
            "/v1/instances",
            "composite",
            composes=("hive.list", "host.list"),
            wire_family="gateway.v1",
            wire_request_schema="instancesRequest",
            wire_result_schema="instancesResponse",
            reason="authorized remote directory composite",
            shape="coarser",
        ),
        _gateway(
            "GET",
            "/v1/instances/{stage}/{slug}/experience",
            "composite",
            composes=("work.list", "work.schedule"),
            wire_family="gateway.experience.v1",
            wire_request_schema="experienceRequest",
            wire_result_schema="experienceResponse",
            reason="one coherent server-selected generated Development experience",
            shape="coarser",
        ),
        _gateway(
            "GET",
            "/v1/factories/{factory_id}",
            "composite",
            composes=("hive.list",),
            wire_family="gateway.read.v1",
            wire_request_schema="hiveListRequest",
            wire_result_schema="factoryOverviewResponse",
            reason="export-gated canonical factory overview candidate",
            shape="coarser",
        ),
        _gateway(
            "GET",
            "/v1/factories/{factory_id}/hives",
            "catalog-entry",
            operation="hive.list",
            wire_family="gateway.read.v1",
            wire_request_schema="hiveListRequest",
            wire_result_schema="canonicalHiveListResponse",
            reason="export-gated canonical factory-qualified hive directory alias",
            shape="richer",
        ),
        _gateway(
            "GET",
            "/v1/factories/{factory_id}/hives/{hive_id:path}/snapshot",
            "composite",
            composes=("work.list", "work.schedule"),
            wire_family="gateway.read.v1",
            wire_request_schema="snapshotRequest",
            wire_result_schema="canonicalSnapshotResponse",
            reason="export-gated canonical factory-qualified hive snapshot alias",
            shape="coarser",
        ),
        _gateway(
            "GET",
            "/v1/factories/{factory_id}/hives/{hive_id:path}/events",
            "transport-mechanic",
            streaming=True,
            wire_family="gateway.read.v1",
            wire_request_schema="eventsRequest",
            wire_result_schema="eventStreamResponse",
            reason="export-gated canonical factory-qualified SSE alias",
            shape="transport-only",
        ),
        _gateway(
            "GET",
            "/v1/instances/{stage}/{slug}/hives",
            "catalog-entry",
            operation="hive.list",
            wire_family="gateway.read.v1",
            wire_request_schema="hiveListRequest",
            wire_result_schema="hiveListResponse",
            reason="richer remote projection of the canonical hive list",
            shape="richer",
        ),
        _gateway(
            "GET",
            "/v1/instances/{stage}/{slug}/hives/{hive_id:path}/snapshot",
            "composite",
            composes=("work.list", "work.schedule"),
            wire_family="gateway.read.v1",
            wire_request_schema="snapshotRequest",
            wire_result_schema="snapshotResponse",
            reason="bounded remote state composite",
            shape="coarser",
        ),
        _gateway(
            "GET",
            "/v1/instances/{stage}/{slug}/hives/{hive_id:path}/events",
            "transport-mechanic",
            streaming=True,
            wire_family="gateway.read.v1",
            wire_request_schema="eventsRequest",
            wire_result_schema="eventStreamResponse",
            reason="remote SSE cursor/replay mechanic",
            shape="transport-only",
        ),
        _gateway(
            "GET",
            "/v1/instances/{stage}/{slug}/snapshot",
            "composite",
            composes=("work.list", "work.schedule"),
            wire_family="gateway.v1",
            wire_request_schema="snapshotRequest",
            wire_result_schema="snapshotResponse",
            reason="legacy coarse snapshot composes canonical work reads behind gateway.v1",
            shape="coarser",
        ),
        _gateway(
            "GET",
            "/v1/instances/{stage}/{slug}/events",
            "transport-mechanic",
            streaming=True,
            wire_family="gateway.v1",
            wire_request_schema="eventsRequest",
            wire_result_schema="eventStreamResponse",
            reason="legacy coarse SSE compatibility mechanic",
            shape="transport-only",
        ),
        _gateway(
            "POST",
            "/v1/instances/{stage}/{slug}/commands/refresh",
            "explicit-exclusion",
            side_effects="bounded-runtime-refresh",
            wire_family="gateway.v1",
            wire_request_schema="refreshRequest",
            wire_result_schema="commandResponse",
            reason="runtime refresh is not canonical sync and has no canonical operation",
            shape="excluded",
        ),
        _gateway(
            "POST",
            "/v1/instances/{stage}/{slug}/commands/{command}",
            "transport-mechanic",
            wire_family="gateway.v1",
            wire_request_schema="unavailableCommandRequest",
            wire_result_schema="errorResponse",
            reason="stable unavailable-command response prevents undeclared mutation exposure",
            shape="transport-only",
        ),
    )
    read_options_paths = tuple(
        spec.identifier.removeprefix("GET ")
        for spec in functional
        if spec.identifier.startswith("GET ") and spec.identifier != "GET /healthz"
    )
    mechanics = tuple(
        _gateway(
            "OPTIONS",
            path,
            "transport-mechanic",
            privilege="network-admission",
            wire_family="gateway.v1",
            wire_request_schema="readPreflightRequest",
            wire_result_schema="emptyResponse",
            reason="exact credentialed CORS preflight allowlist",
            shape="transport-only",
        )
        for path in read_options_paths
    ) + (
        _gateway(
            "OPTIONS",
            "/v1/instances/{stage}/{slug}/commands/refresh",
            "transport-mechanic",
            privilege="network-admission",
            wire_family="gateway.v1",
            wire_request_schema="commandPreflightRequest",
            wire_result_schema="emptyResponse",
            reason="exact credentialed command CORS preflight allowlist",
            shape="transport-only",
        ),
        _gateway(
            "GET",
            "/{path:path}",
            "transport-mechanic",
            wire_family="gateway.v1",
            wire_request_schema="fallbackRequest",
            wire_result_schema="errorResponse",
            reason="closed-world not-found fallback",
            shape="transport-only",
        ),
        _gateway(
            "OPTIONS",
            "/{path:path}",
            "transport-mechanic",
            privilege="network-admission",
            wire_family="gateway.v1",
            wire_request_schema="fallbackRequest",
            wire_result_schema="errorResponse",
            reason="closed-world preflight fallback",
            shape="transport-only",
        ),
    )
    return functional + mechanics


def projections() -> tuple[ProjectionSpec, ...]:
    """Return the complete inventory in stable surface/identifier order."""
    rows = [
        *_catalog_projections(),
        *_operator_projections(),
        *_gateway_projections(),
    ]
    return tuple(sorted(rows, key=lambda row: (row.surface, row.identifier)))


def document() -> dict[str, Any]:
    """Return the checked, language-neutral transport inventory document."""
    rows = []
    for row in projections():
        rendered = asdict(row)
        rendered["composes"] = list(row.composes)
        rendered["canonical_contracts"] = [asdict(contract) for contract in row.canonical_contracts]
        rows.append(rendered)
    return {
        "format_version": 1,
        "inventory_version": "1.5.0",
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
        "composition_roots": [
            {**asdict(root), "test_closure": list(root.test_closure)}
            for root in composition_roots()
        ],
        "projections": rows,
    }
