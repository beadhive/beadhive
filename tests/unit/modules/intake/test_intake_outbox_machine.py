"""The outbox's pure state machine, driven directly: every transition O1 to O19 and every refusal.

``bh-6wj9y`` R5 (the table), R7 (invariants I1 to I3), R14 cases X2, X4 to X7, X11 and X18 at the
domain level. No tracker, no store, no clock: ordering is asserted on ``seq`` and ``Attempt.n``
only, ids come from a deterministic ``IdSource``.
"""

from __future__ import annotations

import dataclasses

import pytest

from beadhive.modules.intake.contracts import OutboxView
from beadhive.modules.intake.domain import (
    AGENT_SEAT_PREFIXES,
    ApprovalRequest,
    Capabilities,
    DraftRequest,
    GrantStatus,
    IntakeRefused,
    IssueBinding,
    LabelBase,
    LabelIntent,
    LeakKind,
    LeakPolicy,
    MarkedComment,
    Message,
    OpKind,
    OpState,
    OutboxDocument,
    OutboxEvent,
    OutboxObservation,
    OutboxPolicy,
    PublicFacts,
    Receipt,
    RecordStatus,
    RefusalReason,
    RenderContext,
    ThreadActor,
    outbox_view,
    reply_revision,
)
from beadhive.modules.intake.domain import outbox as ob
from beadhive.testing.intake import RecordingSanitizer, SequentialIdSource

URL = "https://tracker.example/acme/widgets/issues/7"
BINDING = IssueBinding("acme-r1", "memory", "acme/widgets", URL, "#7")
POLICY = LeakPolicy(
    bead_prefixes=frozenset({"acme", "pub", "bh"}),
    private_dimensions=frozenset({"intake", "origin", "delivery", "notified", "review"}),
    owned_label_prefixes=("beadhive:",),
)
ON = OutboxPolicy(enabled=True, labels_enabled=True)
OPERATOR = "alice"
ALL = Capabilities(frozenset({"comment.create", "labels.change", "comments.read", "issues.read"}))
BOT = ThreadActor("bot-1", "bot", "beadhive[bot]")
LABELS = LabelBase("rev-1", frozenset({"bug", "beadhive:delivery-pending"}))
C, L = OpKind.COMMENT, OpKind.LABELS
FACTS = PublicFacts(tag="v1.2")


def ctx(**changes) -> RenderContext:
    base = RenderContext(
        binding_url=URL,
        policy=POLICY,
        sanitizer=RecordingSanitizer(),
        marker_line="<!-- bh:v1 hive=acme/widgets install=inst-1 -->",
        labels=LABELS,
    )
    return dataclasses.replace(base, **changes)


class Outbox:
    """A scenario: one report's document and a deterministic id source."""

    def __init__(self, policy: OutboxPolicy = ON) -> None:
        self.doc = OutboxDocument()
        self.ids = SequentialIdSource()
        self.policy = policy

    def apply(self, applied: ob.OutboxApplied) -> ob.OutboxApplied:
        self.doc = applied.document
        return applied

    def draft(
        self,
        event: OutboxEvent = OutboxEvent.MERGED,
        revision: str = "c1",
        text: str = "Fixed and merged; it ships in {tag}.",
        *,
        comment: bool = True,
        labels: LabelIntent | None = None,
        render: RenderContext | None = None,
        facts: PublicFacts = FACTS,
    ) -> str:
        oid = self.ids.outbox_id()
        request = DraftRequest(
            BINDING, event, revision, "outbox", facts, Message(event.value, text), comment, labels
        )
        applied = self.apply(
            ob.draft(self.doc, request, outbox_id=oid, policy=self.policy, ctx=render or ctx())
        )
        return applied.outbox_id

    def state(self, oid: str, kind: OpKind = C) -> OpState:
        return self.doc.record(oid).op(kind).state

    def op(self, oid: str, kind: OpKind = C) -> ob.OutboxOperation:
        return self.doc.record(oid).op(kind)

    def digest(self, oid: str, kind: OpKind = C, render: RenderContext | None = None) -> str:
        return ob.render(self.doc.record(oid), kind, render or ctx()).short_digest

    def approve(
        self,
        oid: str,
        kind: OpKind = C,
        *,
        actor: str = OPERATOR,
        render: RenderContext | None = None,
        caps: Capabilities = ALL,
        grant: str | None = None,
        digest: str | None = None,
    ) -> ob.OutboxApplied:
        render = render or ctx()
        request = ApprovalRequest(oid, kind, digest or self.digest(oid, kind, render), actor)
        return self.apply(
            ob.approve(
                self.doc,
                request,
                grant_id=grant or self.ids.grant_id(),
                policy=self.policy,
                ctx=render,
                capabilities=caps,
            )
        )

    def send(self, oid: str, kind: OpKind = C, *, epoch: int = 1, **kw) -> ob.OutboxApplied:
        kw.setdefault("actor", OPERATOR)
        kw.setdefault("ctx", ctx())
        return self.apply(
            ob.begin_send(
                self.doc, oid, kind, policy=self.policy, epoch=epoch, holder="h:1:t", **kw
            )
        )

    def to_unknown(self, oid: str, kind: OpKind = C) -> None:
        self.approve(oid, kind)
        self.send(oid, kind)
        self.apply(ob.record_unknown(self.doc, oid, kind, actor="sender"))

    def observe(self, oid: str, **kw) -> ob.OutboxApplied:
        kw.setdefault("start_seq", self.doc.seq)
        kw.setdefault("complete", True)
        return self.apply(
            ob.observe(self.doc, oid, OutboxObservation(**kw), policy=self.policy, actor="rec")
        )


def refused(expected: RefusalReason, call, /, *args, **kwargs) -> IntakeRefused:
    with pytest.raises(IntakeRefused) as caught:
        call(*args, **kwargs)
    assert caught.value.reason is expected, caught.value
    return caught.value


def mine(oid: str, comment: str = "c9", known: bool = True) -> MarkedComment:
    return MarkedComment(comment, oid, BOT, known)


# -- O1, O2: draft and the DraftKey ----------------------------------------------------------


def test_o1_draft_creates_a_drafted_record_keyed_by_its_draft_key() -> None:
    box = Outbox()
    oid = box.draft()
    record = box.doc.record(oid)

    assert ob.is_outbox_id(oid)
    assert record.draft_key == ob.DraftKey(URL, OutboxEvent.MERGED, "c1")
    assert record.group == ob.MILESTONE_GROUP and record.created_seq == box.doc.seq == 1
    assert box.state(oid) is OpState.DRAFTED
    (step,) = box.op(oid).history
    assert (step.seq, step.rule, step.from_state, step.to_state) == (1, "O1", None, OpState.DRAFTED)
    assert record.status is RecordStatus.OPEN


def test_o2_drafting_an_existing_key_is_a_no_op_without_a_write() -> None:
    """X2 at the domain level: a re-run trigger never drafts twice."""
    box = Outbox()
    oid = box.draft()
    before = box.doc

    again = ob.draft(
        before,
        DraftRequest(BINDING, OutboxEvent.MERGED, "c1", "outbox", message=Message("m", "other")),
        outbox_id=box.ids.outbox_id(),
        policy=ON,
        ctx=ctx(),
    )

    assert again.document is before and not again.changed and again.outbox_id == oid
    assert len(before.records) == 1 and before.seq == 1


def test_x18_the_outbox_is_off_by_default_and_drafts_nothing() -> None:
    box = Outbox(policy=OutboxPolicy())
    assert OutboxPolicy().enabled is False

    refused(RefusalReason.OUTBOX_DISABLED, box.draft)
    assert box.doc == OutboxDocument()


@pytest.mark.parametrize(
    ("request_changes", "reason"),
    [
        ({"binding": None}, RefusalReason.NOT_BOUND),
        ({"comment": False}, RefusalReason.INVALID_DRAFT),
        ({"message": Message("m", "   ")}, RefusalReason.INVALID_DRAFT),
        ({"facts": PublicFacts(commit_url="https://x/commit/1")}, RefusalReason.COMMIT_LINKS_OFF),
        ({"labels": LabelIntent(add=frozenset({"triage:done"}))}, RefusalReason.LABEL_NOT_OWNED),
        ({"labels": LabelIntent()}, RefusalReason.INVALID_DRAFT),
    ],
)
def test_o1_guards_refuse_and_write_nothing(request_changes, reason) -> None:
    request = dataclasses.replace(
        DraftRequest(BINDING, OutboxEvent.MERGED, "c1", "outbox", message=Message("m", "Merged.")),
        **request_changes,
    )
    oid = SequentialIdSource().outbox_id()
    refused(reason, ob.draft, OutboxDocument(), request, outbox_id=oid, policy=ON, ctx=ctx())


def test_o1_a_label_operation_needs_a_snapshot_to_diff_against() -> None:
    request = DraftRequest(
        BINDING,
        OutboxEvent.MERGED,
        "c1",
        "o",
        comment=False,
        labels=LabelIntent(add=frozenset({"beadhive:merged"})),
    )
    oid = SequentialIdSource().outbox_id()
    refused(
        RefusalReason.INVALID_DRAFT,
        ob.draft,
        OutboxDocument(),
        request,
        outbox_id=oid,
        policy=ON,
        ctx=ctx(labels=None),
    )


@pytest.mark.parametrize("bad", ["acme-r1", "bh-g62yn", "ob1_short", "", "OB1_" + "A" * 26])
def test_o1_refuses_an_outbox_id_that_is_not_opaque(bad) -> None:
    request = DraftRequest(BINDING, OutboxEvent.MERGED, "c1", "o", message=Message("m", "Merged."))
    refused(
        RefusalReason.INVALID_OUTBOX_ID,
        ob.draft,
        OutboxDocument(),
        request,
        outbox_id=bad,
        policy=ON,
        ctx=ctx(),
    )


def test_o1_refuses_reusing_an_outbox_id_for_a_new_key() -> None:
    box = Outbox()
    oid = box.draft()
    request = DraftRequest(BINDING, OutboxEvent.MERGED, "c2", "o", message=Message("m", "Merged."))
    refused(
        RefusalReason.INVALID_OUTBOX_ID,
        ob.draft,
        box.doc,
        request,
        outbox_id=oid,
        policy=ON,
        ctx=ctx(),
    )


def test_commit_links_on_lets_a_public_commit_url_render() -> None:
    box = Outbox(policy=OutboxPolicy(enabled=True, commit_links=True))
    oid = box.draft(text="Merged in {commit_url}", facts=PublicFacts(commit_url="https://x/c/1"))
    assert ob.render(box.doc.record(oid), C, ctx()).body == "Merged in https://x/c/1"


def test_a_template_reads_only_public_facts() -> None:
    message = Message("t", "{tag} {version} {progress} {report} {title} {binding}")
    facts = PublicFacts(tag="v1", version="1.0", progress="closed")
    assert ob.render_text(message, facts) == "v1 1.0 closed {report} {title} {binding}"


# -- O3: the check holds -----------------------------------------------------------------------


def test_o3_a_leak_at_draft_holds_the_operation_with_its_findings() -> None:
    box = Outbox()
    oid = box.draft(text="Fixed by acme-w9 on wt/bead/issue/acme-w9.")
    op = box.op(oid)

    assert op.state is OpState.HELD
    assert [s.rule for s in op.history] == ["O1", "O3"]
    assert {f.kind for f in op.findings} == {LeakKind.BEAD_ID, LeakKind.BRANCH}


def test_o3_a_missing_thread_holds_every_operation() -> None:
    box = Outbox()
    oid = box.draft(render=ctx(thread_missing=True))
    assert box.state(oid) is OpState.HELD
    assert "thread missing" in box.op(oid).history[-1].detail


def test_o3_no_sanitizer_bound_holds_fail_closed() -> None:
    box = Outbox()
    oid = box.draft(render=ctx(sanitizer=None))
    assert box.state(oid) is OpState.HELD
    assert [f.kind for f in box.op(oid).findings] == [LeakKind.NO_SANITIZER]


def test_o3_recheck_holds_a_drafted_operation_at_a_later_render() -> None:
    box = Outbox()
    oid = box.draft()
    unchanged = ob.recheck(box.doc, oid, C, ctx=ctx(), actor="outbox")
    assert not unchanged.changed and unchanged.document is box.doc

    held = box.apply(ob.recheck(box.doc, oid, C, ctx=ctx(thread_missing=True), actor="outbox"))
    assert held.steps[0].rule == "O3" and box.state(oid) is OpState.HELD


def test_a_closing_keyword_only_warns() -> None:
    box = Outbox()
    oid = box.draft(text="This fixes #12 upstream.")
    op = box.op(oid)
    assert op.state is OpState.DRAFTED
    assert [f.kind for f in op.findings] == [LeakKind.CLOSING_KEYWORD]
    box.approve(oid)
    assert box.state(oid) is OpState.APPROVED


# -- O4: edit and override ---------------------------------------------------------------------


def test_o4_override_publishes_a_named_finding_with_the_actor() -> None:
    box = Outbox()
    oid = box.draft(text="Tracked as acme-w9.")
    box.apply(ob.override(box.doc, oid, C, ["bead_id:1"], actor=OPERATOR, policy=ON, ctx=ctx()))

    op = box.op(oid)
    assert op.state is OpState.DRAFTED and op.history[-1].rule == "O4"
    (published,) = op.overrides
    assert (published.finding, published.kind, published.match, published.actor) == (
        "bead_id:1",
        LeakKind.BEAD_ID,
        "acme-w9",
        OPERATOR,
    )
    box.approve(oid)
    assert box.op(oid).approval.overrides == (published,)


def test_o4_override_of_some_findings_re_holds_through_o3() -> None:
    box = Outbox()
    oid = box.draft(text="acme-w9 and acme-w10")
    applied = box.apply(
        ob.override(box.doc, oid, C, ["bead_id:1"], actor=OPERATOR, policy=ON, ctx=ctx())
    )
    assert [s.rule for s in applied.steps] == ["O4", "O3"]
    assert box.state(oid) is OpState.HELD and applied.steps[0].seq == applied.steps[1].seq


@pytest.mark.parametrize(
    ("names", "actor", "reason"),
    [
        (["bead_id:9"], OPERATOR, RefusalReason.UNKNOWN_FINDING),
        ([], OPERATOR, RefusalReason.UNKNOWN_FINDING),
        (["secret:1"], OPERATOR, RefusalReason.NEVER_OVERRIDABLE),
        (["bead_id:1"], "dev/intake-i8", RefusalReason.OPERATOR_SEAT_REQUIRED),
    ],
)
def test_o4_override_refusals(names, actor, reason) -> None:
    box = Outbox()
    render = ctx(sanitizer=RecordingSanitizer.of(["hunter2"]))
    oid = box.draft(text="acme-w9 hunter2", render=render)
    before = box.doc
    refused(reason, ob.override, before, oid, C, names, actor=actor, policy=ON, ctx=render)
    assert box.state(oid) is OpState.HELD


def test_o4_override_needs_a_held_operation() -> None:
    box = Outbox()
    oid = box.draft()
    refused(
        RefusalReason.OUTBOX_TRANSITION,
        ob.override,
        box.doc,
        oid,
        C,
        ["bead_id:1"],
        actor=OPERATOR,
        policy=ON,
        ctx=ctx(),
    )


def test_o4_edit_returns_a_held_operation_to_drafted_and_drops_overrides() -> None:
    box = Outbox()
    oid = box.draft(event=OutboxEvent.REPLY, text="see acme-w9")
    applied = box.apply(
        ob.edit(box.doc, oid, actor=OPERATOR, policy=ON, ctx=ctx(), text="Thanks, fixed.")
    )
    assert applied.steps[0].rule == "O4" and box.state(oid) is OpState.DRAFTED
    assert box.doc.record(oid).message.text == "Thanks, fixed." and box.op(oid).overrides == ()


def test_edit_of_an_approved_comment_is_o7_and_voids_the_grant() -> None:
    box = Outbox()
    oid = box.draft()
    box.approve(oid)
    applied = box.apply(
        ob.edit(box.doc, oid, actor=OPERATOR, policy=ON, ctx=ctx(), text="Merged, thanks.")
    )
    assert applied.steps[0].rule == "O7" and box.state(oid) is OpState.DRAFTED
    assert box.op(oid).approvals[-1].grant is GrantStatus.VOIDED


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"actor": "disp/intake"}, RefusalReason.OPERATOR_SEAT_REQUIRED),
        ({"text": " "}, RefusalReason.INVALID_DRAFT),
        ({"facts": PublicFacts(commit_url="https://x/c")}, RefusalReason.COMMIT_LINKS_OFF),
    ],
)
def test_edit_refusals(changes, reason) -> None:
    box = Outbox()
    oid = box.draft()
    kwargs = {"actor": OPERATOR, "policy": ON, "ctx": ctx(), **changes}
    refused(reason, ob.edit, box.doc, oid, **kwargs)


# -- O5: approve (the gate) --------------------------------------------------------------------


def test_o5_an_operator_approval_of_the_fresh_digest_mints_one_grant() -> None:
    box = Outbox()
    oid = box.draft()
    rendered = ob.render(box.doc.record(oid), C, ctx())
    applied = box.approve(oid, digest=rendered.short_digest)

    op = box.op(oid)
    approval = op.approval
    assert op.state is OpState.APPROVED and applied.steps[0].rule == "O5"
    assert approval.actor == OPERATOR and approval.digest == rendered.digest
    assert approval.grant is GrantStatus.MINTED and ob.is_grant_id(approval.grant_id)
    assert approval.seq == box.doc.seq


def test_o5_accepts_the_full_digest_and_refuses_a_short_prefix() -> None:
    box = Outbox()
    oid = box.draft()
    full = ob.render(box.doc.record(oid), C, ctx()).digest
    refused(RefusalReason.STALE_DIGEST, box.approve, oid, digest=full[7:18])  # 11 hex chars
    box.approve(oid, digest=full)
    assert box.state(oid) is OpState.APPROVED


@pytest.mark.parametrize("actor", ["", "  ", "dev/intake-i8", "disp/intake", "crew/x", "coord/y"])
def test_x6_an_agent_seat_or_no_actor_cannot_approve(actor) -> None:
    box = Outbox()
    oid = box.draft()
    refused(RefusalReason.OPERATOR_SEAT_REQUIRED, box.approve, oid, actor=actor)
    assert box.state(oid) is OpState.DRAFTED and box.op(oid).approvals == ()


def test_x6_a_stale_digest_is_refused() -> None:
    box = Outbox()
    oid = box.draft()
    stale = box.digest(oid, render=ctx(marker_line="<!-- another marker -->"))
    refused(RefusalReason.STALE_DIGEST, box.approve, oid, digest=stale)
    assert box.state(oid) is OpState.DRAFTED


@pytest.mark.parametrize(
    "usable",
    [
        frozenset({"comment.create"}),  # comments.read disabled: an unknown could not reconcile
        frozenset({"comments.read"}),  # the write itself is not usable
        frozenset(),
    ],
)
def test_x6_an_unusable_capability_is_refused(usable) -> None:
    box = Outbox()
    oid = box.draft()
    refused(RefusalReason.CAPABILITY_UNUSABLE, box.approve, oid, caps=Capabilities(usable))
    assert box.state(oid) is OpState.DRAFTED


def test_x6_an_unresolved_finding_at_approval_is_refused() -> None:
    box = Outbox()
    oid = box.draft()
    leaky = ctx(sanitizer=RecordingSanitizer.of(["merged"]))
    refused(RefusalReason.UNRESOLVED_FINDING, box.approve, oid, render=leaky)
    refused(RefusalReason.UNRESOLVED_FINDING, box.approve, oid, render=ctx(thread_missing=True))
    assert box.state(oid) is OpState.DRAFTED


def test_o5_refuses_a_held_operation_a_moved_binding_and_a_reused_grant() -> None:
    box = Outbox()
    held = box.draft(revision="c0", text="acme-w9")
    refused(RefusalReason.OUTBOX_TRANSITION, box.approve, held)
    oid = box.draft(event=OutboxEvent.REPLY, revision="r1", text="Thanks!")
    refused(RefusalReason.TARGET_MISMATCH, box.approve, oid, render=ctx(binding_url=URL + "9"))
    refused(RefusalReason.GRANT_REUSED, box.approve, oid, grant="not-a-grant")
    box.approve(oid)
    used = box.op(oid).approval.grant_id
    box.apply(ob.invalidate(box.doc, oid, C, ctx=ctx(marker_line="x"), actor="sender"))
    refused(RefusalReason.GRANT_REUSED, box.approve, oid, grant=used, render=ctx(marker_line="x"))


def test_only_an_operator_approval_reaches_approved() -> None:
    """No rule other than O5 moves an operation to ``approved`` (no automatic approval)."""
    box = Outbox()
    oid = box.draft()
    box.approve(oid)
    box.send(oid)
    box.apply(ob.record_refusal(box.doc, oid, C, "PayloadRejected", actor="sender"))
    box.apply(ob.retry(box.doc, oid, C, actor=OPERATOR, policy=ON))
    rules = [s.rule for s in box.op(oid).history if s.to_state is OpState.APPROVED]
    assert rules == ["O5"]


# -- X7: two operations, two gates -------------------------------------------------------------


def test_x7_comment_and_labels_are_two_approvals_with_two_grants() -> None:
    box = Outbox()
    oid = box.draft(labels=LabelIntent(add=frozenset({"beadhive:merged"})))
    box.approve(oid, C)
    assert box.state(oid, C) is OpState.APPROVED and box.state(oid, L) is OpState.DRAFTED
    box.approve(oid, L)

    comment, labels = box.op(oid, C).approval, box.op(oid, L).approval
    assert comment.grant_id != labels.grant_id
    assert comment.digest != labels.digest
    plan = box.send(oid, C).plan
    assert plan.kind is C and plan.grant_id == comment.grant_id
    assert box.state(oid, L) is OpState.APPROVED  # sending the comment never sends the labels


# -- I9b: the labels gate, its read and the owned-prefix guard ---------------------------------

COMMENT_ONLY = OutboxPolicy(enabled=True)  # the labels flag is its own, and off by default


def test_i9b_a_label_operation_needs_its_own_flag_to_be_approved_or_sent() -> None:
    box = Outbox()
    oid = box.draft(labels=LabelIntent(add=frozenset({"beadhive:merged"})))
    box.approve(oid, L)
    box.policy = COMMENT_ONLY
    refused(RefusalReason.LABELS_DISABLED, box.send, oid, L)  # an approved op is not sendable
    assert box.state(oid, L) is OpState.APPROVED
    other = box.draft(revision="c2", labels=LabelIntent(add=frozenset({"beadhive:released"})))
    refused(RefusalReason.LABELS_DISABLED, box.approve, other, L)
    box.approve(other, C)  # the comment's gate is not the labels'
    assert box.state(other, C) is OpState.APPROVED and box.state(other, L) is OpState.DRAFTED


def test_i9b_a_label_approval_also_needs_issues_read_to_reconcile_an_unknown_outcome() -> None:
    box = Outbox()
    oid = box.draft(labels=LabelIntent(add=frozenset({"beadhive:merged"})))
    no_issue_read = Capabilities(frozenset({"comment.create", "labels.change", "comments.read"}))
    refused(RefusalReason.CAPABILITY_UNUSABLE, box.approve, oid, L, caps=no_issue_read)
    box.approve(oid, C, caps=no_issue_read)  # a comment never needs it


def test_i9b_a_label_change_the_issue_already_has_cannot_be_approved() -> None:
    box = Outbox()
    oid = box.draft(labels=LabelIntent(add=frozenset({"beadhive:delivery-pending"})))
    refused(RefusalReason.INVALID_DRAFT, box.approve, oid, L)
    assert box.state(oid, L) is OpState.DRAFTED


def test_i9b_approve_and_send_refuse_a_stored_diff_outside_the_owned_prefixes() -> None:
    box = Outbox()
    oid = box.draft(labels=LabelIntent(add=frozenset({"beadhive:merged"})))
    box.approve(oid, L)
    record = box.doc.record(oid)
    op = record.op(L)
    # "bug" is already on the issue, so the approved bytes and digest do not move
    stray = dataclasses.replace(
        op, labels=dataclasses.replace(op.labels, add=op.labels.add | {"bug"})
    )
    record = dataclasses.replace(
        record, operations=tuple(stray if o.kind is L else o for o in record.operations)
    )
    box.doc = dataclasses.replace(box.doc, records={**box.doc.records, oid: record})
    refused(RefusalReason.LABEL_NOT_OWNED, box.send, oid, L)
    assert box.state(oid, L) is OpState.APPROVED and not box.op(oid, L).attempts

    fresh = Outbox()
    other = fresh.draft(labels=LabelIntent(add=frozenset({"beadhive:merged"})))
    record = fresh.doc.record(other)
    op = record.op(L)
    stray = dataclasses.replace(op, labels=dataclasses.replace(op.labels, add=frozenset({"x"})))
    record = dataclasses.replace(
        record, operations=tuple(stray if o.kind is L else o for o in record.operations)
    )
    fresh.doc = dataclasses.replace(fresh.doc, records={**fresh.doc.records, other: record})
    refused(RefusalReason.LABEL_NOT_OWNED, fresh.approve, other, L)


# -- O6: supersession (X4, X5) -----------------------------------------------------------------


@pytest.mark.parametrize("approve_first", [False, True])
def test_x4_released_supersedes_a_drafted_or_approved_merged_and_voids_its_grant(
    approve_first,
) -> None:
    box = Outbox()
    merged = box.draft(OutboxEvent.MERGED, "c1")
    if approve_first:
        box.approve(merged)
    released = box.draft(OutboxEvent.RELEASED, "v1.2@c9")

    op = box.op(merged)
    assert op.state is OpState.SUPERSEDED and op.history[-1].rule == "O6"
    assert all(a.grant is GrantStatus.VOIDED for a in op.approvals)
    assert box.doc.record(released).supersedes == merged
    assert box.state(released) is OpState.DRAFTED
    assert box.doc.record(merged).status is RecordStatus.RETIRED


def test_o6_supersedes_a_held_record_too() -> None:
    box = Outbox()
    merged = box.draft(text="acme-w9")
    box.draft(OutboxEvent.RELEASED, "v1")
    assert box.state(merged) is OpState.SUPERSEDED


@pytest.mark.parametrize("stage", ["sending", "unknown", "sent", "failed"])
def test_x5_a_possibly_sent_merged_is_never_superseded(stage) -> None:
    box = Outbox()
    merged = box.draft()
    box.approve(merged)
    box.send(merged)
    if stage == "unknown":
        box.apply(ob.record_unknown(box.doc, merged, C, actor="sender"))
    elif stage == "sent":
        box.apply(ob.record_receipt(box.doc, merged, C, Receipt("c1"), actor="sender"))
    elif stage == "failed":
        box.apply(ob.record_refusal(box.doc, merged, C, "NotVisible", actor="sender"))
    before = box.op(merged)

    box.draft(OutboxEvent.RELEASED, "v1")

    assert box.op(merged) == before


def test_replies_are_never_superseded() -> None:
    box = Outbox()
    first = box.draft(OutboxEvent.REPLY, reply_revision("Thanks!", 3), "Thanks!")
    second = box.draft(OutboxEvent.REPLY, reply_revision("More soon.", 4), "More soon.")
    assert box.doc.record(first).group != box.doc.record(second).group
    assert box.state(first) is box.state(second) is OpState.DRAFTED


# -- O7: invalidate (X11) ----------------------------------------------------------------------


def test_o7_an_unchanged_render_keeps_the_approval() -> None:
    box = Outbox()
    oid = box.draft()
    box.approve(oid)
    kept = ob.invalidate(box.doc, oid, C, ctx=ctx(), actor="sender")
    assert not kept.changed and kept.document is box.doc


def test_o7_a_moved_binding_invalidates_and_voids_the_grant() -> None:
    box = Outbox()
    oid = box.draft()
    box.approve(oid)
    applied = box.apply(
        ob.invalidate(box.doc, oid, C, ctx=ctx(binding_url=URL + "0"), actor="sender")
    )
    assert applied.steps[0].rule == "O7" and box.state(oid) is OpState.DRAFTED
    assert box.op(oid).approval.grant is GrantStatus.VOIDED


def test_x11_a_human_owned_label_change_invalidates_and_the_new_diff_excludes_it() -> None:
    box = Outbox()
    intent = LabelIntent(
        add=frozenset({"beadhive:delivery-merged"}),
        remove=frozenset({"beadhive:delivery-pending"}),
    )
    oid = box.draft(comment=False, labels=intent)
    box.approve(oid, L)
    approved = ob.render(box.doc.record(oid), L, ctx())
    assert approved.remove == {"beadhive:delivery-pending"}

    # A label read with a new revision but no owned change keeps the approval.
    other = ctx(labels=LabelBase("rev-2", LABELS.labels | {"needs-repro"}))
    assert not ob.invalidate(box.doc, oid, L, ctx=other, actor="sender").changed

    # A human removed the owned pending label and added an owned one Beadhive never planned.
    human = ctx(labels=LabelBase("rev-3", frozenset({"bug", "beadhive:wontfix"})))
    refused(RefusalReason.STALE_DIGEST, box.send, oid, L, ctx=human)
    applied = box.apply(ob.invalidate(box.doc, oid, L, ctx=human, actor="sender"))

    assert applied.steps[0].rule == "O7" and box.state(oid, L) is OpState.DRAFTED
    diff = box.op(oid, L).labels
    assert diff.base_revision == "rev-3" and diff.base_owned == {"beadhive:wontfix"}
    fresh = ob.render(box.doc.record(oid), L, human)
    assert fresh.add == {"beadhive:delivery-merged"} and fresh.remove == frozenset()
    assert "beadhive:wontfix" not in fresh.payload
    box.approve(oid, L, render=human)
    assert box.send(oid, L, ctx=human).plan.remove == frozenset()


def test_o7_needs_an_approved_operation() -> None:
    box = Outbox()
    oid = box.draft()
    refused(RefusalReason.OUTBOX_TRANSITION, ob.invalidate, box.doc, oid, C, ctx=ctx(), actor="s")


# -- O8, O18: discard --------------------------------------------------------------------------


@pytest.mark.parametrize("stage", ["drafted", "held", "approved", "failed"])
def test_o8_discard_abandons_and_voids_any_grant(stage) -> None:
    box = Outbox()
    oid = box.draft(text="acme-w9" if stage == "held" else "Merged.")
    if stage in {"approved", "failed"}:
        box.approve(oid)
    if stage == "failed":
        box.send(oid)
        box.apply(ob.record_refusal(box.doc, oid, C, "NotVisible", actor="sender"))
    applied = box.apply(ob.discard(box.doc, oid, reason="not needed", actor=OPERATOR, policy=ON))

    op = box.op(oid)
    assert op.state is OpState.ABANDONED and applied.steps[0].rule == "O8"
    assert op.history[-1].detail == "not needed" and not op.outcome_unknown
    assert all(a.grant is not GrantStatus.MINTED for a in op.approvals)


def test_o8_discard_refusals() -> None:
    box = Outbox()
    oid = box.draft()
    refused(
        RefusalReason.OPERATOR_SEAT_REQUIRED,
        ob.discard,
        box.doc,
        oid,
        reason="x",
        actor="dev/a",
        policy=ON,
    )
    box.approve(oid)
    box.send(oid)
    refused(
        RefusalReason.OUTBOX_TRANSITION,
        ob.discard,
        box.doc,
        oid,
        reason="x",
        actor=OPERATOR,
        policy=ON,
    )
    box.apply(ob.record_receipt(box.doc, oid, C, Receipt("c1"), actor="sender"))
    refused(
        RefusalReason.OUTBOX_TRANSITION,
        ob.discard,
        box.doc,
        oid,
        reason="x",
        actor=OPERATOR,
        policy=ON,
    )
    refused(
        RefusalReason.OUTBOX_TRANSITION,
        ob.discard,
        box.doc,
        oid,
        reason="x",
        actor=OPERATOR,
        policy=ON,
        kind=C,
    )
    refused(
        RefusalReason.UNKNOWN_OUTBOX,
        ob.discard,
        box.doc,
        "ob1_" + "a" * 26,
        reason="x",
        actor=OPERATOR,
        policy=ON,
    )


def test_o18_an_unknown_is_abandoned_only_with_an_explicit_flag() -> None:
    box = Outbox()
    oid = box.draft()
    box.to_unknown(oid)
    refused(
        RefusalReason.UNKNOWN_OUTCOME,
        ob.discard,
        box.doc,
        oid,
        reason="x",
        actor=OPERATOR,
        policy=ON,
    )
    applied = box.apply(
        ob.discard(box.doc, oid, reason="gave up", actor=OPERATOR, policy=ON, accept_unknown=True)
    )
    op = box.op(oid)
    assert applied.steps[0].rule == "O18" and op.state is OpState.ABANDONED
    assert op.outcome_unknown and outbox_view(box.doc).issued(oid)


# -- O9 to O13: send ---------------------------------------------------------------------------


def test_o9_writes_the_attempt_ahead_and_consumes_the_grant() -> None:
    box = Outbox()
    oid = box.draft()
    box.approve(oid)
    grant = box.op(oid).approval.grant_id
    applied = box.send(oid, epoch=4)

    op = box.op(oid)
    assert op.state is OpState.SENDING and applied.steps[0].rule == "O9"
    (attempt,) = op.attempts
    assert (attempt.n, attempt.grant_id, attempt.epoch, attempt.start_seq) == (
        1,
        grant,
        4,
        box.doc.seq,
    )
    assert attempt.end_seq is None and op.approval.grant is GrantStatus.CONSUMED
    plan = applied.plan
    assert (plan.outbox_id, plan.kind, plan.grant_id, plan.target_url) == (oid, C, grant, URL)
    assert plan.body == "Fixed and merged; it ships in v1.2." and "bh:v1" not in plan.body


@pytest.mark.parametrize("stage", ["drafted", "held", "sending", "unknown", "failed", "sent"])
def test_a_post_is_reachable_only_from_approved(stage) -> None:
    box = Outbox()
    oid = box.draft(text="acme-w9" if stage == "held" else "Merged.")
    if stage not in {"drafted", "held"}:
        box.approve(oid)
        box.send(oid)
    if stage == "unknown":
        box.apply(ob.record_unknown(box.doc, oid, C, actor="sender"))
    if stage == "failed":
        box.apply(ob.record_refusal(box.doc, oid, C, "NotVisible", actor="sender"))
    if stage == "sent":
        box.apply(ob.record_receipt(box.doc, oid, C, Receipt("c1"), actor="sender"))
    refused(RefusalReason.OUTBOX_TRANSITION, box.send, oid)


def test_o9_refusals_on_an_approved_operation() -> None:
    box = Outbox()
    oid = box.draft()
    box.approve(oid)
    refused(RefusalReason.OPERATOR_SEAT_REQUIRED, box.send, oid, actor="disp/intake")
    refused(RefusalReason.TARGET_MISMATCH, box.send, oid, ctx=ctx(binding_url="https://x/1"))
    refused(RefusalReason.STALE_DIGEST, box.send, oid, ctx=ctx(marker_line="<!-- moved -->"))
    leaky = ctx(sanitizer=RecordingSanitizer.of(["v1.2"]))
    refused(RefusalReason.UNRESOLVED_FINDING, box.send, oid, ctx=leaky)
    refused(RefusalReason.UNRESOLVED_FINDING, box.send, oid, ctx=ctx(sanitizer=None))
    record = box.doc.record(oid)
    op = record.op(C)
    spent = dataclasses.replace(
        op, approvals=(dataclasses.replace(op.approval, grant=GrantStatus.CONSUMED),)
    )
    tampered = dataclasses.replace(
        box.doc, records={oid: dataclasses.replace(record, operations=(spent,))}
    )
    refused(
        RefusalReason.GRANT_UNUSABLE,
        ob.begin_send,
        tampered,
        oid,
        C,
        actor=OPERATOR,
        policy=ON,
        ctx=ctx(),
        epoch=1,
        holder="h",
    )
    assert box.state(oid) is OpState.APPROVED


def test_o9_checks_the_exact_bytes_with_the_marker() -> None:
    box = Outbox()
    sanitizer = RecordingSanitizer()
    render = ctx(sanitizer=sanitizer)
    oid = box.draft(render=render)
    box.approve(oid, render=render)
    plan = box.send(oid, ctx=render).plan
    assert sanitizer.scanned[-1] == f"{plan.body}\n\n{render.marker_line}"


def test_o10_a_receipt_is_sent_and_reports_notified() -> None:
    box = Outbox()
    oid = box.draft()
    box.approve(oid)
    box.send(oid)
    applied = box.apply(
        ob.record_receipt(box.doc, oid, C, Receipt("c7", "G7", actor=BOT), actor="sender")
    )
    op = box.op(oid)
    assert applied.steps[0].rule == "O10" and op.state is OpState.SENT
    assert applied.notified == "merged" and op.receipt.comment_id == "c7"
    assert op.attempt.end_seq == box.doc.seq - 1
    assert box.doc.record(oid).status is RecordStatus.DELIVERED


def test_o10_o12_o11_need_sending() -> None:
    box = Outbox()
    oid = box.draft()
    for call, args in (
        (ob.record_receipt, (Receipt("c1"),)),
        (ob.record_refusal, ("NotVisible",)),
        (ob.record_unknown, ()),
    ):
        refused(RefusalReason.OUTBOX_TRANSITION, call, box.doc, oid, C, *args, actor="sender")


def test_o11_a_proof_of_no_write_fails_and_a_retry_needs_a_new_approval() -> None:
    box = Outbox()
    oid = box.draft()
    box.approve(oid)
    first = box.op(oid).approval.grant_id
    box.send(oid)
    applied = box.apply(ob.record_refusal(box.doc, oid, C, "PayloadRejected", actor="sender"))

    op = box.op(oid)
    assert applied.steps[0].rule == "O11" and op.state is OpState.FAILED
    assert op.fail_reason == "refused:PayloadRejected" and op.approval.grant is GrantStatus.CONSUMED
    refused(RefusalReason.OUTBOX_TRANSITION, box.send, oid)
    box.apply(ob.retry(box.doc, oid, C, actor=OPERATOR, policy=ON))
    refused(RefusalReason.OUTBOX_TRANSITION, box.send, oid)
    refused(RefusalReason.GRANT_REUSED, box.approve, oid, grant=first)
    box.approve(oid)
    assert box.op(oid).approval.grant_id != first


def test_o12_an_ambiguous_result_is_unknown_and_never_resent_or_redrafted() -> None:
    box = Outbox()
    oid = box.draft()
    box.to_unknown(oid)
    op = box.op(oid)
    assert op.state is OpState.UNKNOWN and op.history[-1].rule == "O12"
    assert op.attempt.end_seq == box.doc.seq - 1
    refused(RefusalReason.OUTBOX_TRANSITION, box.send, oid)
    refused(RefusalReason.OUTBOX_TRANSITION, box.approve, oid, digest="0" * 12)
    refused(RefusalReason.OUTBOX_TRANSITION, ob.retry, box.doc, oid, C, actor=OPERATOR, policy=ON)
    refused(
        RefusalReason.OUTBOX_TRANSITION,
        ob.edit,
        box.doc,
        oid,
        actor=OPERATOR,
        policy=ON,
        ctx=ctx(),
        text="new",
    )
    assert outbox_view(box.doc).pending(oid)


def test_o13_a_dead_holder_or_moved_epoch_recovers_sending_to_unknown() -> None:
    box = Outbox()
    oid = box.draft()
    box.approve(oid)
    box.send(oid, epoch=1)

    alive = ob.recover(box.doc, live_epoch=1, holder_gone=False, actor="sender")
    assert not alive.changed

    moved = ob.recover(box.doc, live_epoch=2, holder_gone=False, actor="sender")
    assert moved.steps[0].rule == "O13" and "epoch 1" in moved.steps[0].detail

    applied = box.apply(ob.recover(box.doc, live_epoch=1, holder_gone=True, actor="sender"))
    assert applied.steps[0].rule == "O13" and box.state(oid) is OpState.UNKNOWN
    assert box.op(oid).attempt.end_seq == box.doc.seq - 1


# -- O14 to O16, O19: reconcile ----------------------------------------------------------------


def test_o14_the_own_marker_from_a_known_actor_reconciles_to_sent() -> None:
    box = Outbox()
    oid = box.draft()
    box.to_unknown(oid)
    applied = box.observe(oid, comments=(mine(oid, "c9"),), complete=False)

    op = box.op(oid)
    assert applied.steps[0].rule == "O14" and op.state is OpState.SENT
    assert op.receipt.comment_id == "c9" and op.receipt.reconciled and not op.receipt.late
    assert applied.notified == "merged"


def test_o14_a_marker_alone_or_another_records_marker_is_not_enough() -> None:
    box = Outbox()
    oid = box.draft()
    box.to_unknown(oid)
    other = box.ids.outbox_id()
    applied = box.observe(oid, comments=(mine(oid, known=False), mine(other, "c3")), complete=False)
    assert not applied.changed and box.state(oid) is OpState.UNKNOWN


def test_o15_o16_k_complete_absences_begun_after_the_attempt_fail_it() -> None:
    box = Outbox()
    oid = box.draft()
    box.to_unknown(oid)
    end = box.op(oid).attempt.end_seq

    assert not box.observe(oid, start_seq=end).changed  # began before the attempt ended
    assert not box.observe(oid, complete=False).changed  # an incomplete listing proves nothing
    first = box.observe(oid)
    assert first.steps[0].rule == "O15" and box.op(oid).absent_confirmations == 1
    assert not box.observe(oid, start_seq=box.op(oid).last_absent_seq).changed  # a replay
    second = box.observe(oid)
    assert [s.rule for s in second.steps] == ["O15", "O16"]
    op = box.op(oid)
    assert op.state is OpState.FAILED and op.fail_reason == ob.ABSENT


def test_o16_threshold_is_configurable() -> None:
    box = Outbox(policy=OutboxPolicy(enabled=True, absent_confirmations=3))
    oid = box.draft()
    box.to_unknown(oid)
    box.observe(oid)
    box.observe(oid)
    assert box.state(oid) is OpState.UNKNOWN
    box.observe(oid)
    assert box.state(oid) is OpState.FAILED
    with pytest.raises(ValueError):
        OutboxPolicy(absent_confirmations=0)


def test_o17_retry_needs_an_operator_and_a_failed_operation() -> None:
    box = Outbox()
    oid = box.draft()
    refused(RefusalReason.OUTBOX_TRANSITION, ob.retry, box.doc, oid, C, actor=OPERATOR, policy=ON)
    box.to_unknown(oid)
    box.observe(oid)
    box.observe(oid)
    refused(
        RefusalReason.OPERATOR_SEAT_REQUIRED, ob.retry, box.doc, oid, C, actor="dev/x", policy=ON
    )
    applied = box.apply(ob.retry(box.doc, oid, C, actor=OPERATOR, policy=ON))
    op = box.op(oid)
    assert applied.steps[0].rule == "O17" and op.state is OpState.DRAFTED
    assert op.absent_confirmations == 0 and op.fail_reason == ""
    box.approve(oid)
    box.send(oid)
    assert box.op(oid).attempt.n == 2


@pytest.mark.parametrize("route", ["abandoned", "failed"])
def test_o19_a_late_marker_moves_to_sent_late(route) -> None:
    box = Outbox()
    oid = box.draft()
    box.to_unknown(oid)
    if route == "abandoned":
        box.apply(
            ob.discard(box.doc, oid, reason="x", actor=OPERATOR, policy=ON, accept_unknown=True)
        )
    else:
        box.observe(oid)
        box.observe(oid)
    applied = box.observe(oid, comments=(mine(oid, "c4"),))

    op = box.op(oid)
    assert applied.steps[0].rule == "O19" and op.state is OpState.SENT
    assert op.receipt.late and op.receipt.reconciled and applied.notified == "merged"


def test_o19_a_second_marked_comment_on_a_sent_operation_flags_a_duplicate() -> None:
    box = Outbox()
    oid = box.draft()
    box.approve(oid)
    box.send(oid)
    box.apply(ob.record_receipt(box.doc, oid, C, Receipt("c2", actor=BOT), actor="sender"))
    same = box.observe(oid, comments=(mine(oid, "c2"),))
    assert not same.changed

    applied = box.observe(oid, comments=(mine(oid, "c1"), mine(oid, "c2")))
    op = box.op(oid)
    assert op.state is OpState.SENT and op.duplicate_observed and op.duplicates == ("c1",)
    assert applied.steps[0].rule == "O19" and applied.notified is None
    assert not box.observe(oid, comments=(mine(oid, "c1"),)).changed  # flagged once


def test_a_late_marker_on_a_refused_or_never_attempted_operation_changes_nothing() -> None:
    box = Outbox()
    drafted = box.draft(revision="c1")
    refused_op = box.draft(event=OutboxEvent.REPLY, revision="r", text="Thanks")
    box.approve(refused_op)
    box.send(refused_op)
    box.apply(ob.record_refusal(box.doc, refused_op, C, "NotVisible", actor="sender"))
    for oid in (drafted, refused_op):
        assert not box.observe(oid, comments=(mine(oid),)).changed


def test_labels_reconcile_by_comparing_the_observed_labels() -> None:
    box = Outbox()
    oid = box.draft(comment=False, labels=LabelIntent(add=frozenset({"beadhive:merged"})))
    box.to_unknown(oid, L)
    assert not box.observe(oid, labels=None).changed  # no label read: nothing to compare
    missing = box.observe(oid, labels=frozenset({"bug"}))
    assert missing.steps[0].rule == "O15"
    applied = box.observe(oid, labels=frozenset({"bug", "beadhive:merged"}), complete=False)
    op = box.op(oid, L)
    assert applied.steps[0].rule == "O14" and op.state is OpState.SENT
    assert op.receipt.labels_after == {"bug", "beadhive:merged"} and applied.notified is None


# -- no transition out of sent; purity; seq ----------------------------------------------------


def test_nothing_leaves_sent() -> None:
    box = Outbox()
    oid = box.draft()
    box.approve(oid)
    box.send(oid)
    box.apply(ob.record_receipt(box.doc, oid, C, Receipt("c1", actor=BOT), actor="sender"))
    sent = box.doc
    for call in (
        lambda: box.approve(oid),
        lambda: box.send(oid),
        lambda: ob.retry(sent, oid, C, actor=OPERATOR, policy=ON),
        lambda: ob.invalidate(sent, oid, C, ctx=ctx(), actor="s"),
        lambda: ob.discard(sent, oid, reason="x", actor=OPERATOR, policy=ON, kind=C),
        lambda: ob.edit(sent, oid, actor=OPERATOR, policy=ON, ctx=ctx(), text="x"),
        lambda: ob.record_unknown(sent, oid, C, actor="s"),
    ):
        with pytest.raises(IntakeRefused):
            call()
    assert not ob.recover(sent, live_epoch=9, holder_gone=True, actor="s").changed
    box.draft(OutboxEvent.RELEASED, "v1")
    assert box.state(oid) is OpState.SENT


def test_every_applied_call_is_one_write_and_never_mutates_its_input() -> None:
    box = Outbox()
    oid = box.draft()
    before = box.doc
    snapshot = repr(before)
    applied = box.approve(oid)
    assert repr(before) == snapshot and before.record(oid).op(C).state is OpState.DRAFTED
    assert applied.document.seq == before.seq + 1
    assert {s.seq for s in applied.steps} == {applied.document.seq}
    seqs = [s.seq for s in box.op(oid).history]
    assert seqs == sorted(seqs)


def test_unknown_record_or_operation_is_refused() -> None:
    box = Outbox()
    oid = box.draft()
    refused(RefusalReason.UNKNOWN_OUTBOX, box.approve, "ob1_" + "b" * 26, digest="0" * 12)
    refused(RefusalReason.UNKNOWN_OUTBOX, box.approve, oid, L, digest="0" * 12)


def test_record_status_is_derived_from_its_operations() -> None:
    box = Outbox()
    oid = box.draft(labels=LabelIntent(add=frozenset({"beadhive:merged"})))
    box.approve(oid)
    box.send(oid)
    box.apply(ob.record_receipt(box.doc, oid, C, Receipt("c1"), actor="sender"))
    assert box.doc.record(oid).status is RecordStatus.OPEN
    box.apply(ob.discard(box.doc, oid, reason="labels later", actor=OPERATOR, policy=ON))
    assert box.doc.record(oid).status is RecordStatus.PARTIAL
    assert box.state(oid, C) is OpState.SENT  # discard of the record left the sent comment


def test_the_ledger_is_the_outbox_view_own_message_recognition_reads() -> None:
    box = Outbox()
    sent = box.draft(revision="c1")
    box.approve(sent)
    box.send(sent)
    box.apply(ob.record_receipt(box.doc, sent, C, Receipt("c5", actor=BOT), actor="sender"))
    pending = box.draft(event=OutboxEvent.REPLY, revision="r", text="Thanks")
    box.to_unknown(pending)
    never = box.draft(event=OutboxEvent.REPLY, revision="r2", text="Hi")

    view = outbox_view(box.doc)
    assert isinstance(view, OutboxView)
    assert view.receipt_ids() == {"c5"} and view.known_actors() == {BOT}
    assert view.pending(pending) and not view.pending(sent) and not view.pending(never)
    assert view.issued(sent) and view.issued(pending) and not view.issued(never)


def test_agent_seat_prefixes_are_configurable() -> None:
    strict = OutboxPolicy(enabled=True, agent_seat_prefixes=(*AGENT_SEAT_PREFIXES, "plan/"))
    assert ob.is_operator_seat("alice", strict) and not ob.is_operator_seat("plan/x", strict)
    assert ob.is_operator_seat("plan/x", ON)
