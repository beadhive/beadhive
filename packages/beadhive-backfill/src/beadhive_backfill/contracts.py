"""Contracts of the hive apply envelope: policy passes, store ports and the audit sink.

Shapes only, no behavior (``docs/design/backfill-platform-adr.md`` sections 1 and 2). The
envelope lifted from ``complexity_backfill`` (plan hash, drift gate against a fresh baseline,
pre-state export, audit, rollback, idempotency proof) is built on these ports
(:mod:`beadhive_backfill.engine`); root wires the bd :class:`StoreReader`,
:class:`StoreLoader` and :class:`PreStateExporter`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from beadlog import BeadEvent, BeadRef, BeadState
from beadlog_sources import JudgmentPort

_EMPTY: Mapping[str, Any] = MappingProxyType({})

# What an operator does with an ``applying`` or ``rollback_failed`` audit.
RECOVERY_INSTRUCTION = "restore the exported pre-state before retrying an incomplete apply"
# What a rolled-back apply that created beads leaves behind: retired beads, not an empty store.
RETIRED_INSTRUCTION = (
    "rollback retired the beads the apply created (closed, reason backfill-rollback; never "
    "deleted): they remain in the store, and a re-run revives them in place"
)


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
        """Folded facets whose change since planning invalidates a plan (the corpus projection).

        Names: ``exists``, ``deleted``, ``erased``, ``type``, ``phase``, ``retired``,
        ``fields`` (every field) or ``fields.<name>``, ``labels``, ``edges`` and ``comments``.
        """
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
class ScopedPass(Protocol):
    """Optional pass hook: which baseline beads the pass governs.

    An excluded bead is counted (by reason) but never projected, proposed for or applied to,
    so a change to it never invalidates a plan (complexity's merge-slot exclusion, generalized).
    """

    def exclusion(self, state: BeadState) -> str | None:
        """The exclusion reason of ``state`` (e.g. ``system_artifact:gt:slot``), or ``None``."""
        ...


@runtime_checkable
class VerifyingPass(Protocol):
    """Optional pass hook: the pass's own post-apply postcondition over in-scope beads."""

    def verify(self, states: Mapping[BeadRef, BeadState]) -> Sequence[str]:
        """Postcondition violations of the in-scope ``states`` (empty when converged)."""
        ...


@runtime_checkable
class ReportingPass(Protocol):
    """Optional pass hook: a JSON report recorded in the plan (tiers, fallbacks, hints)."""

    def report(
        self, states: Mapping[BeadRef, BeadState], proposed: Sequence[BeadEvent]
    ) -> Mapping[str, Any]:
        """A JSON-serializable summary of the in-scope ``states`` and the ``proposed`` events."""
        ...


@runtime_checkable
class StoreReader(Protocol):
    """Reads a store back as a baseline BeadLog (every :class:`StoreLoader` is one)."""

    def export(self) -> Iterable[BeadEvent]:
        """The store's current history as events (re-emitting stored event ids)."""
        ...


@runtime_checkable
class PreStateExporter(Protocol):
    """Writes the recovery artifact an operator restores from; called before the first write."""

    def export_pre_state(self, baseline: Sequence[BeadEvent]) -> str:
        """Persist the pre-apply state (``baseline`` is what the engine just read) durably and
        return a reference to the artifact (a path); raise if it cannot be persisted."""
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
    """How far an apply got: counts of planned / attempted / completed units and the next one.

    A unit is one bead (``<group>/<id>``); ``current`` is the unit being written.
    """

    planned: int
    attempted: int
    completed: int
    next_index: int
    current: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "planned": self.planned,
            "attempted": self.attempted,
            "completed": self.completed,
            "next_index": self.next_index,
            "current": self.current,
        }


@dataclass(frozen=True)
class AuditRecovery:
    """What an operator needs to recover an interrupted apply. ``uncertain`` is the unit whose
    write may be partial (set before its write, cleared after)."""

    pre_state_artifact: str
    uncertain: str | None = None
    rollback_complete: bool | None = None
    instruction: str = RECOVERY_INSTRUCTION

    def to_json(self) -> dict[str, Any]:
        return {
            "pre_state_artifact": self.pre_state_artifact,
            "uncertain": self.uncertain,
            "rollback_complete": self.rollback_complete,
            "instruction": self.instruction,
        }


@dataclass(frozen=True)
class AuditEntry:
    """One durable audit record; the ``applying`` record exists before the first write.

    ``planned`` / ``attempted`` / ``completed`` list unit keys. ``rollback_failures`` are
    inverse writes that raised; ``rollback_residual`` lists applied events with no inverse by
    design (``comment``, ``tombstone``) or whose undo could not be confirmed. ``rollback_retired``
    names the beads the apply created that rollback retired (they stay in the store, closed,
    never deleted). ``losses`` counts the loader's dry-run loss codes;
    ``verification`` holds the post-apply proof (pass errors, second dry-run changes,
    unrecorded judgments).
    """

    pass_name: str
    plan_hash: str
    state: AuditState
    progress: AuditProgress
    recovery: AuditRecovery
    error: str | None = None
    corpus_hash_before: str | None = None
    corpus_hash_expected_after: str | None = None
    corpus_hash_after: str | None = None
    planned: tuple[str, ...] = ()
    attempted: tuple[str, ...] = ()
    completed: tuple[str, ...] = ()
    rollback_failures: tuple[str, ...] = ()
    rollback_residual: tuple[str, ...] = ()
    rollback_retired: tuple[str, ...] = ()
    losses: Mapping[str, int] = field(default_factory=lambda: _EMPTY, hash=False)
    verification: Mapping[str, Any] | None = field(default=None, hash=False)

    def to_json(self) -> dict[str, Any]:
        """The record as a plain JSON object (what a file sink persists)."""
        return {
            "pass": self.pass_name,
            "plan_hash": self.plan_hash,
            "state": str(self.state),
            "error": self.error,
            "corpus_hash_before": self.corpus_hash_before,
            "corpus_hash_expected_after": self.corpus_hash_expected_after,
            "corpus_hash_after": self.corpus_hash_after,
            "planned": list(self.planned),
            "attempted": list(self.attempted),
            "completed": list(self.completed),
            "rollback_failures": list(self.rollback_failures),
            "rollback_residual": list(self.rollback_residual),
            "rollback_retired": list(self.rollback_retired),
            "rollback_retired_count": len(self.rollback_retired),
            "losses": dict(sorted(self.losses.items())),
            "verification": None if self.verification is None else dict(self.verification),
            "progress": self.progress.to_json(),
            "recovery": self.recovery.to_json(),
        }


@runtime_checkable
class AuditSink(Protocol):
    """Durable destination of audit records (a run journal, a file beside the plan)."""

    def record(self, entry: AuditEntry) -> None:
        """Persist ``entry`` durably before returning; raise if it cannot be persisted."""
        ...


__all__ = [
    "RECOVERY_INSTRUCTION",
    "RETIRED_INSTRUCTION",
    "AuditEntry",
    "AuditProgress",
    "AuditRecovery",
    "AuditSink",
    "AuditState",
    "HivePass",
    "PreStateExporter",
    "ReportingPass",
    "ScopedPass",
    "StoreLoader",
    "StoreReader",
    "VerifyingPass",
]
