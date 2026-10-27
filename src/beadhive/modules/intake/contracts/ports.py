"""The Protocols of the intake module (external-intake-module-adr.md section 1).

:class:`IntakePort` is the one consumer-facing port. The outbound ports are intake's own narrow
ones: :class:`ReportStore` (the bead store holding report beads), :class:`CheckpointStore` (opaque
resume tokens) and :class:`LabelPolicy` (the closed label vocabulary). No bd argv, label string or
store path crosses :class:`IntakePort`. Standard library plus this module's domain records.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from ..domain import (
    BeadView,
    Checkpoint,
    DiscoveryOutcome,
    DiscoveryRequest,
    DispositionOutcome,
    DispositionRequest,
    DuplicatePair,
    ImportOutcome,
    ImportRequest,
    IssueBinding,
    LinkOutcome,
    LinkRequest,
    QueueQuery,
    QueueView,
    ReportDraft,
    SourceSnapshot,
    StoreScope,
)


@runtime_checkable
class IntakePort(Protocol):
    """What consumers (CLI, MCP, the work layer's merge and release hooks) may ask of intake.

    Further operations are added to this port, not beside it."""

    def file_report(self, draft: ReportDraft) -> str: ...  # bh report, bh escalate

    def import_issue(self, request: ImportRequest) -> ImportOutcome: ...  # bh work import <url>

    def link(self, request: LinkRequest) -> LinkOutcome: ...  # bh work link

    def unlink(self, report: str, *, actor: str) -> LinkOutcome: ...  # bh work link --remove

    def queue(self, query: QueueQuery) -> QueueView: ...  # bh work intake, MCP

    def dispose(self, request: DispositionRequest) -> DispositionOutcome: ...  # accept/reject/...

    def binding_of(self, report: str) -> IssueBinding | None: ...

    def reports_for_work(self, work: str) -> tuple[IssueBinding, ...]: ...  # merge/release hooks

    def discover(self, request: DiscoveryRequest) -> DiscoveryOutcome: ...  # scheduled discovery


@runtime_checkable
class ReportStore(Protocol):
    """Intake's own narrow bead-store port, bound to ONE operational store.

    The composition root builds it only over a hive's operational store, never a public one."""

    def scope(self) -> StoreScope: ...

    def get(self, bead: str) -> BeadView | None: ...

    def find_by_external_ref(self, url: str) -> tuple[BeadView, ...]: ...  # every status

    def create(self, draft: ReportDraft) -> str: ...  # one create, external_ref included

    def set_state(
        self, bead: str, dimension: str, value: str, *, reason: str, actor: str
    ) -> None: ...

    def record_snapshot(self, bead: str, snapshot: SourceSnapshot, *, actor: str) -> None: ...

    def update_fields(self, bead: str, *, issue_type: str, priority: str, actor: str) -> None: ...

    def close(self, bead: str, *, reason: str, actor: str) -> None: ...

    def assign(self, bead: str, seat: str, *, actor: str) -> None: ...

    def list_open_labeled(self, label: str) -> tuple[BeadView, ...]: ...

    def duplicate_pairs(self, *, threshold: float) -> tuple[DuplicatePair, ...]: ...

    def tracked_work(self, report: str) -> str | None: ...

    def track(self, report: str, work: str, *, actor: str) -> None: ...

    def untrack(self, report: str, work: str, *, actor: str) -> None: ...

    def trackers_of(self, work: str) -> tuple[str, ...]: ...


@runtime_checkable
class CheckpointStore(Protocol):
    """Opaque, local, non-secret resume tokens keyed per binding (not configuration)."""

    def get(self, binding_key: str) -> Checkpoint | None: ...

    def put(self, binding_key: str, checkpoint: Checkpoint) -> None: ...


@runtime_checkable
class LabelPolicy(Protocol):
    """The closed label vocabulary a new bead's labels are checked against before a create."""

    def violations(self, labels: Sequence[str]) -> tuple[str, ...]: ...


__all__ = ["CheckpointStore", "IntakePort", "LabelPolicy", "ReportStore"]
