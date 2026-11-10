"""The one-process batch send and the outbox id index (``bh-pc9rf``) on ``MemoryTracker``.

The same wiring as the comment, label and close sender tests (``OutboxService`` over a counting
``MemoryReportStore``, ``MemoryTracker`` / ``MemoryWorld``, the never-waiting
``InProcessIntakeLock``, ``SequentialIdSource``, ``RecordingSanitizer``) plus the counting
``MemoryOutboxIndex``. It pins the bead's acceptance structurally, never by elapsed time:

* ``send_approved`` sends every approved operation in ONE batch scope, ordered per report comment,
  labels, close, each with its own single-use grant and receipt; a failure or an unknown outcome
  on one item is reported and the rest continue; the labels render from ONE issue listing;
* ``show`` / ``approve`` / ``send`` find a record with ONE report read through the index, scan
  only on a miss or a wrong answer, and rebuild the index from that scan;
* (``bh-25fvb``) a report's operations go under ONE hold, the label batch writes a report's
  approvals ONCE (each still its own grant), the labels sync drafts in one batch scope, and one
  batch sends a record's labels, type and fields in order with their receipts (``--kind type``
  only the type), on the issue-values ``Hive`` harness.
"""

from __future__ import annotations

import dataclasses
from collections import Counter
from contextlib import contextmanager

import pytest

from beadhive.modules.intake.application import IntakeService, OutboxService
from beadhive.modules.intake.domain import (
    SEND_ORDER,
    BatchSend,
    CloseReason,
    DraftRequest,
    GrantStatus,
    IntakeRefused,
    IssueBinding,
    IssueStatus,
    LabelBase,
    LabelIntent,
    LeakPolicy,
    OpKind,
    OpState,
    OutboxApprove,
    OutboxDocument,
    OutboxEvent,
    OutboxPolicy,
    OutboxQuery,
    OwnIdentity,
    RefusalReason,
    RenderContext,
    ReplyDraft,
    ReportDraft,
    SourceSnapshot,
    StorePosture,
    labels_revision,
)
from beadhive.modules.intake.domain import outbox as machine
from beadhive.modules.tracker.contracts import (
    READ_OPERATIONS,
    WRITE_OPERATIONS,
    IssueState,
    OwnMarker,
    join_marker_line,
)
from beadhive.testing.intake import (
    SCOPE,
    InProcessIntakeLock,
    MemoryOutboxIndex,
    MemoryReportStore,
    RecordingSanitizer,
    SequentialIdSource,
)
from beadhive.testing.tracker import MemoryTracker, MemoryWorld, at

ME = OwnIdentity("acme/widgets", "inst-1")
LEAKS = LeakPolicy(bead_prefixes=frozenset({"acme", "pub"}), owned_label_prefixes=("beadhive:",))
EVERY = OutboxPolicy(enabled=True, labels_enabled=True, close_enabled=True)
PRIVATE = StorePosture(published=False)
C, L, X = OpKind.COMMENT, OpKind.LABELS, OpKind.CLOSE
EVERYTHING = READ_OPERATIONS | WRITE_OPERATIONS
ISSUE_LABELS = ("bug", "beadhive:triage")


def marker_line(marker: OwnMarker) -> str:
    return f"<!-- bh:v1 hive={marker.hive} install={marker.install} outbox={marker.outbox_id} -->"


class _NoLabels:
    def violations(self, labels) -> tuple[str, ...]:
        return ()


class CountingStore(MemoryReportStore):
    """``MemoryReportStore`` counting the reads a lookup or a batch makes."""

    def __init__(self, scope) -> None:
        super().__init__(scope)
        self.calls: Counter[str] = Counter()

    def read_outbox(self, bead):
        self.calls["read_outbox"] += 1
        return super().read_outbox(bead)

    def read_outboxes(self):
        self.calls["read_outboxes"] += 1
        return super().read_outboxes()

    def reports_with_outbox(self):
        self.calls["reports_with_outbox"] += 1
        return super().reports_with_outbox()

    def get(self, bead):
        """A report read outside every hold of the binding's lock is a read of its own, apart
        from the hold's one session (``bh-gjo05``): counted as ``get_unheld``."""
        lock = getattr(self, "lock", None)
        self.calls["get_unheld" if lock is not None and not lock.active else "get_held"] += 1
        return super().get(bead)


class ActiveLock(InProcessIntakeLock):
    """Says whether a hold is open right now (``active``), for the store to tell reads apart."""

    active = 0

    def hold(self, binding_key):
        inner = super().hold(binding_key)

        @contextmanager
        def counted():
            with inner as epoch:
                self.active += 1
                try:
                    yield epoch
                finally:
                    self.active -= 1

        return counted()


class DemotableLock(InProcessIntakeLock):
    """Refuses every hold as ``NOT_PRIMARY`` once ``demoted`` (the host lost the primary), or
    after ``allow`` more holds."""

    demoted = False
    allow: int | None = None

    def hold(self, binding_key):
        if self.allow is not None:
            self.demoted, self.allow = self.allow <= 0, self.allow - 1
        if self.demoted:
            raise IntakeRefused(RefusalReason.NOT_PRIMARY, "not the primary")
        return super().hold(binding_key)


class Scene:
    """Bound reports on open memory issues, wired as the root wires them, with an index."""

    def __init__(self, *, policy: OutboxPolicy = EVERY, lock: InProcessIntakeLock | None = None):
        self.world = MemoryWorld()
        self.world.set_write_time(at(2))
        self.tracker = MemoryTracker(self.world, enabled=frozenset(EVERYTHING))
        self.store = CountingStore(SCOPE)
        self.lock = lock or InProcessIntakeLock()
        self.ids = SequentialIdSource()
        self.sanitizer = RecordingSanitizer.of([])
        self.index = MemoryOutboxIndex()
        self.service = OutboxService(
            self.store,
            self.tracker,
            self.lock,
            ids=self.ids,
            identity=ME,
            marker_line=marker_line,
            policy=policy,
            leak_policy=LEAKS,
            sanitizer=self.sanitizer,
            posture=PRIVATE,
            holder="host-1:42",
            index=self.index,
        )
        self.intake = IntakeService(self.store, _NoLabels(), outbox=self.service)

    def new_report(self, title: str):
        issue = self.world.add_issue(title, at=at(1), labels=ISSUE_LABELS)
        binding = IssueBinding("", "memory", self.world.locator, issue.url, issue.identifier)
        report = self.store.create(
            ReportDraft(
                title,
                "bug",
                "origin:github",
                "dev/import",
                binding=binding,
                snapshot=SourceSnapshot(title, "", IssueStatus.OPEN, "r1", labels=ISSUE_LABELS),
            )
        )
        return report, issue

    def binding(self, report: str) -> IssueBinding:
        view = self.store.get(report)
        assert view is not None and view.binding is not None
        return view.binding

    def reply(self, report: str, *, close: CloseReason | None = None) -> str:
        text = "This works as designed; see the configuration guide."
        return self.intake.draft_reply(ReplyDraft(report, text, "alice", close=close)).outbox_id

    def draft_labels(self, report: str) -> str:
        """A label-projection record for the report (the labels sync's job, done by hand, so it
        bypasses the index exactly as every other drafter does)."""
        doc = self.store.read_outbox(report) or OutboxDocument()
        oid = self.ids.outbox_id()
        intent = LabelIntent(frozenset({"beadhive:rejected"}), frozenset({"beadhive:triage"}))
        binding = self.binding(report)
        ctx = RenderContext(
            binding.url,
            LEAKS,
            self.sanitizer,
            labels=LabelBase("r1", frozenset(ISSUE_LABELS)),
            join_marker_line=join_marker_line,
        )
        request = DraftRequest(
            binding,
            OutboxEvent.LABELS,
            labels_revision(doc, binding.url, intent),
            "outbox",
            comment=False,
            labels=intent,
        )
        applied = machine.draft(doc, request, outbox_id=oid, policy=self.service._policy, ctx=ctx)
        self.store.write_outbox(report, applied.document, expect_seq=doc.seq, actor="outbox")
        return oid

    def approve(self, oid: str, kind: OpKind):
        rendered = self.intake.render(oid).of(kind)
        assert rendered is not None
        return self.intake.approve(OutboxApprove(oid, rendered.short_digest, "alice", kind=kind))

    def op(self, report: str, oid: str, kind: OpKind):
        doc = self.store.read_outbox(report)
        assert doc is not None
        return doc.records[oid].op(kind)

    def closing_report(self, title: str):
        """One report with a reply carrying a close and a labels record, all three approved."""
        report, issue = self.new_report(title)
        reply = self.reply(report, close=CloseReason.NOT_PLANNED)
        labels = self.draft_labels(report)
        self.approve(reply, C)
        self.approve(reply, X)
        self.approve(labels, L)
        return report, issue, reply, labels


def refused(expected: RefusalReason, call, /, *args, **kwargs) -> IntakeRefused:
    with pytest.raises(IntakeRefused) as caught:
        call(*args, **kwargs)
    assert caught.value.reason is expected, caught.value
    return caught.value


# -- the batch ------------------------------------------------------------------------------------


def test_every_operation_kind_has_its_place_in_the_send_order() -> None:
    """A kind missing from SEND_ORDER would never be sent by a batch: the reopen goes first (it
    precedes the reply that explains it), the comments next, the close last."""
    assert sorted(SEND_ORDER) == sorted(OpKind) and len(set(SEND_ORDER)) == len(SEND_ORDER)
    assert SEND_ORDER[:2] == (OpKind.REOPEN, C) and SEND_ORDER[-1] is X


def test_every_approved_op_goes_in_one_batch_comment_then_labels_then_close() -> None:
    s = Scene()
    first, first_issue, reply1, labels1 = s.closing_report("one")
    second, second_issue, reply2, labels2 = s.closing_report("two")
    grants = {
        (oid, kind): s.op(report, oid, kind).approval.grant_id
        for report, oid, kind in (
            (first, reply1, C),
            (first, reply1, X),
            (first, labels1, L),
            (second, reply2, C),
            (second, reply2, X),
            (second, labels2, L),
        )
    }
    reads_before = s.world.read_requests()
    s.store.calls.clear()

    outcome = s.intake.send_approved(BatchSend("alice"))

    assert outcome.ok and not outcome.failed
    order = [(r.report, r.outbox_id, r.kind) for r in outcome.results]
    by_report = {first: [reply1, labels1, reply1], second: [reply2, labels2, reply2]}
    for report in (first, second):
        assert [(o, k) for rep, o, k in order if rep == report] == list(
            zip(by_report[report], (C, L, X), strict=True)
        )
    assert all(r.state is OpState.SENT for r in outcome.results)
    for (oid, kind), grant in grants.items():  # every op spent ITS OWN grant, with a receipt
        report = first if oid in (reply1, labels1) else second
        op = s.op(report, oid, kind)
        assert op.state is OpState.SENT and op.receipt is not None
        assert op.approval.grant_id == grant and op.approval.grant is GrantStatus.CONSUMED
    assert len(set(grants.values())) == 6
    assert s.world.write_requests() == 6  # one request per operation
    assert s.world.issues[first_issue.identifier].state is IssueState.CLOSED
    assert s.world.issues[second_issue.identifier].state is IssueState.CLOSED
    assert s.lock.batches == 1 and len(s.lock.batched) == 2  # one scope, a hold per report
    assert s.store.calls["read_outboxes"] == 1  # one pass over the outbox documents
    assert s.world.read_requests() - reads_before <= 2  # one issue listing (a page or two)


def test_the_batch_takes_each_binding_from_the_listing_not_one_read_per_report() -> None:
    """``bh-gjo05``: the outbox listing's rows carry the binding, so no report is read on its own
    before its hold; every operation is still sent in order with its own grant and receipt."""
    s = Scene(lock=ActiveLock())
    s.store.lock = s.lock
    reports = [s.closing_report(f"r{n}") for n in range(3)]
    s.store.calls.clear()

    outcome = s.intake.send_approved(BatchSend("alice"))

    assert outcome.ok and not outcome.failed and len(outcome.results) == 9
    assert s.store.calls["get_unheld"] == 0  # no per-report binding read
    # a listing for the bindings (the documents' own pass may list too), never one per report
    assert s.store.calls["reports_with_outbox"] <= 2
    assert len(s.lock.batched) == 3  # a hold per report, as before
    for report, _, reply, labels in reports:
        assert [(r.outbox_id, r.kind) for r in outcome.results if r.report == report] == [
            (reply, C),
            (labels, L),
            (reply, X),
        ]
        assert s.op(report, reply, X).receipt is not None


def test_a_listing_without_the_binding_falls_back_to_one_read_per_report() -> None:
    """A listing whose rows lack the binding's project (a list row without metadata) cannot
    stand for the binding: that report is read once, before its hold, exactly as ``send`` does."""
    s = Scene(lock=ActiveLock())
    s.store.lock = s.lock
    reports = [s.closing_report(f"r{n}") for n in range(3)]
    listed = s.store.reports_with_outbox

    def bare():
        return tuple(
            dataclasses.replace(v, binding=dataclasses.replace(v.binding, project=""))
            for v in listed()
        )

    s.store.reports_with_outbox = bare
    s.store.calls.clear()

    outcome = s.intake.send_approved(BatchSend("alice"))

    assert outcome.ok and not outcome.failed and len(outcome.results) == 9
    assert s.store.calls["get_unheld"] == 3  # one binding read per report
    assert all(s.op(r, reply, X).receipt is not None for r, _, reply, _ in reports)


def test_the_batch_narrows_to_a_kind_and_to_reports() -> None:
    s = Scene()
    first, _, reply1, labels1 = s.closing_report("one")
    second, _, reply2, labels2 = s.closing_report("two")

    labels_only = s.intake.send_approved(BatchSend("alice", kinds=(L,)))
    assert [(r.outbox_id, r.kind) for r in labels_only.results] == [(labels1, L), (labels2, L)]
    assert s.op(first, reply1, C).state is OpState.APPROVED

    one_report = s.intake.send_approved(BatchSend("alice", reports=(second,)))
    assert [(r.outbox_id, r.kind) for r in one_report.results] == [(reply2, C), (reply2, X)]
    assert s.op(first, reply1, C).state is s.op(first, reply1, X).state is OpState.APPROVED

    assert s.intake.send_approved(BatchSend("alice", reports=(second,))).results == ()


def test_one_unknown_outcome_is_reported_and_the_rest_continue() -> None:
    s = Scene()
    first, first_issue, reply1, labels1 = s.closing_report("one")
    second, _, reply2, labels2 = s.closing_report("two")
    s.world.fail_next_write_ambiguously(applied=False)  # the first comment's one request

    outcome = s.intake.send_approved(BatchSend("alice"))

    assert not outcome.ok
    assert s.op(first, reply1, C).state is OpState.UNKNOWN
    assert s.op(first, labels1, L).state is OpState.SENT  # the labels still went
    # ... but the close never goes out ahead of its comment: refused before any request
    (failure,) = outcome.failed
    assert (failure.outbox_id, failure.kind, failure.reason) == (reply1, X, "close_not_ready")
    assert s.op(first, reply1, X).state is OpState.APPROVED
    assert s.world.issues[first_issue.identifier].state is IssueState.OPEN
    assert all(s.op(second, oid, k).state is OpState.SENT for oid, k in ((reply2, C), (labels2, L)))
    assert s.op(second, reply2, X).state is OpState.SENT
    assert [r.outbox_id for r in outcome.unsettled] == [reply1]


def test_nothing_approved_sends_nothing_and_takes_no_lock() -> None:
    s = Scene()
    report, _ = s.new_report("one")
    s.reply(report)  # drafted, never approved
    holds = list(s.lock.holds)

    outcome = s.intake.send_approved(BatchSend("alice"))

    assert outcome.results == () and outcome.failed == () and outcome.ok
    assert s.world.write_requests() == 0 and s.lock.batches == 0 and s.lock.holds == holds


def test_a_named_kind_that_is_off_refuses_before_any_request() -> None:
    s = Scene(policy=OutboxPolicy(enabled=True, labels_enabled=True))  # the close gate is off
    report, _ = s.new_report("one")
    s.approve(s.reply(report), C)

    refused(RefusalReason.CLOSE_DISABLED, s.intake.send_approved, BatchSend("alice", kinds=(X,)))

    assert s.world.write_requests() == 0 and s.lock.batches == 0


def test_losing_the_primary_stops_the_batch_and_reports_what_was_sent() -> None:
    lock = DemotableLock()
    s = Scene(lock=lock)
    first, _, reply1, labels1 = s.closing_report("one")
    second, _, reply2, labels2 = s.closing_report("two")
    lock.allow = 1  # the first report's hold is the last one granted: then the host is demoted

    outcome = s.intake.send_approved(BatchSend("alice"))

    assert [(r.outbox_id, r.kind, r.state) for r in outcome.results] == [
        (reply1, C, OpState.SENT),
        (labels1, L, OpState.SENT),
        (reply1, X, OpState.SENT),
    ]
    assert [(f.report, f.outbox_id, f.kind, f.reason) for f in outcome.failed] == [
        (second, reply2, C, "not_primary"),
        (second, labels2, L, "not_primary"),
        (second, reply2, X, "not_primary"),
    ]
    assert outcome.stopped == "not the primary" and not outcome.ok
    assert s.world.write_requests() == 3  # nothing after the refusal was tried


def test_the_label_batch_approves_inside_one_batch_scope() -> None:
    from beadhive.modules.intake.domain import LabelBatchApprove

    s = Scene()
    first, _ = s.new_report("one")
    second, _ = s.new_report("two")
    s.draft_labels(first)
    s.draft_labels(second)
    batch = s.intake.review_label_batch()

    outcome = s.intake.approve_label_batch(LabelBatchApprove(batch.short_digest, "alice"))

    assert len(outcome.approved) == 2 and len(set(outcome.grant_ids)) == 2
    assert s.lock.batches == 1 and len(s.lock.batched) == 2
    sent = s.intake.send_approved(BatchSend("alice", kinds=(L,)))
    assert [r.state for r in sent.results] == [OpState.SENT, OpState.SENT]


# -- the index -----------------------------------------------------------------------------------


def _many(s: Scene, n: int) -> list[tuple[str, str]]:
    pairs = []
    for i in range(n):
        report, _ = s.new_report(f"report {i}")
        pairs.append((report, s.reply(report)))
    return pairs


def test_a_drafted_reply_is_indexed_and_found_with_one_report_read() -> None:
    s = Scene()
    pairs = _many(s, 5)
    report, oid = pairs[-1]
    assert s.index.records[oid] == report
    s.store.calls.clear()

    rendered = s.intake.render(oid)

    assert rendered.report == report
    assert s.store.calls["read_outbox"] == 1 and s.store.calls["read_outboxes"] == 0


def test_a_miss_scans_once_and_rebuilds_the_index() -> None:
    s = Scene()
    report, _ = s.new_report("one")
    oid = s.draft_labels(report)  # a drafter that never touches the index
    assert oid not in s.index.records
    s.store.calls.clear()

    assert s.intake.render(oid).report == report

    assert s.store.calls["read_outboxes"] == 1 and s.index.rebuilds == 1
    assert s.index.records[oid] == report
    s.store.calls.clear()
    s.intake.render(oid)
    assert s.store.calls["read_outboxes"] == 0 and s.store.calls["read_outbox"] == 1


def test_a_wrong_index_answer_is_never_trusted() -> None:
    s = Scene()
    (first, oid), (second, _) = _many(s, 2)
    s.index.records[oid] = second  # a stale or corrupted entry

    assert s.intake.render(oid).report == first

    assert s.index.records[oid] == first  # the scan rebuilt it
    refused(RefusalReason.UNKNOWN_OUTBOX, s.intake.render, "ob1_" + "a" * 26)


def test_the_listing_rebuilds_the_index_from_its_one_pass() -> None:
    s = Scene()
    report, _ = s.new_report("one")
    oid = s.draft_labels(report)
    s.store.calls.clear()

    listing = s.intake.outbox(OutboxQuery())

    assert [e.record.outbox_id for e in listing.entries] == [oid]
    assert s.store.calls["read_outboxes"] == 1  # one pass, not a listing plus a read each
    assert s.index.records == {oid: report}


def test_an_index_hint_the_store_cannot_read_is_a_miss() -> None:
    from beadhive.modules.intake.domain import IntakeStoreError

    s = Scene()
    (first, oid), (second, _) = _many(s, 2)
    s.index.records[oid] = "acme-gone"  # a bead the store no longer answers for
    real = s.store.read_outbox

    def read(bead):
        if bead == "acme-gone":
            raise IntakeStoreError("acme-gone not found", bead=bead, operation="read")
        return real(bead)

    s.store.read_outbox = read

    assert s.intake.render(oid).report == first
    assert s.index.records[oid] == first


def test_without_an_index_the_sender_still_scans() -> None:
    s = Scene()
    report, _ = s.new_report("one")
    oid = s.reply(report)
    s.service._index = None

    assert s.intake.render(oid).report == report


# -- the issue type and fields through send_approved (bh-25fvb, from the bh-pc9rf re-review) -----


def _projected_record():
    """A ``Hive`` (the issue-values harness) whose ONE labels record carries a labels, a type
    and a fields operation, each approved by the batch with its own grant."""
    from tests.unit.modules.intake.test_intake_issue_values import Hive

    hive = Hive()
    report, key = hive.report("a crash")
    hive.work(report, issue_type="feature", priority="1")
    hive.define()
    (item,) = hive.sync().items
    _, approved = hive.approve()
    assert [r.kind for r in approved.approved] == [L, OpKind.TYPE, OpKind.FIELDS]
    grants = {r.kind: r.grant_id for r in approved.approved}
    assert len(set(grants.values())) == 3
    return hive, report, key, item.drafted, grants


def test_one_batch_sends_labels_then_type_then_fields_each_with_its_receipt() -> None:
    hive, report, key, oid, grants = _projected_record()
    writes = hive.world.write_requests()
    batches, batched = hive.lock.batches, len(hive.lock.batched)

    outcome = hive.intake.send_approved(BatchSend("alice"))

    assert outcome.ok and not outcome.failed
    assert [(r.report, r.outbox_id, r.kind, r.state) for r in outcome.results] == [
        (report, oid, L, OpState.SENT),
        (report, oid, OpKind.TYPE, OpState.SENT),
        (report, oid, OpKind.FIELDS, OpState.SENT),
    ]
    assert hive.world.write_requests() - writes == 3  # one request per operation
    record = hive.record(report, oid)
    for op in record.operations:  # each spent ITS OWN grant and holds its own receipt
        assert op.state is OpState.SENT and op.receipt is not None
        assert op.approval.grant_id == grants[op.kind]
        assert op.approval.grant is GrantStatus.CONSUMED
    receipts = {op.kind: op.receipt for op in record.operations}
    assert receipts[OpKind.TYPE].values_after == (("type", "Feature"),)
    assert receipts[OpKind.FIELDS].values_after == (("Priority", "High"),)
    assert hive.world.server_issue_type(key) == "Feature"
    assert hive.world.server_issue_fields(key) == {"Priority": "High"}
    assert "bh:type/feature" in hive.world.server_labels(key)
    assert hive.lock.batches - batches == 1  # one batch scope ...
    assert len(hive.lock.batched) - batched == 1  # ... and the report's three ops in ONE hold


def test_send_approved_with_kind_type_sends_only_the_type() -> None:
    hive, report, key, oid, grants = _projected_record()
    labels_before = hive.world.server_labels(key)
    writes = hive.world.write_requests()

    outcome = hive.intake.send_approved(BatchSend("alice", kinds=(OpKind.TYPE,)))

    assert [(r.outbox_id, r.kind, r.state) for r in outcome.results] == [
        (oid, OpKind.TYPE, OpState.SENT)
    ]
    assert hive.world.write_requests() - writes == 1
    assert hive.world.server_issue_type(key) == "Feature"
    assert hive.world.server_issue_fields(key) == {}
    assert hive.world.server_labels(key) == labels_before
    record = hive.record(report, oid)
    assert record.op(OpKind.TYPE).approval.grant is GrantStatus.CONSUMED
    for kind in (L, OpKind.FIELDS):  # still approved, their grants unspent
        assert record.op(kind).state is OpState.APPROVED
        assert record.op(kind).approval.grant_id == grants[kind]
        assert record.op(kind).approval.grant is GrantStatus.MINTED


def test_the_label_batch_writes_a_reports_approvals_once_each_with_its_own_grant() -> None:
    from tests.unit.modules.intake.test_intake_issue_values import Hive

    hive = Hive()
    reports = [hive.report(f"crash {n}") for n in range(2)]
    for report, _ in reports:
        hive.work(report, issue_type="feature", priority="1")
    hive.define()
    hive.sync()
    writes: list[str] = []
    real = hive.store.write_outbox

    def counted(bead, document, *, expect_seq, actor):
        writes.append(bead)
        return real(bead, document, expect_seq=expect_seq, actor=actor)

    hive.store.write_outbox = counted
    holds = len(hive.lock.holds)

    _, outcome = hive.approve()

    assert [r.kind for r in outcome.approved] == [L, OpKind.TYPE, OpKind.FIELDS] * 2
    assert len(set(outcome.grant_ids)) == 6  # every operation its own single-use grant
    assert sorted(writes) == sorted(report for report, _ in reports)  # ONE write per report
    assert len(hive.lock.holds) - holds == 2  # one hold per report
    for report, _ in reports:
        (record,) = hive.store.read_outbox(report).records.values()
        assert all(op.state is OpState.APPROVED for op in record.operations)


def test_the_labels_sync_drafts_inside_one_batch_scope() -> None:
    from tests.unit.modules.intake.test_intake_issue_values import Hive

    hive = Hive()
    for n in range(3):
        hive.report(f"crash {n}")
    hive.define()
    batches, batched = hive.lock.batches, len(hive.lock.batched)

    outcome = hive.sync()

    assert len(outcome.drafted) == 3
    assert hive.lock.batches - batches == 1  # the lock may reuse one host check (bh-25fvb)
    assert len(hive.lock.batched) - batched == 3  # each draft still holds the lock itself
