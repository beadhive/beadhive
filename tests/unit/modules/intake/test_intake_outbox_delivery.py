"""The ``delivery`` and ``notified`` rules beside the outbox: R14 cases X1, X2, X3 and X16 at the
domain level (``bh-6wj9y`` R2, R3, R9).

The milestone triggers themselves (the merge hook, ``bh work delivery release``, the
``set_state`` writes) are a later bead; these are the pure decisions they take, and the drafts
they make, with no writer anywhere (``write_requests() == 0``).
"""

from __future__ import annotations

import pytest

from beadhive.modules.intake.domain import (
    ApprovalRequest,
    Capabilities,
    CompletionPolicy,
    DraftRequest,
    IssueBinding,
    LabelBase,
    LeakPolicy,
    Message,
    OpKind,
    OpState,
    OutboxDocument,
    OutboxEvent,
    OutboxPolicy,
    PublicFacts,
    Receipt,
    RenderContext,
    Verdict,
    completion_event,
    delivery_after_merge,
    delivery_after_release,
    delivery_complete,
    notification_behind,
    notified_of,
)
from beadhive.modules.intake.domain import outbox as ob
from beadhive.testing.intake import RecordingSanitizer, SequentialIdSource
from beadhive.testing.tracker import MemoryWorld

POLICY = LeakPolicy(bead_prefixes=frozenset({"acme"}))
ON = OutboxPolicy(enabled=True)
ALL = Capabilities(frozenset({"comment.create", "comments.read"}))
C = OpKind.COMMENT


def binding(n: int) -> IssueBinding:
    url = f"https://tracker.example/acme/widgets/issues/{n}"
    return IssueBinding(f"acme-r{n}", "memory", "acme/widgets", url, f"#{n}")


def ctx(b: IssueBinding) -> RenderContext:
    return RenderContext(b.url, POLICY, RecordingSanitizer(), labels=LabelBase("r", frozenset()))


def milestone(
    doc: OutboxDocument, b: IssueBinding, event: OutboxEvent, revision: str, ids
) -> tuple[OutboxDocument, str]:
    request = DraftRequest(
        b, event, revision, "outbox", PublicFacts(tag="v1.2"), Message(event.value, "{tag}: done")
    )
    applied = ob.draft(doc, request, outbox_id=ids.outbox_id(), policy=ON, ctx=ctx(b))
    return applied.document, applied.outbox_id


def settle(doc, b, oid, how: str, ids) -> tuple[OutboxDocument, str | None]:
    if how == "abandoned":
        applied = ob.discard(doc, oid, reason="told elsewhere", actor="alice", policy=ON)
        return applied.document, applied.notified
    digest = ob.render(doc.record(oid), C, ctx(b)).short_digest
    doc = ob.approve(
        doc,
        ApprovalRequest(oid, C, digest, "alice"),
        grant_id=ids.grant_id(),
        policy=ON,
        ctx=ctx(b),
        capabilities=ALL,
    ).document
    doc = ob.begin_send(
        doc, oid, C, actor="alice", policy=ON, ctx=ctx(b), epoch=1, holder="h"
    ).document
    applied = ob.record_receipt(doc, oid, C, Receipt("c1"), actor="sender")
    return applied.document, applied.notified


# -- X1, X2: the merge milestone ---------------------------------------------------------------


def test_x1_merge_drafts_one_record_on_each_tracking_report_and_sends_nothing() -> None:
    world = MemoryWorld()  # the tracker side: nothing may reach it
    ids = SequentialIdSource()
    reports = {
        binding(1): ("open", ("delivery:pending",)),
        binding(2): ("open", ("delivery:reverted",)),
        binding(3): ("closed", ("delivery:pending", "intake:rejected")),  # a closed duplicate
    }
    docs = {}
    for b, (_status, labels) in reports.items():
        assert delivery_after_merge(labels) == "merged"
        docs[b], oid = milestone(OutboxDocument(), b, OutboxEvent.MERGED, "c9", ids)
        assert docs[b].record(oid).op(C).state is OpState.DRAFTED

    assert len(docs) == 3 and all(len(d.records) == 1 for d in docs.values())
    assert world.write_requests() == 0


def test_x2_a_re_run_merge_writes_no_record_and_no_set_state() -> None:
    ids = SequentialIdSource()
    b = binding(1)
    doc, oid = milestone(OutboxDocument(), b, OutboxEvent.MERGED, "c9", ids)
    again = ob.draft(
        doc,
        DraftRequest(b, OutboxEvent.MERGED, "c9", "outbox", message=Message("m", "x")),
        outbox_id=ids.outbox_id(),
        policy=ON,
        ctx=ctx(b),
    )
    assert again.document is doc and again.outbox_id == oid and not again.changed
    assert delivery_after_merge(("delivery:merged",)) is None
    assert delivery_after_merge(("delivery:released",)) is None
    assert delivery_after_merge(()) is None  # never linked: nothing to move


# -- X3: the release milestone -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("verdict", "after"), [(Verdict.YES, "released"), (Verdict.NO, None), (Verdict.UNKNOWN, None)]
)
def test_x3_only_a_yes_verdict_moves_merged_to_released(verdict, after) -> None:
    assert delivery_after_release(("delivery:merged",), verdict) == after
    assert delivery_after_release(("delivery:pending",), verdict) is None


# -- X16: notified and the close rule ----------------------------------------------------------


@pytest.mark.parametrize("policy", list(CompletionPolicy))
def test_x16_a_sent_completion_comment_notifies_and_closes(policy) -> None:
    ids = SequentialIdSource()
    b = binding(1)
    event = OutboxEvent(completion_event(policy))
    delivery = event.value
    doc, oid = milestone(OutboxDocument(), b, event, "rev", ids)
    assert not ob.close_rule(doc, delivery=delivery, completion=policy, outbox_enabled=True)

    doc, notified = settle(doc, b, oid, "sent", ids)

    assert notified == delivery
    assert ob.close_rule(doc, delivery=delivery, completion=policy, outbox_enabled=True)
    assert not notification_behind(delivery, notified)


@pytest.mark.parametrize("policy", list(CompletionPolicy))
def test_x16_an_abandoned_completion_comment_closes_with_notification_behind(policy) -> None:
    ids = SequentialIdSource()
    b = binding(1)
    event = OutboxEvent(completion_event(policy))
    doc, oid = milestone(OutboxDocument(), b, event, "rev", ids)

    doc, notified = settle(doc, b, oid, "abandoned", ids)

    assert notified is None  # notified never advances for abandoned or superseded
    assert ob.close_rule(doc, delivery=event.value, completion=policy, outbox_enabled=True)
    assert notification_behind(event.value, notified_of(()))


def test_x16_a_superseded_merged_record_settles_close_on_merge() -> None:
    ids = SequentialIdSource()
    b = binding(1)
    doc, merged = milestone(OutboxDocument(), b, OutboxEvent.MERGED, "c1", ids)
    doc, _ = milestone(doc, b, OutboxEvent.RELEASED, "v1@c1", ids)
    assert doc.record(merged).op(C).state is OpState.SUPERSEDED
    assert ob.close_rule(
        doc, delivery="released", completion=CompletionPolicy.CLOSE_ON_MERGE, outbox_enabled=True
    )


def test_close_rule_with_the_outbox_off_closes_at_the_milestone() -> None:
    empty = OutboxDocument()
    merge, release = CompletionPolicy.CLOSE_ON_MERGE, CompletionPolicy.CLOSE_ON_RELEASE
    assert ob.close_rule(empty, delivery="merged", completion=merge, outbox_enabled=False)
    assert not ob.close_rule(empty, delivery="merged", completion=release, outbox_enabled=False)
    assert ob.close_rule(empty, delivery="released", completion=release, outbox_enabled=False)
    assert not ob.close_rule(empty, delivery="pending", completion=merge, outbox_enabled=False)
    # With the outbox on and no completion record yet, the report waits.
    assert not ob.close_rule(empty, delivery="merged", completion=merge, outbox_enabled=True)


def test_notification_behind_and_completion() -> None:
    assert not notification_behind(None, None)
    assert not notification_behind("pending", None)
    assert notification_behind("merged", "replied")
    assert not notification_behind("merged", "released")  # a later milestone covers it
    assert notification_behind("released", "merged")
    assert notification_behind("reverted", "merged") and not notification_behind(
        "reverted", "corrected"
    )
    assert delivery_complete(CompletionPolicy.CLOSE_ON_MERGE, "released")
    assert not delivery_complete(CompletionPolicy.CLOSE_ON_RELEASE, "merged")
    assert notified_of(("notified:released", "x:y")) == "released"
    assert notified_of(("notified:bogus",)) is None
