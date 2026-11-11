"""The ``notification_behind`` finder (bh-c0x0i): a report closed with its completion comment
abandoned is listed with delivery, notified and the abandoned record id; a report told by hand (a
SENT completion close on any record) never is.
"""

from __future__ import annotations

import dataclasses

from beadhive.modules.intake.application import MilestoneService
from beadhive.modules.intake.application.end_states import (
    abandoned_milestone,
    notification_gap,
    told_by_a_sent_close,
)
from beadhive.modules.intake.domain import (
    LeakPolicy,
    MergeFact,
    MilestonePolicy,
    OpKind,
    OpState,
    OutboxPolicy,
    StorePosture,
    WorkDraft,
)
from beadhive.testing.intake import (
    SCOPE,
    FakeReleaseVerifier,
    InProcessIntakeLock,
    MemoryReportStore,
    RecordingSanitizer,
    SequentialIdSource,
    draft,
)

ACTOR = "merger/test"
SHA = "a" * 40


class Rig:
    def __init__(self) -> None:
        self.store = MemoryReportStore(SCOPE)
        self.service = MilestoneService(
            self.store,
            InProcessIntakeLock(),
            ids=SequentialIdSource(),
            leak_policy=LeakPolicy(bead_prefixes=frozenset({SCOPE.operational_prefix})),
            sanitizer=RecordingSanitizer(),
            posture=StorePosture(published=False),
            outbox=OutboxPolicy(enabled=True, close_enabled=True),
            milestones=MilestonePolicy(),
            verifier=FakeReleaseVerifier(tags={}, contained={}),
        )
        self.work = self.store.create_work(WorkDraft("fix", "bug", ACTOR))
        self.report = self.store.create(draft("crash", bound=True))
        self.store.track(self.report, self.work, actor=ACTOR)
        self.store.set_state(self.report, "delivery", "pending", reason="link", actor=ACTOR)
        self.service.on_work_merged(MergeFact(self.work, SHA, ACTOR))

    def set_ops(self, kind: OpKind, state: OpState) -> None:
        doc = self.store.read_outbox(self.report)
        (record,) = doc.records.values()
        ops = tuple(
            dataclasses.replace(o, state=state) if o.kind is kind else o for o in record.operations
        )
        records = {record.outbox_id: dataclasses.replace(record, operations=ops)}
        self.store.write_outbox(
            self.report,
            dataclasses.replace(doc, seq=doc.seq + 1, records=records),
            expect_seq=doc.seq,
            actor=ACTOR,
        )

    def view(self):
        view = self.store.get(self.report)
        assert view is not None
        return view

    def gap(self):
        return notification_gap(self.view(), self.store.read_outbox(self.report))


def test_an_abandoned_completion_comment_is_listed_with_its_record() -> None:
    rig = Rig()
    rig.set_ops(OpKind.COMMENT, OpState.ABANDONED)
    rig.service.complete(rig.report, ACTOR)
    assert rig.view().status == "closed"

    gap = rig.gap()

    (record,) = rig.store.read_outbox(rig.report).records.values()
    assert gap is not None
    assert (gap.report, gap.delivery, gap.notified, gap.record) == (
        rig.report,
        "merged",
        "",
        record.outbox_id,
    )


def test_a_report_told_by_a_sent_completion_close_is_not_listed() -> None:
    rig = Rig()
    rig.set_ops(OpKind.COMMENT, OpState.ABANDONED)
    assert rig.gap() is not None  # behind and nothing was sent
    rig.set_ops(OpKind.CLOSE, OpState.SENT)  # the close was sent after all
    assert told_by_a_sent_close(rig.store.read_outbox(rig.report))
    assert rig.gap() is None


def test_a_hand_told_report_ending_at_released_replied_is_not_listed() -> None:
    """The 0.35.9 shape: the operator replied by hand (notified:replied), delivery released, and
    the issue was closed by a backfilled close that was SENT. Behind by the label, told in fact."""
    rig = Rig()
    rig.store.set_state(rig.report, "delivery", "released", reason="shipped", actor=ACTOR)
    rig.store.set_state(rig.report, "notified", "replied", reason="reply sent", actor=ACTOR)
    rig.set_ops(OpKind.COMMENT, OpState.ABANDONED)  # the drafted completion record was dropped
    assert rig.gap() is not None  # by the labels alone the reporter is behind
    rig.set_ops(OpKind.CLOSE, OpState.SENT)  # but the backfilled close was sent
    rig.store.close(rig.report, reason="delivery complete", actor=ACTOR)
    assert rig.gap() is None


def test_a_live_milestone_comment_means_a_redraft_is_on_its_way() -> None:
    rig = Rig()
    assert abandoned_milestone(rig.store.read_outbox(rig.report)) is None
    assert rig.gap() is None  # a drafted comment, nothing abandoned


def test_a_report_with_no_delivery_or_already_notified_is_not_behind() -> None:
    rig = Rig()
    rig.set_ops(OpKind.COMMENT, OpState.ABANDONED)
    rig.store.set_state(rig.report, "notified", "merged", reason="told", actor=ACTOR)
    assert rig.gap() is None
