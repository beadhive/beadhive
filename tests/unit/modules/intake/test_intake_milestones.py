"""The merge and release milestones (I10): they DRAFT outbox records and never send.

``MilestoneService`` over ``MemoryReportStore``, the never-waiting ``InProcessIntakeLock``,
``SequentialIdSource``, ``RecordingSanitizer`` and ``FakeReleaseVerifier``; the sender and
``MemoryTracker`` / ``MemoryWorld`` join only where the close rule needs a record to settle. These
are the trigger cases the pure-domain bead deferred (``bh-6wj9y`` R14): X1 (merge on work tracked
by open and closed reports), X2 (a re-run), X3 (the verifier answering YES, NO and UNKNOWN), X4/X5
(a released draft over an unsent or possibly sent merged one), X16 (notified and the close rule,
``sent`` then ``abandoned``) and X18 (intake off). Ordering is asserted on ``seq``, store write
counters and ``write_requests()`` only: no clock, no sleep, no socket.
"""

from __future__ import annotations

import dataclasses
import inspect

import pytest

from beadhive.modules.intake.application import (
    IntakeService,
    MilestoneService,
    NullIntake,
    OutboxService,
)
from beadhive.modules.intake.application.threads import binding_key
from beadhive.modules.intake.domain import (
    CompletionPolicy,
    IntakeRefused,
    IssueBinding,
    IssueStatus,
    LeakPolicy,
    MergeFact,
    MilestonePolicy,
    OpKind,
    OpState,
    OutboxApprove,
    OutboxEvent,
    OutboxPolicy,
    OwnIdentity,
    RefusalReason,
    ReleaseFact,
    ReplyDraft,
    ReportDraft,
    SourceSnapshot,
    StorePosture,
    UnlinkedFact,
    Verdict,
    WorkDraft,
    notification_behind,
    notified_of,
)
from beadhive.modules.tracker.contracts import READ_OPERATIONS, WRITE_OPERATIONS, OwnMarker
from beadhive.testing.intake import (
    SCOPE,
    FakeReleaseVerifier,
    InProcessIntakeLock,
    MemoryReportStore,
    RecordingSanitizer,
    SequentialIdSource,
)
from beadhive.testing.tracker import MemoryTracker, MemoryWorld, at

LEAKS = LeakPolicy(bead_prefixes=frozenset({"acme", "pub"}))
ON = OutboxPolicy(enabled=True)
DRAFT_BOTH = MilestonePolicy(draft_on_merge=True, draft_on_release=True)
PRIVATE = StorePosture(published=False)
C = OpKind.COMMENT
MERGE_SHA = "a" * 40
OTHER_SHA = "b" * 40
THIRD_SHA = "c" * 40
RELEASE_SHA = "d" * 40
ACTOR = "merger/test"
ME = OwnIdentity("acme/widgets", "inst-1")


def marker_line(marker: OwnMarker) -> str:
    return f"<!-- bh:v1 hive={marker.hive} install={marker.install} outbox={marker.outbox_id} -->"


class _NoLabels:
    def violations(self, labels) -> tuple[str, ...]:
        return ()


class Scene:
    """One work bead, the reports tracking it, and the milestone service wired as the root wires
    it, with the sender (and a memory tracker) available for the cases that settle a record."""

    def __init__(
        self,
        *,
        outbox: OutboxPolicy = ON,
        milestones: MilestonePolicy = DRAFT_BOTH,
        posture: StorePosture = PRIVATE,
        verifier: FakeReleaseVerifier | None = None,
        sanitizer="default",
    ) -> None:
        self.world = MemoryWorld()
        self.world.set_write_time(at(2))
        self.tracker = MemoryTracker(
            self.world, enabled=frozenset(READ_OPERATIONS | WRITE_OPERATIONS)
        )
        self.store = MemoryReportStore(SCOPE)
        self.lock = InProcessIntakeLock()
        self.ids = SequentialIdSource()
        self.verifier = verifier if verifier is not None else FakeReleaseVerifier()
        self.sanitizer = RecordingSanitizer.of(["s3cr3t"]) if sanitizer == "default" else sanitizer
        self.outbox_policy = outbox
        self.service = MilestoneService(
            self.store,
            self.lock,
            ids=self.ids,
            leak_policy=LEAKS,
            sanitizer=self.sanitizer,
            posture=posture,
            outbox=outbox,
            milestones=milestones,
            verifier=self.verifier,
        )
        self.sender = OutboxService(
            self.store,
            self.tracker,
            self.lock,
            ids=self.ids,
            identity=ME,
            marker_line=marker_line,
            policy=ON,
            leak_policy=LEAKS,
            sanitizer=self.sanitizer,
            posture=posture,
            completion=self.service.complete,
        )
        self.intake = IntakeService(
            self.store, _NoLabels(), outbox=self.sender, milestones=self.service
        )
        self.work = self.store.create_work(WorkDraft("fix the login crash", "bug", "dev/t"))

    # -- fixtures ----------------------------------------------------------------------------

    def report(self, *, linked: bool = True, status: str = "", work: str = "") -> str:
        issue = self.world.add_issue("a bug", at=at(1))
        report = self.store.create(
            ReportDraft(
                "a bug",
                "bug",
                "origin:github",
                "dev/import",
                binding=IssueBinding("", "memory", self.world.locator, issue.url, issue.identifier),
                snapshot=SourceSnapshot("a bug", "", IssueStatus.OPEN, "r1"),
            )
        )
        if linked:
            self.store.track(report, work or self.work, actor="dev/t")
            self.store.set_state(report, "delivery", "pending", reason="link", actor="dev/t")
        if status:
            self.store.mark(report, status=status)
        return report

    def merge(self, commit: str = MERGE_SHA):
        return self.intake.on_work_merged(MergeFact(self.work, commit, ACTOR))

    def release(self, tag: str = "v1.0.0", commit: str = RELEASE_SHA):
        return self.intake.on_release(ReleaseFact(tag, commit, ACTOR))

    # -- reads -------------------------------------------------------------------------------

    def labels(self, report: str) -> tuple[str, ...]:
        view = self.store.get(report)
        assert view is not None
        return view.labels

    def status(self, report: str) -> str:
        view = self.store.get(report)
        assert view is not None
        return view.status

    def doc(self, report: str):
        doc = self.store.read_outbox(report)
        assert doc is not None
        return doc

    def records(self, report: str, event: OutboxEvent | None = None):
        doc = self.store.read_outbox(report)
        found = list((doc.records if doc else {}).values())
        return [r for r in found if event is None or r.event is event]

    def state(self, report: str, event: OutboxEvent) -> OpState:
        (record,) = self.records(report, event)
        op = record.op(C)
        assert op is not None
        return op.state

    def requests(self) -> tuple[int, int]:
        return self.world.read_requests(), self.world.write_requests()


def verifier(*merge_commits: str) -> FakeReleaseVerifier:
    """A verifier whose ``v1.0.0`` names :data:`RELEASE_SHA`, which contains ``merge_commits``."""
    return FakeReleaseVerifier(
        tags={"v1.0.0": RELEASE_SHA}, contained={RELEASE_SHA: frozenset(merge_commits)}
    )


# -- nothing here can send ---------------------------------------------------------------------


def test_the_milestone_service_has_no_tracker_grant_or_writer_to_send_with() -> None:
    parameters = set(inspect.signature(MilestoneService.__init__).parameters)
    assert not {"tracker", "writer", "grant", "grants", "marker_line", "identity"} & parameters
    source = inspect.getsource(MilestoneService)
    for send_word in ("post_comment", "change_labels", "WriteGrant", "begin_send", "approve("):
        assert send_word not in source


# -- X1: the merge trigger ---------------------------------------------------------------------


def test_x1_a_merge_moves_every_tracking_report_open_or_closed_and_drafts_each_a_record() -> None:
    s = Scene(milestones=MilestonePolicy(draft_on_merge=True, draft_on_release=True))
    first, second = s.report(), s.report()
    duplicate = s.report(status="closed")  # closed early, still tracks the work

    outcome = s.merge()

    assert outcome.reports == (first, second, duplicate) and not outcome.errors
    for report in (first, second, duplicate):
        assert "delivery:merged" in s.labels(report) and "delivery:pending" not in s.labels(report)
        assert s.doc(report).facts.merged.work == s.work
        assert s.doc(report).facts.merged.commit == MERGE_SHA
        assert s.state(report, OutboxEvent.MERGED) is OpState.DRAFTED
    assert [i.drafted != "" for i in outcome.items] == [True, True, True]
    assert s.requests() == (0, 0)  # nothing read or written on a tracker, ever


def test_a_milestone_message_carries_no_bead_id_branch_or_internal_content() -> None:
    s = Scene()
    report = s.report()
    s.merge()
    (record,) = s.records(report, OutboxEvent.MERGED)

    rendered = s.intake.render(record.outbox_id).of(C)
    assert rendered is not None and not rendered.findings
    assert s.work not in rendered.payload and report not in rendered.payload
    assert "acme-" not in rendered.payload and "wt/bead" not in rendered.payload
    assert MERGE_SHA not in rendered.payload and ACTOR not in rendered.payload
    assert record.facts.tag == "" and record.message.template == "milestone.merged"


def test_x2_a_re_run_drafts_nothing_and_writes_nothing_more() -> None:
    s = Scene()
    report = s.report()
    s.merge()
    writes, outbox_writes = s.store.writes, list(s.store.outbox_writes)
    ids_used = s.ids.start  # an id is minted only to be discarded by O2; no record results

    again = s.merge()

    assert len(s.records(report)) == 1
    assert s.store.writes == writes and s.store.outbox_writes == outbox_writes
    assert again.items[0].delivery == "" and again.items[0].drafted == ""
    assert "already delivery:merged" in again.items[0].note
    assert ids_used == s.ids.start


def test_a_merge_with_no_tracking_report_does_nothing_at_all() -> None:
    s = Scene()
    other = s.store.create_work(WorkDraft("elsewhere", "bug", "dev/t"))
    writes = s.store.writes

    outcome = s.intake.on_work_merged(MergeFact(other, MERGE_SHA, ACTOR))

    assert outcome.items == () and s.store.writes == writes


def test_the_outbox_off_records_the_fact_and_the_dimension_but_drafts_no_record() -> None:
    s = Scene(outbox=OutboxPolicy())
    report = s.report()

    outcome = s.merge()

    assert outcome.items[0].delivery == "merged" and outcome.items[0].drafted == ""
    assert "delivery:merged" in s.labels(report)
    assert s.doc(report).facts.merged.commit == MERGE_SHA and s.records(report) == []


def test_each_milestone_drafts_only_when_its_own_switch_is_on() -> None:
    s = Scene(milestones=MilestonePolicy(draft_on_release=True))  # merge drafting off
    report = s.report()

    outcome = s.merge()

    assert outcome.items[0].delivery == "merged" and s.records(report) == []


def test_a_draft_refused_for_a_published_store_still_moves_the_dimension_and_is_reported() -> None:
    s = Scene(posture=StorePosture(published=True))
    first, second = s.report(), s.report()

    outcome = s.merge()

    assert [i.delivery for i in outcome.items] == ["merged", "merged"]
    assert all("no merged record drafted" in i.error for i in outcome.items)
    assert s.records(first) == [] and s.records(second) == []
    assert s.doc(first).facts.merged.commit == MERGE_SHA


def test_one_report_failing_never_hides_the_others() -> None:
    s = Scene()
    first, second = s.report(), s.report()
    s.store.fail_next_outbox_write()  # the first report's document write fails

    outcome = s.merge()

    assert outcome.items[0].error and outcome.items[0].delivery == ""
    assert "delivery:pending" in s.labels(first)  # nothing half-written: still retryable
    assert outcome.items[1].delivery == "merged" and "delivery:merged" in s.labels(second)

    retry = s.merge()  # the re-run finishes it
    assert [i.delivery for i in retry.items] == ["merged", ""]
    assert "delivery:merged" in s.labels(first) and len(s.records(first)) == 1


def test_a_held_binding_is_reported_not_raised() -> None:
    s = Scene()
    report = s.report()
    view = s.store.get(report)
    assert view is not None and view.binding is not None
    with s.lock.hold(binding_key(view.binding)):  # another intake operation is running
        outcome = s.merge()

    assert outcome.items[0].error and "held by another" in outcome.items[0].error
    assert "delivery:pending" in s.labels(report)


def test_a_report_with_no_delivery_state_is_left_alone() -> None:
    s = Scene()
    report = s.report(linked=False)
    s.store.track(report, s.work, actor="dev/t")  # an edge but no `delivery:pending`

    outcome = s.merge()

    assert outcome.items[0].delivery == "" and "no delivery state" in outcome.items[0].note
    assert not any(label.startswith("delivery:") for label in s.labels(report))


def test_a_reverted_report_goes_back_to_merged_on_the_next_merge() -> None:
    s = Scene()
    report = s.report()
    s.store.set_state(report, "delivery", "reverted", reason="operator", actor="alice")

    s.merge(OTHER_SHA)

    assert "delivery:merged" in s.labels(report)
    assert s.doc(report).facts.merged.commit == OTHER_SHA


# -- the completion policy ---------------------------------------------------------------------


def test_merged_but_unreleased_keeps_the_report_open_and_held_under_close_on_release() -> None:
    s = Scene(
        outbox=OutboxPolicy(),
        milestones=MilestonePolicy(completion=CompletionPolicy.CLOSE_ON_RELEASE),
    )
    report = s.report()

    outcome = s.merge()

    assert outcome.items[0].delivery == "merged" and not outcome.items[0].closed
    assert s.status(report) == "open" and "delivery:merged" in s.labels(report)
    assert report not in s.store.ready_ids()  # held for life: never ready work


def test_close_on_merge_with_the_outbox_off_closes_the_report_at_the_merge_with_no_ready_window():
    s = Scene(outbox=OutboxPolicy())  # the default completion policy is close-on-merge
    open_one = s.report()
    closed_one = s.report(status="closed")

    outcome = s.merge()

    assert outcome.items[0].closed and not outcome.items[1].closed  # already closed
    assert s.status(open_one) == "closed" and s.status(closed_one) == "closed"
    assert "delivery:merged" in s.labels(closed_one)  # the closed duplicate still hears of it
    assert open_one not in s.store.ready_ids()
    assert ("close", open_one) in s.store.events()


def test_close_on_merge_with_the_outbox_on_waits_for_the_merged_record_to_settle() -> None:
    s = Scene()
    report = s.report()

    outcome = s.merge()

    assert not outcome.items[0].closed and s.status(report) == "open"  # drafted, not yet told
    assert report not in s.store.ready_ids()


def test_an_outbox_that_will_never_draft_the_completion_milestone_does_not_strand_the_report():
    s = Scene(milestones=MilestonePolicy(draft_on_release=True))  # merge drafting off
    report = s.report()

    outcome = s.merge()

    assert outcome.items[0].closed and s.status(report) == "closed"  # no record can ever come


def test_an_untriaged_or_unlinked_report_is_never_closed_by_a_milestone() -> None:
    s = Scene(outbox=OutboxPolicy())
    untriaged = s.report()
    s.store.set_state(untriaged, "intake", "untriaged", reason="import", actor="dev/t")

    outcome = s.merge()

    assert outcome.items[0].delivery == "merged" and not outcome.items[0].closed
    assert s.status(untriaged) == "open"


def test_a_report_with_a_live_blocker_besides_its_hold_is_reported_not_closed() -> None:
    s = Scene(outbox=OutboxPolicy())
    report = s.report()
    s.store.add_blocker(report, "acme-9999")

    outcome = s.merge()

    assert outcome.items[0].delivery == "merged" and "not closed" in outcome.items[0].error
    assert "blocked" in outcome.items[0].error
    assert s.status(report) == "open" and "delivery:merged" in s.labels(report)


# -- X3: the release trigger -------------------------------------------------------------------


def test_x3_only_a_yes_verdict_moves_to_released_and_no_and_unknown_are_reported_unchanged():
    s = Scene(
        verifier=FakeReleaseVerifier(
            tags={"v1.0.0": RELEASE_SHA},
            contained={RELEASE_SHA: frozenset({MERGE_SHA})},
            unknown=frozenset({THIRD_SHA}),
        )
    )
    works = [s.store.create_work(WorkDraft(f"w{n}", "bug", "dev/t")) for n in range(3)]
    yes, no, unknown = (s.report(work=w) for w in works)
    for work, commit in zip(works, (MERGE_SHA, OTHER_SHA, THIRD_SHA), strict=True):
        s.intake.on_work_merged(MergeFact(work, commit, ACTOR))
    writes_before = s.store.writes

    outcome = s.release()

    by_report = {i.report: i for i in outcome.items}
    assert by_report[yes].verdict is Verdict.YES and by_report[yes].delivery == "released"
    assert by_report[no].verdict is Verdict.NO and by_report[no].delivery == ""
    assert by_report[unknown].verdict is Verdict.UNKNOWN and by_report[unknown].delivery == ""
    assert "not verified" in by_report[no].note and "not verified" in by_report[unknown].note
    assert "delivery:released" in s.labels(yes)
    assert "delivery:merged" in s.labels(no) and "delivery:merged" in s.labels(unknown)
    assert s.doc(yes).facts.released.tag == "v1.0.0" and s.doc(no).facts.released is None
    assert s.state(yes, OutboxEvent.RELEASED) is OpState.DRAFTED
    assert (
        s.records(no, OutboxEvent.RELEASED) == [] and s.records(unknown, OutboxEvent.RELEASED) == []
    )
    assert s.store.writes > writes_before
    assert s.requests() == (0, 0)
    # one question per distinct merge commit, only about commits the reports recorded
    assert sorted(s.verifier.asked) == sorted(
        (RELEASE_SHA, c) for c in (MERGE_SHA, OTHER_SHA, THIRD_SHA)
    )


def test_a_report_with_no_recorded_merge_commit_is_not_verified() -> None:
    s = Scene(verifier=verifier(MERGE_SHA))
    report = s.report()
    s.store.set_state(report, "delivery", "merged", reason="by hand", actor="alice")  # no fact

    outcome = s.release()

    assert outcome.items[0].verdict is Verdict.UNKNOWN and outcome.items[0].delivery == ""
    assert "delivery:merged" in s.labels(report) and s.verifier.asked == []


def test_a_release_re_run_changes_nothing_and_drafts_nothing_more() -> None:
    s = Scene(verifier=verifier(MERGE_SHA))
    report = s.report()
    s.merge()
    s.release()
    writes = s.store.writes

    # the superseded merged draft settled the close-on-merge rule, so the report closed at release
    assert s.status(report) == "closed"

    again = s.release()  # a closed, released report is no candidate any more

    assert again.items == () and len(s.records(report, OutboxEvent.RELEASED)) == 1
    assert s.store.writes == writes


def test_x4_a_released_draft_supersedes_an_unsent_merged_draft() -> None:
    s = Scene(verifier=verifier(MERGE_SHA))
    report = s.report()
    s.merge()
    (merged,) = s.records(report, OutboxEvent.MERGED)

    s.release()

    assert s.state(report, OutboxEvent.MERGED) is OpState.SUPERSEDED
    (released,) = s.records(report, OutboxEvent.RELEASED)
    assert released.supersedes == merged.outbox_id and released.group == merged.group
    assert released.facts.tag == "v1.0.0"
    rendered = s.intake.render(released.outbox_id).of(C)
    assert rendered is not None and "v1.0.0" in rendered.payload and not rendered.findings


def test_x5_a_released_draft_leaves_a_possibly_sent_merged_message_alone() -> None:
    s = Scene(verifier=verifier(MERGE_SHA))
    report = s.report()
    s.merge()
    (merged,) = s.records(report, OutboxEvent.MERGED)
    digest = s.intake.render(merged.outbox_id).of(C).short_digest
    s.intake.approve(OutboxApprove(merged.outbox_id, digest, "alice"))
    s.world.fail_next_write_ambiguously()
    assert s.intake.send(merged.outbox_id, actor="alice").state is OpState.UNKNOWN

    s.release()

    assert s.state(report, OutboxEvent.MERGED) is OpState.UNKNOWN  # untouched
    (released,) = s.records(report, OutboxEvent.RELEASED)
    assert released.operations[0].state is OpState.DRAFTED


def test_a_closed_report_is_released_without_a_record_by_default() -> None:
    s = Scene(outbox=OutboxPolicy(), verifier=verifier(MERGE_SHA))
    report = s.report()
    s.merge()  # close-on-merge with the outbox off closes it
    assert s.status(report) == "closed"

    outcome = s.release()

    assert outcome.items[0].delivery == "released" and "delivery:released" in s.labels(report)
    assert s.records(report) == [] and s.doc(report).facts.released.tag == "v1.0.0"
    assert notification_behind("released", notified_of(s.labels(report)))  # told nothing: visible


def test_append_release_to_closed_drafts_for_a_report_that_closed_at_merge() -> None:
    s = Scene(
        outbox=ON,
        milestones=MilestonePolicy(
            draft_on_merge=False, draft_on_release=True, append_release_to_closed=True
        ),
        verifier=verifier(MERGE_SHA),
    )
    report = s.report()
    s.merge()  # merge drafting is off, so close-on-merge completes and closes the report
    assert s.status(report) == "closed"

    s.release()

    assert "delivery:released" in s.labels(report)
    assert s.state(report, OutboxEvent.RELEASED) is OpState.DRAFTED


def test_a_release_without_a_verifier_refuses_instead_of_assuming() -> None:
    s = Scene()
    bare = MilestoneService(
        s.store,
        s.lock,
        ids=s.ids,
        leak_policy=LEAKS,
        sanitizer=s.sanitizer,
        posture=PRIVATE,
        outbox=ON,
        milestones=DRAFT_BOTH,
    )
    with pytest.raises(IntakeRefused) as refused:
        bare.on_release(ReleaseFact("v1", RELEASE_SHA, ACTOR))
    assert refused.value.reason is RefusalReason.INTAKE_DISABLED


def test_a_tag_shaped_like_a_bead_id_is_held_by_the_leak_check_never_published() -> None:
    s = Scene(verifier=verifier(MERGE_SHA))
    report = s.report()
    s.merge()

    s.intake.on_release(ReleaseFact("acme-1234", RELEASE_SHA, ACTOR))

    assert s.state(report, OutboxEvent.RELEASED) is OpState.HELD
    (record,) = s.records(report, OutboxEvent.RELEASED)
    assert any(f.kind.value == "bead_id" for f in record.operations[0].findings)


def test_an_unlinked_report_is_not_released_from_work_it_no_longer_tracks() -> None:
    s = Scene(verifier=verifier(MERGE_SHA))
    report = s.report()
    s.merge()
    doc = s.doc(report)
    s.store.write_outbox(
        report,
        dataclasses.replace(
            doc, seq=doc.seq + 1, facts=dataclasses.replace(doc.facts, unlinked=UnlinkedFact("x"))
        ),
        expect_seq=doc.seq,
        actor="dev/t",
    )

    outcome = s.release()

    assert outcome.items[0].delivery == "" and "unlinked" in outcome.items[0].note
    assert "delivery:merged" in s.labels(report)


# -- X16: notified and the close rule ----------------------------------------------------------


def _settle_by_sending(s: Scene, record) -> None:
    digest = s.intake.render(record.outbox_id).of(C).short_digest
    s.intake.approve(OutboxApprove(record.outbox_id, digest, "alice"))
    assert s.intake.send(record.outbox_id, actor="alice").state is OpState.SENT


def test_x16_the_released_comment_sent_notifies_and_completes_the_report() -> None:
    s = Scene(
        milestones=MilestonePolicy(
            draft_on_merge=False,
            draft_on_release=True,
            completion=CompletionPolicy.CLOSE_ON_RELEASE,
        ),
        verifier=verifier(MERGE_SHA),
    )
    report = s.report()
    s.merge()
    assert s.status(report) == "open"  # merged but unreleased: open, held, delivery:merged
    s.release()
    assert s.status(report) == "open" and "delivery:released" in s.labels(report)
    (released,) = s.records(report, OutboxEvent.RELEASED)

    _settle_by_sending(s, released)

    assert "notified:released" in s.labels(report) and s.status(report) == "closed"
    assert not notification_behind("released", notified_of(s.labels(report)))
    assert s.world.write_requests() == 1  # the operator's one approved send, nothing else


def test_x16_the_released_comment_abandoned_still_closes_with_notification_behind() -> None:
    s = Scene(
        milestones=MilestonePolicy(
            draft_on_merge=False,
            draft_on_release=True,
            completion=CompletionPolicy.CLOSE_ON_RELEASE,
        ),
        verifier=verifier(MERGE_SHA),
    )
    report = s.report()
    s.merge()
    s.release()
    (released,) = s.records(report, OutboxEvent.RELEASED)

    result = s.intake.discard(released.outbox_id, reason="told them by hand", actor="alice")

    assert result.state is OpState.ABANDONED and "closed" in result.detail
    assert s.status(report) == "closed" and notified_of(s.labels(report)) is None
    assert notification_behind("released", notified_of(s.labels(report)))  # visible, not hidden
    assert s.world.write_requests() == 0


def test_the_merged_comment_sent_completes_a_close_on_merge_report() -> None:
    s = Scene(milestones=MilestonePolicy(draft_on_merge=True))
    report = s.report()
    s.merge()
    assert s.status(report) == "open"
    (merged,) = s.records(report, OutboxEvent.MERGED)

    _settle_by_sending(s, merged)

    assert "notified:merged" in s.labels(report) and s.status(report) == "closed"


def test_a_reply_settling_never_runs_the_completion_policy() -> None:
    s = Scene(milestones=MilestonePolicy(draft_on_merge=True))
    report = s.report()
    s.merge()
    reply = s.intake.draft_reply(ReplyDraft(report, "Thanks, looking into it.", "alice"))
    result = s.intake.discard(reply.outbox_id, reason="no", actor="alice")

    assert result.state is OpState.ABANDONED and s.status(report) == "open"


# -- X18: intake or the outbox off -------------------------------------------------------------


def test_x18_with_intake_off_both_triggers_are_free_and_change_nothing() -> None:
    null = NullIntake()

    merged = null.on_work_merged(MergeFact("w", MERGE_SHA, ACTOR))
    released = null.on_release(ReleaseFact("v1", RELEASE_SHA, ACTOR))

    assert merged.items == () and released.items == ()


def test_a_service_without_the_milestone_triggers_bound_refuses_rather_than_pretending() -> None:
    s = Scene()
    unwired = IntakeService(s.store, _NoLabels())
    with pytest.raises(IntakeRefused) as refused:
        unwired.on_work_merged(MergeFact(s.work, MERGE_SHA, ACTOR))
    assert refused.value.reason is RefusalReason.INTAKE_DISABLED
    with pytest.raises(IntakeRefused):
        unwired.on_release(ReleaseFact("v1", RELEASE_SHA, ACTOR))


def test_a_store_failure_reading_the_document_is_reported_per_report() -> None:
    s = Scene()
    report = s.report()
    s.store.put_metadata(report, "intake.outbox", {"schema": 99})  # a newer document: an error

    outcome = s.merge()

    assert outcome.items[0].error
    assert "delivery:pending" in s.labels(report)
