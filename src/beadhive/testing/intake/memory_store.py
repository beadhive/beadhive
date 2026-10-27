"""``MemoryReportStore``: the in-memory :class:`ReportStore` every intake consumer test uses.

No bd, no filesystem, no clock: ids come from a deterministic counter, and duplicate detection is
a token-overlap on titles (bd's mechanical ``find-duplicates`` is the real store's business). It
models only what the port promises: a state write replaces its dimension's label, a closed bead
stays readable and findable, and a link survives the report's closure.
"""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass, field

from beadhive.modules.intake.contracts import (
    BeadView,
    DuplicatePair,
    IntakeStoreError,
    ReportDraft,
    SourceSnapshot,
    StoreScope,
)


@dataclass
class _Row:
    view: BeadView
    history: list[tuple[str, str, str]] = field(default_factory=list)  # (dimension, value, reason)


def _tokens(title: str) -> frozenset[str]:
    return frozenset(re.findall(r"[a-z0-9]+", title.lower()))


class MemoryReportStore:
    """A fresh store bound to ``scope``. ``writes`` counts every mutating call, so a test can
    assert that a refusal changed nothing."""

    def __init__(self, scope: StoreScope) -> None:
        self._scope = scope
        self._rows: dict[str, _Row] = {}
        self._tracks: dict[str, str] = {}
        self._next = 0
        self.writes = 0

    # -- reads -------------------------------------------------------------------------------

    def scope(self) -> StoreScope:
        return self._scope

    def get(self, bead: str) -> BeadView | None:
        row = self._rows.get(bead)
        return row.view if row else None

    def find_by_external_ref(self, url: str) -> tuple[BeadView, ...]:
        return tuple(r.view for r in self._rows.values() if url and r.view.external_ref == url)

    def list_open_labeled(self, label: str) -> tuple[BeadView, ...]:
        return tuple(
            r.view
            for r in self._rows.values()
            if r.view.status == "open" and label in r.view.labels
        )

    def duplicate_pairs(self, *, threshold: float) -> tuple[DuplicatePair, ...]:
        open_rows = [r.view for r in self._rows.values() if r.view.status == "open"]
        pairs: list[DuplicatePair] = []
        for index, first in enumerate(open_rows):
            for second in open_rows[index + 1 :]:
                a, b = _tokens(first.title), _tokens(second.title)
                similarity = len(a & b) / len(a | b) if a | b else 0.0
                if similarity >= threshold:
                    pairs.append(DuplicatePair(first.id, second.id, similarity))
        return tuple(pairs)

    def tracked_work(self, report: str) -> str | None:
        return self._tracks.get(report)

    def trackers_of(self, work: str) -> tuple[str, ...]:
        return tuple(sorted(r for r, w in self._tracks.items() if w == work))

    # -- writes ------------------------------------------------------------------------------

    def create(self, draft: ReportDraft) -> str:
        self.writes += 1
        self._next += 1
        bead = f"{self._scope.operational_prefix}-{self._next:04d}"
        binding = draft.binding
        view = BeadView(
            id=bead,
            title=draft.title,
            issue_type=draft.issue_type,
            labels=tuple(self._scope.hive.labels()),
            source_system=binding.tracker if binding else "",
            external_ref=binding.url if binding else "",
            binding=dataclasses.replace(binding, report=bead) if binding else None,
            snapshot=draft.snapshot,
        )
        self._rows[bead] = _Row(view)
        return bead

    def set_state(self, bead: str, dimension: str, value: str, *, reason: str, actor: str) -> None:
        row = self._row(bead)
        self.writes += 1
        kept = tuple(label for label in row.view.labels if not label.startswith(f"{dimension}:"))
        row.view = dataclasses.replace(row.view, labels=(*kept, f"{dimension}:{value}"))
        row.history.append((dimension, value, reason))

    def record_snapshot(self, bead: str, snapshot: SourceSnapshot, *, actor: str) -> None:
        row = self._row(bead)
        self.writes += 1
        row.view = dataclasses.replace(row.view, snapshot=snapshot)

    def update_fields(self, bead: str, *, issue_type: str, priority: str, actor: str) -> None:
        row = self._row(bead)
        self.writes += 1
        row.view = dataclasses.replace(
            row.view,
            issue_type=issue_type or row.view.issue_type,
            priority=priority or row.view.priority,
        )

    def close(self, bead: str, *, reason: str, actor: str) -> None:
        row = self._row(bead)
        self.writes += 1
        row.view = dataclasses.replace(row.view, status="closed")
        row.history.append(("status", "closed", reason))

    def assign(self, bead: str, seat: str, *, actor: str) -> None:
        row = self._row(bead)
        self.writes += 1
        row.view = dataclasses.replace(row.view, assignee=seat)

    def track(self, report: str, work: str, *, actor: str) -> None:
        self._row(report)
        self.writes += 1
        self._tracks[report] = work

    def untrack(self, report: str, work: str, *, actor: str) -> None:
        self._row(report)
        self.writes += 1
        if self._tracks.get(report) == work:
            del self._tracks[report]

    # -- test controls (not part of the port) ------------------------------------------------

    def history(self, bead: str) -> tuple[tuple[str, str, str], ...]:
        """Every state write and the close, in order, as ``(dimension, value, reason)``."""
        return tuple(self._row(bead).history)

    def _row(self, bead: str) -> _Row:
        row = self._rows.get(bead)
        if row is None:
            raise IntakeStoreError(f"{bead} not found", bead=bead)
        return row


__all__ = ["MemoryReportStore"]
