"""The ``beadhive.testing.intake`` kit: MemoryReportStore passes every case, and the cases bite.

Each deliberately broken ``MemoryReportStore`` variant below must be rejected by the named case,
so no contract case is vacuous. Nothing here reads a clock, sleeps or opens a socket.
"""

from __future__ import annotations

import dataclasses

import pytest

from beadhive.modules.intake.contracts import (
    OUTBOX_KEY,
    THREAD_KEY,
    BeadView,
    DuplicatePair,
    ReportStore,
    outbox_to_document,
    thread_to_document,
)
from beadhive.testing import ConformanceFailure
from beadhive.testing.intake import (
    REPORT_STORE_CASES,
    MemoryReportStore,
    assert_report_store_conforms,
    memory_store,
    outbox,
    run_report_store_case,
    thread,
)

_CASES = {case.case_id: case for case in REPORT_STORE_CASES}


def test_memory_report_store_conforms_and_satisfies_the_port() -> None:
    assert_report_store_conforms(memory_store, subject="memory")
    assert isinstance(memory_store(), ReportStore)


def test_case_ids_are_unique_and_stable() -> None:
    assert len(_CASES) == len(REPORT_STORE_CASES) == 28
    assert all(case_id.startswith("store.") for case_id in _CASES)


class _AppendsState(MemoryReportStore):
    def set_state(self, bead, dimension, value, *, reason, actor):
        row = self._row(bead)
        row.view = dataclasses.replace(row.view, labels=(*row.view.labels, f"{dimension}:{value}"))


class _ClearsOnEmpty(MemoryReportStore):
    """Honours an empty value by clearing the dimension, which bd cannot do."""

    def set_state(self, bead, dimension, value, *, reason, actor):
        if value:
            return super().set_state(bead, dimension, value, reason=reason, actor=actor)
        row = self._row(bead)
        kept = tuple(label for label in row.view.labels if not label.startswith(f"{dimension}:"))
        row.view = dataclasses.replace(row.view, labels=kept)


class _QueueIncludesClosed(MemoryReportStore):
    def list_open_labeled(self, label):
        return tuple(r.view for r in self._rows.values() if label in r.view.labels)


class _ClosedFindableNoMore(MemoryReportStore):
    def find_by_external_ref(self, url):
        return tuple(
            r.view
            for r in self._rows.values()
            if r.view.external_ref == url and r.view.status == "open"
        )


class _ClosedVanishes(MemoryReportStore):
    def get(self, bead):
        view = super().get(bead)
        return None if view is not None and view.status == "closed" else view


class _CreateStampsOrigin(MemoryReportStore):
    def create(self, draft):
        bead = super().create(draft)
        self.set_state(bead, "origin", "report", reason="x", actor="a")
        return bead


class _EmptyRefMatchesNative(MemoryReportStore):
    def find_by_external_ref(self, url):
        return tuple(r.view for r in self._rows.values() if r.view.external_ref == url)


class _EmptyFieldsOverwrite(MemoryReportStore):
    def update_fields(self, bead, *, issue_type, priority, actor):
        row = self._row(bead)
        row.view = dataclasses.replace(row.view, issue_type=issue_type, priority=priority)


class _IgnoresThreshold(MemoryReportStore):
    def duplicate_pairs(self, *, threshold):
        views = [r.view for r in self._rows.values()]
        return tuple(
            DuplicatePair(a.id, b.id, 1.0) for i, a in enumerate(views) for b in views[i + 1 :]
        )


class _LinkDiesWithTheReport(MemoryReportStore):
    def trackers_of(self, work):
        return tuple(
            r
            for r in super().trackers_of(work)
            if (view := self.get(r)) is not None and view.status == "open"
        )


class _UntrackIsANoOp(MemoryReportStore):
    def untrack(self, report, work, *, actor):
        return None


class _SilentOnUnknown(MemoryReportStore):
    def close(self, bead, *, reason, actor):
        if bead in self._rows:
            super().close(bead, reason=reason, actor=actor)


class _SnapshotDropped(MemoryReportStore):
    def record_snapshot(self, bead, snapshot, *, actor):
        self._row(bead)


class _AssignDropped(MemoryReportStore):
    def assign(self, bead, seat, *, actor):
        self._row(bead)


class _IdsRepeat(MemoryReportStore):
    def create(self, draft):
        self._next = 0
        return super().create(draft)


class _ScopeDrifts(MemoryReportStore):
    def scope(self):
        self._flip = not getattr(self, "_flip", False)
        return dataclasses.replace(self._scope, operational_prefix="a" if self._flip else "b")


class _ThreadSeqUnchecked(MemoryReportStore):
    def write_thread(self, bead, thread, *, expect_seq, actor):
        stored = self.read_thread(bead)
        super().write_thread(bead, thread, expect_seq=stored.seq if stored else 0, actor=actor)


class _ThreadNeverStored(MemoryReportStore):
    def read_thread(self, bead):
        return None


class _ThreadMerged(MemoryReportStore):
    def write_thread(self, bead, thread, *, expect_seq, actor):
        stored = self.read_thread(bead)
        if stored is not None:
            thread = dataclasses.replace(thread, records={**stored.records, **thread.records})
        super().write_thread(bead, thread, expect_seq=expect_seq, actor=actor)


class _SnapshotClobbersThread(MemoryReportStore):
    def record_snapshot(self, bead, snapshot, *, actor):
        super().record_snapshot(bead, snapshot, actor=actor)
        self._row(bead).metadata.pop(THREAD_KEY, None)


class _ThreadRefusedWhenClosed(MemoryReportStore):
    def write_thread(self, bead, thread, *, expect_seq, actor):
        view = self.get(bead)
        if view is not None and view.status == "closed":
            return
        super().write_thread(bead, thread, expect_seq=expect_seq, actor=actor)


class _ThreadAnyBead(MemoryReportStore):
    def write_thread(self, bead, thread, *, expect_seq, actor):
        if bead in self._rows:
            self._row(bead).metadata[THREAD_KEY] = thread_to_document(thread)


class _ThreadForEveryBead(MemoryReportStore):
    def read_thread(self, bead):
        return super().read_thread(bead) or thread(0)


class _BoundCreateUnheld(MemoryReportStore):
    """Creates a bound report open and unheld: it would be offered as work."""

    def create(self, draft):
        bead = super().create(draft)
        for gate in self._gates.values():
            gate.open = False
        return bead


class _NativeCreateHeld(MemoryReportStore):
    """Holds a born-native report too, which would keep every `bh report` item out of ready."""

    def create(self, draft):
        bead = super().create(draft)
        if draft.binding is None:
            self.open_hold(bead, actor="a")
        return bead


class _CloseLeavesTheHold(MemoryReportStore):
    def close(self, bead, *, reason, actor):
        self._finish_close(self._row(bead), bead, reason)


class _CloseRefusedWhileHeld(MemoryReportStore):
    """A plain close: refused by the bound report's own hold, so it can never be closed."""

    def close(self, bead, *, reason, actor):
        self.plain_close(bead)


class _ResolveClosesEveryHold(MemoryReportStore):
    def resolve_hold(self, gate, *, actor):
        blocked = self._gates[gate].blocks
        for other in self._gates.values():
            if other.blocks == blocked:
                other.open = False


class _OpenHoldIgnored(MemoryReportStore):
    def open_hold(self, report, *, actor):
        self._row(report)


class _OutboxSeqUnchecked(MemoryReportStore):
    def write_outbox(self, bead, document, *, expect_seq, actor):
        stored = self.read_outbox(bead)
        super().write_outbox(bead, document, expect_seq=stored.seq if stored else 0, actor=actor)


class _OutboxMerged(MemoryReportStore):
    def write_outbox(self, bead, document, *, expect_seq, actor):
        stored = self.read_outbox(bead)
        if stored is not None:
            document = dataclasses.replace(document, records={**stored.records, **document.records})
        super().write_outbox(bead, document, expect_seq=expect_seq, actor=actor)


class _ThreadClobbersOutbox(MemoryReportStore):
    def write_thread(self, bead, thread, *, expect_seq, actor):
        super().write_thread(bead, thread, expect_seq=expect_seq, actor=actor)
        self._row(bead).metadata.pop(OUTBOX_KEY, None)


class _OutboxAnyBead(MemoryReportStore):
    def write_outbox(self, bead, document, *, expect_seq, actor):
        if bead in self._rows:
            self._row(bead).metadata[OUTBOX_KEY] = outbox_to_document(document)


class _OutboxForEveryBead(MemoryReportStore):
    def read_outbox(self, bead):
        return super().read_outbox(bead) or outbox(0, texts=())


class _WorkBornAsUntriagedReport(MemoryReportStore):
    """Creates the work bead of `accept --new-work` as a queued report: it would be triaged."""

    def create_work(self, draft):
        bead = super().create_work(draft)
        self.set_state(bead, "intake", "untriaged", reason="x", actor="a")
        return bead


class _WorkDropsPriority(MemoryReportStore):
    def create_work(self, draft):
        return super().create_work(dataclasses.replace(draft, priority=""))


_BROKEN = [
    (_AppendsState, "store.set-state-replaces-its-dimension"),
    (_ClearsOnEmpty, "store.set-state-replaces-but-never-clears"),
    (_QueueIncludesClosed, "store.queue-key-is-open-labeled"),
    (_ClosedFindableNoMore, "store.closed-stays-readable-and-findable"),
    (_ClosedVanishes, "store.closed-stays-readable-and-findable"),
    (_CreateStampsOrigin, "store.create-reads-back"),
    (_EmptyRefMatchesNative, "store.external-ref-lookup"),
    (_EmptyFieldsOverwrite, "store.update-fields-leaves-empty-unchanged"),
    (_IgnoresThreshold, "store.duplicate-pairs"),
    (_LinkDiesWithTheReport, "store.tracks-many-reports-to-one-work"),
    (_UntrackIsANoOp, "store.tracks-many-reports-to-one-work"),
    (_WorkBornAsUntriagedReport, "store.create-work-is-plain-work"),
    (_WorkDropsPriority, "store.create-work-is-plain-work"),
    (_SilentOnUnknown, "store.unknown-bead-is-a-store-error"),
    (_SnapshotDropped, "store.record-snapshot"),
    (_AssignDropped, "store.assign"),
    (_IdsRepeat, "store.create-reads-back"),
    (_ScopeDrifts, "store.scope-is-stable"),
    (_ThreadSeqUnchecked, "store.thread-write-rechecks-seq"),
    (_ThreadNeverStored, "store.thread-round-trips-whole"),
    (_ThreadMerged, "store.thread-round-trips-whole"),
    (_SnapshotClobbersThread, "store.thread-keeps-other-documents"),
    (_ThreadRefusedWhenClosed, "store.thread-on-a-closed-report"),
    (_ThreadAnyBead, "store.thread-write-needs-its-bound-report"),
    (_ThreadForEveryBead, "store.thread-absent-reads-none"),
    (_BoundCreateUnheld, "store.bound-create-opens-a-hold"),
    (_NativeCreateHeld, "store.bound-create-opens-a-hold"),
    (_CloseLeavesTheHold, "store.close-is-the-held-close"),
    (_CloseRefusedWhileHeld, "store.close-is-the-held-close"),
    (_ResolveClosesEveryHold, "store.holds-open-and-resolve-one-at-a-time"),
    (_OpenHoldIgnored, "store.holds-open-and-resolve-one-at-a-time"),
    (_OutboxSeqUnchecked, "store.outbox-write-rechecks-seq"),
    (_OutboxMerged, "store.outbox-round-trips-whole"),
    (_ThreadClobbersOutbox, "store.outbox-keeps-other-documents"),
    (_OutboxAnyBead, "store.outbox-on-every-status-and-bound-only"),
    (_OutboxForEveryBead, "store.outbox-absent-reads-none"),
]


@pytest.mark.parametrize(("broken", "case_id"), _BROKEN, ids=lambda v: getattr(v, "__name__", v))
def test_the_named_case_rejects_a_broken_store(broken, case_id) -> None:
    with pytest.raises(ConformanceFailure) as failure:
        run_report_store_case(lambda: broken(memory_store().scope()), _CASES[case_id])

    assert failure.value.case_id == case_id


def test_every_case_is_bitten_by_at_least_one_broken_store() -> None:
    assert {case_id for _, case_id in _BROKEN} == set(_CASES)


def test_unused_view_fields_default_to_a_born_native_report() -> None:
    view = BeadView("x-1", "t", "bug")
    assert (view.status, view.external_ref, view.binding, view.snapshot) == ("open", "", None, None)
