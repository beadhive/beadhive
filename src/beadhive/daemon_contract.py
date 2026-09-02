"""Product-owned v1 wire contracts and exact daemon identity resolution.

MCP is intentionally absent: FastMCP discovery remains authoritative for that surface.  This
module owns only the versioned REST/SSE/WebSocket boundary ratified by the unified-host ADR.
"""

from __future__ import annotations

import hmac
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal
from urllib.parse import quote, unquote_to_bytes

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from . import registry

CONTRACT_VERSION = "beadhive.host-daemon/v1"
WIRE_SCHEMA_VERSION = 1
TERMINAL_PROTOCOL = "bh-terminal.v1"
_UNRESERVED = "-._~"
_PERCENT = re.compile(r"%[0-9A-Fa-f]{2}")
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
_OPAQUE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")


def _camel(name: str) -> str:
    head, *tail = name.split("_")
    return head + "".join(part.capitalize() for part in tail)


class WireModel(BaseModel):
    """Strict immutable model with the UI's lower-camel JSON spelling."""

    model_config = ConfigDict(
        alias_generator=_camel,
        populate_by_name=True,
        extra="forbid",
        frozen=True,
    )

    def to_wire(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)


class AuthScope(StrEnum):
    OPERATOR_READ = "operator:read"
    ACTIVITY_PUBLISH = "activity:publish"
    TERMINAL_ATTACH = "terminal:attach"


@dataclass(frozen=True)
class RouteSpec:
    method: str
    path: str
    scope: AuthScope | None
    statuses: tuple[int, ...]
    response_model: type[WireModel] | None = None


class IdentityErrorCode(StrEnum):
    MALFORMED = "identity_malformed"
    UNSAFE_ENCODING = "identity_unsafe_encoding"
    DOT_SEGMENT = "identity_dot_segment"
    UNKNOWN = "identity_unknown"
    AMBIGUOUS = "identity_ambiguous"
    PAYLOAD_MISMATCH = "run_id_payload_mismatch"
    CURSOR_MISMATCH = "cursor_mismatch"


class ExactIdentityError(ValueError):
    """Fail-closed identity error whose message never reflects attacker-controlled input."""

    def __init__(self, code: IdentityErrorCode):
        self.code = code
        super().__init__(code.value)


def _decode_canonical_segment(raw: str, *, safe: str = _UNRESERVED) -> str:
    """Decode one raw URL path segment exactly once and require canonical encoding.

    ASGI routing commonly exposes an already-decoded value.  Daemon handlers must extract the
    parameter from ``scope['raw_path']`` and call this function before flexible framework path
    conversion can erase the distinction between a separator and encoded data.
    """

    if not isinstance(raw, str) or not raw or len(raw) > 2_048 or "/" in raw or "\\" in raw:
        raise ExactIdentityError(IdentityErrorCode.MALFORMED)
    index = 0
    while index < len(raw):
        if raw[index] == "%":
            match = _PERCENT.match(raw, index)
            if match is None:
                raise ExactIdentityError(IdentityErrorCode.UNSAFE_ENCODING)
            index += 3
        else:
            index += 1
    try:
        decoded = unquote_to_bytes(raw).decode("utf-8", errors="strict")
    except (UnicodeDecodeError, ValueError) as exc:
        raise ExactIdentityError(IdentityErrorCode.UNSAFE_ENCODING) from exc
    if "%" in decoded or any(ord(char) < 0x20 or ord(char) == 0x7F for char in decoded):
        raise ExactIdentityError(IdentityErrorCode.UNSAFE_ENCODING)
    if quote(decoded, safe=safe) != raw:
        raise ExactIdentityError(IdentityErrorCode.UNSAFE_ENCODING)
    return decoded


def encode_hive_id(hive_id: str) -> str:
    """Encode one canonical provider/org/repository identity as a single path segment."""

    parts = hive_id.split("/") if isinstance(hive_id, str) else []
    if len(parts) != 3 or any(not part or part in {".", ".."} for part in parts):
        raise ExactIdentityError(IdentityErrorCode.MALFORMED)
    if any("%" in part or "\\" in part for part in parts):
        raise ExactIdentityError(IdentityErrorCode.UNSAFE_ENCODING)
    return quote(hive_id, safe=_UNRESERVED)


def decode_hive_id(raw_segment: str) -> str:
    hive_id = _decode_canonical_segment(raw_segment)
    parts = hive_id.split("/")
    if len(parts) != 3 or any(not part for part in parts):
        raise ExactIdentityError(IdentityErrorCode.MALFORMED)
    if any(part in {".", ".."} for part in parts):
        raise ExactIdentityError(IdentityErrorCode.DOT_SEGMENT)
    return hive_id


def resolve_exact_hive(cfg: Mapping[str, Any], raw_segment: str) -> Mapping[str, Any]:
    """Resolve only a full encoded registry triplet; prefixes and short IDs never participate."""

    hive_id = decode_hive_id(raw_segment)
    matches = [entry for entry in registry.hives(cfg) if registry.hive_key(entry) == hive_id]
    if not matches:
        raise ExactIdentityError(IdentityErrorCode.UNKNOWN)
    if len(matches) != 1:
        raise ExactIdentityError(IdentityErrorCode.AMBIGUOUS)
    return matches[0]


def encode_run_id(run_id: str) -> str:
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id) or run_id in {".", ".."}:
        raise ExactIdentityError(IdentityErrorCode.MALFORMED)
    return quote(run_id, safe=_UNRESERVED + ":")


def decode_run_id(raw_segment: str) -> str:
    run_id = _decode_canonical_segment(raw_segment, safe=_UNRESERVED + ":")
    if run_id in {".", ".."}:
        raise ExactIdentityError(IdentityErrorCode.DOT_SEGMENT)
    if not _RUN_ID.fullmatch(run_id):
        raise ExactIdentityError(IdentityErrorCode.MALFORMED)
    return run_id


def resolve_exact_run_id(raw_segment: str, known_run_ids: Iterable[str]) -> str:
    """Resolve an opaque outer run id by exact equality, never prefix or continuation."""

    run_id = decode_run_id(raw_segment)
    matches = [candidate for candidate in known_run_ids if candidate == run_id]
    if not matches:
        raise ExactIdentityError(IdentityErrorCode.UNKNOWN)
    if len(matches) != 1:
        raise ExactIdentityError(IdentityErrorCode.AMBIGUOUS)
    return matches[0]


class HealthStatus(StrEnum):
    LIVE = "live"
    STOPPING = "stopping"


class ReadinessStatus(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class HealthResponse(WireModel):
    """Public, deliberately minimal response.  No identity or dependency details."""

    schema_version: Literal[1] = WIRE_SCHEMA_VERSION
    status: HealthStatus
    ready: bool
    contract: str = CONTRACT_VERSION


class DependencyStatus(WireModel):
    name: Literal["hq", "dolt", "bead-state", "run-journals"]
    status: ReadinessStatus
    reason_code: str | None = None


class DaemonStatus(WireModel):
    readiness: ReadinessStatus
    accepting_work: bool
    started_at: int = Field(ge=0)
    dependencies: tuple[DependencyStatus, ...]


class HiveDescriptor(WireModel):
    hive_id: str
    encoded_hive_id: str
    provider: str
    org: str
    repo: str
    prefix: str
    kind: str
    readiness: ReadinessStatus

    @model_validator(mode="after")
    def _identity_matches(self) -> HiveDescriptor:
        expected = f"{self.provider}/{self.org}/{self.repo}"
        if self.hive_id != expected or self.encoded_hive_id != encode_hive_id(expected):
            raise ValueError("hive descriptor identity fields disagree")
        return self


class FactoryHost(WireModel):
    host_id: str
    service_instance_id: str


class FactoryRelationship(WireModel):
    from_hive_id: str
    to_hive_id: str | None
    kind: str


class FactoryAssignment(WireModel):
    hive_id: str
    host_id: str
    role: str
    state: Literal["held", "renewal-due", "expired", "unassigned", "unknown"]
    lease_expires_at: int | None = Field(default=None, ge=0)


class FactoryCapability(WireModel):
    name: Literal["snapshot", "events", "activity-read", "activity-publish", "terminal"]
    available: bool
    reason_code: str | None = None


class FactorySourceCoverage(WireModel):
    state: Literal["complete", "partial", "unavailable"]
    detail: str | None = None
    generated_at: int = Field(ge=0)


class FactoryCoverage(WireModel):
    state: Literal["complete", "partial", "unavailable"]
    generated_at: int = Field(ge=0)
    sources: dict[str, FactorySourceCoverage]


class FactoryResponse(WireModel):
    schema_version: Literal[1] = WIRE_SCHEMA_VERSION
    contract_version: str = CONTRACT_VERSION
    generated_at: int = Field(ge=0)
    host: FactoryHost
    status: DaemonStatus
    hives: tuple[HiveDescriptor, ...]
    relationships: tuple[FactoryRelationship, ...] = ()
    assignments: tuple[FactoryAssignment, ...] = ()
    capabilities: tuple[FactoryCapability, ...]
    coverage: FactoryCoverage


class AdvertisedActionTarget(WireModel):
    hive_id: str | None
    kind: Literal["hive", "work-item", "agent"]
    id: str = Field(min_length=1)


class AdvertisedActionPreconditions(WireModel):
    source_revision: str | None
    must_match: bool


class AdvertisedActionInput(WireModel):
    transport: Literal["none", "parameters", "stdin"]
    required: bool
    input_schema: dict[str, Any] | None = Field(alias="schema")


class AdvertisedAction(WireModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9-]*(\.[a-z][a-z0-9-]*)+$")
    capability: str = Field(min_length=1)
    target: AdvertisedActionTarget
    availability: Literal["allowed", "confirmation-required", "forbidden", "unavailable"]
    reason_code: str | None
    reason: str = Field(min_length=1)
    consequence: Literal["navigate", "read", "reversible-write", "approval", "destructive"]
    advertised_at: int = Field(ge=0)
    source_revision: str | None
    preconditions: AdvertisedActionPreconditions
    input: AdvertisedActionInput


class FactoryHiveAvailability(WireModel):
    state: Literal["available", "unavailable"]
    reason: str | None


class FactoryHiveCounts(WireModel):
    open: int | None = Field(ge=0)
    ready: int | None = Field(ge=0)
    active: int | None = Field(ge=0)
    blocked: int | None = Field(ge=0)


class FactoryHiveCoverage(WireModel):
    state: Literal["complete", "partial", "unavailable"]
    reason: str | None


class FactoryHiveSummary(WireModel):
    id: str = Field(pattern=r"^[A-Za-z0-9._~-]+/[A-Za-z0-9._~-]+/[A-Za-z0-9._~-]+$")
    display_label: str = Field(min_length=1)
    opaque_ref: str = Field(pattern=r"^hive-sha256-[a-f0-9]{64}$")
    prefix: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    org: str = Field(min_length=1)
    repo: str = Field(min_length=1)
    kind: str
    availability: FactoryHiveAvailability
    counts: FactoryHiveCounts
    revision: str | None
    as_of: int | None = Field(default=None, ge=0)
    coverage: FactoryHiveCoverage
    advertised_actions: tuple[AdvertisedAction, ...]

    @model_validator(mode="after")
    def _identity_matches(self) -> FactoryHiveSummary:
        if self.id != f"{self.provider}/{self.org}/{self.repo}":
            raise ValueError("factory hive summary identity fields disagree")
        return self


class FactoryHivePage(WireModel):
    schema_version: Literal[1] = WIRE_SCHEMA_VERSION
    revision: str = Field(min_length=1)
    generated_at: int = Field(ge=0)
    items: tuple[FactoryHiveSummary, ...] = Field(max_length=200)
    returned_count: int = Field(ge=0, le=200)
    limit: int = Field(ge=1, le=200)
    truncated: bool
    next_cursor: str | None = Field(default=None, min_length=1)
    warnings: tuple[str, ...]

    @model_validator(mode="after")
    def _page_is_consistent(self) -> FactoryHivePage:
        if self.returned_count != len(self.items) or self.returned_count > self.limit:
            raise ValueError("factory hive page counts disagree")
        if self.truncated != (self.next_cursor is not None):
            raise ValueError("factory hive page cursor and truncation disagree")
        return self


class StreamCursor(WireModel):
    subscription_id: str
    producer_epoch: str
    sequence: int = Field(ge=0)
    observed_at: int = Field(ge=0)

    @field_validator("subscription_id", "producer_epoch")
    @classmethod
    def _opaque_ids(cls, value: str) -> str:
        if not _OPAQUE_ID.fullmatch(value):
            raise ValueError("stream identity must be a path-independent opaque id")
        return value

    @property
    def event_id(self) -> str:
        return f"{self.producer_epoch}:{self.sequence}"


@dataclass(frozen=True)
class ReplayCursor:
    producer_epoch: str
    sequence: int

    @property
    def event_id(self) -> str:
        return f"{self.producer_epoch}:{self.sequence}"


def parse_event_cursor(value: str) -> ReplayCursor:
    """Parse the byte-identical SSE ``id``/reconnect cursor without guessing."""

    if not isinstance(value, str) or value.count(":") != 1:
        raise ExactIdentityError(IdentityErrorCode.MALFORMED)
    epoch, raw_sequence = value.split(":", 1)
    if not _OPAQUE_ID.fullmatch(epoch) or not raw_sequence.isascii() or not raw_sequence.isdigit():
        raise ExactIdentityError(IdentityErrorCode.MALFORMED)
    if len(raw_sequence) > 1 and raw_sequence.startswith("0"):
        raise ExactIdentityError(IdentityErrorCode.MALFORMED)
    return ReplayCursor(epoch, int(raw_sequence))


def resolve_reconnect_cursor(
    *, last_event_id: str | None, after: str | None
) -> ReplayCursor | None:
    """Apply the ADR's Last-Event-ID/after equality rule before replay lookup."""

    if (
        last_event_id is not None
        and after is not None
        and not hmac.compare_digest(last_event_id, after)
    ):
        raise ExactIdentityError(IdentityErrorCode.CURSOR_MISMATCH)
    value = last_event_id if last_event_id is not None else after
    return parse_event_cursor(value) if value is not None else None


class SourceProvenance(WireModel):
    system: str
    instance: str | None = None
    run_id: str | None = None
    document_ref: str | None = None


class OperatorSourceCoverage(WireModel):
    state: Literal["complete", "partial", "unavailable"]
    requested: int | None = Field(default=None, ge=0)
    returned: int | None = Field(default=None, ge=0)
    from_cache: int = Field(0, ge=0)
    detail: str | None = None
    generated_at: int = Field(ge=0)
    provenance: SourceProvenance


class OperatorCoverage(WireModel):
    state: Literal["complete", "partial", "unavailable"]
    generated_at: int = Field(ge=0)
    sources: dict[str, OperatorSourceCoverage]


class HiveInfo(WireModel):
    prefix: str
    provider: str
    org: str
    repo: str
    kind: str

    @property
    def hive_id(self) -> str:
        return f"{self.provider}/{self.org}/{self.repo}"


class HiveSnapshotResponse(WireModel):
    """Top-level UI snapshot envelope; entity bodies retain the shared UI contract."""

    schema_version: Literal[1] = WIRE_SCHEMA_VERSION
    hive: HiveInfo
    revision: str
    generated_at: int = Field(ge=0)
    cursor: StreamCursor | None
    coverage: OperatorCoverage
    work_items: tuple[dict[str, Any], ...] = ()
    dependencies: tuple[dict[str, Any], ...] = ()
    epics: tuple[dict[str, Any], ...] = ()
    gates: tuple[dict[str, Any], ...] = ()
    agents: tuple[dict[str, Any], ...] = ()
    assignments: tuple[dict[str, Any], ...] = ()
    schedules: tuple[dict[str, Any], ...] = ()
    evidence: tuple[dict[str, Any], ...] = ()
    advertised_actions: tuple[dict[str, Any], ...] = ()


class EntityUpsertPayload(WireModel):
    kind: Literal["entity-upsert"]
    entity: dict[str, Any]


class EntityRemovePayload(WireModel):
    kind: Literal["entity-remove"]
    entity: dict[str, Any]
    revision: str


class InvalidatePayload(WireModel):
    kind: Literal["invalidate"]
    scopes: tuple[str, ...]
    reason: str


class ResetPayload(WireModel):
    kind: Literal["reset"]
    reason: str


class HeartbeatPayload(WireModel):
    kind: Literal["heartbeat"]


OperatorEventPayload = (
    EntityUpsertPayload | EntityRemovePayload | InvalidatePayload | ResetPayload | HeartbeatPayload
)


class OperatorEvent(WireModel):
    schema_version: Literal[1] = WIRE_SCHEMA_VERSION
    hive_id: str
    subscription_id: str
    producer_epoch: str
    sequence: int = Field(ge=1)
    base_sequence: int | None = Field(default=None, ge=0)
    observed_at: int = Field(ge=0)
    generated_at: int = Field(ge=0)
    source: Literal["beads", "runtime", "git", "mcp", "supervisor"]
    revision: str
    entity: dict[str, Any] | None
    payload: OperatorEventPayload = Field(discriminator="kind")

    @field_validator("subscription_id", "producer_epoch")
    @classmethod
    def _stream_ids(cls, value: str) -> str:
        if not _OPAQUE_ID.fullmatch(value):
            raise ValueError("stream identity must be a path-independent opaque id")
        return value

    @model_validator(mode="after")
    def _strict_sequence(self) -> OperatorEvent:
        if self.sequence == 1:
            if self.base_sequence is not None:
                raise ValueError("the first event in an epoch has no base sequence")
        elif self.base_sequence != self.sequence - 1:
            raise ValueError("baseSequence must be the immediately preceding sequence")
        if self.payload.kind == "reset" and self.sequence != 1:
            raise ValueError("reset must be the first event in a fresh producer epoch")
        return self

    @property
    def event_id(self) -> str:
        return f"{self.producer_epoch}:{self.sequence}"


def encode_sse_event(event: OperatorEvent) -> bytes:
    """Serialize the sole v1 SSE event shape with cursor and payload tied together."""

    payload = json.dumps(event.to_wire(), ensure_ascii=False, separators=(",", ":"))
    return f"id: {event.event_id}\nevent: operator-event\ndata: {payload}\n\n".encode()


class ResnapshotInstruction(WireModel):
    action: Literal["resnapshot"] = "resnapshot"
    hive_id: str
    reason: Literal["unknown_epoch", "expired_cursor", "future_sequence", "retention_gap"]


class ActivityRecord(WireModel):
    activity_id: str
    run_id: str
    idempotency_key: str
    source: str
    kind: str
    occurred_at: int = Field(ge=0)
    appended_at: int = Field(ge=0)
    revision: str
    payload: dict[str, Any]


class ActivityViewResponse(WireModel):
    schema_version: Literal[1] = WIRE_SCHEMA_VERSION
    run_id: str
    revision: str
    activities: tuple[ActivityRecord, ...]


class ActivityAppendRequest(WireModel):
    schema_version: Literal[1] = WIRE_SCHEMA_VERSION
    run_id: str
    idempotency_key: str
    source: str = Field(min_length=1, max_length=128)
    kind: str = Field(min_length=1, max_length=128)
    occurred_at: int = Field(ge=0)
    expires_at: int = Field(ge=0)
    payload: dict[str, Any]

    @field_validator("run_id")
    @classmethod
    def _exact_outer_run(cls, value: str) -> str:
        encode_run_id(value)
        return value

    @field_validator("idempotency_key")
    @classmethod
    def _stable_key(cls, value: str) -> str:
        if not _IDEMPOTENCY_KEY.fullmatch(value):
            raise ValueError("idempotencyKey must be a stable opaque token")
        return value

    @model_validator(mode="after")
    def _bounded_expiry(self) -> ActivityAppendRequest:
        if self.expires_at < self.occurred_at:
            raise ValueError("activity expiry may not precede occurrence")
        try:
            json.dumps(self.payload, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("activity payload must be finite JSON") from exc
        return self


def resolve_activity_run_id(
    raw_segment: str, payload: ActivityAppendRequest, known_run_ids: Iterable[str]
) -> str:
    path_run_id = resolve_exact_run_id(raw_segment, known_run_ids)
    if not hmac.compare_digest(path_run_id, payload.run_id):
        raise ExactIdentityError(IdentityErrorCode.PAYLOAD_MISMATCH)
    return path_run_id


class ActivityAppendResponse(WireModel):
    schema_version: Literal[1] = WIRE_SCHEMA_VERSION
    status: Literal["created", "duplicate"]
    run_id: str
    idempotency_key: str
    activity_id: str
    revision: str


class TerminalAttachTokenResponse(WireModel):
    schema_version: Literal[1] = WIRE_SCHEMA_VERSION
    attach_token: str
    protocol: Literal["bh-terminal.v1"] = TERMINAL_PROTOCOL
    host_id: str
    principal: str
    target: str
    audience: Literal["bh-terminal.v1"] = TERMINAL_PROTOCOL
    expires_at: int = Field(ge=0)
    single_use: Literal[True] = True


class TerminalUnavailable(WireModel):
    schema_version: Literal[1] = WIRE_SCHEMA_VERSION
    type: Literal["terminal.unavailable"] = "terminal.unavailable"
    code: Literal["pty_verdict_pending"] = "pty_verdict_pending"
    message: Literal["Terminal attachment is not available on this host."] = (
        "Terminal attachment is not available on this host."
    )
    retryable: Literal[False] = False
    verdict_bead: Literal["bh-lx6e.3"] = "bh-lx6e.3"


class ErrorCode(StrEnum):
    BAD_REQUEST = "bad_request"
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    RATE_LIMITED = "rate_limited"
    UNAVAILABLE = "unavailable"
    INTERNAL = "internal"


_PUBLIC_ERRORS: dict[ErrorCode, tuple[str, bool]] = {
    ErrorCode.BAD_REQUEST: ("The request is invalid.", False),
    ErrorCode.UNAUTHORIZED: ("Authentication is required.", False),
    ErrorCode.FORBIDDEN: ("The credential does not grant this operation.", False),
    ErrorCode.NOT_FOUND: ("The requested resource was not found.", False),
    ErrorCode.CONFLICT: ("The request conflicts with current authoritative state.", True),
    ErrorCode.RATE_LIMITED: ("The request limit was exceeded.", True),
    ErrorCode.UNAVAILABLE: ("The service is temporarily unavailable.", True),
    ErrorCode.INTERNAL: ("The request could not be completed.", True),
}


class ErrorDetail(WireModel):
    code: ErrorCode
    message: str
    retryable: bool
    action: Literal["retry", "resnapshot", "reauthenticate"] | None = None
    request_id: str | None = None


class ErrorResponse(WireModel):
    schema_version: Literal[1] = WIRE_SCHEMA_VERSION
    error: ErrorDetail


def redacted_error(
    code: ErrorCode,
    *,
    action: Literal["retry", "resnapshot", "reauthenticate"] | None = None,
    request_id: str | None = None,
    cause: BaseException | None = None,
) -> ErrorResponse:
    """Build a fixed public error; ``cause`` is deliberately ignored and never serialized."""

    del cause
    message, retryable = _PUBLIC_ERRORS[code]
    return ErrorResponse(
        error=ErrorDetail(
            code=code,
            message=message,
            retryable=retryable,
            action=action,
            request_id=request_id,
        )
    )


NON_MCP_ROUTES: tuple[RouteSpec, ...] = (
    RouteSpec("GET", "/health", None, (200, 503), HealthResponse),
    RouteSpec(
        "GET", "/api/v1/factory", AuthScope.OPERATOR_READ, (200, 401, 403, 503), FactoryResponse
    ),
    RouteSpec(
        "GET",
        "/api/v1/factory/hives",
        AuthScope.OPERATOR_READ,
        (200, 304, 400, 401, 403, 409, 503),
        FactoryHivePage,
    ),
    RouteSpec(
        "GET",
        "/api/v1/hives/{hive_id}/snapshot",
        AuthScope.OPERATOR_READ,
        (200, 400, 401, 403, 404, 503),
        HiveSnapshotResponse,
    ),
    RouteSpec(
        "GET",
        "/api/v1/hives/{hive_id}/events",
        AuthScope.OPERATOR_READ,
        (200, 400, 401, 403, 404, 409, 429, 503),
        None,
    ),
    RouteSpec(
        "GET",
        "/api/v1/runs/{run_id}/activity",
        AuthScope.OPERATOR_READ,
        (200, 400, 401, 403, 404, 503),
        ActivityViewResponse,
    ),
    RouteSpec(
        "POST",
        "/api/v1/runs/{run_id}/activity",
        AuthScope.ACTIVITY_PUBLISH,
        (200, 201, 400, 401, 403, 404, 409, 413, 429, 503),
        ActivityAppendResponse,
    ),
    RouteSpec(
        "POST",
        "/api/v1/terminal/attach-token",
        AuthScope.TERMINAL_ATTACH,
        (201, 400, 401, 403, 409, 429, 503),
        TerminalAttachTokenResponse,
    ),
    RouteSpec(
        "WEBSOCKET",
        "/ws/terminal",
        AuthScope.TERMINAL_ATTACH,
        (101, 400, 401, 403, 409, 429, 503),
        TerminalUnavailable,
    ),
    RouteSpec("GET", "/openapi.json", AuthScope.OPERATOR_READ, (200, 401, 403, 503), None),
)


WIRE_MODELS: tuple[type[WireModel], ...] = (
    HealthResponse,
    FactoryResponse,
    FactoryHivePage,
    HiveSnapshotResponse,
    OperatorEvent,
    ResnapshotInstruction,
    ActivityViewResponse,
    ActivityAppendRequest,
    ActivityAppendResponse,
    TerminalAttachTokenResponse,
    TerminalUnavailable,
    ErrorResponse,
)
