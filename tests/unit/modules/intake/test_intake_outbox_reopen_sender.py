"""The supervised ``issue.reopen`` sender (``bh-ih484``) end to end on ``MemoryTracker``.

The same wiring as the close sender tests (``OutboxService`` over ``MemoryReportStore``,
``MemoryTracker`` / ``MemoryWorld``, the never-waiting ``InProcessIntakeLock``), for a reopen. It
pins the bead's acceptance: a reopen rides on the reply that explains a corrected close with its own
gate (off by default), digest, single-use grant and receipt; it is sent BEFORE that reply (the reply
is refused until it is sent or retired) and per report the batch order is reopen, comment, labels,
close; an unsent close of the same issue holds the reopen back; an ambiguous reopen reconciles by
reading the issue's state back; and the operator's ``reopen`` verb drafts only for an issue that
is closed, never twice for one issue, with a dry run, and a reopen is retired with its reply.

Ordering is asserted on states, ``write_requests()`` and the world's issue state only. No clock, no
sleep, no socket, and no real tracker: every write lands in the in-memory world.
"""

from __future__ import annotations

import pytest

from beadhive.modules.intake.application import IntakeService, OutboxService
from beadhive.modules.intake.domain import (
    BatchSend,
    CloseReason,
    Disposition,
    DispositionRequest,
    GrantStatus,
    IntakeRefused,
    IssueBinding,
    IssueStatus,
    LeakPolicy,
    OpKind,
    OpState,
    OutboxApprove,
    OutboxPolicy,
    OwnIdentity,
    RefusalReason,
    ReopenRequest,
    ReplyDraft,
    ReportDraft,
    SourceSnapshot,
    StorePosture,
    reopen_reply_text,
)
from beadhive.modules.intake.domain import outbox as machine
from beadhive.modules.tracker.contracts import (
    READ_OPERATIONS,
    WRITE_OPERATIONS,
    IssueState,
    Operation,
    OwnMarker,
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
REOPENING = OutboxPolicy(enabled=True, labels_enabled=True, close_enabled=True, reopen_enabled=True)
NO_REOPEN = OutboxPolicy(enabled=True, labels_enabled=True, close_enabled=True)
PRIVATE = StorePosture(published=False)
C, X, R = OpKind.COMMENT, OpKind.CLOSE, OpKind.REOPEN
EVERYTHING = READ_OPERATIONS | WRITE_OPERATIONS
TEXT = "On a second look this needs more attention, so we are reopening this issue."


def marker_line(marker: OwnMarker) -> str:
    return f"<!-- bh:v1 hive={marker.hive} install={marker.install} outbox={marker.outbox_id} -->"


class _NoLabels:
    def violations(self, labels) -> tuple[str, ...]:
        return ()


class Scene:
    """One bound report on one CLOSED memory issue, wired as the root wires it."""

    def __init__(self, *, policy: OutboxPolicy = REOPENING, enabled=EVERYTHING) -> None:
        self.world = MemoryWorld()
        self.world.set_write_time(at(2))
        self.tracker = MemoryTracker(self.world, enabled=frozenset(enabled))
        self.store = MemoryReportStore(SCOPE)
        self.lock = InProcessIntakeLock()
        self.ids = SequentialIdSource()
        self.service = OutboxService(
            self.store,
            self.tracker,
            self.lock,
            ids=self.ids,
            identity=ME,
            marker_line=marker_line,
            policy=policy,
            leak_policy=LEAKS,
            sanitizer=RecordingSanitizer.of([]),
            posture=PRIVATE,
            holder="host-1:42",
        )
        self.intake = IntakeService(self.store, _NoLabels(), outbox=self.service)
        self.report, self.issue = self.new_report("a bug", state=IssueState.CLOSED)

    def new_report(self, title: str, *, state: IssueState = IssueState.OPEN):
        issue = self.world.add_issue(title, at=at(1), state=state, labels=("bug",))
        binding = IssueBinding("", "memory", self.world.locator, issue.url, issue.identifier)
        report = self.store.create(
            ReportDraft(
                title,
                "bug",
                "origin:github",
                "dev/import",
                binding=binding,
                snapshot=SourceSnapshot(title, "", IssueStatus.OPEN, "r1", labels=("bug",)),
            )
        )
        return report, issue

    def reply(self, *, reopen: bool = True, close: CloseReason | None = None, report=None) -> str:
        draft = ReplyDraft(report or self.report, TEXT, "alice", close=close, reopen=reopen)
        return self.intake.draft_reply(draft).outbox_id

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

    def state(self, issue=None) -> IssueState:
        return self.world.issues[(issue or self.issue).identifier].state


def refused(expected: RefusalReason, call, /, *args, **kwargs) -> IntakeRefused:
    with pytest.raises(IntakeRefused) as caught:
        call(*args, **kwargs)
    assert caught.value.reason is expected, caught.value
    return caught.value


# -- the gate ------------------------------------------------------------------------------------


def test_the_reopen_gate_is_off_by_default_and_a_reply_is_then_drafted_without_a_reopen() -> None:
    assert OutboxPolicy().reopen_enabled is False
    s = Scene(policy=NO_REOPEN)
    oid = s.reply()
    assert s.op(oid, C) is not None and s.op(oid, R) is None
    refused(RefusalReason.REOPEN_DISABLED, s.intake.send, oid, actor="alice", kind=R)
    approval = OutboxApprove(oid, "0" * 12, "alice", kind=R)
    refused(RefusalReason.REOPEN_DISABLED, s.intake.approve, approval)
    refused(RefusalReason.REOPEN_DISABLED, s.intake.reopen, ReopenRequest(s.report, "alice"))
    assert s.world.write_requests() == 0 and s.state() is IssueState.CLOSED


def test_a_reopen_needs_the_adapters_issue_reopen_to_be_usable() -> None:
    s = Scene(enabled=EVERYTHING - {Operation.ISSUE_REOPEN})
    oid = s.reply()
    refused(RefusalReason.CAPABILITY_UNUSABLE, s.approve, oid, R)
    assert s.op(oid, R).state is OpState.DRAFTED and s.world.write_requests() == 0


def test_a_reopen_needs_a_comment_to_ride_on() -> None:
    s = Scene()
    draft = machine.DraftRequest(
        s.service._binding(s.report),
        machine.OutboxEvent.REPLY,
        "rev",
        "alice",
        message=machine.Message("reply", TEXT),
        comment=False,
        reopen=True,
    )
    ctx = s.service._ctx(s.report, s.service._binding(s.report), "ob1_x")
    with pytest.raises(IntakeRefused) as caught:
        machine.draft(
            machine.OutboxDocument(), draft, outbox_id=s.ids.outbox_id(), policy=REOPENING, ctx=ctx
        )
    assert caught.value.reason is RefusalReason.INVALID_DRAFT


# -- one record, its own digest and grant, a fixed order ----------------------------------------


def test_the_reopen_rides_on_the_reply_with_its_own_digest_and_grant() -> None:
    s = Scene()
    oid = s.reply()
    rendered = s.intake.render(oid)
    reopen = rendered.of(R)
    assert reopen is not None and reopen.payload == "state: open" and reopen.findings == ()
    assert reopen.digest != rendered.of(C).digest

    comment, reopening = s.approve(oid, C), s.approve(oid, R)

    assert comment.grant_id and reopening.grant_id and comment.grant_id != reopening.grant_id
    assert s.op(oid, C).state is s.op(oid, R).state is OpState.APPROVED
    assert s.world.write_requests() == 0  # approving sends nothing


def test_the_comment_before_its_reopen_is_refused_before_any_request() -> None:
    s = Scene()
    oid = s.reply()
    s.approve(oid, C)
    s.approve(oid, R)

    error = refused(RefusalReason.REOPEN_NOT_READY, s.send, oid, C)

    assert "reopen" in error.detail
    assert s.world.write_requests() == 0 and s.state() is IssueState.CLOSED
    comment = s.op(oid, C)
    assert comment.state is OpState.APPROVED and comment.attempts == ()
    assert comment.approval.grant is GrantStatus.MINTED  # the grant is not spent by a refusal


def test_reopen_then_comment_each_with_its_receipt() -> None:
    s = Scene()
    oid = s.reply()
    for kind in (C, R):
        s.approve(oid, kind)

    reopened = s.send(oid, R)
    assert reopened.state is OpState.SENT and reopened.kind is R
    assert s.state() is IssueState.OPEN and s.world.comment_count(s.issue) == 0
    assert s.world.server_labels(s.issue) == {"bug"}  # the labels were never touched
    assert s.send(oid, C).state is OpState.SENT

    assert s.world.comment_count(s.issue) == 1 and s.world.write_requests() == 2
    receipt = s.op(oid, R).receipt
    assert receipt is not None and receipt.state_after == "open" and not receipt.reconciled
    assert s.op(oid, R).approval.grant is GrantStatus.CONSUMED


def test_discarding_the_reopen_retires_the_reply_that_says_it_is_reopening() -> None:
    s = Scene()
    oid = s.reply()
    s.approve(oid, C)
    s.approve(oid, R)

    s.intake.discard(oid, reason="not wanted", actor="alice", kind=R)

    assert s.op(oid, R).state is OpState.ABANDONED and s.op(oid, C).state is OpState.ABANDONED
    assert s.op(oid, C).approval.grant is GrantStatus.VOIDED  # the reply's grant is not left usable
    refused(RefusalReason.OUTBOX_TRANSITION, s.send, oid, C)
    assert s.world.write_requests() == 0 and s.state() is IssueState.CLOSED


def test_discarding_only_the_reply_leaves_the_reopen_pending() -> None:
    s = Scene()
    oid = s.reply()

    s.intake.discard(oid, reason="wrong words", actor="alice", kind=C)

    assert s.op(oid, C).state is OpState.ABANDONED and s.op(oid, R).state is OpState.DRAFTED


def test_the_batch_sends_the_reopen_before_the_reply_that_explains_it() -> None:
    s = Scene()
    oid = s.reply()
    for kind in (C, R):
        s.approve(oid, kind)

    outcome = s.intake.send_approved(BatchSend("alice"))

    assert [r.kind for r in outcome.results] == [R, C]  # per report: the reopen, then the comment
    assert outcome.ok and s.state() is IssueState.OPEN and s.world.write_requests() == 2
    only = s.reply(reopen=True, report=s.new_report("b", state=IssueState.CLOSED)[0])
    s.approve(only, C)  # a reply whose reopen was never approved
    again = s.intake.send_approved(BatchSend("alice"))
    assert [f.reason for f in again.failed] == ["reopen_not_ready"] and not again.results


def test_a_batch_narrowed_to_the_reopen_sends_only_it() -> None:
    s = Scene()
    oid = s.reply()
    for kind in (C, R):
        s.approve(oid, kind)
    outcome = s.intake.send_approved(BatchSend("alice", kinds=(R,)))
    assert [r.kind for r in outcome.results] == [R] and s.world.comment_count(s.issue) == 0


# -- an unsent close of the same issue holds the reopen back -------------------------------------


def test_an_unsent_close_of_the_same_issue_holds_the_reopen_back() -> None:
    s = Scene()
    closing = s.reply(reopen=False, close=CloseReason.NOT_PLANNED)  # an earlier correction target
    s.approve(closing, X)
    oid = s.service.draft_reply(
        ReplyDraft(s.report, TEXT + " again", "alice", reopen=True)
    ).outbox_id
    s.approve(oid, R)

    error = refused(RefusalReason.REOPEN_NOT_READY, s.send, oid, R)

    assert closing in error.detail and "close" in error.detail
    assert s.world.write_requests() == 0 and s.state() is IssueState.CLOSED
    s.intake.discard(closing, reason="reversed", actor="alice", kind=X)
    assert s.send(oid, R).state is OpState.SENT and s.state() is IssueState.OPEN


# -- reconcile -----------------------------------------------------------------------------------


def test_an_ambiguous_reopen_is_unknown_and_reconciles_by_reading_the_state() -> None:
    s = Scene()
    oid = s.reply()
    for kind in (C, R):
        s.approve(oid, kind)
    s.world.fail_next_write_ambiguously()  # applied, but the sender cannot know

    assert s.send(oid, R).state is OpState.UNKNOWN
    results = s.intake.reconcile(oid, actor="alice")

    (result,) = [r for r in results if r.kind is R]
    assert result.state is OpState.SENT and result.complete
    receipt = s.op(oid, R).receipt
    assert receipt is not None and receipt.reconciled and receipt.state_after == "open"
    assert s.world.write_requests() == 1  # never re-sent
    assert s.send(oid, C).state is OpState.SENT  # the reply may now go


def test_an_absent_reopen_counts_absences_from_complete_issue_reads() -> None:
    s = Scene()
    oid = s.reply()
    for kind in (C, R):
        s.approve(oid, kind)
    s.world.fail_next_write_ambiguously(applied=False)  # cut off before it was applied
    s.send(oid, R)

    s.intake.reconcile(oid, actor="alice")
    results = s.intake.reconcile(oid, actor="alice")

    (second,) = [r for r in results if r.kind is R]
    assert second.state is OpState.FAILED and s.op(oid, R).fail_reason == machine.ABSENT
    assert s.state() is IssueState.CLOSED and s.world.write_requests() == 1


# -- the operator's reopen verb -------------------------------------------------------------------


def reopen(s: Scene, text: str = "", **kwargs):
    return s.intake.reopen(ReopenRequest(kwargs.pop("report", s.report), "alice", text, **kwargs))


def records_with_reopen(s: Scene, report: str | None = None):
    doc = s.store.read_outbox(report or s.report)
    return [] if doc is None else [r for r in doc.records.values() if r.op(R) is not None]


def test_the_verb_drafts_the_reopen_and_its_reply_for_a_closed_issue_and_sends_nothing() -> None:
    s = Scene()

    outcome = reopen(s, TEXT)

    assert not outcome.dry_run and outcome.text == TEXT and outcome.target_url == s.issue.url
    assert outcome.payload == "state: open" and outcome.findings == ()
    oid = outcome.result.outbox_id
    assert s.op(oid, R).state is OpState.DRAFTED and s.op(oid, C).state is OpState.DRAFTED
    assert s.intake.render(oid).of(C).payload.startswith(TEXT)
    assert s.world.write_requests() == 0 and s.state() is IssueState.CLOSED  # nothing sent
    assert s.op(oid, R).approval is None  # nothing approved


def test_the_verb_uses_the_template_reply_when_given_no_text() -> None:
    s = Scene()
    outcome = reopen(s, "  ")
    assert outcome.text == reopen_reply_text()
    stored = s.store.read_outbox(s.report).records[outcome.result.outbox_id]
    assert stored.message.text == outcome.text


def test_the_verb_reopens_an_issue_closed_by_anyone_and_refuses_an_open_one() -> None:
    s = Scene()  # the issue was closed in the world, not by this hive: the operator chose to
    assert reopen(s, TEXT).result is not None
    opened, _ = s.new_report("already open")
    error = refused(RefusalReason.REOPEN_NOT_NEEDED, reopen, s, TEXT, report=opened)
    assert "open" in error.detail and records_with_reopen(s, opened) == []


def test_the_verb_needs_the_gate_the_outbox_and_a_bound_report() -> None:
    off = Scene(policy=NO_REOPEN)
    refused(RefusalReason.REOPEN_DISABLED, reopen, off, TEXT)
    assert off.store.read_outbox(off.report) is None
    disabled = Scene(policy=OutboxPolicy(reopen_enabled=True))
    refused(RefusalReason.OUTBOX_DISABLED, reopen, disabled, TEXT)


def test_a_pending_reopen_blocks_a_second_one_for_the_same_issue() -> None:
    s = Scene()
    first = reopen(s, TEXT).result.outbox_id

    error = refused(RefusalReason.REOPEN_PENDING, reopen, s, TEXT + " More.")

    assert first in error.detail
    assert [r.outbox_id for r in records_with_reopen(s)] == [first]  # never two
    refused(RefusalReason.REOPEN_PENDING, reopen, s, TEXT)  # nor the same text again
    refused(RefusalReason.REOPEN_PENDING, reopen, s, TEXT, dry_run=True)  # the dry run says so too
    assert len(records_with_reopen(s)) == 1


def test_an_approved_or_unknown_reopen_is_still_pending() -> None:
    s = Scene()
    oid = reopen(s, TEXT).result.outbox_id
    s.approve(oid, R)
    refused(RefusalReason.REOPEN_PENDING, reopen, s, "other")
    s.world.fail_next_write_ambiguously(applied=False)
    assert s.send(oid, R).state is OpState.UNKNOWN
    refused(RefusalReason.REOPEN_PENDING, reopen, s, "other")


@pytest.mark.parametrize("how", ["sent", "discarded"])
def test_a_settled_reopen_no_longer_blocks_a_new_one(how: str) -> None:
    s = Scene()
    oid = reopen(s, TEXT).result.outbox_id
    if how == "sent":
        s.approve(oid, R)
        s.send(oid, R)
        s.world.update_issue(s.issue, at=at(3), state=IssueState.CLOSED)  # closed again
    else:
        s.intake.discard(oid, reason="no", actor="alice", kind=R)

    second = reopen(s, "A different reply.").result

    assert second is not None and second.outbox_id != oid
    assert len(records_with_reopen(s)) == 2
    refused(RefusalReason.REOPEN_PENDING, reopen, s, "A third.")


def test_the_same_text_as_a_sent_reopen_is_refused_rather_than_silently_reusing_it() -> None:
    s = Scene()
    oid = reopen(s, TEXT).result.outbox_id
    s.approve(oid, R)
    s.send(oid, R)
    s.world.update_issue(s.issue, at=at(3), state=IssueState.CLOSED)

    error = refused(RefusalReason.INVALID_DRAFT, reopen, s, TEXT)

    assert oid in error.detail and len(records_with_reopen(s)) == 1


def test_the_dry_run_plans_the_reopen_and_the_reply_and_writes_nothing() -> None:
    s = Scene()

    outcome = reopen(s, TEXT, dry_run=True)

    assert outcome.dry_run and outcome.result is None
    assert outcome.text == TEXT and outcome.payload == "state: open"
    assert outcome.target_url == s.issue.url
    assert s.store.read_outbox(s.report) is None  # no record, so no id was spent
    assert s.world.write_requests() == 0 and s.state() is IssueState.CLOSED
    assert reopen(s, TEXT).result is not None  # and the real run is not blocked by the dry one


def test_the_dry_run_makes_the_same_refusals_and_names_a_leak() -> None:
    s = Scene()
    opened, _ = s.new_report("already open")
    refused(RefusalReason.REOPEN_NOT_NEEDED, reopen, s, TEXT, report=opened, dry_run=True)
    refused(RefusalReason.REOPEN_DISABLED, reopen, Scene(policy=NO_REOPEN), TEXT, dry_run=True)
    leaky = reopen(s, "see acme-1234 for the details", dry_run=True)
    assert leaky.findings and s.store.read_outbox(s.report) is None


def test_the_verb_leaves_a_leaky_reply_held_for_the_operator() -> None:
    s = Scene()
    drafted = reopen(s, "see acme-1234 for the details")
    assert drafted.findings
    oid = drafted.result.outbox_id
    assert s.op(oid, C).state is OpState.HELD
    assert s.op(oid, R).state is OpState.DRAFTED


def test_an_issue_that_cannot_be_read_is_not_reopened(monkeypatch) -> None:
    s = Scene()
    monkeypatch.setattr(s.service, "_read_issue", lambda binding: None)
    refused(RefusalReason.REOPEN_NOT_NEEDED, reopen, s, TEXT)
    assert s.store.read_outbox(s.report) is None


# -- a report closed afterwards retires its pending reopen ----------------------------------------


def test_a_report_closed_afterwards_retires_its_pending_reopen_and_reply() -> None:
    s = Scene()
    oid = reopen(s, TEXT).result.outbox_id
    s.approve(oid, R)
    s.approve(oid, C)

    retired = s.service.retire_reopens(s.report, "report closed: rejected", "disp/claude")

    assert retired == (oid,)
    for kind in (R, C):
        assert s.op(oid, kind).state is OpState.SUPERSEDED
        assert s.op(oid, kind).approval.grant is GrantStatus.VOIDED
    refused(RefusalReason.OUTBOX_TRANSITION, s.send, oid, R)
    assert s.service.retire_reopens(s.report, "again", "disp/claude") == ()  # nothing pending
    assert s.world.write_requests() == 0
    assert reopen(s, "A new one is possible once it is retired.").result is not None


def test_retiring_leaves_a_sent_reopen_and_other_records_alone() -> None:
    s = Scene()
    sent = reopen(s, TEXT).result.outbox_id
    s.approve(sent, R)
    s.send(sent, R)
    plain = s.service.draft_reply(ReplyDraft(s.report, "an unrelated note", "alice")).outbox_id

    assert s.service.retire_reopens(s.report, "closed", "disp/claude") == ()

    assert s.op(sent, R).state is OpState.SENT and s.op(sent, C).state is OpState.DRAFTED
    assert s.op(plain, C).state is OpState.DRAFTED


def test_retiring_is_a_no_op_with_the_reopen_gate_off() -> None:
    off = Scene(policy=NO_REOPEN)
    assert off.service.retire_reopens(off.report, "closed", "disp/claude") == ()


def test_rejecting_the_report_retires_its_pending_reopen() -> None:
    s = Scene()
    oid = reopen(s, TEXT).result.outbox_id
    s.store.set_state(s.report, "intake", "untriaged", reason="imported", actor="dev/import")

    s.intake.dispose(DispositionRequest(s.report, Disposition.REJECT, "disp/claude", reason="no"))

    assert s.op(oid, R).state is OpState.SUPERSEDED and s.op(oid, C).state is OpState.SUPERSEDED


# -- ordering across records ----------------------------------------------------------------------


def test_only_send_approved_orders_separate_records_a_lone_question_is_not_held() -> None:
    """A question drafted on its own for a still-closed issue is a separate record, so the
    reopen's own-record blocker does not hold it: the operator orders separate records (or runs
    ``send-approved``, which sends every reopen before every comment)."""
    s = Scene()
    reopening = reopen(s, TEXT).result.outbox_id
    question = s.service.draft_reply(ReplyDraft(s.report, "Which version?", "alice")).outbox_id
    s.approve(question, C)
    s.approve(reopening, R)
    s.approve(reopening, C)

    assert s.send(question, C).state is OpState.SENT  # not refused: nothing ties it to the reopen
    assert s.state() is IssueState.CLOSED

    batch = s.intake.send_approved(BatchSend("alice"))
    assert [r.kind for r in batch.results] == [R, C] and s.state() is IssueState.OPEN
