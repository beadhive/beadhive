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

    def __post_init__(self) -> None:
        object.__setattr__(self, "labels", tuple(self.labels))
        object.__setattr__(self, "dependencies", tuple(self.dependencies))


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

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope", StreamScope(self.scope))
        object.__setattr__(self, "issues", tuple(self.issues))
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
    schema_version: int = field(init=False, default=SCHEMA_VERSION)
    frame: str = field(init=False, default="snapshot")


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
    schema_version: int = field(init=False, default=SCHEMA_VERSION)
    frame: str = field(init=False, default="delta")


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


def _validate_event(request: StreamRequest, event: ProviderEvent) -> None:
    if event.scope is not request.scope:
        raise StateStreamContractError(
            f"provider emitted {event.scope.value} state for a {request.scope.value} stream"
        )
    if request.scope is StreamScope.HIVE and isinstance(event, ProviderSnapshot):
        foreign = sorted({issue.hive for issue in event.issues if issue.hive != request.hive})
        if foreign:
            raise StateStreamContractError(
                f"hive stream {request.hive!r} contains issue records from {foreign!r}"
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
            _issue_map(event)
            previous = event
            started = True
            yield _snapshot_frame(event, SnapshotReason.INITIAL)
            continue

        if awaiting_resync_snapshot:
            if not isinstance(event, ProviderSnapshot):
                raise StateStreamContractError("resync must be immediately followed by a snapshot")
            _issue_map(event)
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
        if event.revision == previous.revision:
            if changed or removed:
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
            reason=frame.reason.value,
        )
    elif isinstance(frame, DeltaFrame):
        payload.update(
            revision=frame.revision,
            since_revision=frame.since_revision,
            changed=[_issue_payload(issue) for issue in frame.changed],
            removed=list(frame.removed),
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
