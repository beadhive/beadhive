"""Polling implementation of the backend-neutral state-stream provider.

Each refresh performs one export-shaped read through the configured engine.  The exported JSONL
already contains dependency edges, so this adapter never performs a list query followed by
per-issue or batched show calls.  Concurrent callers for the same scope share one in-flight
refresh; different scopes remain independent.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

from . import config, engine, registry
from .state_stream import (
    Assignment,
    EpicSchedule,
    GateRequest,
    ProviderEvent,
    ProviderReset,
    ProviderSnapshot,
    ResyncReason,
    StateStreamProvider,
    StreamDependency,
    StreamIssue,
    StreamRequest,
    StreamScope,
    WorkDependency,
    projection_id,
)
from .state_stream_epic_schedule import project_epic_schedules
from .state_stream_gate_projection import project_gate_requests
from .state_stream_process import StreamProcessScope

DEFAULT_POLL_INTERVAL = 2.0
DEFAULT_HISTORY_SIZE = 8


class PollingSnapshotError(RuntimeError):
    """The current backend could not produce a complete export-shaped read."""


@dataclass(frozen=True)
class AcceptedExportRecord:
    """One validated raw row paired with its backend-neutral issue identity.

    The raw mapping stays inside the polling adapter so sibling projectors can consume fields
    intentionally omitted from ``StreamIssue`` without starting another backend read.
    """

    raw: dict[str, object]
    issue: StreamIssue


@dataclass(frozen=True)
class AcceptedExport:
    """The single dependency-capable export pass accepted for one scope refresh."""

    records: tuple[AcceptedExportRecord, ...]
    partial_reason: str | None = None


@dataclass(frozen=True)
class SnapshotProjection:
    """Composable normalized snapshot content shared by operator projector modules."""

    accepted: AcceptedExport
    issues: tuple[StreamIssue, ...] = ()
    work_dependencies: tuple[WorkDependency, ...] = ()
    gate_requests: tuple[GateRequest, ...] = ()
    epic_schedules: tuple[EpicSchedule, ...] = ()
    assignments: tuple[Assignment, ...] = ()
    partial_reason: str | None = None


@dataclass
class _RefreshState:
    condition: threading.Condition = field(default_factory=threading.Condition)
    refreshing: bool = False
    generation: int = 0
    refreshed_at: float | None = None
    snapshot: ProviderSnapshot | None = None
    error: Exception | None = None
    history: OrderedDict[str, ProviderSnapshot] = field(default_factory=OrderedDict)


def _scope_key(request: StreamRequest) -> tuple[StreamScope, str]:
    return request.scope, request.hive or ""


def _hive_slug(entry: dict) -> str:
    """The registry-owned repo slug used by the v1 ``StreamIssue.hive`` field."""

    return str(entry["repo"])


def _labels(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, list | tuple | set):
        return ()
    return tuple(sorted({str(label) for label in raw if str(label)}))


def _priority(raw: object) -> str:
    value = str(raw if raw is not None else "").strip()
    if value.upper().startswith("P"):
        return value.upper()
    return f"P{value}" if value else "P2"


def _dependency(raw: object) -> StreamDependency | None:
    if not isinstance(raw, dict):
        return None
    issue_id = str(raw.get("issue_id") or "")
    depends_on_id = str(raw.get("depends_on_id") or "")
    dep_type = str(raw.get("type") or "")
    if not issue_id or not depends_on_id or not dep_type:
        return None
    return StreamDependency(issue_id=issue_id, depends_on_id=depends_on_id, type=dep_type)


def _work_dependency(raw: object, hive: str) -> WorkDependency | None:
    dep = _dependency(raw)
    if dep is None or not isinstance(raw, dict):
        return None
    created_at = raw.get("created_at")
    created_by = raw.get("created_by")
    return WorkDependency(
        id=projection_id("work-dependency", (hive, dep.issue_id, dep.depends_on_id, dep.type)),
        hive=hive,
        issue_id=dep.issue_id,
        depends_on_id=dep.depends_on_id,
        type=dep.type,
        created_at=str(created_at) if created_at is not None else None,
        created_by=str(created_by) if created_by is not None else None,
    )


def _entry_labels(entry: dict) -> tuple[str, str, str]:
    return (
        f"provider:{entry['provider']}",
        f"org:{entry['org']}",
        f"repo:{entry['repo']}",
    )


def project_operator_core(accepted: AcceptedExport) -> SnapshotProjection:
    """Project WorkDependency and Assignment from the already accepted export records."""

    dependencies: list[WorkDependency] = []
    assignments: list[Assignment] = []
    for record in accepted.records:
        raw_dependencies = record.raw.get("dependencies")
        if isinstance(raw_dependencies, list | tuple):
            for raw_dependency in raw_dependencies:
                dependency = _work_dependency(raw_dependency, record.issue.hive)
                if dependency is not None:
                    dependencies.append(dependency)
        if record.issue.assignee:
            assignments.append(
                Assignment(
                    id=projection_id("assignment", (record.issue.hive, record.issue.id)),
                    hive=record.issue.hive,
                    issue_id=record.issue.id,
                    seat=record.issue.assignee,
                )
            )
    return SnapshotProjection(
        accepted=accepted,
        issues=tuple(
            sorted((record.issue for record in accepted.records), key=lambda item: item.id)
        ),
        work_dependencies=tuple(sorted(dependencies, key=lambda item: item.id)),
        assignments=tuple(sorted(assignments, key=lambda item: item.id)),
        partial_reason=accepted.partial_reason,
    )


def _partial_reason(
    core_reason: str | None,
    gate_source_reason: str | None,
    gate_projection_reason: str | None,
) -> str | None:
    """Return the normative degradation precedence for one coalesced scope refresh."""

    return core_reason or gate_source_reason or gate_projection_reason


class PollingStateStreamProvider:
    """The first concrete ``StateStreamProvider``, backed by engine JSONL exports.

    ``backend`` is injected only at this internal adapter boundary.  Consumers depend on
    ``StateStreamProvider`` and canonical frames, so replacing this implementation with an
    event log, database connection, or remote service does not change their API.
    """

    name = "polling"

    def __init__(
        self,
        cfg: dict | None = None,
        *,
        backend=None,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        history_size: int = DEFAULT_HISTORY_SIZE,
        sleeper: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        process_scope: StreamProcessScope | None = None,
    ) -> None:
        self._cfg = cfg if cfg is not None else config.load()
        self._backend = backend if backend is not None else engine.get_engine(self._cfg)
        self._poll_interval = max(0.0, poll_interval)
        self._history_size = max(1, history_size)
        self._sleep = sleeper
        self._now = now or (lambda: datetime.now(UTC))
        self._monotonic = monotonic
        self._process_scope = process_scope
        self._instance = uuid.uuid4().hex
        self._states_lock = threading.Lock()
        self._states: dict[tuple[StreamScope, str], _RefreshState] = {}
        self._entries_by_labels = {
            _entry_labels(entry): entry for entry in self._cfg.get("managed_repos", []) or []
        }

    def _state(self, request: StreamRequest) -> _RefreshState:
        key = _scope_key(request)
        with self._states_lock:
            return self._states.setdefault(key, _RefreshState())

    def _target(self, request: StreamRequest) -> Path:
        if request.scope is StreamScope.FACTORY:
            return config.hq_dir()
        if request.scope is StreamScope.HUB:
            return config.hub_dir()
        entry = registry.resolve_hive(self._cfg, request.hive or "")
        return registry.hive_dir(entry)

    def _entry_for_labels(self, labels: tuple[str, ...]) -> dict | None:
        values = set(labels)
        exact = tuple(
            next((label for label in values if label.startswith(f"{kind}:")), "")
            for kind in ("provider", "org", "repo")
        )
        entry = self._entries_by_labels.get(exact)
        if entry is not None:
            return entry
        repo = exact[2].removeprefix("repo:")
        matches = [
            candidate
            for candidate in self._cfg.get("managed_repos", []) or []
            if repo and str(candidate.get("repo")) == repo
        ]
        return matches[0] if len(matches) == 1 else None

    def _normalize_issue(
        self, raw: object, request: StreamRequest
    ) -> tuple[StreamIssue | None, str | None]:
        if not isinstance(raw, dict) or raw.get("_type", "issue") != "issue":
            return None, "invalid_export_record"
        issue_id = str(raw.get("id") or "")
        title = str(raw.get("title") or "")
        updated_at = str(raw.get("updated_at") or "")
        if not issue_id or not title or not updated_at:
            return None, "invalid_export_record"

        labels = _labels(raw.get("labels"))
        if request.scope is StreamScope.HIVE:
            hive = request.hive or ""
        else:
            entry = self._entry_for_labels(labels)
            if entry is None:
                return None, "hive_identity_unavailable"
            hive = _hive_slug(entry)

        dependencies: list[StreamDependency] = []
        raw_dependencies = raw.get("dependencies")
        partial_reason = None
        if not isinstance(raw_dependencies, list | tuple):
            raw_dependencies = ()
            partial_reason = "dependency_data_unavailable"
        for item in raw_dependencies:
            dep = _dependency(item)
            if dep is None:
                partial_reason = "invalid_dependency"
                continue
            dependencies.append(dep)
        dependencies.sort(key=lambda dep: (dep.issue_id, dep.depends_on_id, dep.type))
        parent_id = raw.get("parent_id") or raw.get("parent")
        if not parent_id:
            parent_id = next(
                (
                    dep.depends_on_id
                    for dep in dependencies
                    if dep.issue_id == issue_id and dep.type == "parent-child"
                ),
                None,
            )

        return (
            StreamIssue(
                id=issue_id,
                hive=hive,
                issue_type=str(raw.get("issue_type") or "task"),
                status=str(raw.get("status") or "open"),
                priority=_priority(raw.get("priority")),
                title=title,
                updated_at=updated_at,
                labels=labels,
                assignee=str(raw["assignee"]) if raw.get("assignee") is not None else None,
                parent_id=str(parent_id) if parent_id is not None else None,
                dependencies=tuple(dependencies),
                description=str(raw.get("description") or ""),
                design=str(raw.get("design") or ""),
                acceptance_criteria=str(
                    raw.get("acceptance_criteria") or raw.get("acceptance") or ""
                ),
                notes=str(raw.get("notes") or ""),
                mol_type=(
                    str(raw.get("mol_type") or raw.get("molecule_type"))
                    if raw.get("mol_type") or raw.get("molecule_type")
                    else None
                ),
                owner=str(raw["owner"]) if raw.get("owner") is not None else None,
                created_by=(str(raw["created_by"]) if raw.get("created_by") is not None else None),
                created_at=(str(raw["created_at"]) if raw.get("created_at") is not None else None),
                closed_at=str(raw["closed_at"]) if raw.get("closed_at") is not None else None,
                due_at=str(raw["due_at"]) if raw.get("due_at") is not None else None,
                defer_until=(
                    str(raw["defer_until"]) if raw.get("defer_until") is not None else None
                ),
                lease_expires_at=(
                    str(raw["lease_expires_at"])
                    if raw.get("lease_expires_at") is not None
                    else None
                ),
            ),
            partial_reason,
        )

    def _accept_export(self, raw_lines: list[str], request: StreamRequest) -> AcceptedExport:
        records: list[AcceptedExportRecord] = []
        partial_reason = None
        for line in raw_lines:
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except ValueError:
                partial_reason = partial_reason or "invalid_export_record"
                continue
            normalized, reason = self._normalize_issue(raw, request)
            partial_reason = partial_reason or reason
            if normalized is not None:
                records.append(AcceptedExportRecord(raw=raw, issue=normalized))
        records.sort(key=lambda record: record.issue.id)
        return AcceptedExport(records=tuple(records), partial_reason=partial_reason)

    def _read_gate_rows(self, target: Path) -> tuple[tuple[object, ...], str | None]:
        """Perform the one scope-level all-states gate read, degrading on source failure."""

        try:
            if self._process_scope is None:
                result = self._backend.list_gates(target)
            else:
                command = getattr(self._backend, "stream_gate_list_command", None)
                if command is None:
                    return (), "gate_source_unavailable"
                result = self._process_scope.run(
                    list(command(target)),
                    label=(
                        f"{getattr(self._backend, 'name', 'backend')} stream gate list "
                        f"against {target}"
                    ),
                )
        except Exception:
            return (), "gate_source_unavailable"
        if result.returncode:
            return (), "gate_source_unavailable"
        try:
            data = json.loads(result.stdout)
        except (TypeError, ValueError):
            return (), "gate_source_unavailable"
        if not isinstance(data, list):
            return (), "gate_source_unavailable"
        return tuple(data), None

    def _read_export(self, request: StreamRequest) -> ProviderSnapshot:
        target = self._target(request)
        with tempfile.TemporaryDirectory(prefix="bh-state-stream-") as temp:
            exported = Path(temp) / "issues.jsonl"
            if self._process_scope is None:
                result = self._backend.export_jsonl(target, exported)
            else:
                result = self._process_scope.export_jsonl(self._backend, target, exported)
            if result.returncode or not exported.is_file():
                detail = (getattr(result, "stderr", "") or "").strip().splitlines()
                tail = detail[-1] if detail else f"exit {result.returncode}"
                raise PollingSnapshotError(
                    f"state export failed for {request.scope.value} scope: {tail}"
                )
            raw_lines = exported.read_text(encoding="utf-8").splitlines()

        accepted = self._accept_export(raw_lines, request)
        projection = project_operator_core(accepted)
        raw_gates, gate_source_reason = self._read_gate_rows(target)
        as_of_instant = self._now().astimezone(UTC)
        gate_projection = project_gate_requests(
            raw_gates,
            request=request,
            work_dependencies=projection.work_dependencies,
            as_of=as_of_instant,
        )
        projection = replace(
            projection,
            epic_schedules=project_epic_schedules(accepted),
            gate_requests=gate_projection.gate_requests,
            partial_reason=_partial_reason(
                projection.partial_reason,
                gate_source_reason,
                gate_projection.partial_reason,
            ),
        )
        digest_input = {
            "scope": request.scope.value,
            "hive": request.hive,
            "issues": [asdict(item) for item in projection.issues],
            "work_dependencies": [asdict(item) for item in projection.work_dependencies],
            "gate_requests": [asdict(item) for item in projection.gate_requests],
            "epic_schedules": [asdict(item) for item in projection.epic_schedules],
            "assignments": [asdict(item) for item in projection.assignments],
            "partial_reason": projection.partial_reason,
        }
        digest = hashlib.sha256(
            json.dumps(digest_input, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        as_of = as_of_instant.isoformat().replace("+00:00", "Z")
        return ProviderSnapshot(
            scope=request.scope,
            revision=f"{self._instance}:{digest}",
            as_of=as_of,
            issues=projection.issues,
            work_dependencies=projection.work_dependencies,
            gate_requests=projection.gate_requests,
            epic_schedules=projection.epic_schedules,
            assignments=projection.assignments,
            partial=projection.partial_reason is not None,
            partial_reason=projection.partial_reason,
        )

    def refresh(self, request: StreamRequest) -> ProviderSnapshot:
        """Return fresh state, sharing one in-flight export among same-scope callers."""

        state = self._state(request)
        with state.condition:
            if (
                state.snapshot is not None
                and state.refreshed_at is not None
                and self._monotonic() - state.refreshed_at < self._poll_interval
            ):
                return state.snapshot
            if state.refreshing:
                generation = state.generation
                while state.refreshing and state.generation == generation:
                    state.condition.wait()
                if state.error is not None:
                    raise state.error
                assert state.snapshot is not None
                return state.snapshot
            state.refreshing = True

        try:
            snapshot = self._read_export(request)
        except Exception as exc:
            with state.condition:
                state.error = exc
                state.refreshing = False
                state.generation += 1
                state.condition.notify_all()
            raise

        with state.condition:
            state.snapshot = snapshot
            state.refreshed_at = self._monotonic()
            state.error = None
            state.history[snapshot.revision] = snapshot
            state.history.move_to_end(snapshot.revision)
            while len(state.history) > self._history_size:
                state.history.popitem(last=False)
            state.refreshing = False
            state.generation += 1
            state.condition.notify_all()
        return snapshot

    def _retained(self, request: StreamRequest) -> ProviderSnapshot | None:
        if not request.since_revision:
            return None
        state = self._state(request)
        with state.condition:
            return state.history.get(request.since_revision)

    def updates(self, request: StreamRequest) -> Iterator[ProviderEvent]:
        retained = self._retained(request)
        current = self.refresh(request)
        if retained is not None:
            yield retained
            if retained.revision != current.revision:
                yield current
        else:
            yield current
        last_revision = current.revision

        while True:
            self._sleep(self._poll_interval)
            try:
                current = self.refresh(request)
            except PollingSnapshotError:
                yield ProviderReset(
                    scope=request.scope,
                    reason=ResyncReason.ADAPTER_ERROR,
                    as_of=self._now().astimezone(UTC).isoformat().replace("+00:00", "Z"),
                )
                while True:
                    self._sleep(self._poll_interval)
                    try:
                        current = self.refresh(request)
                    except PollingSnapshotError:
                        continue
                    break
                yield current
                last_revision = current.revision
                continue
            if current.revision == last_revision:
                continue
            yield current
            last_revision = current.revision


def get_polling_provider(
    cfg: dict | None = None, *, process_scope: StreamProcessScope | None = None
) -> StateStreamProvider:
    """Construct today's provider behind the stable port return type."""

    return PollingStateStreamProvider(cfg, process_scope=process_scope)
