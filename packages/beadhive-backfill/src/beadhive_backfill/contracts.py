"""Contracts of the hive apply envelope: policy passes, store loaders and the audit sink.

Shapes only, no behavior (``docs/design/backfill-platform-adr.md`` sections 1 and 2). The
envelope lifted from ``complexity_backfill`` (plan hash, drift gate against a fresh baseline,
pre-state export, audit, rollback, idempotency proof) is built on these ports; root wires the
bd :class:`StoreLoader`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from beadlog import BeadEvent
from beadlog_sources import JudgmentPort


@runtime_checkable
class HivePass(Protocol):
    """A hive-policy backfill pass (complexity labels, identity labels, lifecycle conventions).

    A pass proposes events against a baseline; classification against that baseline is
    reconcile's job, not the pass's.
    """

    @property
    def name(self) -> str:
        """Stable pass name, e.g. ``complexity``."""
        ...

    @property
    def schema_version(self) -> str:
        """Version of the pass's plan semantics, recorded in the plan manifest."""
        ...

    @property
    def invalidating_fields(self) -> frozenset[str]:
        """Folded facets whose change since planning invalidates a plan (the corpus projection)."""
        ...

    @property
    def drift_applicable(self) -> bool:
        """Whether DRIFT proposals of this pass may be applied (otherwise only reported)."""
        ...

    def propose(
        self,
        baseline: Sequence[BeadEvent],
        *,
        extracted_at: str,
        judge: JudgmentPort,
    ) -> Iterable[BeadEvent]:
        """Emit proposed events for the hive whose current history is ``baseline``."""
        ...


@runtime_checkable
class StoreLoader(Protocol):
    """Maps BeadLog events onto one store, and reads the store back as a baseline BeadLog."""

    @property
    def name(self) -> str: ...

    @property
    def version(self) -> str: ...

    @property
    def capabilities(self) -> frozenset[str]:
        """Loader capabilities (``beadlog.LoaderCapability`` values) reported in loss reports."""
        ...

    def export(self) -> Iterable[BeadEvent]:
        """The store's current history as events (re-emitting stored event ids)."""
        ...

    def load(self, events: Sequence[BeadEvent], *, dry_run: bool) -> Mapping[str, Any]:
        """Apply (or, with ``dry_run``, only assess) ``events``; return the loss report object
        (``docs/schemas/beadlog/v0.1/loss-report.schema.json``). A dry run and the apply of the
        same events report the same losses."""
        ...


class AuditState(StrEnum):
    """Lifecycle of one apply run, as recorded in its audit."""

    APPLYING = "applying"
    APPLIED = "applied"
    ROLLED_BACK = "rolled_back"
    ROLLBACK_FAILED = "rollback_failed"


@dataclass(frozen=True)
class AuditProgress:
    """How far an apply got: counts of planned / attempted / completed units and the next one."""

    planned: int
    attempted: int
    completed: int
    next_index: int
    current: str | None = None


@dataclass(frozen=True)
class AuditRecovery:
    """What an operator needs to recover an interrupted apply."""

    pre_state_artifact: str
    uncertain: str | None = None
    rollback_complete: bool | None = None


@dataclass(frozen=True)
class AuditEntry:
    """One durable audit record; the ``applying`` record exists before the first write."""

    pass_name: str
    plan_hash: str
    state: AuditState
    progress: AuditProgress
    recovery: AuditRecovery
    error: str | None = None


@runtime_checkable
class AuditSink(Protocol):
    """Durable destination of audit records (a run journal, a file beside the plan)."""

    def record(self, entry: AuditEntry) -> None:
        """Persist ``entry`` durably before returning; raise if it cannot be persisted."""
        ...


__all__ = [
    "AuditEntry",
    "AuditProgress",
    "AuditRecovery",
    "AuditSink",
    "AuditState",
    "HivePass",
    "StoreLoader",
]
