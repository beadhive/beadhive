"""The supervised ``issue.close`` sender (``bh-pr5ll``) end to end on ``MemoryTracker``.

The same wiring as the comment and label sender tests (``OutboxService`` over
``MemoryReportStore``, ``MemoryTracker`` / ``MemoryWorld``, the never-waiting
``InProcessIntakeLock``, ``SequentialIdSource``, ``RecordingSanitizer``), for a close. It pins the
bead's acceptance: a close rides on the reply with its own gate, digest, single-use grant and
receipt; it is sendable only after that record's comment AND the report's latest labels record
are sent (or there is none), refused before any request otherwise; an ambiguous close reconciles by
reading the issue's state back; and the backfill drafts the closes of reports already closed in
the store whose issue is still open, once.

Ordering is asserted on states, ``write_requests()`` and the world's issue state only. No clock, no
sleep, no socket, and no real tracker: every write lands in the in-memory world.
"""

from __future__ import annotations

import dataclasses

import pytest

from beadhive.modules.intake.application import IntakeService, OutboxService
from beadhive.modules.intake.domain import (
    CanonicalIssue,
    CloseBackfill,
    CloseReason,
    DraftRequest,
    GrantStatus,
    IntakeRefused,
    IssueBinding,
    IssueStatus,
    LabelBase,
    LabelIntent,
    LeakPolicy,
    MergedFact,
    OpKind,
    OpState,
    OutboxApprove,
    OutboxDocument,
    OutboxEvent,
    OutboxPolicy,
    OwnIdentity,
    ProposalDisposition,
    ProposalStatus,
    RefusalReason,
    ReleasedFact,
    RenderContext,
    ReplyDraft,
    ReportDraft,
    SourceSnapshot,
    StorePosture,
    UnlinkedFact,
    labels_revision,
)
from beadhive.modules.intake.domain import outbox as machine
from beadhive.modules.intake.domain.proposal import new_proposal
from beadhive.modules.tracker.contracts import (
    READ_OPERATIONS,
    WRITE_OPERATIONS,
    IssueState,
    Operation,
    OwnMarker,
    join_marker_line,
)
from beadhive.testing.intake import (
    SCOPE,
    InProcessIntakeLock,
    MemoryReportStore,
    RecordingSanitizer,
    SequentialIdSource,
)
from beadhive.testing.tracker import MemoryTracker, MemoryWorld, at

ME = OwnIdentity("acme/widgets", "inst-1")
LEAKS = LeakPolicy(bead_prefixes=frozenset({"acme", "pub"}), owned_label_prefixes=("beadhive:",))
CLOSING = OutboxPolicy(enabled=True, labels_enabled=True, close_enabled=True)
NO_CLOSE = OutboxPolicy(enabled=True, labels_enabled=True)  # the close gate is off by default
PRIVATE = StorePosture(published=False)
C, L, X = OpKind.COMMENT, OpKind.LABELS, OpKind.CLOSE
EVERYTHING = READ_OPERATIONS | WRITE_OPERATIONS
ISSUE_LABELS = ("bug", "beadhive:triage")
REPLY = "This works as designed; see the configuration guide."


def marker_line(marker: OwnMarker) -> str:
    return f"<!-- bh:v1 hive={marker.hive} install={marker.install} outbox={marker.outbox_id} -->"


class _NoLabels:
    def violations(self, labels) -> tuple[str, ...]:
        return ()


class Scene:
    """One bound report on one open memory issue, wired as the root wires it."""

    def __init__(self, *, policy: OutboxPolicy = CLOSING, enabled=EVERYTHING) -> None:
        self.world = MemoryWorld()
        self.world.set_write_time(at(2))
        self.tracker = MemoryTracker(self.world, enabled=frozenset(enabled))
        self.store = MemoryReportStore(SCOPE)
        self.lock = InProcessIntakeLock()
        self.ids = SequentialIdSource()
        self.sanitizer = RecordingSanitizer.of([])
        self.policy = policy
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
        )
        self.intake = IntakeService(self.store, _NoLabels(), outbox=self.service)
        self.report, self.issue = self.new_report("a bug")

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

    def binding(self, report: str | None = None) -> IssueBinding:
        view = self.store.get(report or self.report)
        assert view is not None and view.binding is not None
        return view.binding

    def reply(self, *, close: CloseReason | None = CloseReason.NOT_PLANNED, report=None) -> str:
        draft = ReplyDraft(report or self.report, REPLY, "alice", close=close)
        return self.intake.draft_reply(draft).outbox_id

    def draft_labels(self, report: str | None = None) -> str:
        """A label-projection record for the report (the labels sync's job, done by hand)."""
        report = report or self.report
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
        applied = machine.draft(doc, request, outbox_id=oid, policy=self.policy, ctx=ctx)
        self.store.write_outbox(report, applied.document, expect_seq=doc.seq, actor="outbox")
        return oid

    def approve(self, oid: str, kind: OpKind, *, actor: str = "alice"):
        rendered = self.intake.render(oid).of(kind)
        assert rendered is not None
        return self.intake.approve(OutboxApprove(oid, rendered.short_digest, actor, kind=kind))

    def send(self, oid: str, kind: OpKind):
        return self.intake.send(oid, actor="alice", kind=kind)

    def op(self, oid: str, kind: OpKind, report: str | None = None):
        doc = self.store.read_outbox(report or self.report)
        assert doc is not None
        return doc.records[oid].op(kind)

    def issue_state(self, issue=None) -> IssueState:
        return self.world.issues[(issue or self.issue).identifier].state


def refused(expected: RefusalReason, call, /, *args, **kwargs) -> IntakeRefused:
    with pytest.raises(IntakeRefused) as caught:
        call(*args, **kwargs)
    assert caught.value.reason is expected, caught.value
    return caught.value


# -- the gate ------------------------------------------------------------------------------------


def test_the_close_gate_is_off_by_default_and_a_reply_is_then_drafted_without_a_close() -> None:
    assert OutboxPolicy().close_enabled is False
    s = Scene(policy=NO_CLOSE)
    oid = s.reply()
    assert s.op(oid, C) is not None and s.op(oid, X) is None
    refused(RefusalReason.CLOSE_DISABLED, s.intake.send, oid, actor="alice", kind=X)
    refused(RefusalReason.CLOSE_DISABLED, s.intake.backfill_closes, CloseBackfill("alice"))
    assert s.world.write_requests() == 0


def test_a_close_needs_the_adapters_issue_close_to_be_usable() -> None:
    s = Scene(enabled=EVERYTHING - {Operation.ISSUE_CLOSE})
    oid = s.reply()
    refused(RefusalReason.CAPABILITY_UNUSABLE, s.approve, oid, X)
    assert s.op(oid, X).state is OpState.DRAFTED and s.world.write_requests() == 0


# -- one record, three gates, a fixed order ------------------------------------------------------


def test_the_close_rides_on_the_reply_with_its_own_digest_and_grant() -> None:
    s = Scene()
    oid = s.reply()
    rendered = s.intake.render(oid)
    close = rendered.of(X)
    assert close is not None and close.payload == "state: closed\nstate_reason: not_planned"
    assert close.close is CloseReason.NOT_PLANNED and close.digest != rendered.of(C).digest
    assert close.findings == ()

    comment, closing = s.approve(oid, C), s.approve(oid, X)

    assert comment.grant_id and closing.grant_id and comment.grant_id != closing.grant_id
    assert s.op(oid, C).state is s.op(oid, X).state is OpState.APPROVED
    assert s.world.write_requests() == 0  # approving sends nothing


def test_a_close_before_its_comment_is_refused_before_any_request() -> None:
    s = Scene()
    oid = s.reply()
    s.approve(oid, C)
    s.approve(oid, X)

    error = refused(RefusalReason.CLOSE_NOT_READY, s.send, oid, X)

    assert "comment" in error.detail
    assert s.world.write_requests() == 0 and s.issue_state() is IssueState.OPEN
    close = s.op(oid, X)
    assert close.state is OpState.APPROVED and close.attempts == ()
    assert close.approval.grant is GrantStatus.MINTED  # the grant is not spent by a refusal


def test_comment_then_labels_then_close_each_with_its_receipt() -> None:
    s = Scene()
    oid = s.reply()
    labels = s.draft_labels()
    for kind in (C, X):
        s.approve(oid, kind)
    s.approve(labels, L)

    assert s.send(oid, C).state is OpState.SENT
    error = refused(RefusalReason.CLOSE_NOT_READY, s.send, oid, X)  # the labels are not sent
    assert labels in error.detail and s.issue_state() is IssueState.OPEN
    assert s.send(labels, L).state is OpState.SENT
    done = s.send(oid, X)

    assert done.state is OpState.SENT and done.kind is X
    assert s.issue_state() is IssueState.CLOSED
    assert s.world.issues[s.issue.identifier].closed_reason == "not_planned"
    assert s.world.server_labels(s.issue) == {"bug", "beadhive:rejected"}  # the labels stayed
    assert s.world.comment_count(s.issue) == 1 and s.world.write_requests() == 3
    receipt = s.op(oid, X).receipt
    assert receipt is not None and receipt.state_after == "closed" and not receipt.reconciled
    assert s.op(oid, X).approval.grant is GrantStatus.CONSUMED


def test_with_no_labels_record_the_close_follows_the_comment() -> None:
    s = Scene()
    oid = s.reply(close=CloseReason.DUPLICATE)
    for kind in (C, X):
        s.approve(oid, kind)
    s.send(oid, C)
    assert s.send(oid, X).state is OpState.SENT
    assert s.world.issues[s.issue.identifier].closed_reason == "duplicate"


def test_an_unsent_newer_labels_record_holds_the_close_back_again() -> None:
    s = Scene()
    oid = s.reply()
    first = s.draft_labels()
    s.approve(first, L)
    s.approve(oid, C)
    s.approve(oid, X)
    s.send(oid, C)
    s.send(first, L)
    newer = s.draft_labels()  # a re-sync after the first labels were sent
    assert newer != first
    error = refused(RefusalReason.CLOSE_NOT_READY, s.send, oid, X)
    assert newer in error.detail and s.issue_state() is IssueState.OPEN


def test_an_ambiguous_close_is_unknown_and_reconciles_by_reading_the_state() -> None:
    s = Scene()
    oid = s.reply()
    for kind in (C, X):
        s.approve(oid, kind)
    s.send(oid, C)
    s.world.fail_next_write_ambiguously()  # applied, but the sender cannot know

    assert s.send(oid, X).state is OpState.UNKNOWN
    (result,) = s.intake.reconcile(oid, actor="alice")

    assert result.kind is X and result.state is OpState.SENT and result.complete
    receipt = s.op(oid, X).receipt
    assert receipt is not None and receipt.reconciled and receipt.state_after == "closed"
    assert s.world.write_requests() == 2  # never re-sent


def test_an_absent_close_counts_absences_from_complete_issue_reads() -> None:
    s = Scene()
    oid = s.reply()
    for kind in (C, X):
        s.approve(oid, kind)
    s.send(oid, C)
    s.world.fail_next_write_ambiguously(applied=False)  # cut off before it was applied
    s.send(oid, X)

    s.intake.reconcile(oid, actor="alice")
    (second,) = s.intake.reconcile(oid, actor="alice")

    assert second.state is OpState.FAILED and s.op(oid, X).fail_reason == machine.ABSENT
    assert s.issue_state() is IssueState.OPEN and s.world.write_requests() == 2


# -- the backfill --------------------------------------------------------------------------------


def _closed_by_triage(s: Scene, report: str, disposition: ProposalDisposition) -> None:
    proposal = dataclasses.replace(new_proposal(disposition), status=ProposalStatus.APPLIED)
    s.store.write_proposal(report, proposal, expect_seq=0, actor="alice")
    s.store.close(report, reason="triaged", actor="alice")


def _sent_reply(s: Scene, report: str) -> str:
    oid = s.reply(close=None, report=report)
    rendered = s.intake.render(oid).of(C)
    s.intake.approve(OutboxApprove(oid, rendered.short_digest, "alice"))
    s.intake.send(oid, actor="alice")
    return oid


def test_the_backfill_drafts_one_close_per_closed_report_whose_issue_is_open() -> None:
    s = Scene()
    reject = s.report
    duplicate, _ = s.new_report("a duplicate")
    asked, _ = s.new_report("needs more info")
    shut, shut_issue = s.new_report("closed on both sides")
    silent, _ = s.new_report("no reply yet")
    for report, disposition in (
        (reject, ProposalDisposition.REJECT),
        (duplicate, ProposalDisposition.DUPLICATE_OF),
        (asked, ProposalDisposition.NEEDS_INFO),
        (shut, ProposalDisposition.FIXED),
        (silent, ProposalDisposition.FIXED),
    ):
        if report != silent:
            _sent_reply(s, report)
        _closed_by_triage(s, report, disposition)
    s.world.update_issue(shut_issue, at=at(3), state=IssueState.CLOSED)
    writes = s.world.write_requests()

    planned = s.intake.backfill_closes(CloseBackfill("alice", dry_run=True))

    assert planned.dry_run and planned.drafted == ()
    assert {(i.report, i.reason) for i in planned.planned} == {
        (reject, CloseReason.NOT_PLANNED),
        (duplicate, CloseReason.DUPLICATE),
    }
    # no triage binding here, so a fixed report's template text cannot be computed
    assert [(i.report, i.detail) for i in planned.skipped] == [
        (silent, "no reply to carry the close, and no reviewed text to draft one from")
    ]
    assert s.op(planned.planned[0].outbox_id, X, planned.planned[0].report) is None

    done = s.intake.backfill_closes(CloseBackfill("alice"))

    assert {i.report for i in done.drafted} == {reject, duplicate}
    for item in done.drafted:
        close = s.op(item.outbox_id, X, item.report)
        assert close is not None and close.state is OpState.DRAFTED and close.close is item.reason
        assert close.history[0].rule == "O1" and close.history[0].detail == "backfill"
    assert s.world.write_requests() == writes  # drafted only: nothing approved or sent

    again = s.intake.backfill_closes(CloseBackfill("alice"))
    assert again.drafted == () and again.planned == ()
    assert {i.detail for i in again.skipped} >= {"its reply already carries a close"}


def test_a_backfilled_close_is_sent_after_its_already_sent_reply() -> None:
    s = Scene()
    oid = _sent_reply(s, s.report)
    _closed_by_triage(s, s.report, ProposalDisposition.REJECT)
    (item,) = s.intake.backfill_closes(CloseBackfill("alice")).drafted
    assert item.outbox_id == oid

    s.approve(oid, X)
    assert s.send(oid, X).state is OpState.SENT
    assert s.issue_state() is IssueState.CLOSED


# -- review follow-ups: a missing reply is drafted, and nothing overtakes the close ------------


def _applied(s: Scene, report: str, disposition: ProposalDisposition, **fields) -> None:
    proposal = new_proposal(disposition, **fields)
    stored = dataclasses.replace(proposal, status=ProposalStatus.APPLIED)
    s.store.write_proposal(report, stored, expect_seq=0, actor="alice")
    s.store.close(report, reason="triaged", actor="alice")


def test_the_backfill_drafts_the_missing_reply_with_its_close_once() -> None:
    s = Scene()
    canonical = s.report  # bound to #1
    duplicate, _ = s.new_report("a duplicate that never got its reply (#75)")
    rejected, _ = s.new_report("a reject whose reply was never drafted")
    _applied(s, duplicate, ProposalDisposition.DUPLICATE_OF, share_with=canonical)
    _applied(s, rejected, ProposalDisposition.REJECT, reporter_text="Works as designed.")
    writes = s.world.write_requests()

    planned = s.intake.backfill_closes(CloseBackfill("alice", dry_run=True))

    texts = {i.report: i.reply_text for i in planned.planned}
    assert texts == {
        duplicate: "This is a duplicate of #1; the work is tracked there.",
        rejected: "Works as designed.",
    }
    assert s.store.read_outbox(duplicate) is None and s.store.read_outbox(rejected) is None

    done = s.intake.backfill_closes(CloseBackfill("alice"))

    assert {i.report for i in done.drafted} == {duplicate, rejected}
    for item in done.drafted:
        (record,) = s.store.read_outbox(item.report).records.values()
        assert record.event is OutboxEvent.REPLY and record.message.text == texts[item.report]
        assert record.op(C).state is OpState.DRAFTED  # the reply waits for its own approval
        assert record.op(X).close is item.reason and record.op(X).state is OpState.DRAFTED
    assert s.world.write_requests() == writes  # drafted only

    again = s.intake.backfill_closes(CloseBackfill("alice"))
    assert again.drafted == () and again.planned == ()
    assert {i.detail for i in again.skipped} == {"its reply already carries a close"}
    for report in (duplicate, rejected):
        assert len(s.store.read_outbox(report).records) == 1  # never a second reply

    # both digests are approved by the operator, then the reply goes out before the close
    (item,) = [i for i in done.drafted if i.report == duplicate]
    s.approve(item.outbox_id, C)
    s.approve(item.outbox_id, X)
    refused(RefusalReason.CLOSE_NOT_READY, s.send, item.outbox_id, X)
    s.send(item.outbox_id, C)
    assert s.send(item.outbox_id, X).state is OpState.SENT


def test_a_newer_unsent_comment_on_the_report_holds_the_close() -> None:
    s = Scene()
    oid = s.reply()
    for kind in (C, X):
        s.approve(oid, kind)
    s.send(oid, C)
    later = s.intake.draft_reply(ReplyDraft(s.report, "One more detail.", "alice")).outbox_id

    error = refused(RefusalReason.CLOSE_NOT_READY, s.send, oid, X)

    assert later in error.detail and "send or discard" in error.detail
    assert s.issue_state() is IssueState.OPEN and s.op(oid, X).attempts == ()
    s.intake.discard(later, reason="said in the first reply", actor="alice")
    assert s.send(oid, X).state is OpState.SENT


def test_an_abandoned_latest_labels_record_asks_for_a_re_sync() -> None:
    s = Scene()
    oid = s.reply()
    labels = s.draft_labels()
    for kind in (C, X):
        s.approve(oid, kind)
    s.send(oid, C)
    s.intake.discard(labels, reason="not now", actor="alice")

    error = refused(RefusalReason.CLOSE_NOT_READY, s.send, oid, X)

    assert labels in error.detail and "re-sync the labels" in error.detail
    assert "send it first" not in error.detail and s.issue_state() is IssueState.OPEN


# -- a duplicate closes as duplicate OF its canonical (bh-iamru) -------------------------------


def test_a_duplicate_close_names_the_canonical_reports_issue_and_the_tracker_records_it() -> None:
    s = Scene()
    canonical, canonical_issue = s.report, s.issue
    member, member_issue = s.new_report("the same bug again")
    draft = ReplyDraft(member, REPLY, "alice", close=CloseReason.DUPLICATE, duplicate_of=canonical)
    oid = s.intake.draft_reply(draft).outbox_id

    close = s.op(oid, X, member)
    assert close.duplicate_of == CanonicalIssue(canonical_issue.identifier, canonical_issue.url)
    rendered = s.intake.render(oid).of(X)
    assert rendered.payload == (
        f"state: closed\nstate_reason: duplicate\nduplicate_of: {canonical_issue.url}"
    )
    assert rendered.findings == ()  # the canonical's URL is the tracker's own, never a leak

    for kind in (C, X):
        s.approve(oid, kind)
    s.send(oid, C)
    assert s.send(oid, X).state is OpState.SENT
    row = s.world.issues[member_issue.identifier]
    assert row.state is IssueState.CLOSED and row.closed_reason == "duplicate"
    assert s.world.duplicate_of(member_issue) == canonical_issue.url
    assert s.issue_state(canonical_issue) is IssueState.OPEN  # the canonical is never touched


def test_a_canonical_with_no_issue_in_this_project_closes_as_a_plain_duplicate() -> None:
    s = Scene()
    member, member_issue = s.new_report("a duplicate")
    native = s.store.create(ReportDraft("filed natively", "bug", "origin:report", "dev/report"))
    elsewhere = s.store.create(
        ReportDraft(
            "bound to another project",
            "bug",
            "origin:github",
            "dev/import",
            binding=IssueBinding("", "memory", "acme/other", "memory://acme/other/issues/1", "#1"),
        )
    )
    for n, canonical in enumerate((native, elsewhere, "acme-missing", member)):
        text = f"{REPLY} ({n})"  # a fresh draft key each time
        reply = ReplyDraft(
            member, text, "alice", close=CloseReason.DUPLICATE, duplicate_of=canonical
        )
        oid = s.intake.draft_reply(reply).outbox_id
        assert s.op(oid, X, member).duplicate_of is None, canonical
        assert "duplicate_of" not in s.intake.render(oid).of(X).payload


def test_the_backfill_names_the_canonical_of_a_duplicate() -> None:
    s = Scene()
    canonical, canonical_issue = s.report, s.issue
    replied, _ = s.new_report("a duplicate that got its reply")
    silent, _ = s.new_report("a duplicate that never got one")
    _sent_reply(s, replied)
    for report in (replied, silent):
        _applied(s, report, ProposalDisposition.DUPLICATE_OF, share_with=canonical)

    planned = s.intake.backfill_closes(CloseBackfill("alice", dry_run=True))
    assert {(i.report, i.duplicate_of) for i in planned.planned} == {
        (replied, canonical_issue.url),
        (silent, canonical_issue.url),
    }

    done = s.intake.backfill_closes(CloseBackfill("alice"))

    assert {i.report for i in done.drafted} == {replied, silent}
    for item in done.drafted:
        close = s.op(item.outbox_id, X, item.report)
        assert close.duplicate_of == CanonicalIssue(canonical_issue.identifier, canonical_issue.url)


# -- tracked reports closed locally at delivery with the issue still open (bh-iyog8) -----------


def _closed_at(s: Scene, report: str, delivery: str, **facts) -> None:
    """A tracked report closed locally at ``delivery`` before completion carried a close."""
    s.store.set_state(report, "delivery", delivery, reason="work shipped", actor="alice")
    doc = s.store.read_outbox(report) or OutboxDocument()
    kept = dataclasses.replace(
        doc.facts,
        merged=MergedFact("bh-w1", "c0ffee0123456789"),
        released=ReleasedFact("v1.2.3", "c0ffee0123456789", True)
        if delivery == "released"
        else None,
        **facts,
    )
    s.store.write_outbox(
        report,
        dataclasses.replace(doc, seq=doc.seq + 1, facts=kept),
        expect_seq=doc.seq,
        actor="alice",
    )
    s.store.close(report, reason=f"delivery complete ({delivery})", actor="alice")


def test_backfill_attaches_the_close_to_the_latest_sent_reply_of_a_tracked_report() -> None:
    s = Scene()
    oid = _sent_reply(s, s.report)
    _closed_at(s, s.report, "released")
    writes = s.world.write_requests()

    (planned,) = s.intake.backfill_closes(CloseBackfill("alice", dry_run=True)).planned
    assert planned.outbox_id == oid and planned.reason is CloseReason.COMPLETED
    assert s.op(oid, X) is None  # a dry run writes nothing

    (item,) = s.intake.backfill_closes(CloseBackfill("alice")).drafted
    assert item.outbox_id == oid
    close = s.op(oid, X)
    assert close.state is OpState.DRAFTED and close.close is CloseReason.COMPLETED
    assert s.world.write_requests() == writes  # drafted only

    # the comment is already sent: the close approves and sends on its own
    s.approve(oid, X)
    assert s.send(oid, X).state is OpState.SENT
    assert s.issue_state() is IssueState.CLOSED


def test_backfill_drafts_the_completion_record_when_a_tracked_report_has_none() -> None:
    s = Scene()
    released, released_issue = s.report, s.issue
    merged, _ = s.new_report("merged only")
    _closed_at(s, released, "released")
    _closed_at(s, merged, "merged")

    planned = s.intake.backfill_closes(CloseBackfill("alice", dry_run=True))
    texts = {i.report: i.reply_text for i in planned.planned}
    assert "v1.2.3" in texts[released] and "v1.2.3" not in texts[merged]
    assert s.store.read_outbox(released).records == {}

    done = s.intake.backfill_closes(CloseBackfill("alice"))

    assert {i.report for i in done.drafted} == {released, merged}
    for item in done.drafted:
        (record,) = s.store.read_outbox(item.report).records.values()
        expected = OutboxEvent.RELEASED if item.report == released else OutboxEvent.MERGED
        assert record.event is expected
        assert record.op(C).state is OpState.DRAFTED  # the comment still needs its approval
        assert record.op(X).close is CloseReason.COMPLETED and record.op(X).state is OpState.DRAFTED
    assert s.issue_state(released_issue) is IssueState.OPEN


def test_backfill_of_tracked_reports_skips_with_a_reason() -> None:
    s = Scene()
    pending, _ = s.new_report("still pending")
    unlinked, _ = s.new_report("unlinked")
    shut, shut_issue = s.new_report("issue closed already")
    carrying, _ = s.new_report("reply carries a close")
    _closed_at(s, pending, "pending")
    _closed_at(s, unlinked, "merged", unlinked=UnlinkedFact("bh-w1"))
    _closed_at(s, shut, "released")
    s.world.update_issue(shut_issue, at=at(3), state=IssueState.CLOSED)
    s.reply(close=CloseReason.COMPLETED, report=carrying)
    _closed_at(s, carrying, "released")
    s.store.close(s.report, reason="never tracked", actor="alice")  # no delivery: not a candidate

    done = s.intake.backfill_closes(CloseBackfill("alice"))

    assert done.drafted == ()
    reasons = {i.report: i.detail for i in done.skipped}
    assert reasons == {
        pending: "delivery is pending: the work has not shipped",
        unlinked: "the report is unlinked from its work",
        shut: "the issue is closed already",
        carrying: "its reply already carries a close",
    }


def test_backfill_of_tracked_reports_is_idempotent() -> None:
    s = Scene()
    replied, _ = s.new_report("has a sent reply")
    silent, _ = s.new_report("has none")
    _sent_reply(s, replied)
    _closed_at(s, replied, "released")
    _closed_at(s, silent, "merged")

    first = s.intake.backfill_closes(CloseBackfill("alice"))
    again = s.intake.backfill_closes(CloseBackfill("alice"))
    dry = s.intake.backfill_closes(CloseBackfill("alice", dry_run=True))

    assert {i.report for i in first.drafted} == {replied, silent}
    assert again.drafted == () and dry.drafted == () and dry.planned == ()
    assert len(s.store.read_outbox(replied).records) == 1
    assert len(s.store.read_outbox(silent).records) == 1


def test_backfill_carries_the_close_on_the_latest_reply_record_only() -> None:
    s = Scene()
    first = _sent_reply(s, s.report)
    later = s.intake.draft_reply(
        ReplyDraft(s.report, "A later manual reply.", "alice", close=None)
    ).outbox_id
    s.intake.discard(later, reason="not wanted", actor="alice")
    newest = s.intake.draft_reply(
        ReplyDraft(s.report, "The newest reply.", "alice", close=None)
    ).outbox_id
    _closed_at(s, s.report, "released")

    (item,) = s.intake.backfill_closes(CloseBackfill("alice")).drafted

    assert item.outbox_id == newest  # the discarded one is never a carrier
    assert s.op(first, X) is None and s.op(later, X) is None
