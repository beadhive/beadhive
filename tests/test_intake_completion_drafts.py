"""Completion drafts the milestone reply WITH an issue close before a report closes (bh-cir2q).

``MilestoneService`` over the in-memory ``ReportStore``: a bound report whose work merges (or
releases) used to close locally with nothing drafted whenever ``draft_on_<event>`` was off (the
default), so its issue never heard. Now the completion milestone always drafts its record, with an
``issue.close`` (reason completed) when the tracker enables it, and the report closes locally only
once that record's comment is settled. No tracker, no clock: only drafts and local closes.
"""

from __future__ import annotations

import dataclasses

import pytest

from beadhive.modules.intake.application import MilestoneService
from beadhive.modules.intake.domain import (
    CompletionPolicy,
    LeakPolicy,
    MergeFact,
    MilestonePolicy,
    OpKind,
    OpState,
    OutboxPolicy,
    ReleaseFact,
    StorePosture,
    WorkDraft,
)
from beadhive.testing.intake import (
    SCOPE,
    FakeReleaseVerifier,
    InProcessIntakeLock,
    RecordingSanitizer,
    SequentialIdSource,
    draft,
)
from beadhive.testing.intake.memory_store import MemoryReportStore

ACTOR = "merger/test"
MERGE_SHA = "a" * 40
RELEASE_SHA = "b" * 40


class Rig:
    def __init__(
        self,
        *,
        completion: CompletionPolicy = CompletionPolicy.CLOSE_ON_MERGE,
        outbox: bool = True,
        close: bool = True,
        draft_on_merge: bool = False,
        draft_on_release: bool = False,
    ) -> None:
        self.store = MemoryReportStore(SCOPE)
        self.service = MilestoneService(
            self.store,
            InProcessIntakeLock(),
            ids=SequentialIdSource(),
            leak_policy=LeakPolicy(bead_prefixes=frozenset({SCOPE.operational_prefix})),
            sanitizer=RecordingSanitizer(),
            posture=StorePosture(published=False),
            outbox=OutboxPolicy(enabled=outbox, close_enabled=close),
            milestones=MilestonePolicy(
                draft_on_merge=draft_on_merge,
                draft_on_release=draft_on_release,
                completion=completion,
            ),
            verifier=FakeReleaseVerifier(
                tags={"v1.0.0": RELEASE_SHA}, contained={RELEASE_SHA: frozenset({MERGE_SHA})}
            ),
        )
        self.work = self.store.create_work(WorkDraft("fix the crash", "bug", ACTOR))

    def report(self, *, bound: bool = True) -> str:
        bead = self.store.create(draft("crash", bound=bound))
        self.store.track(bead, self.work, actor=ACTOR)
        if bound:
            self.store.set_state(bead, "delivery", "pending", reason="link", actor=ACTOR)
        return bead

    def merge(self) -> None:
        outcome = self.service.on_work_merged(MergeFact(self.work, MERGE_SHA, ACTOR))
        assert not outcome.errors

    def release(self) -> None:
        outcome = self.service.on_release(ReleaseFact("v1.0.0", RELEASE_SHA, ACTOR))
        assert not outcome.errors

    def status(self, report: str) -> str:
        view = self.store.get(report)
        assert view is not None
        return view.status

    def records(self, report: str, event: str):
        doc = self.store.read_outbox(report)
        return [] if doc is None else [r for r in doc.records.values() if r.event.value == event]

    def settle(self, report: str, event: str, state: OpState) -> None:
        """Move the ``event`` record's comment to ``state`` (what the sender does on a send)."""
        doc = self.store.read_outbox(report)
        assert doc is not None
        (record,) = self.records(report, event)
        ops = tuple(
            dataclasses.replace(o, state=state) if o.kind is OpKind.COMMENT else o
            for o in record.operations
        )
        records = {**doc.records, record.outbox_id: dataclasses.replace(record, operations=ops)}
        self.store.write_outbox(
            report,
            dataclasses.replace(doc, seq=doc.seq + 1, records=records),
            expect_seq=doc.seq,
            actor=ACTOR,
        )
        self.service.complete(report, ACTOR)


def assert_comment_and_close(rig: Rig, report: str, event: str) -> None:
    (record,) = rig.records(report, event)
    assert record.op(OpKind.COMMENT) is not None
    close = record.op(OpKind.CLOSE)
    assert close is not None and close.close.value == "completed"


def test_merge_completion_with_drafting_off_drafts_comment_and_close_and_waits() -> None:
    rig = Rig()
    report = rig.report()
    rig.merge()
    assert_comment_and_close(rig, report, "merged")
    assert rig.status(report) == "open"


@pytest.mark.parametrize("state", [OpState.SENT, OpState.ABANDONED, OpState.SUPERSEDED])
def test_the_report_closes_once_the_completion_comment_settles(state: OpState) -> None:
    rig = Rig()
    report = rig.report()
    rig.merge()
    rig.settle(report, "merged", state)
    assert rig.status(report) == "closed"


def test_an_unbound_report_is_left_alone_and_drafts_nothing() -> None:
    rig = Rig()
    report = rig.report(bound=False)
    rig.merge()
    assert rig.store.read_outbox(report) is None


def test_the_outbox_off_closes_at_once() -> None:
    rig = Rig(outbox=False)
    report = rig.report()
    rig.merge()
    assert rig.status(report) == "closed" and not rig.records(report, "merged")


def test_close_disabled_drafts_the_comment_only_and_still_waits() -> None:
    rig = Rig(close=False)
    report = rig.report()
    rig.merge()
    (record,) = rig.records(report, "merged")
    assert record.op(OpKind.CLOSE) is None and rig.status(report) == "open"


def test_a_rerun_merge_drafts_nothing_new() -> None:
    rig = Rig()
    report = rig.report()
    rig.merge()
    writes = list(rig.store.outbox_writes)
    rig.merge()
    assert len(rig.records(report, "merged")) == 1 and rig.store.outbox_writes == writes


def test_release_completion_drafts_comment_and_close_after_a_merge() -> None:
    rig = Rig(completion=CompletionPolicy.CLOSE_ON_RELEASE)
    report = rig.report()
    rig.merge()
    assert rig.status(report) == "open" and not rig.records(report, "merged")
    rig.release()
    assert_comment_and_close(rig, report, "released")
    assert rig.status(report) == "open"
    rig.settle(report, "released", OpState.SENT)
    assert rig.status(report) == "closed"


def test_a_rerun_release_drafts_nothing_new() -> None:
    rig = Rig(completion=CompletionPolicy.CLOSE_ON_RELEASE)
    report = rig.report()
    rig.merge()
    rig.release()
    rig.release()
    assert len(rig.records(report, "released")) == 1


def test_the_released_record_inherits_the_close_of_the_unsent_merged_record() -> None:
    rig = Rig(draft_on_release=True)
    report = rig.report()
    rig.merge()
    rig.release()
    assert_comment_and_close(rig, report, "released")
    # the superseded merged record settles close-on-merge (X16); the close lives on the released one
    assert rig.status(report) == "closed"


def test_a_completion_record_is_drafted_for_an_open_report_that_completed_without_one() -> None:
    rig = Rig()
    report = rig.report()
    rig.store.set_state(report, "delivery", "merged", reason="before the fix", actor=ACTOR)
    rig.merge()
    assert_comment_and_close(rig, report, "merged")
    assert rig.status(report) == "open"
