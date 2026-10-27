"""The supervised ``labels.change`` sender (I9b) end to end on ``MemoryTracker``.

The same wiring as the ``comment.create`` sender tests (``OutboxService`` over
``MemoryReportStore``, ``MemoryTracker`` / ``MemoryWorld``, the never-waiting
``InProcessIntakeLock``, ``SequentialIdSource``, ``RecordingSanitizer``), for a label operation. It
pins the bead's acceptance: only labels under the owned prefixes are ever changed; a separate
enable flag and a separate grant from ``comment.create``; and that an ambiguous outcome reconciles
(by reading the issue's labels back, since labels carry no marker) without a duplicate change.

Ordering is asserted on ``seq``, ``Attempt.n``, ``write_requests()``, ``read_requests()`` and
``server_labels()`` only. No clock, no sleep, no socket, and no real tracker: every write lands in
the in-memory world.
"""

from __future__ import annotations

import dataclasses

import pytest

from beadhive.modules.intake.application import IntakeService, NullIntake, OutboxService
from beadhive.modules.intake.application.threads import binding_key
from beadhive.modules.intake.domain import (
    DraftRequest,
    GrantStatus,
    IntakeRefused,
    IssueBinding,
    IssueStatus,
    LabelBase,
    LabelDiff,
    LabelIntent,
    LeakPolicy,
    Message,
    OpKind,
    OpState,
    OutboxApprove,
    OutboxDocument,
    OutboxEvent,
    OutboxPolicy,
    OwnIdentity,
    RefusalReason,
    RenderContext,
    ReplyDraft,
    ReportDraft,
    SourceSnapshot,
    StorePosture,
)
from beadhive.modules.intake.domain import outbox as machine
from beadhive.modules.tracker.contracts import (
    READ_OPERATIONS,
    WRITE_OPERATIONS,
    LabelOwnership,
    Operation,
    OwnMarker,
    TransientFailure,
)
from beadhive.testing.intake import (
    SCOPE,
    InProcessIntakeLock,
    MemoryReportStore,
    RecordingSanitizer,
    SequentialIdSource,
)
from beadhive.testing.tracker import MemoryTracker, MemoryWorld, at, without_resolver

ME = OwnIdentity("acme/widgets", "inst-1")
LEAKS = LeakPolicy(bead_prefixes=frozenset({"acme", "pub"}), owned_label_prefixes=("beadhive:",))
BOTH = OutboxPolicy(enabled=True, labels_enabled=True)
COMMENT_ONLY = OutboxPolicy(enabled=True)  # the labels flag is off (the default)
PRIVATE = StorePosture(published=False)
C, L = OpKind.COMMENT, OpKind.LABELS
EVERYTHING = READ_OPERATIONS | WRITE_OPERATIONS
ISSUE_LABELS = ("bug", "beadhive:triage", "needs-love")  # one human-owned label among them


def marker_line(marker: OwnMarker) -> str:
    return f"<!-- bh:v1 hive={marker.hive} install={marker.install} outbox={marker.outbox_id} -->"


class Crash(BaseException):
    """The sender process dying between the write-ahead and the request."""


class _NoLabels:
    def violations(self, labels) -> tuple[str, ...]:
        return ()


class Scene:
    """One bound report on one memory issue that carries labels, wired as the root wires it."""

    def __init__(
        self,
        *,
        policy: OutboxPolicy = BOTH,
        enabled=EVERYTHING,
        leaks: LeakPolicy = LEAKS,
        tracker_cls=MemoryTracker,
    ) -> None:
        self.world = MemoryWorld()
        self.issue = self.world.add_issue("a bug", at=at(1), labels=ISSUE_LABELS)
        self.world.set_write_time(at(2))
        self.tracker = tracker_cls(self.world, enabled=frozenset(enabled))
        self.store = MemoryReportStore(SCOPE)
        binding = IssueBinding(
            "", "memory", self.world.locator, self.issue.url, self.issue.identifier
        )
        self.report = self.store.create(
            ReportDraft(
                "a bug",
                "bug",
                "origin:github",
                "dev/import",
                binding=binding,
                snapshot=SourceSnapshot("a bug", "", IssueStatus.OPEN, "r1", labels=ISSUE_LABELS),
            )
        )
        self.lock = InProcessIntakeLock()
        self.ids = SequentialIdSource()
        self.sanitizer = RecordingSanitizer.of([])
        self.policy = policy
        self.leaks = leaks
        self.service = self.sender(self.tracker)
        self.intake = IntakeService(self.store, _NoLabels(), outbox=self.service)

    def sender(self, tracker, *, policy: OutboxPolicy | None = None, leaks=None) -> OutboxService:
        return OutboxService(
            self.store,
            tracker,
            self.lock,
            ids=self.ids,
            identity=ME,
            marker_line=marker_line,
            policy=policy or self.policy,
            leak_policy=leaks or self.leaks,
            sanitizer=self.sanitizer,
            posture=PRIVATE,
            holder="host-1:42",
        )

    @property
    def binding(self) -> IssueBinding:
        view = self.store.get(self.report)
        assert view is not None and view.binding is not None
        return view.binding

    # -- drafting (a milestone trigger's job; the sender never drafts a label change) -----------

    def draft(
        self,
        *,
        add=("beadhive:accepted",),
        remove=("beadhive:triage",),
        comment: bool = False,
        leaks: LeakPolicy | None = None,
    ) -> str:
        doc = self.store.read_outbox(self.report) or OutboxDocument()
        view = self.store.get(self.report)
        assert view is not None and view.snapshot is not None
        oid = self.ids.outbox_id()
        ctx = RenderContext(
            self.binding.url,
            leaks or self.leaks,
            self.sanitizer,
            marker_line=marker_line(OwnMarker(ME.hive, ME.install, oid)),
            labels=LabelBase(view.snapshot.revision, frozenset(view.snapshot.labels)),
        )
        request = DraftRequest(
            self.binding,
            OutboxEvent.MERGED,
            "c1",
            "outbox",
            message=Message("merged", "Merged; thanks for the report."),
            comment=comment,
            labels=LabelIntent(frozenset(add), frozenset(remove)),
        )
        applied = machine.draft(doc, request, outbox_id=oid, policy=self.policy, ctx=ctx)
        self.store.write_outbox(self.report, applied.document, expect_seq=doc.seq, actor="outbox")
        return oid

    def digest(self, oid: str, kind: OpKind = L) -> str:
        rendered = self.intake.render(oid).of(kind)
        assert rendered is not None
        return rendered.short_digest

    def approve(self, oid: str, kind: OpKind = L, *, actor: str = "alice", publish=()):
        request = OutboxApprove(oid, self.digest(oid, kind), actor, publish, kind=kind)
        return self.intake.approve(request)

    def op(self, oid: str, kind: OpKind = L):
        doc = self.store.read_outbox(self.report)
        assert doc is not None
        op = doc.records[oid].op(kind)
        assert op is not None
        return op

    def tamper(self, oid: str, *, add=(), remove=()) -> None:
        """Rewrite the stored diff behind the machine's back (an edited store)."""
        doc = self.store.read_outbox(self.report)
        assert doc is not None
        record = doc.records[oid]
        op = record.op(L)
        assert op is not None and op.labels is not None
        diff = LabelDiff(
            frozenset(add), frozenset(remove), op.labels.base_revision, op.labels.base_owned
        )
        edited = dataclasses.replace(
            record,
            operations=tuple(
                dataclasses.replace(o, labels=diff) if o.kind is L else o for o in record.operations
            ),
        )
        new = dataclasses.replace(doc, seq=doc.seq + 1, records={**doc.records, oid: edited})
        self.store.write_outbox(self.report, new, expect_seq=doc.seq, actor="tamper")

    def server(self) -> frozenset[str]:
        return self.world.server_labels(self.issue)

    def requests(self) -> tuple[int, int]:
        return self.world.read_requests(), self.world.write_requests()


def refused(expected: RefusalReason, call, /, *args, **kwargs) -> None:
    with pytest.raises(IntakeRefused) as caught:
        call(*args, **kwargs)
    assert caught.value.reason is expected, caught.value


# -- a separate enable flag --------------------------------------------------------------------


def test_the_labels_flag_is_off_by_default_and_separate_from_the_outbox() -> None:
    assert OutboxPolicy().labels_enabled is False
    assert OutboxPolicy(enabled=True).labels_enabled is False
    assert machine.OutboxPolicy(enabled=True, labels_enabled=True).enabled is True


def test_with_the_outbox_on_but_the_labels_flag_off_no_label_is_approved_or_sent() -> None:
    s = Scene(policy=COMMENT_ONLY)
    oid = s.draft(comment=True)
    before = s.requests()
    refused(RefusalReason.LABELS_DISABLED, s.approve, oid, L)
    refused(RefusalReason.LABELS_DISABLED, s.intake.send, oid, actor="alice", kind=L)
    assert s.requests() == before  # refused before any tracker request, the probe included
    assert s.op(oid).state is OpState.DRAFTED and s.op(oid).approvals == ()

    # the comment's own gate is untouched by the labels flag
    assert s.approve(oid, C).state is OpState.APPROVED
    assert s.intake.send(oid, actor="alice").state is OpState.SENT
    assert s.world.comment_count(s.issue) == 1 and s.server() == frozenset(ISSUE_LABELS)
    assert s.op(oid).state is OpState.DRAFTED  # the label operation is still unapproved


def test_the_labels_flag_alone_does_not_turn_the_outbox_on() -> None:
    s = Scene(policy=OutboxPolicy(labels_enabled=True))
    oid = "ob1_" + "a" * 26
    refused(RefusalReason.OUTBOX_DISABLED, s.approve, oid, L)
    refused(RefusalReason.OUTBOX_DISABLED, s.intake.send, oid, actor="alice", kind=L)
    refused(RefusalReason.OUTBOX_DISABLED, s.intake.reconcile, actor="alice")
    assert s.requests() == (0, 0)


def test_the_pure_machine_refuses_a_label_approval_and_send_without_the_flag() -> None:
    s = Scene()
    oid = s.draft()
    s.approve(oid)
    doc = s.store.read_outbox(s.report)
    assert doc is not None
    ctx = RenderContext(
        s.binding.url, LEAKS, s.sanitizer, labels=LabelBase("r1", frozenset(ISSUE_LABELS))
    )
    refused(
        RefusalReason.LABELS_DISABLED,
        machine.begin_send,
        doc,
        oid,
        L,
        actor="alice",
        policy=COMMENT_ONLY,
        ctx=ctx,
        epoch=1,
        holder="h",
    )


def test_null_intake_refuses_a_label_send_like_every_other_operation() -> None:
    oid = "ob1_" + "a" * 26
    refused(RefusalReason.INTAKE_DISABLED, NullIntake().send, oid, actor="alice", kind=L)
    refused(RefusalReason.INTAKE_DISABLED, NullIntake().retry, oid, actor="alice", kind=L)


def test_without_the_adapters_own_labels_enable_approve_refuses_and_nothing_is_sent() -> None:
    s = Scene(enabled=READ_OPERATIONS | {Operation.COMMENT_CREATE})  # labels.change is not enabled
    oid = s.draft()
    refused(RefusalReason.CAPABILITY_UNUSABLE, s.approve, oid, L)
    assert s.op(oid).state is OpState.DRAFTED and s.world.write_requests() == 0


def test_without_issues_read_an_unknown_label_outcome_could_not_be_reconciled() -> None:
    s = Scene(enabled=frozenset({Operation.COMMENTS_READ, Operation.LABELS_CHANGE}))
    oid = s.draft()
    refused(RefusalReason.CAPABILITY_UNUSABLE, s.approve, oid, L)
    assert s.world.write_requests() == 0


def test_an_enable_withdrawn_after_approval_fails_the_label_send_and_changes_nothing() -> None:
    s = Scene()
    oid = s.draft()
    s.approve(oid)
    withdrawn = s.sender(MemoryTracker(s.world, enabled=READ_OPERATIONS))
    result = withdrawn.send(oid, actor="alice", kind=L)

    assert result.state is OpState.FAILED and result.rules == ("O9", "O11")
    assert s.op(oid).fail_reason == "refused:OperationNotEnabled"
    assert s.world.write_requests() == 0 and s.server() == frozenset(ISSUE_LABELS)


def test_an_adapter_with_no_label_writer_is_refused_before_the_write_ahead() -> None:
    class ReadOnly:
        """Reads only: no ``change_labels``."""

        def __init__(self, inner: MemoryTracker) -> None:
            self._inner = inner

        def __getattr__(self, name: str):
            if name in {"change_labels", "post_comment"}:
                raise AttributeError(name)
            return getattr(self._inner, name)

    s = Scene()
    oid = s.draft()
    s.approve(oid)
    writes = list(s.store.outbox_writes)
    refused(
        RefusalReason.CAPABILITY_UNUSABLE,
        s.sender(ReadOnly(s.tracker)).send,
        oid,
        actor="alice",
        kind=L,
    )
    assert s.store.outbox_writes == writes and s.op(oid).state is OpState.APPROVED


# -- a separate grant --------------------------------------------------------------------------


def test_approving_the_comment_never_approves_the_labels_and_each_mints_its_own_grant() -> None:
    s = Scene()
    oid = s.draft(comment=True)
    comment = s.approve(oid, C)
    assert comment.state is OpState.APPROVED and comment.kind is C
    assert s.op(oid, L).state is OpState.DRAFTED and s.op(oid, L).approvals == ()
    refused(RefusalReason.OUTBOX_TRANSITION, s.intake.send, oid, actor="alice", kind=L)
    assert s.world.write_requests() == 0

    labels = s.approve(oid, L)
    assert labels.kind is L and labels.state is OpState.APPROVED
    assert machine.is_grant_id(labels.grant_id) and labels.grant_id != comment.grant_id
    assert s.digest(oid, L) != s.digest(oid, C)  # two different bytes, two different digests

    sent = s.intake.send(oid, actor="alice", kind=L)
    assert sent.state is OpState.SENT and sent.kind is L
    assert s.op(oid, L).approval.grant is GrantStatus.CONSUMED
    assert s.op(oid, C).approval.grant is GrantStatus.MINTED  # the comment's grant is still unused
    assert s.world.write_requests() == 1 and s.world.comment_count(s.issue) == 0

    assert s.intake.send(oid, actor="alice").state is OpState.SENT
    assert s.world.write_requests() == 2 and s.world.comment_count(s.issue) == 1


def test_a_grant_is_single_use_and_a_second_send_is_refused() -> None:
    s = Scene()
    oid = s.draft()
    s.approve(oid)
    s.intake.send(oid, actor="alice", kind=L)
    refused(RefusalReason.OUTBOX_TRANSITION, s.intake.send, oid, actor="alice", kind=L)
    refused(RefusalReason.OUTBOX_TRANSITION, s.approve, oid, L)
    assert s.world.write_requests() == 1


def test_the_approval_is_checked_by_digest_and_an_agent_seat_cannot_approve_or_send() -> None:
    s = Scene()
    oid = s.draft()
    refused(
        RefusalReason.STALE_DIGEST, s.intake.approve, OutboxApprove(oid, "f" * 12, "alice", kind=L)
    )
    refused(RefusalReason.OPERATOR_SEAT_REQUIRED, s.approve, oid, L, actor="dev/agent")
    s.approve(oid)
    refused(RefusalReason.OPERATOR_SEAT_REQUIRED, s.intake.send, oid, actor="disp/x", kind=L)
    assert s.op(oid).state is OpState.APPROVED and s.world.write_requests() == 0


def test_a_label_change_the_issue_already_has_cannot_be_approved() -> None:
    s = Scene()
    oid = s.draft(add=("beadhive:triage",), remove=("beadhive:never-set",))
    refused(RefusalReason.INVALID_DRAFT, s.approve, oid, L)
    assert s.op(oid).state is OpState.DRAFTED and s.world.write_requests() == 0


# -- only owned labels are ever changed --------------------------------------------------------


def test_a_label_send_changes_only_owned_labels_and_leaves_every_human_label_alone() -> None:
    s = Scene()
    oid = s.draft(add=("beadhive:accepted",), remove=("beadhive:triage",))
    shown = s.intake.render(oid).of(L)
    assert shown is not None and shown.findings == ()
    assert shown.add == {"beadhive:accepted"} and shown.remove == {"beadhive:triage"}
    assert shown.payload == "add: beadhive:accepted\nremove: beadhive:triage"
    before = s.requests()
    s.approve(oid)
    s.intake.send(oid, actor="alice", kind=L)

    assert s.server() == {"bug", "needs-love", "beadhive:accepted"}
    op = s.op(oid)
    assert op.receipt.labels_after == s.server() and not op.receipt.reconciled
    assert s.requests()[1] - before[1] == 1


def test_a_draft_naming_a_label_outside_the_owned_prefixes_is_refused_and_stores_nothing() -> None:
    s = Scene()
    for add, remove in ((("wontfix",), ()), ((), ("bug",)), (("beadhive:ok", "needs-love"), ())):
        with pytest.raises(IntakeRefused) as caught:
            s.draft(add=add, remove=remove)
        assert caught.value.reason is RefusalReason.LABEL_NOT_OWNED
    assert s.store.read_outbox(s.report) is None
    assert s.world.write_requests() == 0


def test_with_no_owned_prefixes_configured_no_label_can_be_written_at_all() -> None:
    nothing = LeakPolicy(bead_prefixes=frozenset({"acme"}))
    s = Scene(leaks=nothing)
    with pytest.raises(IntakeRefused) as caught:
        s.draft()
    assert caught.value.reason is RefusalReason.LABEL_NOT_OWNED
    assert s.world.write_requests() == 0


@pytest.mark.parametrize(
    ("add", "remove"),
    [
        (("wontfix",), ()),
        ((), ("bug",)),
        (("beadhive:accepted", "needs-love"), ("beadhive:triage",)),
    ],
    ids=["add-foreign", "remove-human", "mixed"],
)
def test_a_stored_diff_outside_the_owned_prefixes_is_refused_at_approve(add, remove) -> None:
    """The domain rules are not the only guard: a diff edited in the store is refused again at
    approve, before any grant is minted and before any request."""
    s = Scene()
    oid = s.draft()
    s.tamper(oid, add=add, remove=remove)
    refused(RefusalReason.LABEL_NOT_OWNED, s.approve, oid, L)
    assert s.op(oid).state is OpState.DRAFTED and s.op(oid).approvals == ()
    assert s.world.write_requests() == 0


def test_a_foreign_label_slipped_into_an_approved_diff_is_refused_before_the_write_ahead() -> None:
    """The foreign add is already on the issue, so the approved bytes (and digest) are unchanged:
    only the ownership guard stands between it and the adapter."""
    s = Scene()
    oid = s.draft()
    s.approve(oid)
    s.tamper(oid, add=("beadhive:accepted", "bug"), remove=("beadhive:triage",))
    writes = list(s.store.outbox_writes)
    refused(RefusalReason.LABEL_NOT_OWNED, s.intake.send, oid, actor="alice", kind=L)
    assert s.store.outbox_writes == writes  # no write-ahead
    assert s.op(oid).state is OpState.APPROVED and s.op(oid).attempts == ()
    assert s.world.write_requests() == 0 and s.server() == frozenset(ISSUE_LABELS)


def test_a_foreign_label_changing_an_approved_render_voids_it_and_sends_nothing() -> None:
    s = Scene()
    oid = s.draft()
    s.approve(oid)
    s.tamper(oid, add=("beadhive:accepted",), remove=("beadhive:triage", "bug"))
    result = s.intake.send(oid, actor="alice", kind=L)
    assert result.state in {OpState.DRAFTED, OpState.HELD} and "O7" in result.rules
    assert s.op(oid).attempts == () and s.op(oid).approval.grant is GrantStatus.VOIDED
    assert [f.kind.value for f in s.op(oid).findings] == ["label_not_owned"]
    refused(RefusalReason.NEVER_OVERRIDABLE, s.approve, oid, L, publish=("label_not_owned:1",))
    assert s.world.write_requests() == 0 and s.server() == frozenset(ISSUE_LABELS)


def test_a_narrowed_owned_set_refuses_a_diff_drafted_under_a_wider_one() -> None:
    wide = LeakPolicy(
        bead_prefixes=frozenset({"acme"}), owned_label_prefixes=("beadhive:", "triage:")
    )
    s = Scene(leaks=wide)
    oid = s.draft(add=("triage:done",), remove=("beadhive:triage",))
    narrow = s.sender(s.tracker, leaks=LEAKS)  # the config lost the "triage:" prefix since
    intake = IntakeService(s.store, _NoLabels(), outbox=narrow)
    refused(
        RefusalReason.LABEL_NOT_OWNED, intake.approve, OutboxApprove(oid, "0" * 12, "alice", kind=L)
    )
    assert s.world.write_requests() == 0


def test_the_write_ahead_is_durable_before_the_one_request_and_carries_the_ownership() -> None:
    seen: list = []
    s = Scene()

    class Watching(MemoryTracker):
        def change_labels(self, issue, *, add, remove, ownership, grant):
            seen.append((s.op(oid), add, remove, ownership, grant))
            return super().change_labels(
                issue, add=add, remove=remove, ownership=ownership, grant=grant
            )

    oid = s.draft()
    s.approve(oid)
    result = s.sender(Watching(s.world, enabled=EVERYTHING)).send(oid, actor="alice", kind=L)

    ((op, add, remove, ownership, grant),) = seen
    assert op.state is OpState.SENDING and [a.n for a in op.attempts] == [1]
    assert op.attempt.holder == "host-1:42" and op.attempt.epoch == s.lock.epoch
    assert op.approval.grant is GrantStatus.CONSUMED and grant.grant_id == op.attempt.grant_id
    assert grant.operation is Operation.LABELS_CHANGE and grant.target == s.issue
    assert (add, remove) == (frozenset({"beadhive:accepted"}), frozenset({"beadhive:triage"}))
    assert ownership == LabelOwnership(("beadhive:",))  # exactly the owned prefixes, no more
    assert result.rules == ("O9", "O10") and result.notified == ""
    assert [seq for _, seq in s.store.outbox_writes] == [1, 2, 3, 4]  # O1, O5, O9, O10


def test_a_label_receipt_marks_sent_and_stamps_no_notified_dimension() -> None:
    s = Scene()
    oid = s.draft()
    s.approve(oid)
    result = s.intake.send(oid, actor="alice", kind=L)
    view = s.store.get(s.report)
    assert view is not None
    assert result.state is OpState.SENT and result.comment_id == "" and result.notified == ""
    assert not any(label.startswith("notified:") for label in view.labels)


# -- a human edit between approval and send ----------------------------------------------------


def test_a_human_changing_an_owned_label_after_approval_voids_it_and_sends_nothing() -> None:
    s = Scene()
    oid = s.draft()
    first = s.approve(oid).grant_id
    s.store.record_snapshot(
        s.report,
        SourceSnapshot(
            "a bug", "", IssueStatus.OPEN, "r2", labels=("bug", "needs-love", "beadhive:wontfix")
        ),
        actor="dev/import",
    )
    result = s.intake.send(oid, actor="alice", kind=L)

    assert result.state is OpState.DRAFTED and result.rules == ("O7",)
    assert s.op(oid).approval.grant is GrantStatus.VOIDED
    assert s.op(oid).approval.grant_id == first
    assert s.world.write_requests() == 0
    assert s.approve(oid).grant_id != first  # a fresh approval of the re-rendered diff


# -- an ambiguous outcome reconciles without a duplicate change --------------------------------


def test_k1_an_ambiguous_applied_label_write_is_found_by_reconcile_not_changed_twice() -> None:
    s = Scene()
    oid = s.draft()
    s.approve(oid)
    s.world.fail_next_write_ambiguously()  # applied, then the tracker raised
    result = s.intake.send(oid, actor="alice", kind=L)
    assert result.state is OpState.UNKNOWN and result.rules == ("O9", "O12")
    assert s.server() == {"bug", "needs-love", "beadhive:accepted"}  # it did land

    refused(RefusalReason.OUTBOX_TRANSITION, s.intake.send, oid, actor="alice", kind=L)
    refused(RefusalReason.OUTBOX_TRANSITION, s.approve, oid, L)
    refused(RefusalReason.OUTBOX_TRANSITION, s.intake.retry, oid, actor="alice", kind=L)

    (found,) = s.intake.reconcile(actor="alice")
    assert found.kind is L and found.state is OpState.SENT
    assert found.rules == ("O14",) and found.complete
    op = s.op(oid)
    assert op.receipt.reconciled and op.receipt.labels_after == s.server()
    assert s.world.write_requests() == 1  # one change, ever
    assert s.intake.reconcile(actor="alice") == ()  # nothing left to reconcile


def test_k1_a_label_write_never_applied_needs_two_complete_reads_then_one_new_change() -> None:
    s = Scene()
    oid = s.draft()
    first = s.approve(oid).grant_id
    s.world.fail_next_write_ambiguously(applied=False)  # cut off before anything was applied
    assert s.intake.send(oid, actor="alice", kind=L).state is OpState.UNKNOWN
    assert s.server() == frozenset(ISSUE_LABELS)

    (once,) = s.intake.reconcile(actor="alice")
    assert once.state is OpState.UNKNOWN and once.rules == ("O15",)
    assert s.op(oid).absent_confirmations == 1
    refused(RefusalReason.OUTBOX_TRANSITION, s.intake.send, oid, actor="alice", kind=L)
    refused(RefusalReason.OUTBOX_TRANSITION, s.intake.retry, oid, actor="alice", kind=L)

    (twice,) = s.intake.reconcile(actor="alice")
    assert twice.state is OpState.FAILED and twice.rules == ("O15", "O16")
    assert s.op(oid).fail_reason == machine.ABSENT
    refused(RefusalReason.OUTBOX_TRANSITION, s.intake.send, oid, actor="alice", kind=L)  # no grant

    assert s.intake.retry(oid, actor="alice", kind=L).state is OpState.DRAFTED
    second = s.approve(oid).grant_id
    assert second != first
    assert s.intake.send(oid, actor="alice", kind=L).state is OpState.SENT
    assert [a.n for a in s.op(oid).attempts] == [1, 2]
    assert s.server() == {"bug", "needs-love", "beadhive:accepted"}
    assert s.world.write_requests() == 2


def test_a_label_read_that_fails_changes_nothing_and_counts_no_absence() -> None:
    class Flaky(MemoryTracker):
        def get(self, key):
            raise TransientFailure("the read timed out")

    s = Scene()
    oid = s.draft()
    s.approve(oid)
    s.world.fail_next_write_ambiguously(applied=False)
    s.intake.send(oid, actor="alice", kind=L)
    flaky = s.sender(Flaky(s.world, enabled=EVERYTHING))
    for _ in range(3):
        (result,) = flaky.reconcile(oid, actor="alice")
        assert not result.complete and result.rules == () and result.state is OpState.UNKNOWN
        assert "failed part-way" in result.detail
    assert s.op(oid).absent_confirmations == 0 and s.world.write_requests() == 1


def test_reconcile_without_an_issue_resolver_pages_the_issue_listing() -> None:
    s = Scene(tracker_cls=lambda world, **kw: without_resolver(MemoryTracker(world, **kw)))
    oid = s.draft()
    s.approve(oid)
    s.world.fail_next_write_ambiguously()
    assert s.intake.send(oid, actor="alice", kind=L).state is OpState.UNKNOWN
    (found,) = s.intake.reconcile(actor="alice")
    assert found.state is OpState.SENT and found.rules == ("O14",)
    assert s.world.write_requests() == 1


def test_reconcile_reads_only_what_the_operation_needs() -> None:
    s = Scene()
    oid = s.draft()
    s.approve(oid)
    s.world.fail_next_write_ambiguously(applied=False)
    s.intake.send(oid, actor="alice", kind=L)
    reads = s.world.read_requests()
    s.intake.reconcile(actor="alice")
    assert s.world.read_requests() - reads == 1  # one issue read, and no comment listing

    both = Scene()
    oid = both.draft(comment=True)
    both.approve(oid, C)
    both.approve(oid, L)
    both.world.fail_next_write_ambiguously(applied=False)
    both.intake.send(oid, actor="alice", kind=C)
    both.world.fail_next_write_ambiguously()  # the label change is applied, but unconfirmed
    both.intake.send(oid, actor="alice", kind=L)
    reads = both.world.read_requests()
    results = both.intake.reconcile(actor="alice")
    assert [(r.kind, r.state) for r in results] == [(C, OpState.UNKNOWN), (L, OpState.SENT)]
    assert both.world.read_requests() - reads == 2  # one listing and one issue read
    assert both.world.write_requests() == 2


def test_a_crash_after_the_label_write_ahead_is_recovered_without_a_request() -> None:
    class Dies(MemoryTracker):
        def change_labels(self, issue, *, add, remove, ownership, grant):
            raise Crash

    s = Scene()
    oid = s.draft()
    s.approve(oid)
    with pytest.raises(Crash):
        s.sender(Dies(s.world, enabled=EVERYTHING)).send(oid, actor="alice", kind=L)
    assert s.op(oid).state is OpState.SENDING and not s.lock.held(binding_key(s.binding))
    assert s.world.write_requests() == 0

    (first,) = s.intake.reconcile(actor="alice")  # O13 on acquiring the lock, then one read
    assert first.rules == ("O13", "O15") and first.state is OpState.UNKNOWN
    (second,) = s.intake.reconcile(actor="alice")
    assert second.state is OpState.FAILED
    s.intake.retry(oid, actor="alice", kind=L)
    s.approve(oid)
    assert s.intake.send(oid, actor="alice", kind=L).state is OpState.SENT
    assert s.world.write_requests() == 1


def test_an_abandoned_ambiguous_label_change_that_turns_up_is_sent_late_and_not_repeated() -> None:
    s = Scene()
    oid = s.draft()
    s.approve(oid)
    s.world.fail_next_write_ambiguously()
    s.intake.send(oid, actor="alice", kind=L)
    refused(RefusalReason.UNKNOWN_OUTCOME, s.intake.discard, oid, reason="x", actor="alice", kind=L)
    gone = s.intake.discard(oid, reason="give up", actor="alice", accept_unknown=True, kind=L)
    assert gone.rules == ("O18",) and s.op(oid).outcome_unknown

    (late,) = s.intake.reconcile(actor="alice")
    assert late.kind is L and late.rules == ("O19",) and s.op(oid).receipt.late
    assert s.world.write_requests() == 1


def test_discarding_one_operation_leaves_the_other_untouched() -> None:
    s = Scene()
    oid = s.draft(comment=True)
    s.approve(oid, L)
    result = s.intake.discard(oid, reason="not now", actor="alice", kind=L)
    assert result.kind is L and result.state is OpState.ABANDONED and result.rules == ("O8",)
    assert s.op(oid, L).approval.grant is GrantStatus.VOIDED
    assert s.op(oid, C).state is OpState.DRAFTED
    assert s.world.write_requests() == 0


def test_a_comment_only_reply_still_has_no_label_operation_to_send() -> None:
    s = Scene()
    reply = s.intake.draft_reply(ReplyDraft(s.report, "Thanks.", "alice"))
    refused(RefusalReason.UNKNOWN_OUTBOX, s.intake.send, reply.outbox_id, actor="alice", kind=L)
    request = OutboxApprove(reply.outbox_id, "0" * 12, "alice", kind=L)
    refused(RefusalReason.UNKNOWN_OUTBOX, s.intake.approve, request)
    assert s.world.write_requests() == 0
