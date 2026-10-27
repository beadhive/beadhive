"""The supervised ``comment.create`` sender (I9a) end to end on ``MemoryTracker``.

``OutboxService`` over ``MemoryReportStore``, ``MemoryTracker`` / ``MemoryWorld``, the never-waiting
``InProcessIntakeLock``, ``SequentialIdSource`` and ``RecordingSanitizer``. It pins the bead's
acceptance: the dry run and the single-use, digest-checked grant; that an ambiguous timeout never
causes a second post (kit extension K1); that nothing is sent while the outbox is off, without the
adapter's per-operation enable or without a grant; and R7's write-ahead, receipts, crash recovery
and the two complete listings reconciliation needs. Ordering is asserted on ``seq``,
``Attempt.n``, ``write_requests()`` and ``comment_count()`` only. No clock, no sleep, no socket,
and no real tracker: every write lands in the in-memory world.
"""

from __future__ import annotations

import pytest

from beadhive.modules.intake.application import (
    IntakeService,
    NullIntake,
    OutboxService,
    StoreOutboxView,
    ThreadService,
)
from beadhive.modules.intake.application import outbox as translate
from beadhive.modules.intake.application.threads import binding_key
from beadhive.modules.intake.domain import (
    Evidence,
    GrantStatus,
    IntakeRefused,
    IssueBinding,
    IssueStatus,
    LeakPolicy,
    OpKind,
    OpState,
    OutboxApprove,
    OutboxPolicy,
    OutboxQuery,
    OwnIdentity,
    RefusalReason,
    ReplyDraft,
    ReportDraft,
    Role,
    SourceSnapshot,
    StorePosture,
    ThreadRequest,
    outbox_view,
)
from beadhive.modules.intake.domain import outbox as machine
from beadhive.modules.tracker.contracts import (
    READ_OPERATIONS,
    WRITE_OPERATIONS,
    CommentQuery,
    Operation,
    OperationNotEnabled,
    OwnMarker,
    TransientFailure,
    WriteGrant,
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
ON = OutboxPolicy(enabled=True)
PRIVATE = StorePosture(published=False)
C = OpKind.COMMENT
EVERYTHING = READ_OPERATIONS | WRITE_OPERATIONS
SECRET = "s3cr3t-value"


def marker_line(marker: OwnMarker) -> str:
    """The adapter's footer line, as the GitHub adapter encodes it (``bh:v1``)."""
    return f"<!-- bh:v1 hive={marker.hive} install={marker.install} outbox={marker.outbox_id} -->"


class Crash(BaseException):
    """The sender process dying between the write-ahead and the request (not an ``Exception``,
    so no handler classifies it: the record stays ``sending``)."""


class _NoLabels:
    def violations(self, labels) -> tuple[str, ...]:
        return ()


class Scene:
    """One bound report on one memory issue, with the whole sender wired as the root wires it."""

    def __init__(
        self,
        *,
        policy: OutboxPolicy = ON,
        enabled=EVERYTHING,
        page_size: int = 50,
        sanitizer: RecordingSanitizer | None = None,
        posture: StorePosture = PRIVATE,
        project: str = "",
    ) -> None:
        self.world = MemoryWorld()
        self.issue = self.world.add_issue("a bug", at=at(1))
        self.world.set_write_time(at(2))
        self.tracker = MemoryTracker(self.world, enabled=frozenset(enabled), page_size=page_size)
        self.store = MemoryReportStore(SCOPE)
        binding = IssueBinding(
            "",
            "memory",
            project or self.world.locator,
            self.issue.url,
            self.issue.identifier,
        )
        self.report = self.store.create(
            ReportDraft(
                "a bug",
                "bug",
                "origin:github",
                "dev/import",
                binding=binding,
                snapshot=SourceSnapshot("a bug", "", IssueStatus.OPEN, "r1"),
            )
        )
        self.lock = InProcessIntakeLock()
        self.ids = SequentialIdSource()
        self.sanitizer = sanitizer if sanitizer is not None else RecordingSanitizer.of([SECRET])
        self.policy = policy
        self.posture = posture
        self.service = self.sender(self.tracker)
        self.intake = IntakeService(self.store, _NoLabels(), outbox=self.service)

    def sender(self, tracker, *, identity: OwnIdentity = ME, sanitizer="default") -> OutboxService:
        return OutboxService(
            self.store,
            tracker,
            self.lock,
            ids=self.ids,
            identity=identity,
            marker_line=marker_line,
            policy=self.policy,
            leak_policy=LEAKS,
            sanitizer=self.sanitizer if sanitizer == "default" else sanitizer,
            posture=self.posture,
            holder="host-1:42",
        )

    @property
    def binding(self) -> IssueBinding:
        view = self.store.get(self.report)
        assert view is not None and view.binding is not None
        return view.binding

    def draft(self, text: str = "Thanks, we can reproduce this.") -> str:
        return self.intake.draft_reply(ReplyDraft(self.report, text, "alice")).outbox_id

    def digest(self, oid: str) -> str:
        rendered = self.intake.render(oid).of(C)
        assert rendered is not None
        return rendered.short_digest

    def approve(self, oid: str, *, actor: str = "alice", publish: tuple[str, ...] = ()):
        return self.intake.approve(OutboxApprove(oid, self.digest(oid), actor, publish))

    def op(self, oid: str):
        doc = self.store.read_outbox(self.report)
        assert doc is not None
        op = doc.records[oid].op(C)
        assert op is not None
        return op

    def labels(self) -> tuple[str, ...]:
        view = self.store.get(self.report)
        assert view is not None
        return view.labels

    def requests(self) -> tuple[int, int]:
        return self.world.read_requests(), self.world.write_requests()


def refused(expected: RefusalReason, call, /, *args, **kwargs) -> None:
    with pytest.raises(IntakeRefused) as caught:
        call(*args, **kwargs)
    assert caught.value.reason is expected, caught.value


# -- off by default ----------------------------------------------------------------------------


def test_the_outbox_is_off_by_default_and_every_operation_refuses_before_any_request() -> None:
    s = Scene(policy=OutboxPolicy())
    assert OutboxPolicy().enabled is False
    writes = s.store.writes
    oid = "ob1_" + "a" * 26
    for call in (
        lambda: s.intake.draft_reply(ReplyDraft(s.report, "hello", "alice")),
        lambda: s.intake.outbox(OutboxQuery()),
        lambda: s.intake.render(oid),
        lambda: s.intake.approve(OutboxApprove(oid, "0" * 12, "alice")),
        lambda: s.intake.send(oid, actor="alice"),
        lambda: s.intake.reconcile(actor="alice"),
        lambda: s.intake.discard(oid, reason="no", actor="alice"),
        lambda: s.intake.retry(oid, actor="alice"),
    ):
        refused(RefusalReason.OUTBOX_DISABLED, call)

    unwired = IntakeService(s.store, _NoLabels())  # the root binds no sender when it is off
    refused(RefusalReason.OUTBOX_DISABLED, unwired.send, oid, actor="alice")
    refused(RefusalReason.OUTBOX_DISABLED, unwired.draft_reply, ReplyDraft(s.report, "x", "a"))
    null = NullIntake()  # intake itself off
    refused(RefusalReason.INTAKE_DISABLED, null.send, oid, actor="alice")
    refused(RefusalReason.INTAKE_DISABLED, null.approve, OutboxApprove(oid, "0" * 12, "alice"))
    assert null.outbox(OutboxQuery()).entries == ()

    assert s.requests() == (0, 0) and s.store.writes == writes
    assert s.world.comment_count(s.issue) == 0


def test_without_the_per_operation_enable_approve_refuses_and_nothing_is_sent() -> None:
    s = Scene(enabled=READ_OPERATIONS)  # comment.create is not enabled on the adapter
    oid = s.draft()
    refused(RefusalReason.CAPABILITY_UNUSABLE, s.approve, oid)
    assert s.op(oid).state is OpState.DRAFTED and s.op(oid).approvals == ()
    refused(RefusalReason.OUTBOX_TRANSITION, s.intake.send, oid, actor="alice")
    assert s.world.write_requests() == 0 and s.world.comment_count(s.issue) == 0


def test_without_comments_read_an_unknown_outcome_could_not_be_reconciled_so_approve_refuses():
    s = Scene(enabled=frozenset({Operation.ISSUES_READ, Operation.COMMENT_CREATE}))
    oid = s.draft()
    refused(RefusalReason.CAPABILITY_UNUSABLE, s.approve, oid)
    assert s.world.write_requests() == 0


def test_an_enable_withdrawn_after_approval_fails_the_send_and_posts_nothing() -> None:
    s = Scene()
    oid = s.draft()
    s.approve(oid)
    withdrawn = s.sender(MemoryTracker(s.world, enabled=READ_OPERATIONS))
    result = withdrawn.send(oid, actor="alice")

    assert result.state is OpState.FAILED and result.rules == ("O9", "O11")
    assert s.op(oid).fail_reason == "refused:OperationNotEnabled"
    assert s.world.write_requests() == 0 and s.world.comment_count(s.issue) == 0


def test_without_a_grant_nothing_is_sent() -> None:
    s = Scene()
    oid = s.draft()
    refused(RefusalReason.OUTBOX_TRANSITION, s.intake.send, oid, actor="alice")  # drafted
    held = s.draft(f"use {SECRET} to log in")
    assert s.op(held).state is OpState.HELD
    refused(RefusalReason.OUTBOX_TRANSITION, s.intake.send, held, actor="alice")
    assert s.world.write_requests() == 0


# -- the dry run and the gate ------------------------------------------------------------------


def test_approve_renders_the_dry_run_and_mints_one_single_use_grant_checked_by_digest() -> None:
    s = Scene()
    oid = s.draft()
    writes, requests = s.store.writes, s.requests()
    shown = s.intake.render(oid).of(C)
    assert shown is not None
    marker = marker_line(OwnMarker(ME.hive, ME.install, oid))
    assert shown.payload == f"Thanks, we can reproduce this.\n\n{marker}"  # the posted bytes
    assert shown.target_url == s.issue.url and shown.findings == ()
    assert (s.store.writes, s.requests()) == (writes, requests)  # a dry run reads nothing remote

    refused(RefusalReason.STALE_DIGEST, s.intake.approve, OutboxApprove(oid, "f" * 12, "alice"))
    short = shown.short_digest[:11]
    refused(RefusalReason.STALE_DIGEST, s.intake.approve, OutboxApprove(oid, short, "alice"))
    refused(RefusalReason.OPERATOR_SEAT_REQUIRED, s.approve, oid, actor="dev/agent")
    refused(RefusalReason.OPERATOR_SEAT_REQUIRED, s.approve, oid, actor="disp/intake")
    assert s.store.writes == writes  # every refusal wrote nothing

    result = s.intake.approve(OutboxApprove(oid, shown.short_digest, "alice"))
    assert result.state is OpState.APPROVED and result.rules == ("O5",)
    assert result.rendered == shown and machine.is_grant_id(result.grant_id)
    approval = s.op(oid).approval
    assert approval is not None
    assert (approval.actor, approval.digest, approval.grant) == (
        "alice",
        shown.digest,
        GrantStatus.MINTED,
    )
    doc = s.store.read_outbox(s.report)
    assert doc is not None
    grant = translate.write_grant(doc.records[oid], C, approval)
    assert grant == WriteGrant(result.grant_id, Operation.COMMENT_CREATE, s.issue)

    sent = s.intake.send(oid, actor="alice")
    assert sent.state is OpState.SENT and s.op(oid).approval.grant is GrantStatus.CONSUMED
    with pytest.raises(OperationNotEnabled):  # single use, in-process too
        s.tracker.post_comment(s.issue, "again", marker=translate.own_marker(ME, oid), grant=grant)
    assert s.world.write_requests() == 1 and s.world.comment_count(s.issue) == 1


def test_the_write_ahead_attempt_is_durable_before_the_one_request() -> None:
    s = Scene()
    seen: list = []

    class Watching(MemoryTracker):
        def post_comment(self, issue, body, *, marker, grant):
            seen.append((s.op(oid), body, marker, grant))  # what the store holds at request time
            return super().post_comment(issue, body, marker=marker, grant=grant)

    sender = s.sender(Watching(s.world, enabled=EVERYTHING))
    oid = s.draft()
    s.approve(oid)
    result = sender.send(oid, actor="alice")

    ((op, body, marker, grant),) = seen
    assert op.state is OpState.SENDING and [a.n for a in op.attempts] == [1]
    assert op.attempt.holder == "host-1:42" and op.attempt.epoch == s.lock.epoch
    assert op.approval.grant is GrantStatus.CONSUMED and grant.grant_id == op.attempt.grant_id
    assert body == "Thanks, we can reproduce this." and marker == OwnMarker(
        ME.hive, ME.install, oid
    )
    assert result.rules == ("O9", "O10")
    assert [seq for _, seq in s.store.outbox_writes] == [1, 2, 3, 4]  # O1, O5, O9, O10


def test_a_receipt_marks_the_comment_sent_and_stamps_notified_replied() -> None:
    s = Scene()
    oid = s.draft()
    s.approve(oid)
    result = s.intake.send(oid, actor="alice")

    op = s.op(oid)
    assert result.state is OpState.SENT and result.comment_id == op.receipt.comment_id
    assert result.comment_id in s.world.comments and not op.receipt.reconciled
    assert result.notified == "replied" and "notified:replied" in s.labels()
    assert outbox_view(s.store.read_outbox(s.report)).receipt_ids() == {result.comment_id}


def test_send_twice_after_sent_is_refused_and_posts_once() -> None:
    s = Scene()
    oid = s.draft()
    s.approve(oid)
    s.intake.send(oid, actor="alice")
    refused(RefusalReason.OUTBOX_TRANSITION, s.intake.send, oid, actor="alice")
    refused(RefusalReason.OUTBOX_TRANSITION, s.approve, oid)
    assert s.world.write_requests() == 1 and s.world.comment_count(s.issue) == 1


def test_an_approval_whose_render_moved_is_invalidated_and_not_sent() -> None:
    s = Scene()
    oid = s.draft()
    first = s.approve(oid).grant_id
    moved = s.sender(s.tracker, identity=OwnIdentity(ME.hive, "inst-2"))  # another marker
    result = moved.send(oid, actor="alice")

    assert result.state is OpState.DRAFTED and result.rules == ("O7",)
    assert s.op(oid).approval.grant is GrantStatus.VOIDED and s.op(oid).approval.grant_id == first
    assert s.world.write_requests() == 0


def test_an_agent_seat_can_neither_send_nor_retry() -> None:
    s = Scene()
    oid = s.draft()
    s.approve(oid)
    refused(RefusalReason.OPERATOR_SEAT_REQUIRED, s.intake.send, oid, actor="dev/agent")
    assert s.op(oid).state is OpState.APPROVED and s.world.write_requests() == 0


# -- K1: an ambiguous timeout never causes a second post ---------------------------------------


def test_k1_an_ambiguous_applied_write_is_found_by_reconcile_and_never_posted_twice() -> None:
    s = Scene()
    oid = s.draft()
    s.approve(oid)
    s.world.fail_next_write_ambiguously()  # stored, then the tracker raised
    result = s.intake.send(oid, actor="alice")
    assert result.state is OpState.UNKNOWN and result.rules == ("O9", "O12")
    assert "notified:replied" not in s.labels()

    refused(RefusalReason.OUTBOX_TRANSITION, s.intake.send, oid, actor="alice")
    refused(RefusalReason.OUTBOX_TRANSITION, s.approve, oid)
    refused(RefusalReason.OUTBOX_TRANSITION, s.intake.retry, oid, actor="alice")

    (found,) = s.intake.reconcile(oid, actor="alice")
    assert found.state is OpState.SENT and found.rules == ("O14",) and found.complete
    assert s.op(oid).receipt.reconciled and found.notified == "replied"
    assert s.world.write_requests() == 1 and s.world.comment_count(s.issue) == 1


def test_k1_a_write_never_applied_needs_two_complete_listings_then_one_new_post() -> None:
    s = Scene()
    oid = s.draft()
    first = s.approve(oid).grant_id
    s.world.fail_next_write_ambiguously(applied=False)  # K1: cut off before anything was stored
    assert s.intake.send(oid, actor="alice").state is OpState.UNKNOWN
    assert s.world.comment_count(s.issue) == 0

    (once,) = s.intake.reconcile(actor="alice")
    assert once.state is OpState.UNKNOWN and once.rules == ("O15",)
    assert s.op(oid).absent_confirmations == 1
    refused(RefusalReason.OUTBOX_TRANSITION, s.intake.send, oid, actor="alice")
    refused(RefusalReason.OUTBOX_TRANSITION, s.intake.retry, oid, actor="alice")

    (twice,) = s.intake.reconcile(actor="alice")
    assert twice.state is OpState.FAILED and twice.rules == ("O15", "O16")
    assert s.op(oid).fail_reason == machine.ABSENT
    refused(RefusalReason.OUTBOX_TRANSITION, s.intake.send, oid, actor="alice")  # no grant

    assert s.intake.retry(oid, actor="alice").state is OpState.DRAFTED
    second = s.approve(oid).grant_id
    assert second != first
    assert s.intake.send(oid, actor="alice").state is OpState.SENT
    assert [a.n for a in s.op(oid).attempts] == [1, 2]
    assert s.world.write_requests() == 2 and s.world.comment_count(s.issue) == 1


def test_a_listing_begun_before_the_attempt_ended_never_counts_an_absence() -> None:
    s = Scene()
    oid = s.draft()
    s.approve(oid)
    s.world.fail_next_write_ambiguously(applied=False)
    s.intake.send(oid, actor="alice")
    doc = s.store.read_outbox(s.report)
    ended = doc.records[oid].op(C).attempt.end_seq
    stale = machine.observe(  # a listing begun before the attempt ended (ordered by seq)
        doc,
        oid,
        machine.OutboxObservation(ended, True, ()),
        policy=ON,
        actor="alice",
    )
    assert not stale.changed
    s.intake.reconcile(oid, actor="alice")
    assert s.op(oid).absent_confirmations == 1 and s.op(oid).state is OpState.UNKNOWN


def test_a_reconcile_listing_that_fails_part_way_changes_nothing() -> None:
    s = Scene(page_size=1)
    s.world.add_comment(s.issue, "a human comment", at=at(1, 5))
    s.world.add_comment(s.issue, "another one", at=at(1, 6))

    class Flaky(MemoryTracker):
        def list_comments(self, query: CommentQuery):
            if query.cursor is not None:
                raise TransientFailure("the second page timed out")
            return super().list_comments(query)

    oid = s.draft()
    s.approve(oid)
    s.world.fail_next_write_ambiguously(applied=False)
    s.intake.send(oid, actor="alice")
    flaky = s.sender(Flaky(s.world, enabled=EVERYTHING, page_size=1))
    for _ in range(3):
        (result,) = flaky.reconcile(oid, actor="alice")
        assert not result.complete and result.rules == () and result.state is OpState.UNKNOWN
    assert s.op(oid).absent_confirmations == 0 and s.world.write_requests() == 1


def test_a_crash_after_the_write_ahead_is_recovered_on_the_next_lock_without_a_request() -> None:
    s = Scene()

    class Dies(MemoryTracker):
        def post_comment(self, issue, body, *, marker, grant):
            raise Crash

    oid = s.draft()
    s.approve(oid)
    with pytest.raises(Crash):
        s.sender(Dies(s.world, enabled=EVERYTHING)).send(oid, actor="alice")
    assert s.op(oid).state is OpState.SENDING and not s.lock.held(binding_key(s.binding))
    assert s.world.write_requests() == 0

    (first,) = s.intake.reconcile(actor="alice")  # O13 on acquiring the lock, then one listing
    assert first.rules == ("O13", "O15") and first.state is OpState.UNKNOWN
    (second,) = s.intake.reconcile(actor="alice")
    assert second.state is OpState.FAILED
    s.intake.retry(oid, actor="alice")
    s.approve(oid)
    assert s.intake.send(oid, actor="alice").state is OpState.SENT
    assert s.world.write_requests() == 1 and s.world.comment_count(s.issue) == 1


def test_any_mutation_recovers_a_dead_senders_attempt_first() -> None:
    s = Scene()

    class Dies(MemoryTracker):
        def post_comment(self, issue, body, *, marker, grant):
            raise Crash

    oid = s.draft()
    s.approve(oid)
    with pytest.raises(Crash):
        s.sender(Dies(s.world, enabled=EVERYTHING)).send(oid, actor="alice")
    other = s.draft("A second, unrelated reply.")
    assert s.op(oid).state is OpState.UNKNOWN and s.op(oid).attempt.end_seq is not None
    assert s.op(other).state is OpState.DRAFTED


def test_a_send_while_the_binding_is_held_refuses_busy_and_writes_nothing() -> None:
    s = Scene()
    oid = s.draft()
    s.approve(oid)
    writes = list(s.store.outbox_writes)
    with s.lock.hold(binding_key(s.binding)):
        refused(RefusalReason.INTAKE_BUSY, s.intake.send, oid, actor="alice")
        refused(RefusalReason.INTAKE_BUSY, s.intake.reconcile, oid, actor="alice")
    assert s.store.outbox_writes == writes and s.world.write_requests() == 0
    assert s.op(oid).state is OpState.APPROVED


def test_a_failed_write_ahead_sends_nothing() -> None:
    s = Scene()
    oid = s.draft()
    s.approve(oid)
    s.store.fail_next_outbox_write()
    with pytest.raises(Exception, match="injected store failure"):
        s.intake.send(oid, actor="alice")
    assert s.op(oid).state is OpState.APPROVED and s.world.write_requests() == 0


# -- leaks, discard, retry, refusals -----------------------------------------------------------


def test_a_reply_naming_an_internal_id_is_held_and_published_only_by_name() -> None:
    s = Scene()
    oid = s.draft("Tracked internally as acme-12.")
    op = s.op(oid)
    assert op.state is OpState.HELD and [f.id for f in op.findings] == ["bead_id:1"]
    refused(RefusalReason.OUTBOX_TRANSITION, s.approve, oid)  # held: fix it or publish it

    result = s.approve(oid, publish=("bead_id:1",))
    assert result.state is OpState.APPROVED and result.rules == ("O4", "O5")
    (override,) = s.op(oid).approval.overrides
    assert (override.finding, override.actor) == ("bead_id:1", "alice")


def test_a_secret_is_never_publishable_and_the_exact_bytes_are_scanned() -> None:
    s = Scene()
    oid = s.draft(f"Try the key {SECRET}.")
    assert s.op(oid).state is OpState.HELD
    refused(RefusalReason.NEVER_OVERRIDABLE, s.approve, oid, publish=("secret:1",))
    assert all(SECRET not in f.match for f in s.op(oid).findings)  # stored redacted
    assert any(SECRET in text for text in s.sanitizer.scanned)
    assert s.world.write_requests() == 0


def test_with_no_sanitizer_bound_every_reply_is_held() -> None:
    s = Scene()
    s.service = s.sender(s.tracker, sanitizer=None)
    s.intake = IntakeService(s.store, _NoLabels(), outbox=s.service)
    oid = s.draft()
    assert s.op(oid).state is OpState.HELD
    refused(RefusalReason.NEVER_OVERRIDABLE, s.approve, oid, publish=("no_sanitizer:1",))


def test_redrafting_the_same_reply_to_the_same_thread_is_a_no_op() -> None:
    s = Scene()
    first = s.draft()
    writes = len(s.store.outbox_writes)
    assert s.draft() == first and len(s.store.outbox_writes) == writes  # O2


def test_discard_voids_an_approval_and_an_unknown_needs_accept_unknown() -> None:
    s = Scene()
    oid = s.draft()
    s.approve(oid)
    discarded = s.intake.discard(oid, reason="wrong issue", actor="alice")
    assert discarded.state is OpState.ABANDONED and discarded.rules == ("O8",)
    assert s.op(oid).approval.grant is GrantStatus.VOIDED

    ambiguous = s.draft("Second try.")
    s.approve(ambiguous)
    s.world.fail_next_write_ambiguously()
    s.intake.send(ambiguous, actor="alice")
    refused(RefusalReason.UNKNOWN_OUTCOME, s.intake.discard, ambiguous, reason="x", actor="alice")
    refused(RefusalReason.OPERATOR_SEAT_REQUIRED, s.intake.discard, ambiguous, reason="x", actor="")
    result = s.intake.discard(ambiguous, reason="give up", actor="alice", accept_unknown=True)
    assert result.rules == ("O18",) and s.op(ambiguous).outcome_unknown

    (late,) = s.intake.reconcile(actor="alice")  # the post did land: O19, never a second one
    assert late.outbox_id == ambiguous and late.rules == ("O19",) and s.op(ambiguous).receipt.late
    assert s.world.write_requests() == 1


def test_retry_is_operator_only() -> None:
    s = Scene()
    oid = s.draft()
    s.approve(oid)
    s.world.fail_next_write_ambiguously(applied=False)
    s.intake.send(oid, actor="alice")
    s.intake.reconcile(actor="alice")
    s.intake.reconcile(actor="alice")
    refused(RefusalReason.OPERATOR_SEAT_REQUIRED, s.intake.retry, oid, actor="dev/agent")
    assert s.op(oid).state is OpState.FAILED


def test_labels_change_is_not_sendable_in_this_build() -> None:
    s = Scene()
    oid = s.draft()
    request = OutboxApprove(oid, s.digest(oid), "alice", kind=OpKind.LABELS)
    refused(RefusalReason.NOT_AVAILABLE, s.intake.approve, request)


def test_a_published_operational_store_refuses_before_any_tracker_request() -> None:
    s = Scene(posture=StorePosture(published=True))
    oid = "ob1_" + "a" * 26
    refused(RefusalReason.PUBLISHED_STORE, s.intake.draft_reply, ReplyDraft(s.report, "x", "a"))
    refused(RefusalReason.PUBLISHED_STORE, s.intake.approve, OutboxApprove(oid, "0" * 12, "a"))
    refused(RefusalReason.PUBLISHED_STORE, s.intake.send, oid, actor="alice")
    refused(RefusalReason.PUBLISHED_STORE, s.intake.reconcile, actor="alice")
    assert s.requests() == (0, 0)


def test_a_report_bound_to_another_project_is_refused_before_any_request() -> None:
    s = Scene(project="someone/else")
    oid = s.draft()
    refused(RefusalReason.OTHER_PROJECT, s.approve, oid)
    refused(RefusalReason.OTHER_PROJECT, s.intake.send, oid, actor="alice")
    assert s.requests() == (0, 0)


def test_a_bead_id_is_never_an_outbox_id_and_unknown_ids_are_refused() -> None:
    s = Scene()
    refused(RefusalReason.INVALID_OUTBOX_ID, s.intake.render, s.report)
    refused(RefusalReason.UNKNOWN_OUTBOX, s.intake.render, "ob1_" + "b" * 26)
    refused(RefusalReason.NOT_FOUND, s.intake.draft_reply, ReplyDraft("acme-ghost", "x", "a"))
    native = s.store.create(ReportDraft("born native", "bug", "origin:report", "dev/x"))
    refused(RefusalReason.NOT_BOUND, s.intake.draft_reply, ReplyDraft(native, "x", "a"))


def test_the_listing_is_oldest_first_and_open_only_drops_retired_records() -> None:
    s = Scene()
    first = s.draft("One.")
    second = s.draft("Two.")
    s.intake.discard(first, reason="duplicate", actor="alice")
    listing = s.intake.outbox(OutboxQuery(report=s.report))
    assert [e.record.outbox_id for e in listing.entries] == [first, second]
    open_only = s.intake.outbox(OutboxQuery(open_only=True))
    assert [(e.report, e.record.outbox_id) for e in open_only.entries] == [(s.report, second)]


def test_outbox_writes_leave_every_other_document_on_the_report_untouched() -> None:
    s = Scene()
    s.store.put_metadata(s.report, "git.commits", {"commits": ["abc123"]})
    s.world.add_comment(s.issue, "a human comment", at=at(1, 5))
    _ingest(s)
    before = s.store.metadata(s.report)
    oid = s.draft()
    s.approve(oid)
    s.intake.send(oid, actor="alice")
    after = s.store.metadata(s.report)
    assert {k: v for k, v in after.items() if k != "intake.outbox"} == before
    assert after["git.commits"] == {"commits": ["abc123"]} and "intake.thread" in after


# -- the thread view with issued() -------------------------------------------------------------


def _ingest(s: Scene):
    threads = ThreadService(
        s.store,
        s.tracker,
        s.lock,
        posture=PRIVATE,
        identity=ME,
        outbox=StoreOutboxView(s.store),
    )
    threads.ingest_thread(ThreadRequest(s.report, "dev/ingest"))
    thread = s.store.read_thread(s.report)
    assert thread is not None
    return thread


def test_the_thread_view_classifies_an_ambiguous_post_as_own_while_pending() -> None:
    s = Scene()
    oid = s.draft()
    s.approve(oid)
    s.world.fail_next_write_ambiguously()
    s.intake.send(oid, actor="alice")
    view = StoreOutboxView(s.store)
    assert view.pending(oid) and view.issued(oid) and view.receipt_ids() == frozenset()

    (record,) = _ingest(s).records.values()
    assert (record.role, record.evidence, record.outbox_id) == (Role.OWN, Evidence.RECONCILED, oid)


def test_an_abandoned_ambiguous_post_that_turns_up_is_still_own_through_issued() -> None:
    s = Scene()
    oid = s.draft()
    s.approve(oid)
    s.world.fail_next_write_ambiguously()
    s.intake.send(oid, actor="alice")
    s.intake.discard(oid, reason="give up", actor="alice", accept_unknown=True)
    view = StoreOutboxView(s.store)
    assert not view.pending(oid) and view.issued(oid)

    (record,) = _ingest(s).records.values()
    assert record.role is Role.OWN and record.evidence is Evidence.RECONCILED


def test_a_sent_reply_is_own_by_its_receipt_and_a_draft_is_never_issued() -> None:
    s = Scene()
    drafted = s.draft("Not sent yet.")
    oid = s.draft()
    s.approve(oid)
    s.intake.send(oid, actor="alice")
    view = StoreOutboxView(s.store)
    assert not view.issued(drafted) and not view.pending(drafted)
    assert view.known_actors() == {translate.thread_actor(s.tracker.whoami())}

    (record,) = _ingest(s).records.values()
    assert record.role is Role.OWN and record.evidence is Evidence.RECEIPT
