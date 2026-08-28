"""Backend-neutral state-stream port and canonical v1 consumer types.

Providers own *how* state is read.  This module owns what consumers observe: canonical scopes,
normalized issue records, opaque revisions, snapshot-first ordering, delta construction, and the
resync handshake decided in ``docs/design/beadhive-stream-v1-contract.md``.

The split is deliberate.  A provider yields full normalized snapshots (plus an explicit reset
signal when it loses continuity); :func:`stream_frames` applies the contract once for every
provider.  Polling, event-log, direct-database, and remote-service implementations can therefore
be substituted without teaching consumers a backend-specific API or reimplementing ordering.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, TypeAlias, runtime_checkable

SCHEMA_VERSION = 1


class StreamScope(StrEnum):
    """The three canonical, lifetime-bound stream scopes."""

    FACTORY = "factory"
    HUB = "hub"
    HIVE = "hive"


class SnapshotReason(StrEnum):
    INITIAL = "initial"
    RESYNC = "resync"


class ResyncReason(StrEnum):
    UNKNOWN_REVISION = "unknown_revision"
    SCOPE_MISMATCH = "scope_mismatch"
    ADAPTER_ERROR = "adapter_error"


class StateStreamContractError(ValueError):
    """A provider produced an event sequence that cannot satisfy the v1 contract."""


@dataclass(frozen=True)
class StreamDependency:
    """The backend-neutral dependency edge exposed on a stream issue."""

    issue_id: str
    depends_on_id: str
    type: str


@dataclass(frozen=True)
class StreamIssue:
    """The curated consumer record; backend storage records never cross this boundary."""

    id: str
    hive: str
    issue_type: str
    status: str
    priority: str
    title: str
    updated_at: str
    labels: tuple[str, ...] = ()
    assignee: str | None = None
    parent_id: str | None = None
    dependencies: tuple[StreamDependency, ...] = ()
    # Exact-detail facts intentionally do not widen the v1 state-stream wire payload.  They
    # travel with the provider snapshot so bounded resource projections can close the old
    # partial-detail gap without breaking existing stream consumers.
    description: str = ""
    design: str = ""
    acceptance_criteria: str = ""
    notes: str = ""
    mol_type: str | None = None
    owner: str | None = None
    created_by: str | None = None
    created_at: str | None = None
    closed_at: str | None = None
    due_at: str | None = None
    defer_until: str | None = None
    lease_expires_at: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "labels", tuple(self.labels))
        object.__setattr__(self, "dependencies", tuple(self.dependencies))


def projection_id(entity: str, natural_key: tuple[str, ...] | list[str]) -> str:
    """Return the contract's deterministic opaque ID for an operator entity."""

    prefixes = {
        "work-dependency",
        "assignment",
        "gate-request",
        "epic-schedule",
    }
    if entity not in prefixes:
        raise StateStreamContractError(f"unknown operator entity {entity!r}")
    encoded = json.dumps(list(natural_key), ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    return f"{entity}:sha256:{hashlib.sha256(encoded).hexdigest()}"


@dataclass(frozen=True)
class WorkDependency:
    id: str
    hive: str
    issue_id: str
    depends_on_id: str
    type: str
    created_at: str | None
    created_by: str | None


@dataclass(frozen=True)
class Assignment:
    id: str
    hive: str
    issue_id: str
    seat: str


@dataclass(frozen=True)
class GateRequest:
    """Policy-neutral GateRequest carrier; projection policy lives in its adapter module."""

    id: str
    hive: str
    gate_id: str
    blocks: tuple[str, ...]
    gate_type: str | None
    gate_kind: str
    status: str
    reason: str
    opened_at: str
    resolved_at: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "blocks", tuple(self.blocks))


@dataclass(frozen=True)
class ScheduleGroup:
    kind: str
    batch: str | None
    issue_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "issue_ids", tuple(self.issue_ids))


@dataclass(frozen=True)
class EpicSchedule:
    """Policy-neutral EpicSchedule carrier; scheduler mapping lands in its adapter module."""

    id: str
    hive: str
    epic_id: str
    groups: tuple[ScheduleGroup, ...]
    singletons: tuple[str, ...]
    coordinators: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "groups", tuple(self.groups))
        object.__setattr__(self, "singletons", tuple(self.singletons))
        object.__setattr__(self, "coordinators", tuple(self.coordinators))


OperatorEntity: TypeAlias = WorkDependency | GateRequest | EpicSchedule | Assignment

_OPERATOR_COLLECTIONS = (
    "work_dependencies",
    "gate_requests",
    "epic_schedules",
    "assignments",
)
_OPERATOR_TYPES = {
    "work_dependencies": WorkDependency,
    "gate_requests": GateRequest,
    "epic_schedules": EpicSchedule,
    "assignments": Assignment,
}


def _operator_natural_key(record: OperatorEntity) -> tuple[str, tuple[str, ...]]:
    if isinstance(record, WorkDependency):
        return (
            "work-dependency",
            (record.hive, record.issue_id, record.depends_on_id, record.type),
        )
    if isinstance(record, Assignment):
        return "assignment", (record.hive, record.issue_id)
    if isinstance(record, GateRequest):
        return "gate-request", (record.hive, record.gate_id)
    return "epic-schedule", (record.hive, record.epic_id)


def _canonical_operator_records(
    records: tuple[OperatorEntity, ...] | list[OperatorEntity], name: str
) -> tuple[OperatorEntity, ...]:
    expected_type = _OPERATOR_TYPES[name.removesuffix("_changed")]
    if any(not isinstance(record, expected_type) for record in records):
        raise StateStreamContractError(f"{name} contains a record of the wrong entity type")
    ordered = tuple(sorted(records, key=lambda record: record.id))
    ids = [record.id for record in ordered]
    if any(not record_id for record_id in ids):
        raise StateStreamContractError(f"{name} records require non-empty ids")
    if len(ids) != len(set(ids)):
        raise StateStreamContractError(f"{name} contains duplicate entity ids")
    if any(not record.hive for record in ordered):
        raise StateStreamContractError(f"{name} records require non-empty hives")
    for record in ordered:
        entity, natural_key = _operator_natural_key(record)
        if any(not value for value in natural_key):
            raise StateStreamContractError(f"{name} records require complete natural keys")
        if record.id != projection_id(entity, natural_key):
            raise StateStreamContractError(
                f"{name} record {record.id!r} does not match its stable natural-key id"
            )
    return ordered


def _canonicalize_operator_collections(value: object) -> None:
    for name in _OPERATOR_COLLECTIONS:
        object.__setattr__(
            value,
            name,
            _canonical_operator_records(getattr(value, name), name),
        )


@dataclass(frozen=True)
class StreamRequest:
    """A backend-neutral stream selection.

    ``hive`` is the registry slug, never a local path or an id-prefix inference.  Providers map
    it to their own locator (filesystem, database key, remote resource, ...). ``since_revision``
    is passed through as an opaque token.  A provider that recognizes it starts by yielding the
    complete retained snapshot for that revision; otherwise it starts with current full state.
    Either way, :func:`stream_frames` emits a snapshot first.
    """

    scope: StreamScope
    hive: str | None = None
    since_revision: str | None = None

    def __post_init__(self) -> None:
        try:
            scope = StreamScope(self.scope)
        except ValueError as exc:
            raise StateStreamContractError(f"unknown stream scope {self.scope!r}") from exc
        object.__setattr__(self, "scope", scope)
        if scope is StreamScope.HIVE and not self.hive:
            raise StateStreamContractError("hive scope requires a hive slug")
        if scope is not StreamScope.HIVE and self.hive is not None:
            raise StateStreamContractError(f"{scope.value} scope cannot select one hive")


def _validate_partial(partial: bool, partial_reason: str | None) -> None:
    if partial and not partial_reason:
        raise StateStreamContractError("partial state requires partial_reason")
    if not partial and partial_reason is not None:
        raise StateStreamContractError("partial_reason requires partial state")


@dataclass(frozen=True)
class ProviderSnapshot:
    """One complete normalized provider state, before it becomes a public frame.

    Providers may retain snapshots in memory to honor ``since_revision``.  The first value from
    every call to ``updates`` must be a complete snapshot, including when the requested revision
    is unknown.  Later snapshots are diffed centrally into replacement-record deltas.
    """

    scope: StreamScope
    revision: str
    as_of: str
    issues: tuple[StreamIssue, ...]
    partial: bool = False
    partial_reason: str | None = None
    work_dependencies: tuple[WorkDependency, ...] = ()
    gate_requests: tuple[GateRequest, ...] = ()
    epic_schedules: tuple[EpicSchedule, ...] = ()
    assignments: tuple[Assignment, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope", StreamScope(self.scope))
        object.__setattr__(self, "issues", tuple(self.issues))
        _canonicalize_operator_collections(self)
        if not self.revision:
            raise StateStreamContractError("a snapshot revision cannot be empty")
        _validate_partial(self.partial, self.partial_reason)


@dataclass(frozen=True)
class ProviderReset:
    """A provider lost continuity after startup; the next event must be a full snapshot."""

    scope: StreamScope
    reason: ResyncReason
    as_of: str
    partial: bool = False
    partial_reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope", StreamScope(self.scope))
        object.__setattr__(self, "reason", ResyncReason(self.reason))
        _validate_partial(self.partial, self.partial_reason)


ProviderEvent: TypeAlias = ProviderSnapshot | ProviderReset


@runtime_checkable
class StateStreamProvider(Protocol):
    """Read full normalized states for one request without exposing backend mechanics."""

    name: str

    def updates(self, request: StreamRequest) -> Iterator[ProviderEvent]:
        """Yield a full snapshot first, then changed snapshots or reset/snapshot pairs.

        ``request.since_revision`` affects which *full snapshot* is first; it never authorizes
        a delta-first or reset-first sequence.  The iterator may be finite for bounded callers
        and tests, or remain live for the CLI stream.
        """
        ...


@dataclass(frozen=True)
class SnapshotFrame:
    scope: StreamScope
    revision: str
    as_of: str
    issues: tuple[StreamIssue, ...]
    reason: SnapshotReason
    partial: bool = False
    partial_reason: str | None = None
    work_dependencies: tuple[WorkDependency, ...] = ()
    gate_requests: tuple[GateRequest, ...] = ()
    epic_schedules: tuple[EpicSchedule, ...] = ()
    assignments: tuple[Assignment, ...] = ()
    schema_version: int = field(init=False, default=SCHEMA_VERSION)
    frame: str = field(init=False, default="snapshot")

    def __post_init__(self) -> None:
        object.__setattr__(self, "issues", tuple(self.issues))
        _canonicalize_operator_collections(self)


@dataclass(frozen=True)
class DeltaFrame:
    scope: StreamScope
    revision: str
    as_of: str
    since_revision: str
    changed: tuple[StreamIssue, ...]
    removed: tuple[str, ...]
    partial: bool = False
    partial_reason: str | None = None
    work_dependencies_changed: tuple[WorkDependency, ...] = ()
    work_dependencies_removed: tuple[str, ...] = ()
    gate_requests_changed: tuple[GateRequest, ...] = ()
    gate_requests_removed: tuple[str, ...] = ()
    epic_schedules_changed: tuple[EpicSchedule, ...] = ()
    epic_schedules_removed: tuple[str, ...] = ()
    assignments_changed: tuple[Assignment, ...] = ()
    assignments_removed: tuple[str, ...] = ()
    schema_version: int = field(init=False, default=SCHEMA_VERSION)
    frame: str = field(init=False, default="delta")

    def __post_init__(self) -> None:
        object.__setattr__(self, "changed", tuple(self.changed))
        object.__setattr__(self, "removed", tuple(sorted(self.removed)))
        for name in _OPERATOR_COLLECTIONS:
            changed_name = f"{name}_changed"
            removed_name = f"{name}_removed"
            object.__setattr__(
                self,
                changed_name,
                _canonical_operator_records(getattr(self, changed_name), changed_name),
            )
            removed = tuple(sorted(getattr(self, removed_name)))
            if len(removed) != len(set(removed)):
                raise StateStreamContractError(f"{removed_name} contains duplicate entity ids")
            object.__setattr__(self, removed_name, removed)


@dataclass(frozen=True)
class ResyncFrame:
    scope: StreamScope
    as_of: str
    reason: ResyncReason
    partial: bool = False
    partial_reason: str | None = None
    schema_version: int = field(init=False, default=SCHEMA_VERSION)
    frame: str = field(init=False, default="resync")


StreamFrame: TypeAlias = SnapshotFrame | DeltaFrame | ResyncFrame


def _snapshot_frame(snapshot: ProviderSnapshot, reason: SnapshotReason) -> SnapshotFrame:
    return SnapshotFrame(
        scope=snapshot.scope,
        revision=snapshot.revision,
        as_of=snapshot.as_of,
        issues=snapshot.issues,
        reason=reason,
        work_dependencies=snapshot.work_dependencies,
        gate_requests=snapshot.gate_requests,
        epic_schedules=snapshot.epic_schedules,
        assignments=snapshot.assignments,
        partial=snapshot.partial,
        partial_reason=snapshot.partial_reason,
    )


def _issue_map(snapshot: ProviderSnapshot) -> dict[str, StreamIssue]:
    issues = {issue.id: issue for issue in snapshot.issues}
    if len(issues) != len(snapshot.issues):
        raise StateStreamContractError(
            f"provider snapshot {snapshot.revision!r} contains duplicate issue ids"
        )
    return issues


def _operator_maps(snapshot: ProviderSnapshot) -> dict[str, dict[str, OperatorEntity]]:
    result: dict[str, dict[str, OperatorEntity]] = {}
    for name in _OPERATOR_COLLECTIONS:
        records = getattr(snapshot, name)
        mapped = {record.id: record for record in records}
        if len(mapped) != len(records):
            raise StateStreamContractError(
                f"provider snapshot {snapshot.revision!r} contains duplicate {name} ids"
            )
        result[name] = mapped
    return result


def _validate_snapshot(snapshot: ProviderSnapshot) -> None:
    _issue_map(snapshot)
    _operator_maps(snapshot)


def _validate_event(request: StreamRequest, event: ProviderEvent) -> None:
    if event.scope is not request.scope:
        raise StateStreamContractError(
            f"provider emitted {event.scope.value} state for a {request.scope.value} stream"
        )
    if request.scope is StreamScope.HIVE and isinstance(event, ProviderSnapshot):
        records = [*event.issues]
        for name in _OPERATOR_COLLECTIONS:
            records.extend(getattr(event, name))
        foreign = sorted({record.hive for record in records if record.hive != request.hive})
        if foreign:
            raise StateStreamContractError(
                f"hive stream {request.hive!r} contains records from {foreign!r}"
            )


def stream_frames(provider: StateStreamProvider, request: StreamRequest) -> Iterator[StreamFrame]:
    """Apply the v1 ordering and delta contract to any provider implementation.

    This is the only consumer entry point providers need to satisfy.  In particular, adapters
    cannot choose delta-first reconnect semantics: the first provider event must be a complete
    snapshot, and startup ``since_revision`` handling stays behind the port.
    """

    previous: ProviderSnapshot | None = None
    started = False
    awaiting_resync_snapshot = False

    for event in provider.updates(request):
        _validate_event(request, event)

        if not started:
            if not isinstance(event, ProviderSnapshot):
                raise StateStreamContractError("every stream session must start with a snapshot")
            _validate_snapshot(event)
            previous = event
            started = True
            yield _snapshot_frame(event, SnapshotReason.INITIAL)
            continue

        if awaiting_resync_snapshot:
            if not isinstance(event, ProviderSnapshot):
                raise StateStreamContractError("resync must be immediately followed by a snapshot")
            _validate_snapshot(event)
            previous = event
            awaiting_resync_snapshot = False
            yield _snapshot_frame(event, SnapshotReason.RESYNC)
            continue

        if isinstance(event, ProviderReset):
            awaiting_resync_snapshot = True
            previous = None
            yield ResyncFrame(
                scope=event.scope,
                as_of=event.as_of,
                reason=event.reason,
                partial=event.partial,
                partial_reason=event.partial_reason,
            )
            continue

        assert previous is not None  # established by the snapshot-first and resync branches
        old = _issue_map(previous)
        new = _issue_map(event)
        changed = tuple(issue for issue in event.issues if old.get(issue.id) != issue)
        removed = tuple(sorted(old.keys() - new.keys()))
        old_operators = _operator_maps(previous)
        new_operators = _operator_maps(event)
        operator_delta: dict[str, tuple[OperatorEntity, ...] | tuple[str, ...]] = {}
        for name in _OPERATOR_COLLECTIONS:
            operator_delta[f"{name}_changed"] = tuple(
                record
                for record in getattr(event, name)
                if old_operators[name].get(record.id) != record
            )
            operator_delta[f"{name}_removed"] = tuple(
                sorted(old_operators[name].keys() - new_operators[name].keys())
            )
        operator_changed = any(operator_delta.values())
        partial_changed = (event.partial, event.partial_reason) != (
            previous.partial,
            previous.partial_reason,
        )
        if event.revision == previous.revision:
            if changed or removed or operator_changed or partial_changed:
                raise StateStreamContractError(
                    f"revision {event.revision!r} names two different provider states"
                )
            continue
        yield DeltaFrame(
            scope=event.scope,
            revision=event.revision,
            as_of=event.as_of,
            since_revision=previous.revision,
            changed=changed,
            removed=removed,
            **operator_delta,
            partial=event.partial,
            partial_reason=event.partial_reason,
        )
        previous = event

    if not started:
        raise StateStreamContractError("provider ended before the required initial snapshot")
    if awaiting_resync_snapshot:
        raise StateStreamContractError("provider ended before the snapshot required by resync")


def frame_payload(frame: StreamFrame) -> dict[str, object]:
    """Return the exact JSON-ready v1 envelope; the CLI only owns NDJSON transport."""

    payload: dict[str, object] = {
        "schema_version": frame.schema_version,
        "frame": frame.frame,
        "scope": frame.scope.value,
        "as_of": frame.as_of,
        "partial": frame.partial,
        "partial_reason": frame.partial_reason,
    }
    if isinstance(frame, SnapshotFrame):
        payload.update(
            revision=frame.revision,
            issues=[_issue_payload(issue) for issue in frame.issues],
            work_dependencies=[_operator_payload(record) for record in frame.work_dependencies],
            gate_requests=[_operator_payload(record) for record in frame.gate_requests],
            epic_schedules=[_operator_payload(record) for record in frame.epic_schedules],
            assignments=[_operator_payload(record) for record in frame.assignments],
            reason=frame.reason.value,
        )
    elif isinstance(frame, DeltaFrame):
        payload.update(
            revision=frame.revision,
            since_revision=frame.since_revision,
            changed=[_issue_payload(issue) for issue in frame.changed],
            removed=list(frame.removed),
            work_dependencies_changed=[
                _operator_payload(record) for record in frame.work_dependencies_changed
            ],
            work_dependencies_removed=list(frame.work_dependencies_removed),
            gate_requests_changed=[
                _operator_payload(record) for record in frame.gate_requests_changed
            ],
            gate_requests_removed=list(frame.gate_requests_removed),
            epic_schedules_changed=[
                _operator_payload(record) for record in frame.epic_schedules_changed
            ],
            epic_schedules_removed=list(frame.epic_schedules_removed),
            assignments_changed=[_operator_payload(record) for record in frame.assignments_changed],
            assignments_removed=list(frame.assignments_removed),
        )
    else:
        payload["reason"] = frame.reason.value
    return payload


def _issue_payload(issue: StreamIssue) -> dict[str, object]:
    return {
        "id": issue.id,
        "hive": issue.hive,
        "issue_type": issue.issue_type,
        "status": issue.status,
        "priority": issue.priority,
        "title": issue.title,
        "labels": list(issue.labels),
        "assignee": issue.assignee,
        "parent_id": issue.parent_id,
        "dependencies": [
            {
                "issue_id": dep.issue_id,
                "depends_on_id": dep.depends_on_id,
                "type": dep.type,
            }
            for dep in issue.dependencies
        ],
        "updated_at": issue.updated_at,
    }


def _operator_payload(record: OperatorEntity) -> dict[str, object]:
    if isinstance(record, WorkDependency):
        return {
            "id": record.id,
            "hive": record.hive,
            "issue_id": record.issue_id,
            "depends_on_id": record.depends_on_id,
            "type": record.type,
            "created_at": record.created_at,
            "created_by": record.created_by,
        }
    if isinstance(record, Assignment):
        return {
            "id": record.id,
            "hive": record.hive,
            "issue_id": record.issue_id,
            "seat": record.seat,
        }
    if isinstance(record, GateRequest):
        return {
            "id": record.id,
            "hive": record.hive,
            "gate_id": record.gate_id,
            "blocks": list(record.blocks),
            "gate_type": record.gate_type,
            "gate_kind": record.gate_kind,
            "status": record.status,
            "reason": record.reason,
            "opened_at": record.opened_at,
            "resolved_at": record.resolved_at,
        }
    return {
        "id": record.id,
        "hive": record.hive,
        "epic_id": record.epic_id,
        "groups": [
            {
                "kind": group.kind,
                "batch": group.batch,
                "issue_ids": list(group.issue_ids),
            }
            for group in record.groups
        ],
        "singletons": list(record.singletons),
        "coordinators": list(record.coordinators),
    }
