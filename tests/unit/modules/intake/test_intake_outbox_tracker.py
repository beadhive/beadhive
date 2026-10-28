"""The outbox machine against ``MemoryTracker``: R14 cases X8, X9 (R7 steps 1 to 7, with kit
extension K1) and X10, plus the application layer's translation to and from the port.

The product has no sender yet (that is the supervised-sender bead): :class:`Sender` below is a
test-local driver of the pure rules and the translation helpers, doing what R6 prescribes in its
order (persist O9, then one request; classify the outcome; reconcile from a complete listing begun
after the attempt ended). Ordering is asserted on ``seq``, ``Attempt.n`` and ``write_requests()``
only. No clock, no sleep, no socket.
"""

from __future__ import annotations

import dataclasses

import pytest

from beadhive.modules.intake.application import outbox as translate
from beadhive.modules.intake.application.paging import iterate_pages
from beadhive.modules.intake.application.threads import thread_actor
from beadhive.modules.intake.domain import (
    ApprovalRequest,
    DraftRequest,
    IntakeRefused,
    IssueBinding,
    LabelBase,
    LabelIntent,
    LeakPolicy,
    Message,
    OpKind,
    OpState,
    OutboxDocument,
    OutboxEvent,
    OutboxObservation,
    OutboxPolicy,
    OwnIdentity,
    RefusalReason,
    RenderContext,
    outbox_view,
)
from beadhive.modules.intake.domain import outbox as ob
from beadhive.modules.tracker.contracts import (
    READ_OPERATIONS,
    WRITE_OPERATIONS,
    AuthenticationFailed,
    CommentQuery,
    LabelOwnership,
    NotVisible,
    Operation,
    OperationNotEnabled,
    PayloadRejected,
    PermissionDenied,
    RateLimited,
    TargetStateRefused,
    TrackerError,
    TransientFailure,
    WriteGrant,
    WriteOutcomeUnknown,
)
from beadhive.testing.intake import RecordingSanitizer, SequentialIdSource
from beadhive.testing.tracker import MemoryTracker, MemoryWorld, at

ME = OwnIdentity("acme/widgets", "inst-1")
POLICY = LeakPolicy(bead_prefixes=frozenset({"acme", "bh"}), owned_label_prefixes=("beadhive:",))
ON = OutboxPolicy(enabled=True, labels_enabled=True)
C, L = OpKind.COMMENT, OpKind.LABELS
OWNED = LabelOwnership(("beadhive:",))


class Crash(Exception):
    """A sender process dying between the write-ahead and the request."""


class Sender:
    """A scenario: a memory tracker, one bound report's outbox document, deterministic ids."""

    def __init__(
        self,
        *,
        enabled=READ_OPERATIONS | WRITE_OPERATIONS,
        page_size: int = 50,
        ownership: LabelOwnership = OWNED,
    ) -> None:
        self.world = MemoryWorld()
        self.issue = self.world.add_issue("a bug", at=at(1), labels=("bug",))
        self.world.set_write_time(at(2))
        self.tracker = MemoryTracker(self.world, enabled=frozenset(enabled), page_size=page_size)
        self.binding = IssueBinding(
            "acme-r1", "memory", self.world.locator, self.issue.url, self.issue.identifier
        )
        self.ownership = ownership
        self.ids = SequentialIdSource()
        self.doc = OutboxDocument()
        self.epoch = 1

    # -- the pure rules, with what the root would supply ------------------------------------

    def ctx(self, oid: str) -> RenderContext:
        row = self.world.issues.get(self.issue.identifier)  # gone once a test deletes it
        labels = frozenset(row.labels) if row is not None else frozenset()
        return RenderContext(
            self.binding.url,
            POLICY,
            RecordingSanitizer(),
            marker_line=f"<!-- bh:v1 hive={ME.hive} install={ME.install} outbox={oid} -->",
            labels=LabelBase("r", labels),
        )

    def apply(self, applied: ob.OutboxApplied) -> ob.OutboxApplied:
        self.doc = applied.document
        return applied

    def draft(self, text: str = "Merged; thanks for the report.", **kw) -> str:
        oid = self.ids.outbox_id()
        request = DraftRequest(
            self.binding,
            OutboxEvent.MERGED,
            f"c{len(self.doc.records)}",
            "outbox",
            message=Message("merged", text),
            **kw,
        )
        return self.apply(
            ob.draft(self.doc, request, outbox_id=oid, policy=ON, ctx=self.ctx(oid))
        ).outbox_id

    def approve(self, oid: str, kind: OpKind = C) -> str:
        ctx = self.ctx(oid)
        digest = ob.render(self.doc.record(oid), kind, ctx).short_digest
        self.apply(
            ob.approve(
                self.doc,
                ApprovalRequest(oid, kind, digest, "alice"),
                grant_id=self.ids.grant_id(),
                policy=ON,
                ctx=ctx,
                capabilities=translate.capabilities(self.tracker.probe_capabilities()),
            )
        )
        return self.doc.record(oid).op(kind).approval.grant_id

    def send(self, oid: str, kind: OpKind = C, *, crash: bool = False) -> OpState:
        """O9 persisted first, then exactly one request, then O10, O11 or O12."""
        applied = self.apply(
            ob.begin_send(
                self.doc,
                oid,
                kind,
                actor="alice",
                policy=ON,
                ctx=self.ctx(oid),
                epoch=self.epoch,
                holder="host:1:token",
            )
        )
        if crash:
            raise Crash
        record, plan = self.doc.record(oid), applied.plan
        grant = translate.plan_grant(record, plan)
        try:
            if kind is C:
                answer = self.tracker.post_comment(
                    translate.issue_key(record),
                    plan.body,
                    marker=translate.own_marker(ME, oid),
                    grant=grant,
                )
            else:
                answer = self.tracker.change_labels(
                    translate.issue_key(record),
                    add=plan.add,
                    remove=plan.remove,
                    ownership=self.ownership,
                    grant=grant,
                )
        except Exception as exc:  # noqa: BLE001 - the classification is the point
            if translate.send_outcome(exc) is translate.SendOutcome.REFUSED:
                self.apply(
                    ob.record_refusal(self.doc, oid, kind, translate.refusal_name(exc), actor="s")
                )
            else:
                self.apply(ob.record_unknown(self.doc, oid, kind, actor="s", detail="ambiguous"))
        else:
            self.apply(ob.record_receipt(self.doc, oid, kind, translate.receipt(answer), actor="s"))
        return self.doc.record(oid).op(kind).state

    def listing(self, *, fail_after_pages: int | None = None) -> tuple[tuple, bool]:
        comments: list = []
        query = CommentQuery(issue=translate.issue_key(next(iter(self.doc.records.values()))))
        try:
            for n, page in enumerate(
                iterate_pages(
                    lambda cursor: self.tracker.list_comments(
                        dataclasses.replace(query, cursor=cursor)
                    )
                ),
                start=1,
            ):
                comments.extend(page.items)
                if fail_after_pages is not None and n >= fail_after_pages:
                    self.world.throttle(1, 10_000)  # the next page is refused past the budget
        except TrackerError:
            return tuple(comments), False
        return tuple(comments), True

    def reconcile(self, oid: str, *, start_seq: int | None = None, **kw) -> ob.OutboxApplied:
        start = self.doc.seq if start_seq is None else start_seq
        comments, complete = self.listing(**kw)
        known = outbox_view(self.doc).known_actors() | {thread_actor(self.tracker.whoami())}
        marked = translate.marked_comments(comments, ME, known)
        return self.apply(
            ob.observe(
                self.doc,
                oid,
                OutboxObservation(start, complete, marked),
                policy=ON,
                actor="reconciler",
            )
        )

    def state(self, oid: str, kind: OpKind = C) -> OpState:
        return self.doc.record(oid).op(kind).state


def refused(expected: RefusalReason, call, /, *args, **kwargs) -> None:
    with pytest.raises(IntakeRefused) as caught:
        call(*args, **kwargs)
    assert caught.value.reason is expected, caught.value


# -- X8: a sent operation is never sent again --------------------------------------------------


def test_x8_send_twice_after_sent_is_refused_and_posts_once() -> None:
    s = Sender()
    oid = s.draft()
    grant = s.approve(oid)
    assert s.send(oid) is OpState.SENT
    refused(RefusalReason.OUTBOX_TRANSITION, s.send, oid)

    assert s.world.write_requests() == 1 and s.world.comment_count(s.issue) == 1
    op = s.doc.record(oid).op(C)
    assert op.receipt.comment_id in outbox_view(s.doc).receipt_ids()
    # Single use is enforced twice: the adapter refuses the reused grant in-process as well.
    with pytest.raises(OperationNotEnabled):
        s.tracker.post_comment(
            s.issue,
            "again",
            marker=translate.own_marker(ME, oid),
            grant=WriteGrant(grant, Operation.COMMENT_CREATE, s.issue),
        )
    assert s.world.write_requests() == 1


# -- X9: R7 steps 1 to 7 ---------------------------------------------------------------------


def test_x9_steps_1_to_3_an_ambiguous_applied_write_reconciles_without_a_second_post() -> None:
    s = Sender()
    oid = s.draft()
    s.approve(oid)
    s.world.fail_next_write_ambiguously()
    assert s.send(oid) is OpState.UNKNOWN  # step 1: stored, then the tracker raised
    refused(RefusalReason.OUTBOX_TRANSITION, s.send, oid)  # step 2: not approved

    applied = s.reconcile(oid)  # step 3
    op = s.doc.record(oid).op(C)
    assert applied.steps[0].rule == "O14" and op.state is OpState.SENT and op.receipt.reconciled
    assert s.world.write_requests() == 1 and s.world.comment_count(s.issue) == 1


def test_x9_step_4_k1_an_ambiguous_write_never_applied_is_retried_once() -> None:
    s = Sender()
    oid = s.draft()
    first = s.approve(oid)
    s.world.fail_next_write_ambiguously(applied=False)  # K1: cut off before anything was stored
    assert s.send(oid) is OpState.UNKNOWN
    assert s.world.comment_count(s.issue) == 0

    s.reconcile(oid)
    op = s.doc.record(oid).op(C)
    assert op.state is OpState.UNKNOWN and op.absent_confirmations == 1
    s.reconcile(oid)
    assert s.state(oid) is OpState.FAILED and s.doc.record(oid).op(C).fail_reason == ob.ABSENT

    s.apply(ob.retry(s.doc, oid, C, actor="alice", policy=ON))
    assert s.approve(oid) != first
    assert s.send(oid) is OpState.SENT
    op = s.doc.record(oid).op(C)
    assert [a.n for a in op.attempts] == [1, 2]
    assert s.world.write_requests() == 2 and s.world.comment_count(s.issue) == 1


def test_x9_step_5_a_crash_after_the_write_ahead_is_recovered_without_a_request() -> None:
    s = Sender()
    oid = s.draft()
    s.approve(oid)
    with pytest.raises(Crash):
        s.send(oid, crash=True)
    assert s.state(oid) is OpState.SENDING and s.world.write_requests() == 0

    applied = s.apply(ob.recover(s.doc, live_epoch=s.epoch, holder_gone=True, actor="next"))
    assert applied.steps[0].rule == "O13" and s.state(oid) is OpState.UNKNOWN
    s.reconcile(oid)
    s.reconcile(oid)
    assert s.state(oid) is OpState.FAILED
    s.apply(ob.retry(s.doc, oid, C, actor="alice", policy=ON))
    s.approve(oid)
    assert s.send(oid) is OpState.SENT
    assert s.world.write_requests() == 1 and s.world.comment_count(s.issue) == 1


def test_x9_step_6_a_snapshot_begun_before_the_attempt_ended_is_ignored() -> None:
    s = Sender()
    oid = s.draft()
    s.approve(oid)
    s.world.fail_next_write_ambiguously(applied=False)
    s.send(oid)
    ended = s.doc.record(oid).op(C).attempt.end_seq

    stale = s.reconcile(oid, start_seq=ended)  # ordered by local seq, never by time
    assert not stale.changed and s.doc.record(oid).op(C).absent_confirmations == 0


def test_x9_step_7_an_incomplete_listing_changes_nothing() -> None:
    s = Sender(page_size=1)
    s.world.add_comment(s.issue, "a human comment", at=at(1, 5))
    s.world.add_comment(s.issue, "another one", at=at(1, 6))
    oid = s.draft()
    s.approve(oid)
    s.world.fail_next_write_ambiguously(applied=False)
    s.send(oid)

    applied = s.reconcile(oid, fail_after_pages=1)  # a TrackerError mid-pagination
    assert not applied.changed and s.state(oid) is OpState.UNKNOWN
    assert s.doc.record(oid).op(C).absent_confirmations == 0


def test_x9_a_late_original_after_a_repost_is_flagged_never_fixed() -> None:
    """A1 failing: the first attempt's comment shows up after the operation was re-sent (O19)."""
    s = Sender()
    oid = s.draft()
    s.approve(oid)
    s.world.fail_next_write_ambiguously(applied=False)
    s.send(oid)
    s.reconcile(oid)
    s.reconcile(oid)
    s.apply(ob.retry(s.doc, oid, C, actor="alice", policy=ON))
    s.approve(oid)
    s.send(oid)
    late = s.world.add_comment(s.issue, "the original", at=at(2), author="maintainer")
    s.world.comments[late.id].marker = translate.own_marker(ME, oid)
    s.world.comments[late.id].author = s.tracker.whoami()

    applied = s.reconcile(oid)
    op = s.doc.record(oid).op(C)
    assert applied.steps[0].rule == "O19" and op.duplicates == (late.id,)
    assert op.state is OpState.SENT and s.world.write_requests() == 2


# -- X10: every proof-of-no-write class fails the operation ------------------------------------


class RefusingWriter:
    """A writer whose every write raises ``error`` before anything is stored."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    def post_comment(self, issue, body, *, marker, grant):
        raise self.error


@pytest.mark.parametrize(
    "error",
    [
        OperationNotEnabled("comment.create is not enabled"),
        AuthenticationFailed("revoked"),
        PermissionDenied("no", operation=Operation.COMMENT_CREATE),
        NotVisible("gone"),
        TargetStateRefused("locked"),
        PayloadRejected("too long"),
        RateLimited("throttled", retry_after=None, kind="throttle"),
    ],
    ids=lambda e: type(e).__name__,
)
def test_x10_a_scripted_refusal_fails_and_a_retry_needs_a_new_approval(error) -> None:
    s = Sender()
    oid = s.draft()
    first = s.approve(oid)
    real, s.tracker = s.tracker, RefusingWriter(error)
    assert s.send(oid) is OpState.FAILED
    op = s.doc.record(oid).op(C)
    assert op.fail_reason == f"refused:{type(error).__name__}" and op.history[-1].rule == "O11"
    refused(RefusalReason.OUTBOX_TRANSITION, s.send, oid)

    s.tracker = real
    s.apply(ob.retry(s.doc, oid, C, actor="alice", policy=ON))
    assert s.approve(oid) != first
    assert s.send(oid) is OpState.SENT


def test_x10_memory_tracker_refusals_and_a_throttle_past_the_budget_fail() -> None:
    disabled = Sender(enabled=READ_OPERATIONS | {Operation.LABELS_CHANGE})
    oid = disabled.draft()
    refused(RefusalReason.CAPABILITY_UNUSABLE, disabled.approve, oid)  # the probe says no

    throttled = Sender()
    oid = throttled.draft()
    throttled.approve(oid)
    throttled.world.throttle(1, 10_000)
    assert throttled.send(oid) is OpState.FAILED
    assert throttled.doc.record(oid).op(C).fail_reason == "refused:RateLimited"
    assert throttled.world.comment_count(throttled.issue) == 0

    gone = Sender()
    oid = gone.draft()
    gone.approve(oid)
    gone.world.delete_issue(gone.issue)
    assert gone.send(oid) is OpState.FAILED
    assert gone.doc.record(oid).op(C).fail_reason == "refused:NotVisible"

    narrow = Sender(ownership=LabelOwnership(("bot:",)))
    oid = narrow.draft(comment=False, labels=LabelIntent(add=frozenset({"beadhive:merged"})))
    narrow.approve(oid, L)
    assert narrow.send(oid, L) is OpState.FAILED
    assert narrow.doc.record(oid).op(L).fail_reason == "refused:LabelNotOwned"
    assert narrow.world.write_requests() == 0


@pytest.mark.parametrize(
    "error", [WriteOutcomeUnknown("cut off"), TransientFailure("5xx"), RuntimeError("bug")]
)
def test_o12_every_other_outcome_is_unknown(error) -> None:
    assert translate.send_outcome(error) is translate.SendOutcome.UNKNOWN


# -- X7 on the tracker: the label write is its own grant ---------------------------------------


def test_x7_the_label_write_needs_its_own_grant_and_changes_only_owned_labels() -> None:
    s = Sender()
    s.world.add_human_label(s.issue, "beadhive:delivery-pending")
    oid = s.draft(
        labels=LabelIntent(
            add=frozenset({"beadhive:delivery-merged"}),
            remove=frozenset({"beadhive:delivery-pending"}),
        )
    )
    comment = s.approve(oid, C)
    refused(RefusalReason.OUTBOX_TRANSITION, s.send, oid, L)  # the comment's approval is not it
    labels = s.approve(oid, L)
    assert comment != labels
    assert s.send(oid, L) is OpState.SENT
    assert s.world.server_labels(s.issue) == {"bug", "beadhive:delivery-merged"}
    assert s.world.comment_count(s.issue) == 0 and s.world.write_requests() == 1
    assert s.send(oid, C) is OpState.SENT and s.world.write_requests() == 2


# -- translation -------------------------------------------------------------------------------


def test_write_grant_is_scoped_to_one_operation_and_the_records_issue() -> None:
    s = Sender()
    oid = s.draft()
    s.approve(oid)
    record = s.doc.record(oid)
    approval = record.op(C).approval
    grant = translate.write_grant(record, C, approval)
    assert grant == WriteGrant(approval.grant_id, Operation.COMMENT_CREATE, s.issue)
    other = dataclasses.replace(approval, grant_id=s.ids.grant_id())
    refused(RefusalReason.GRANT_UNUSABLE, translate.write_grant, record, C, other)


def test_capabilities_reads_only_usable_operations() -> None:
    reads = translate.capabilities(MemoryTracker(MemoryWorld()).probe_capabilities())
    assert reads.usable == {"issues.read", "comments.read"}


def test_marked_comments_need_this_hive_and_install() -> None:
    s = Sender()
    oid = s.draft()
    s.approve(oid)
    s.send(oid)
    foreign = s.world.add_comment(s.issue, "peer", at=at(3))
    s.world.comments[foreign.id].marker = translate.own_marker(OwnIdentity("other/repo", "x"), oid)
    comments, complete = s.listing()
    marked = translate.marked_comments(comments, ME, {thread_actor(s.tracker.whoami())})
    assert complete and [(m.outbox_id, m.known_actor) for m in marked] == [(oid, True)]
