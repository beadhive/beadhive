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
from typing import Annotated, Any, Literal
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
    MCP_CONTROL = "mcp:control"
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
    request_model: type[WireModel] | None = None
    request_headers: tuple[str, ...] = ()


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
    sequence: int = Field(strict=True, ge=0, le=2**53 - 1)
    observed_at: int = Field(strict=True, ge=0, le=2**53 - 1)

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
    requested: int | None = Field(default=None, strict=True, ge=0, le=2**53 - 1)
    returned: int | None = Field(default=None, strict=True, ge=0, le=2**53 - 1)
    from_cache: int = Field(0, strict=True, ge=0, le=2**53 - 1)
    detail: str | None = None
    generated_at: int = Field(strict=True, ge=0, le=2**53 - 1)
    provenance: SourceProvenance


class SnapshotProjectionLimits(WireModel):
    max_bytes: Literal[917_504]
    max_work_items: Literal[4_096]


class WorkItemRetrievalCapability(WireModel):
    contract: Literal["beadhive.work-items/v1"]
    revision: str = Field(min_length=1, max_length=256)
    views: tuple[Literal["ready", "active", "blocked", "recent"], ...] = Field(
        min_length=4, max_length=4
    )
    max_page_items: Literal[200]
    max_page_bytes: Literal[917_504]
    max_detail_bytes: Literal[917_504]


class OperatorCoverage(WireModel):
    state: Literal["complete", "partial"]
    generated_at: int = Field(strict=True, ge=0, le=2**53 - 1)
    eligible: int = Field(strict=True, ge=0, le=2**53 - 1)
    returned: int = Field(strict=True, ge=0, le=4_096)
    reason: Literal["byte_budget", "structural_cap"] | None
    policy: Literal["beadhive.snapshot-summary/v1"]
    source_revision: str = Field(min_length=1)
    limits: SnapshotProjectionLimits
    sources: dict[str, OperatorSourceCoverage]

    @model_validator(mode="after")
    def _selection_is_truthful(self) -> OperatorCoverage:
        if self.returned > self.eligible:
            raise ValueError("snapshot coverage returned exceeds eligible")
        is_partial = self.returned < self.eligible
        if (self.state == "partial") != is_partial:
            raise ValueError("snapshot coverage state disagrees with counts")
        if is_partial != (self.reason is not None):
            raise ValueError("snapshot coverage reason disagrees with counts")
        if self.reason == "structural_cap" and self.returned != self.limits.max_work_items:
            raise ValueError("structural-cap coverage must return the structural limit")
        return self


class RemoteOperatorCoverage(OperatorCoverage):
    work_item_retrieval: WorkItemRetrievalCapability

    @model_validator(mode="after")
    def _retrieval_is_revision_pinned(self) -> RemoteOperatorCoverage:
        if self.work_item_retrieval.revision != self.source_revision:
            raise ValueError("work-item retrieval revision must match snapshot source revision")
        if self.work_item_retrieval.views != (
            "ready",
            "active",
            "blocked",
            "recent",
        ):
            raise ValueError("work-item retrieval views must use canonical order")
        return self


class HiveInfo(WireModel):
    prefix: str
    provider: str
    org: str
    repo: str
    kind: str

    @property
    def hive_id(self) -> str:
        return f"{self.provider}/{self.org}/{self.repo}"


SnapshotLabel = Annotated[str, Field(min_length=1, max_length=256)]


class SnapshotWorkItemSummary(WireModel):
    id: str = Field(min_length=1, max_length=256)
    title: str = Field(max_length=4_096)
    status: Literal["open", "in_progress", "blocked"]
    readiness: Literal["ready", "active", "blocked"]
    issue_type: str = Field(min_length=1, max_length=128)
    priority: int = Field(strict=True, ge=0, le=4)
    labels: tuple[SnapshotLabel, ...] = Field(max_length=12)
    remaining_label_count: int = Field(strict=True, ge=0, le=2**53 - 1)
    assignee: str | None = Field(default=None, max_length=256)
    owner: str | None = Field(default=None, max_length=256)
    updated_at: int = Field(strict=True, ge=0, le=2**53 - 1)
    blocker_count: int = Field(strict=True, ge=0, le=2**53 - 1)
    open_gate_count: int = Field(strict=True, ge=0, le=2**53 - 1)
    live_agent_count: int = Field(strict=True, ge=0, le=2**53 - 1)


class HiveSnapshotResponse(WireModel):
    """Compact, byte-first seed snapshot for one exact hive."""

    schema_version: Literal[1] = WIRE_SCHEMA_VERSION
    hive: HiveInfo
    revision: str
    generated_at: int = Field(strict=True, ge=0, le=2**53 - 1)
    cursor: StreamCursor | None
    projection_policy: Literal["beadhive.snapshot-summary/v1"]
    limits: SnapshotProjectionLimits
    coverage: OperatorCoverage
    work_items: tuple[SnapshotWorkItemSummary, ...] = Field(max_length=4_096)

    @model_validator(mode="after")
    def _projection_is_consistent(self) -> HiveSnapshotResponse:
        if self.projection_policy != self.coverage.policy:
            raise ValueError("snapshot projection policy fields disagree")
        if self.limits != self.coverage.limits:
            raise ValueError("snapshot projection limit fields disagree")
        if self.revision != self.coverage.source_revision:
            raise ValueError("snapshot coverage source revision disagrees")
        if len(self.work_items) != self.coverage.returned:
            raise ValueError("snapshot work-item count disagrees with coverage")
        return self


class RemoteHiveSnapshotResponse(HiveSnapshotResponse):
    coverage: RemoteOperatorCoverage


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


class ActivityEventPayload(WireModel):
    kind: Literal["activity"]
    run_id: str
    activity: dict[str, Any]


class ActivityResetPayload(WireModel):
    kind: Literal["activity-reset"]
    run_id: str
    producer_epoch: str
    reason: str


OperatorEventPayload = (
    InvalidatePayload
    | ResetPayload
    | HeartbeatPayload
    | ActivityEventPayload
    | ActivityResetPayload
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


class EventResnapshotResponse(WireModel):
    """The exact short error body emitted before an SSE client resnapshots."""

    error: str = Field(min_length=1)
    action: Literal["resnapshot"] = "resnapshot"


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
    run_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
    source: Literal["baml", "hitch", "beadhive"]
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


class ProjectionFreshness(WireModel):
    state: Literal["fresh"]
    as_of: int | None = Field(ge=0)


class ProjectionSourceCoverage(WireModel):
    state: str
    detail: str | None


class ProjectionCoverage(WireModel):
    state: Literal["complete", "partial", "unavailable"]
    sources: dict[str, ProjectionSourceCoverage]


class WorkItemRef(WireModel):
    hive_id: str = Field(min_length=1)
    kind: Literal["work-item"]
    id: str = Field(min_length=1)


class WorkItemReadiness(WireModel):
    state: Literal["ready", "active", "blocked", "completed", "unavailable"]
    reason: str


class WorkItemRow(WireModel):
    ref: WorkItemRef
    revision: str = Field(min_length=1)
    hive_id: str = Field(min_length=1)
    id: str = Field(min_length=1)
    title: str
    issue_type: str
    priority: int
    status: str
    readiness: WorkItemReadiness
    assignee: str | None
    owner: str | None
    parent_id: str | None
    blocker_count: int = Field(ge=0)
    blocked_dependent_count: int = Field(ge=0)
    labels: tuple[str, ...] = Field(max_length=12)
    remaining_label_count: int = Field(ge=0)
    open_gate_count: int = Field(ge=0)
    live_agent_count: int = Field(ge=0)
    updated_at: int | None = Field(ge=0)


class WorkDependencyDetail(WireModel):
    id: str
    title: str | None
    type: str
    state: str
    direction: Literal["prerequisite", "dependent"]


class WorkItemExact(WorkItemRow):
    description: str
    design: str
    acceptance_criteria: str
    notes: str
    molecule_type: str | None
    labels: tuple[str, ...]
    remaining_label_count: Literal[0]
    created_by: str | None
    created_at: int | None = Field(ge=0)
    closed_at: int | None = Field(ge=0)
    due_at: int | None = Field(ge=0)
    defer_until: int | None = Field(ge=0)
    claim: dict[str, Any]
    dependencies: tuple[WorkDependencyDetail, ...]
    dependents: tuple[WorkDependencyDetail, ...]
    gates: tuple[dict[str, Any], ...]
    agents: tuple[dict[str, Any], ...]
    advertised_actions: tuple[AdvertisedAction, ...]


class WorkItemQueue(WireModel):
    """Published loopback queue contract retained for compatibility."""

    schema_version: Literal[1] = WIRE_SCHEMA_VERSION
    hive_id: str
    queue: Literal["ready", "active", "blocked", "recent"]
    revision: str
    generated_at: int | None = Field(ge=0)
    freshness: ProjectionFreshness
    coverage: ProjectionCoverage
    limit: int = Field(ge=1, le=200)
    returned: int = Field(ge=0, le=200)
    truncated: bool
    next_cursor: str | None
    items: tuple[WorkItemRow, ...] = Field(max_length=200)
    warnings: tuple[str, ...]


class WorkItemDetail(WireModel):
    """Published loopback exact-detail contract retained for compatibility."""

    schema_version: Literal[1] = WIRE_SCHEMA_VERSION
    hive_id: str
    revision: str
    generated_at: int | None = Field(ge=0)
    freshness: ProjectionFreshness
    coverage: ProjectionCoverage
    item: WorkItemExact
    warnings: tuple[str, ...]


RemoteShortText = Annotated[str, Field(min_length=1, max_length=4_096)]
RemoteWarning = Annotated[str, Field(min_length=1, max_length=4_096)]
RemoteLabelFilter = Annotated[str, Field(min_length=1, max_length=64)]
RemoteSourceName = Annotated[str, Field(min_length=1, max_length=128)]


class RemoteProjectionSourceCoverage(WireModel):
    state: Literal["complete", "partial", "unavailable", "degraded", "unknown"]
    detail: str | None = Field(default=None, max_length=4_096)


class RemoteProjectionCoverage(WireModel):
    state: Literal["complete", "partial", "unavailable"]
    sources: dict[RemoteSourceName, RemoteProjectionSourceCoverage] = Field(max_length=16)


class RemoteProjectionFreshness(WireModel):
    state: Literal["fresh"]
    as_of: int = Field(strict=True, ge=0, le=2**53 - 1)


class RemoteAdvertisedActionTarget(WireModel):
    hive_id: str = Field(min_length=1, max_length=768)
    kind: Literal["work-item"]
    id: str = Field(min_length=1, max_length=256)


class RemoteAdvertisedActionPreconditions(WireModel):
    source_revision: str = Field(min_length=1, max_length=256)
    must_match: bool = Field(strict=True)


class RemoteAdvertisedActionInput(WireModel):
    transport: Literal["none", "parameters"]
    required: bool = Field(strict=True)
    input_schema: dict[str, Any] | None = Field(alias="schema")


class RemoteAdvertisedAction(WireModel):
    id: Literal["work-item.inspect", "work-item.refresh", "work-item.launch"]
    capability: Literal["inspect", "refresh", "launch"]
    target: RemoteAdvertisedActionTarget
    availability: Literal["allowed", "confirmation-required", "forbidden", "unavailable"]
    reason_code: str | None = Field(default=None, min_length=1, max_length=256)
    reason: str = Field(min_length=1, max_length=4_096)
    consequence: Literal["navigate", "read", "reversible-write"]
    advertised_at: int = Field(strict=True, ge=0, le=2**53 - 1)
    source_revision: str = Field(min_length=1, max_length=256)
    preconditions: RemoteAdvertisedActionPreconditions
    input: RemoteAdvertisedActionInput


class RemoteWorkItemRef(WireModel):
    hive_id: str = Field(min_length=1, max_length=768)
    kind: Literal["work-item"]
    id: str = Field(min_length=1, max_length=256)


class RemoteWorkDependencyDetail(WireModel):
    id: str = Field(min_length=1, max_length=4_096)
    title: str | None = Field(default=None, max_length=4_096)
    type: str = Field(min_length=1, max_length=4_096)
    state: str = Field(min_length=1, max_length=4_096)
    direction: Literal["prerequisite", "dependent"]


class RemoteWorkItemClaim(WireModel):
    actor: str | None = Field(default=None, max_length=4_096)
    lease_expires_at: int | None = Field(default=None, strict=True, ge=0, le=2**53 - 1)


class RemoteWorkItemGate(WireModel):
    id: str = Field(min_length=1, max_length=4_096)
    kind: str = Field(min_length=1, max_length=4_096)
    type: str | None = Field(default=None, max_length=4_096)
    status: str = Field(min_length=1, max_length=4_096)
    reason: str = Field(max_length=131_072)
    opened_at: int | None = Field(default=None, strict=True, ge=0, le=2**53 - 1)
    resolved_at: int | None = Field(default=None, strict=True, ge=0, le=2**53 - 1)


class RemoteWorkItemAgent(WireModel):
    id: str = Field(min_length=1, max_length=4_096)
    state: str = Field(min_length=1, max_length=128)
    owner_seat: str | None = Field(default=None, max_length=4_096)
    started_at: int | None = Field(default=None, strict=True, ge=0, le=2**53 - 1)
    updated_at: int | None = Field(default=None, strict=True, ge=0, le=2**53 - 1)
    ended_at: int | None = Field(default=None, strict=True, ge=0, le=2**53 - 1)


class RemoteSnapshotWorkItemSummary(SnapshotWorkItemSummary):
    status: Literal["open", "in_progress", "blocked", "closed"]
    readiness: Literal["ready", "active", "blocked", "completed"]


class RemoteWorkItemExact(RemoteSnapshotWorkItemSummary):
    ref: RemoteWorkItemRef
    revision: str = Field(min_length=1, max_length=256)
    readiness_reason: str = Field(max_length=4_096)
    parent_id: str | None = Field(default=None, max_length=4_096)
    blocked_dependent_count: int = Field(strict=True, ge=0, le=2**53 - 1)
    description: str = Field(max_length=131_072)
    design: str = Field(max_length=131_072)
    acceptance_criteria: str = Field(max_length=131_072)
    notes: str = Field(max_length=131_072)
    molecule_type: str | None = Field(default=None, max_length=4_096)
    labels: tuple[Annotated[str, Field(min_length=1, max_length=256)], ...] = Field(max_length=256)
    remaining_label_count: Literal[0]
    created_by: str | None = Field(default=None, max_length=4_096)
    created_at: int | None = Field(default=None, strict=True, ge=0, le=2**53 - 1)
    closed_at: int | None = Field(default=None, strict=True, ge=0, le=2**53 - 1)
    due_at: int | None = Field(default=None, strict=True, ge=0, le=2**53 - 1)
    defer_until: int | None = Field(default=None, strict=True, ge=0, le=2**53 - 1)
    claim: RemoteWorkItemClaim
    dependencies: tuple[RemoteWorkDependencyDetail, ...] = Field(max_length=1_024)
    dependents: tuple[RemoteWorkDependencyDetail, ...] = Field(max_length=1_024)
    gates: tuple[RemoteWorkItemGate, ...] = Field(max_length=256)
    agents: tuple[RemoteWorkItemAgent, ...] = Field(max_length=256)
    advertised_actions: tuple[RemoteAdvertisedAction, ...] = Field(min_length=3, max_length=3)

    @model_validator(mode="after")
    def _actions_are_declarative_and_allowlisted(self) -> RemoteWorkItemExact:
        if self.ref.id != self.id:
            raise ValueError("work-item detail id and reference disagree")
        expected = {
            "work-item.inspect": ("inspect", "navigate", "none", False, None),
            "work-item.refresh": ("refresh", "read", "none", False, None),
            "work-item.launch": (
                "launch",
                "reversible-write",
                "parameters",
                True,
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [],
                    "properties": {
                        "actor": {"type": "string", "minLength": 1},
                        "kind": {"type": "string", "minLength": 1},
                        "direction": {"enum": ["right", "down"], "default": "right"},
                        "focus": {"type": "boolean", "default": False},
                        "adoptExpired": {"type": "boolean", "default": False},
                    },
                },
            ),
        }
        if tuple(action.id for action in self.advertised_actions) != tuple(expected):
            raise ValueError("work-item advertised actions must use the fixed allowlist")
        for action in self.advertised_actions:
            capability, consequence, transport, must_match, schema = expected[action.id]
            if (
                action.capability != capability
                or action.consequence != consequence
                or action.target.to_wire() != self.ref.to_wire()
                or action.source_revision != self.revision
                or action.preconditions.source_revision != self.revision
                or action.preconditions.must_match is not must_match
                or action.input.transport != transport
                or action.input.required
                or action.input.input_schema != schema
            ):
                raise ValueError("work-item advertised action disagrees with its fixed contract")
        inspect, refresh, _ = self.advertised_actions
        if (
            inspect.availability != "allowed"
            or inspect.reason_code is not None
            or inspect.reason != "the exact work item is present in the projection"
            or refresh.availability != "allowed"
            or refresh.reason_code is not None
            or refresh.reason != "the exact work item can be refreshed"
        ):
            raise ValueError("work-item read action availability disagrees with fixed contract")
        return self


class RemoteWorkItemQueueLimits(WireModel):
    max_bytes: Literal[917_504]
    max_items: Literal[200]


class RemoteWorkItemDetailLimits(WireModel):
    max_bytes: Literal[917_504]


class RemoteWorkItemFilters(WireModel):
    priorities: tuple[Literal["P0", "P1", "P2", "P3", "P4"], ...] = Field(max_length=5)
    labels: tuple[RemoteLabelFilter, ...] = Field(max_length=8)
    assignee: str | None = Field(default=None, min_length=1, max_length=256)
    type: str | None = Field(default=None, min_length=1, max_length=256)
    parent: str | None = Field(default=None, min_length=1, max_length=256)
    ordering: Literal["beadhive.work-items/v1", "beadhive.ready-order/v1"]

    @model_validator(mode="after")
    def _filters_are_canonical(self) -> RemoteWorkItemFilters:
        if self.priorities != tuple(sorted(set(self.priorities))):
            raise ValueError("work-item priorities must be unique and canonical")
        if self.labels != tuple(sorted(set(self.labels))):
            raise ValueError("work-item labels must be unique and canonical")
        return self


class RemoteWorkItemQueueCoverage(RemoteProjectionCoverage):
    eligible: int = Field(strict=True, ge=0, le=2**53 - 1)
    returned: int = Field(strict=True, ge=0, le=200)
    truncated: bool = Field(strict=True)
    next_cursor: str | None = Field(default=None, min_length=1, max_length=4_096)

    @model_validator(mode="after")
    def _coverage_is_consistent(self) -> RemoteWorkItemQueueCoverage:
        if self.returned > self.eligible:
            raise ValueError("work-item coverage returned exceeds eligible")
        if self.truncated != (self.next_cursor is not None):
            raise ValueError("work-item coverage cursor and truncation disagree")
        return self


class RemoteWorkItemQueue(WireModel):
    """Typed envelope emitted by ``operator_work_items.queue_payload``."""

    schema_version: Literal[1] = WIRE_SCHEMA_VERSION
    projection_policy: Literal["beadhive.snapshot-summary/v1"]
    hive_id: str = Field(min_length=1, max_length=768)
    queue: Literal["ready", "active", "blocked", "recent"]
    revision: str = Field(min_length=1, max_length=256)
    generated_at: int = Field(strict=True, ge=0, le=2**53 - 1)
    limits: RemoteWorkItemQueueLimits
    filters: RemoteWorkItemFilters
    coverage: RemoteWorkItemQueueCoverage
    limit: int = Field(strict=True, ge=1, le=200)
    returned: int = Field(strict=True, ge=0, le=200)
    truncated: bool = Field(strict=True)
    next_cursor: str | None = Field(default=None, min_length=1, max_length=4_096)
    items: tuple[RemoteSnapshotWorkItemSummary, ...] = Field(max_length=200)
    warnings: tuple[RemoteWarning, ...] = Field(max_length=16)

    @model_validator(mode="after")
    def _page_is_consistent(self) -> RemoteWorkItemQueue:
        if self.returned != len(self.items) or self.returned > self.limit:
            raise ValueError("work-item page counts disagree")
        if self.truncated != (self.next_cursor is not None):
            raise ValueError("work-item page cursor and truncation disagree")
        if (
            self.coverage.returned != self.returned
            or self.coverage.truncated != self.truncated
            or self.coverage.next_cursor != self.next_cursor
        ):
            raise ValueError("work-item page coverage disagrees")
        if (
            len(json.dumps(self.to_wire(), separators=(",", ":"), ensure_ascii=False).encode())
            > self.limits.max_bytes
        ):
            raise ValueError("work-item page exceeds encoded limit")
        return self


class RemoteWorkItemDetail(WireModel):
    """Typed envelope emitted by ``operator_work_items.detail_payload``."""

    schema_version: Literal[1] = WIRE_SCHEMA_VERSION
    projection_policy: Literal["beadhive.snapshot-summary/v1"]
    hive_id: str = Field(min_length=1, max_length=768)
    revision: str = Field(min_length=1, max_length=256)
    generated_at: int = Field(strict=True, ge=0, le=2**53 - 1)
    limits: RemoteWorkItemDetailLimits
    freshness: RemoteProjectionFreshness
    coverage: RemoteProjectionCoverage
    item: RemoteWorkItemExact
    warnings: tuple[RemoteWarning, ...] = Field(max_length=16)

    @model_validator(mode="after")
    def _detail_is_bounded(self) -> RemoteWorkItemDetail:
        if self.item.ref.hive_id != self.hive_id or self.item.revision != self.revision:
            raise ValueError("work-item detail identity or revision disagrees")
        if self.freshness.as_of != self.generated_at:
            raise ValueError("work-item detail freshness disagrees")
        expected_launch = {
            ("partial", self.item.readiness): (
                "unavailable",
                "work_item_projection_partial",
                "authoritative launch prerequisites are only partially observed",
            ),
            ("complete", "ready"): ("allowed", None, self.item.readiness_reason),
            ("complete", "blocked"): (
                "forbidden",
                "work_item_blocked",
                self.item.readiness_reason,
            ),
            ("complete", "completed"): (
                "forbidden",
                "work_item_completed",
                self.item.readiness_reason,
            ),
            ("complete", "active"): (
                "unavailable",
                "work_item_claim_ownership_required",
                "the current caller's claim ownership must be proven before reuse",
            ),
        }.get((self.coverage.state, self.item.readiness))
        if expected_launch is None:
            expected_launch = (
                "unavailable",
                "work_item_state_unavailable",
                self.item.readiness_reason,
            )
        launch = self.item.advertised_actions[2]
        if (launch.availability, launch.reason_code, launch.reason) != expected_launch:
            raise ValueError("work-item launch availability disagrees with detail state")
        if any(
            action.advertised_at != self.generated_at for action in self.item.advertised_actions
        ):
            raise ValueError("work-item action timestamp disagrees with detail")
        if (
            len(json.dumps(self.to_wire(), separators=(",", ":"), ensure_ascii=False).encode())
            > self.limits.max_bytes
        ):
            raise ValueError("work-item detail exceeds encoded limit")
        return self


class RunActivityEnvelope(WireModel):
    """Allowlisted activity record emitted by ``operator_contract``."""

    schema_version: Literal[1] = WIRE_SCHEMA_VERSION
    hive_id: str
    run_id: str
    bead_id: str | None
    provider_session_id: str | None
    driver: str
    provider: str
    protocol: str
    occurred_at: int = Field(ge=0)
    elapsed_ms: int = Field(ge=0)
    source_revision: str
    producer_epoch: str
    sequence: int = Field(ge=1)
    payload: dict[str, Any]


class RunActivityCoverage(WireModel):
    state: Literal["complete", "partial", "unavailable"]
    detail: str | None


class RunActivityFrame(WireModel):
    """Typed snapshot/delta/reset envelope emitted by the exact-run activity feed."""

    schema_version: Literal[1] = WIRE_SCHEMA_VERSION
    kind: Literal["snapshot", "delta", "reset"]
    hive_id: str
    run_id: str
    producer_epoch: str
    sequence: int = Field(ge=0)
    base_sequence: int = Field(ge=0)
    source_revision: str
    coverage: RunActivityCoverage
    reset_reason: str | None
    activities: tuple[RunActivityEnvelope, ...]


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
    RouteSpec("GET", "/health", None, (200, 400, 403, 408, 413, 503), HealthResponse),
    RouteSpec(
        "GET",
        "/api/v1/factory",
        AuthScope.OPERATOR_READ,
        (200, 400, 401, 403, 408, 413, 429, 503),
        FactoryResponse,
    ),
    RouteSpec(
        "GET",
        "/api/v1/factory/hives",
        AuthScope.OPERATOR_READ,
        (200, 304, 400, 401, 403, 408, 409, 413, 429, 503),
        FactoryHivePage,
        request_headers=("If-None-Match",),
    ),
    RouteSpec(
        "GET",
        "/api/v1/hives/{hive_id}/snapshot",
        AuthScope.OPERATOR_READ,
        (200, 400, 401, 403, 404, 408, 413, 429, 503),
        HiveSnapshotResponse,
    ),
    RouteSpec(
        "GET",
        "/api/v1/hives/{hive_id}/snapshot-with-work-items",
        AuthScope.OPERATOR_READ,
        (200, 400, 401, 403, 404, 408, 413, 429, 503),
        RemoteHiveSnapshotResponse,
    ),
    RouteSpec(
        "GET",
        "/api/v1/hives/{hive_id}/work-items",
        AuthScope.OPERATOR_READ,
        (200, 304, 400, 401, 403, 404, 408, 409, 413, 429, 503),
        WorkItemQueue,
        request_headers=("If-None-Match",),
    ),
    RouteSpec(
        "GET",
        "/api/v1/hives/{hive_id}/work-items/{bead_id}",
        AuthScope.OPERATOR_READ,
        (200, 304, 400, 401, 403, 404, 408, 413, 429, 503),
        WorkItemDetail,
        request_headers=("If-None-Match",),
    ),
    RouteSpec(
        "GET",
        "/api/v1/hives/{hive_id}/work-item-pages",
        AuthScope.OPERATOR_READ,
        (200, 304, 400, 401, 403, 404, 408, 409, 413, 429, 503),
        RemoteWorkItemQueue,
        request_headers=("If-None-Match",),
    ),
    RouteSpec(
        "GET",
        "/api/v1/hives/{hive_id}/work-item-details/{bead_id}",
        AuthScope.OPERATOR_READ,
        (200, 304, 400, 401, 403, 404, 408, 413, 429, 503),
        RemoteWorkItemDetail,
        request_headers=("If-None-Match",),
    ),
    RouteSpec(
        "GET",
        "/api/v1/hives/{hive_id}/events",
        AuthScope.OPERATOR_READ,
        (200, 400, 401, 403, 404, 408, 409, 413, 429, 503),
        OperatorEvent,
        request_headers=("Last-Event-ID",),
    ),
    RouteSpec(
        "GET",
        "/api/v1/runs/{run_id}/activity",
        AuthScope.OPERATOR_READ,
        (200, 400, 401, 403, 404, 408, 409, 410, 413, 429, 503),
        RunActivityFrame,
    ),
    RouteSpec(
        "POST",
        "/api/v1/runs/{run_id}/activity",
        AuthScope.ACTIVITY_PUBLISH,
        (200, 201, 400, 401, 403, 404, 408, 409, 413, 429, 503),
        ActivityAppendResponse,
        ActivityAppendRequest,
    ),
    RouteSpec(
        "POST",
        "/api/v1/terminal/attach-token",
        AuthScope.TERMINAL_ATTACH,
        (400, 401, 403, 408, 413, 429, 503),
        TerminalUnavailable,
    ),
    RouteSpec(
        "WEBSOCKET",
        "/ws/terminal",
        AuthScope.TERMINAL_ATTACH,
        (503,),
        TerminalUnavailable,
    ),
    RouteSpec(
        "GET",
        "/openapi.json",
        AuthScope.OPERATOR_READ,
        (200, 400, 401, 403, 408, 413, 429, 503),
        None,
    ),
)


WIRE_MODELS: tuple[type[WireModel], ...] = (
    HealthResponse,
    FactoryResponse,
    FactoryHivePage,
    HiveSnapshotResponse,
    RemoteHiveSnapshotResponse,
    OperatorEvent,
    ResnapshotInstruction,
    EventResnapshotResponse,
    ActivityViewResponse,
    ActivityAppendRequest,
    ActivityAppendResponse,
    TerminalAttachTokenResponse,
    TerminalUnavailable,
    ProjectionFreshness,
    ProjectionSourceCoverage,
    ProjectionCoverage,
    WorkItemRef,
    WorkItemReadiness,
    WorkItemRow,
    WorkDependencyDetail,
    WorkItemExact,
    WorkItemQueue,
    WorkItemDetail,
    RemoteWorkItemRef,
    RemoteWorkDependencyDetail,
    RemoteWorkItemClaim,
    RemoteWorkItemGate,
    RemoteWorkItemAgent,
    RemoteSnapshotWorkItemSummary,
    RemoteWorkItemExact,
    RemoteWorkItemQueueLimits,
    RemoteWorkItemDetailLimits,
    RemoteWorkItemFilters,
    RemoteWorkItemQueueCoverage,
    RemoteWorkItemQueue,
    RemoteWorkItemDetail,
    RunActivityEnvelope,
    RunActivityCoverage,
    RunActivityFrame,
    ErrorResponse,
)
