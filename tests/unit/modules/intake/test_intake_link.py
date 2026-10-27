"""Many reports to one work bead (I5): ``IntakeService`` link, unlink, ``accept --work`` /
``--new-work`` and the derived work progress, over ``MemoryReportStore``.

Pins the acceptance of ``bh-v2rr6``: two reports link to one work bead and unlinking one leaves
the other intact; the report stays held and never becomes ready work while the work bead is
dispatchable; D-a's refusal and ``delivery:pending`` at link; no edge crosses stores. No bd, no
clock, no sleep: explicit ids, an in-process never-waiting lock.
"""

from __future__ import annotations

import pytest

from beadhive.modules.intake import (
    Disposition,
    DispositionRequest,
    IntakeRefused,
    IntakeService,
    IntakeStoreError,
    LinkKind,
    LinkRequest,
    NullIntake,
    RefusalReason,
    ReportDraft,
    WorkProgress,
)
from beadhive.modules.intake.domain import (
    DeliveryFacts,
    OutboxDocument,
    UnlinkedFact,
    delivery_at_link,
    outbox_from_document,
    outbox_to_document,
    work_progress_of,
)
from beadhive.testing.intake import (
    InProcessIntakeLock,
    MemoryReportStore,
    draft,
    memory_store,
)
from beadhive.testing.intake.cases import outbox

ACTOR = "dev/triage"
BINDING_KEY = "memory:acme/widgets"


class _NoLabels:
    def violations(self, labels):
        return ()


class Rig:
    def __init__(self) -> None:
        self.store: MemoryReportStore = memory_store()
        self.lock = InProcessIntakeLock()
        self.service = IntakeService(self.store, _NoLabels(), lock=self.lock)

    def report(self, title: str = "login is broken") -> str:
        """An imported report: bound, held, untriaged."""
        bead = self.store.create(draft(title, bound=True))
        self.store.set_state(bead, "origin", "github", reason="imported", actor=ACTOR)
        self.store.set_state(bead, "intake", "untriaged", reason="imported", actor=ACTOR)
        return bead

    def work(self, title: str = "fix the login crash") -> str:
        return self.store.create(draft(title))

    def link(self, report: str, work: str, **kwargs):
        return self.service.link(LinkRequest(report, work, ACTOR, **kwargs))

    def labels(self, bead: str) -> set[str]:
        view = self.store.get(bead)
        assert view is not None
        return set(view.labels)

    def delivery(self, bead: str) -> str:
        return next((x for x in self.labels(bead) if x.startswith("delivery:")), "")

    def facts(self, bead: str) -> DeliveryFacts:
        doc = self.store.read_outbox(bead)
        return doc.facts if doc is not None else DeliveryFacts()


def draft_for(title: str) -> ReportDraft:
    return ReportDraft(title, "bug", "origin:report", ACTOR)


@pytest.fixture
def rig() -> Rig:
    return Rig()


# -- acceptance: many reports to one work bead ------------------------------------------------


def test_two_reports_link_to_one_work_and_unlinking_one_leaves_the_other_intact(rig) -> None:
    first, second, work = rig.report("first"), rig.report("second"), rig.work()

    assert rig.link(first, work).kind is LinkKind.LINKED
    assert rig.link(second, work).kind is LinkKind.LINKED
    assert rig.store.trackers_of(work) == tuple(sorted((first, second)))

    outcome = rig.service.unlink(first, actor=ACTOR)

    assert (outcome.kind, outcome.work) == (LinkKind.UNLINKED, work)
    assert rig.store.trackers_of(work) == (second,)
    assert rig.store.tracked_work(second) == work and rig.store.tracked_work(first) is None
    # the other report is untouched: still pending, still held, no fact about it
    assert rig.delivery(second) == "delivery:pending"
    assert rig.facts(second).unlinked is None
    assert [h.blocks for h in rig.store.holds() if second in h.blocks] != []
    assert [b.report for b in rig.service.reports_for_work(work)] == [second]


def test_the_report_stays_held_and_never_ready_while_the_work_is_dispatchable(rig) -> None:
    report, work = rig.report(), rig.work()
    assert report not in rig.store.ready_ids() and work in rig.store.ready_ids()

    rig.link(report, work)

    assert report not in rig.store.ready_ids()  # held for life; a tracks edge alone gates nothing
    assert work in rig.store.ready_ids()  # a link never makes the work wait on its reports
    assert len(rig.store.holds()) == 1
    assert "intake:untriaged" in rig.labels(report)  # linking changes no triage state


def test_a_link_writes_delivery_pending(rig) -> None:
    report, work = rig.report(), rig.work()
    assert rig.delivery(report) == ""

    outcome = rig.link(report, work)

    assert rig.delivery(report) == "delivery:pending" and outcome.delivery == "pending"
    assert rig.store.history(report)[-1] == ("delivery", "pending", f"linked to {work}")


def test_linking_the_same_work_again_is_a_no_op(rig) -> None:
    report, work = rig.report(), rig.work()
    rig.link(report, work)
    writes = rig.store.writes

    outcome = rig.link(report, work)

    assert outcome.kind is LinkKind.ALREADY_LINKED and rig.store.writes == writes


def test_a_repeat_link_repairs_a_missing_delivery_value(rig) -> None:
    report, work = rig.report(), rig.work()
    rig.store.track(report, work, actor=ACTOR)  # a link that died before it stamped delivery

    outcome = rig.link(report, work)

    assert outcome.kind is LinkKind.ALREADY_LINKED and rig.delivery(report) == "delivery:pending"


# -- replace -----------------------------------------------------------------------------------


def test_a_report_tracking_other_work_is_refused_unless_replace(rig) -> None:
    report, one, two = rig.report(), rig.work("one"), rig.work("two")
    rig.link(report, one)
    writes = rig.store.writes

    with pytest.raises(IntakeRefused) as refused:
        rig.link(report, two)
    assert refused.value.reason is RefusalReason.ALREADY_LINKED_ELSEWHERE
    assert "--replace" in refused.value.detail and rig.store.writes == writes

    outcome = rig.link(report, two, replace=True)

    assert (outcome.kind, outcome.work, outcome.previous) == (LinkKind.RELINKED, two, one)
    assert rig.store.tracked_work(report) == two and rig.store.trackers_of(one) == ()


def test_replace_resets_a_milestone_value_that_described_the_old_work(rig) -> None:
    report, one, two = rig.report(), rig.work("one"), rig.work("two")
    rig.link(report, one)
    rig.store.set_state(report, "delivery", "merged", reason="merge trigger", actor=ACTOR)

    rig.link(report, two, replace=True)

    assert rig.delivery(report) == "delivery:pending"  # the new work has not merged


# -- unlink ------------------------------------------------------------------------------------


def test_unlink_keeps_the_delivery_value_and_records_an_unlinked_fact(rig) -> None:
    report, work = rig.report(), rig.work()
    rig.link(report, work)

    outcome = rig.service.unlink(report, actor=ACTOR)

    assert rig.delivery(report) == "delivery:pending"  # bd set-state cannot clear it
    assert outcome.delivery == "pending"
    assert rig.facts(report).unlinked == UnlinkedFact(work)
    assert rig.service.work_progress(report) is WorkProgress.UNLINKED


def test_the_unlinked_fact_is_the_outbox_documents_and_keeps_its_records(rig) -> None:
    report, work = rig.report(), rig.work()
    rig.link(report, work)
    rig.store.write_outbox(report, outbox(2, texts=("thanks",)), expect_seq=0, actor=ACTOR)

    rig.service.unlink(report, actor=ACTOR)

    doc = rig.store.read_outbox(report)
    assert doc is not None and doc.seq == 3 and len(doc.records) == 1  # one write, whole document
    assert doc.facts.unlinked == UnlinkedFact(work)
    assert rig.store.outbox_writes[-1] == (report, 3)


def test_unlinking_a_report_that_tracks_nothing_writes_nothing(rig) -> None:
    report = rig.report()
    writes = rig.store.writes

    outcome = rig.service.unlink(report, actor=ACTOR)

    assert (outcome.kind, outcome.work) == (LinkKind.UNLINKED, "") and rig.store.writes == writes


def test_relinking_the_work_it_was_unlinked_from_keeps_a_milestone_value(rig) -> None:
    report, work = rig.report(), rig.work()
    rig.link(report, work)
    rig.store.set_state(report, "delivery", "merged", reason="merge trigger", actor=ACTOR)
    rig.service.unlink(report, actor=ACTOR)

    outcome = rig.link(report, work)

    assert rig.delivery(report) == "delivery:merged" and outcome.delivery == "merged"
    assert rig.facts(report).unlinked is None  # linked again: the fact is cleared


def test_linking_other_work_after_an_unlink_goes_back_to_pending(rig) -> None:
    report, one, two = rig.report(), rig.work("one"), rig.work("two")
    rig.link(report, one)
    rig.store.set_state(report, "delivery", "released", reason="release trigger", actor=ACTOR)
    rig.service.unlink(report, actor=ACTOR)

    rig.link(report, two)

    assert rig.delivery(report) == "delivery:pending" and rig.facts(report).unlinked is None


# -- refusals: all decided before the first write ----------------------------------------------


def _refusal(rig: Rig, request: LinkRequest) -> IntakeRefused:
    writes, holds = rig.store.writes, list(rig.lock.holds)
    with pytest.raises(IntakeRefused) as refused:
        rig.service.link(request)
    assert rig.store.writes == writes and rig.lock.holds == holds  # nothing written, no lock
    return refused.value


def test_d_a_a_bound_report_is_never_a_link_target(rig) -> None:
    report, other = rig.report("a"), rig.report("b")

    refused = _refusal(rig, LinkRequest(report, other, ACTOR))

    assert refused.reason is RefusalReason.TARGET_IS_REPORT
    assert "accept --new-work" in refused.detail


def test_d_a_holds_for_an_accepted_bound_report_and_a_chain(rig) -> None:
    report, accepted, work = rig.report("a"), rig.report("b"), rig.work()
    rig.link(accepted, work)
    rig.service.dispose(DispositionRequest(accepted, Disposition.ACCEPT, ACTOR))

    # accepted (no longer untriaged) and itself tracking work: still a report, never work
    assert _refusal(rig, LinkRequest(report, accepted, ACTOR)).reason is (
        RefusalReason.TARGET_IS_REPORT
    )


def test_a_public_store_id_is_refused_on_either_side(rig) -> None:
    report, work = rig.report(), rig.work()

    assert _refusal(rig, LinkRequest(report, "pub-abc1", ACTOR)).reason is (
        RefusalReason.PUBLIC_STORE_ID
    )
    assert _refusal(rig, LinkRequest("pub-abc1", work, ACTOR)).reason is (
        RefusalReason.PUBLIC_STORE_ID
    )


def test_a_work_bead_of_another_store_is_refused_and_never_looked_up(rig) -> None:
    report = rig.report()

    refused = _refusal(rig, LinkRequest(report, "other-abc1", ACTOR))

    assert refused.reason is RefusalReason.CROSS_STORE_LINK
    assert _refusal(rig, LinkRequest(report, "acme-ghost", ACTOR)).reason is (
        RefusalReason.CROSS_STORE_LINK
    )  # a bead of this prefix that does not exist reads the same


def test_the_remaining_refusals(rig) -> None:
    report, work = rig.report(), rig.work()
    native = rig.service.file_report(draft_for("a native report"))
    closed = rig.report("closed one")
    rig.store.close(closed, reason="dup", actor=ACTOR)

    assert _refusal(rig, LinkRequest(report, report, ACTOR)).reason is RefusalReason.SELF_LINK
    assert _refusal(rig, LinkRequest("acme-ghost", work, ACTOR)).reason is RefusalReason.NOT_FOUND
    assert _refusal(rig, LinkRequest(native, work, ACTOR)).reason is RefusalReason.NOT_A_REPORT
    assert _refusal(rig, LinkRequest(closed, work, ACTOR)).reason is RefusalReason.REPORT_CLOSED
    assert _refusal(rig, LinkRequest(report, native, ACTOR)).reason is (
        RefusalReason.TARGET_UNTRIAGED  # an untriaged intake bead is not work yet
    )


def test_an_accepted_born_native_report_may_be_the_work(rig) -> None:
    report = rig.report()
    native = rig.service.file_report(draft_for("an ordinary backlog item"))
    rig.service.dispose(DispositionRequest(native, Disposition.ACCEPT, ACTOR))

    assert rig.link(report, native).kind is LinkKind.LINKED


def test_a_busy_lock_refuses_before_any_write(rig) -> None:
    report, work = rig.report(), rig.work()
    writes = rig.store.writes

    with rig.lock.hold(BINDING_KEY), pytest.raises(IntakeRefused) as refused:
        rig.link(report, work)

    assert refused.value.reason is RefusalReason.INTAKE_BUSY and rig.store.writes == writes
    rig.link(report, work)  # released: the same call now goes through
    assert rig.lock.holds.count(BINDING_KEY) == 2


def test_a_store_failure_on_the_edge_names_the_step_and_the_retry_is_idempotent(rig) -> None:
    report, work = rig.report(), rig.work()

    class Flaky(MemoryReportStore):
        fail = True

        def track(self, report, work, *, actor):
            if self.fail:
                self.fail = False
                raise IntakeStoreError("bd busy", bead=report, operation="track")
            super().track(report, work, actor=actor)

    flaky = Flaky(rig.store.scope())
    flaky._rows, flaky._gates, flaky._next = rig.store._rows, rig.store._gates, rig.store._next
    service = IntakeService(flaky, _NoLabels(), lock=rig.lock)

    with pytest.raises(IntakeStoreError) as failed:
        service.link(LinkRequest(report, work, ACTOR))
    assert (failed.value.operation, failed.value.bead) == ("track", report)
    assert flaky.tracked_work(report) is None

    assert service.link(LinkRequest(report, work, ACTOR)).kind is LinkKind.LINKED


# -- accept --work / --new-work ----------------------------------------------------------------


def _accept(rig: Rig, report: str, **kwargs):
    return rig.service.dispose(DispositionRequest(report, Disposition.ACCEPT, ACTOR, **kwargs))


def test_accept_new_work_creates_plain_dispatchable_work_the_report_tracks(rig) -> None:
    report = rig.report("the checkout hangs")

    outcome = _accept(rig, report, new_work=True, issue_type="task", priority="1")

    work = outcome.work
    assert outcome.created_work and work and work != report
    view = rig.store.get(work)
    assert view is not None and view.title == "the checkout hangs"
    assert (view.issue_type, view.priority) == ("task", "1")  # type and priority go to the work
    # born internal: nothing of a report, never copied from the issue
    assert view.external_ref == "" and view.binding is None and view.source_system == ""
    assert not [x for x in view.labels if x.startswith(("origin:", "intake:", "delivery:"))]
    assert rig.store.tracked_work(report) == work
    assert rig.delivery(report) == "delivery:pending"
    assert "intake:untriaged" not in rig.labels(report) and "intake:accepted" in rig.labels(report)
    assert rig.store.get(report).issue_type == "bug"  # the report keeps its own type
    # the report stays held and is not work; the work bead is
    assert report not in rig.store.ready_ids() and work in rig.store.ready_ids()


def test_the_new_work_description_is_triages_and_never_the_issue_body(rig) -> None:
    store = rig.store
    report = rig.report()

    drafts = []
    create_work = store.create_work

    def spy(work_draft):
        drafts.append(work_draft)
        return create_work(work_draft)

    store.create_work = spy  # type: ignore[method-assign]
    _accept(rig, report, new_work=True)

    (made,) = drafts
    assert report in made.description and "steps" not in made.description  # draft()'s body
    assert made.issue_type == "bug"  # defaults to the report's type
    assert made.actor == ACTOR


def test_accept_work_tracks_existing_work_and_applies_type_and_priority_to_the_report(rig) -> None:
    report, work = rig.report(), rig.work()

    outcome = _accept(rig, report, work=work, issue_type="feature", priority="2")

    assert (outcome.work, outcome.created_work) == (work, False)
    view = rig.store.get(report)
    assert view is not None and (view.issue_type, view.priority) == ("feature", "2")
    assert rig.store.tracked_work(report) == work and rig.delivery(report) == "delivery:pending"


def test_a_second_report_accepted_to_the_same_work_shares_it(rig) -> None:
    first, second = rig.report("one"), rig.report("two")

    work = _accept(rig, first, new_work=True).work
    _accept(rig, second, work=work)

    assert rig.store.trackers_of(work) == tuple(sorted((first, second)))
    assert len([b for b in rig.service.reports_for_work(work)]) == 2


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"work": "acme-0002", "new_work": True}, RefusalReason.INVALID_REQUEST),
        ({"work": "pub-abc1"}, RefusalReason.PUBLIC_STORE_ID),
        ({"work": "other-abc1"}, RefusalReason.CROSS_STORE_LINK),
        ({"work": "acme-ghost"}, RefusalReason.CROSS_STORE_LINK),
    ],
)
def test_a_refused_accept_writes_nothing_and_leaves_the_report_untriaged(
    rig, kwargs, reason
) -> None:
    report = rig.report()
    rig.work()
    writes = rig.store.writes

    with pytest.raises(IntakeRefused) as refused:
        _accept(rig, report, **kwargs)

    assert refused.value.reason is reason and rig.store.writes == writes
    assert "intake:untriaged" in rig.labels(report)


def test_accept_to_a_bound_report_target_is_refused_before_anything_is_created(rig) -> None:
    report, other = rig.report("a"), rig.report("b")
    writes = rig.store.writes

    with pytest.raises(IntakeRefused) as refused:
        _accept(rig, report, work=other)

    assert refused.value.reason is RefusalReason.TARGET_IS_REPORT and rig.store.writes == writes


def test_accept_with_work_refuses_a_born_native_report_before_anything_is_created(rig) -> None:
    native = rig.service.file_report(draft_for("a native report"))
    writes = rig.store.writes

    for kwargs in ({"new_work": True}, {"work": rig.work()}):
        writes = rig.store.writes
        with pytest.raises(IntakeRefused) as refused:
            _accept(rig, native, **kwargs)
        assert refused.value.reason is RefusalReason.NOT_A_REPORT
        assert rig.store.writes == writes


def test_new_work_refuses_a_report_that_already_tracks_work_and_names_the_fix(rig) -> None:
    report, work = rig.report(), rig.work()
    rig.link(report, work)
    writes = rig.store.writes

    with pytest.raises(IntakeRefused) as refused:
        _accept(rig, report, new_work=True)

    assert refused.value.reason is RefusalReason.ALREADY_LINKED_ELSEWHERE
    assert f"--work {work}" in refused.value.detail and rig.store.writes == writes


def test_an_interrupted_accept_is_finished_by_repeating_it_with_work(rig) -> None:
    report, work = rig.report(), rig.work()
    rig.link(report, work)  # the link landed; clearing intake did not

    outcome = _accept(rig, report, work=work)

    assert outcome.state == "accepted" and "intake:untriaged" not in rig.labels(report)
    assert rig.store.trackers_of(work) == (report,)


def test_a_link_that_fails_after_the_new_work_was_created_closes_that_work(rig) -> None:
    report = rig.report()

    class DiesOnTrack(MemoryReportStore):
        def track(self, report, work, *, actor):
            raise IntakeStoreError("bd down", bead=report, operation="track")

    broken = DiesOnTrack(rig.store.scope())
    broken._rows, broken._gates, broken._next = rig.store._rows, rig.store._gates, rig.store._next
    service = IntakeService(broken, _NoLabels(), lock=rig.lock)

    with pytest.raises(IntakeStoreError):
        service.dispose(DispositionRequest(report, Disposition.ACCEPT, ACTOR, new_work=True))

    orphan = [b for b, row in broken._rows.items() if b not in {report}][-1]
    assert broken.get(orphan).status == "closed"  # not left behind as stray open work
    assert "intake:untriaged" in rig.labels(report)  # and the report is still queued


# -- work progress -----------------------------------------------------------------------------


def test_work_progress_is_derived_from_the_tracked_work_never_stored(rig) -> None:
    report, work = rig.report(), rig.work()
    assert rig.service.work_progress(report) is WorkProgress.UNLINKED
    rig.link(report, work)

    assert rig.service.work_progress(report) is WorkProgress.OPEN
    rig.store.mark(work, status="in_progress")
    assert rig.service.work_progress(report) is WorkProgress.IN_PROGRESS
    rig.store.mark(work, labels=("review:pending",))
    assert rig.service.work_progress(report) is WorkProgress.IN_REVIEW
    rig.store.close(work, reason="merged", actor=ACTOR)
    writes = rig.store.writes
    assert rig.service.work_progress(report) is WorkProgress.CLOSED

    assert rig.store.writes == writes  # a read: nothing was stored
    assert not [x for x in rig.labels(report) if x.startswith("work")]


@pytest.mark.parametrize(
    ("status", "labels", "expected"),
    [
        ("open", (), WorkProgress.OPEN),
        ("blocked", (), WorkProgress.OPEN),
        ("deferred", (), WorkProgress.OPEN),
        ("in_progress", (), WorkProgress.IN_PROGRESS),
        ("hooked", (), WorkProgress.IN_PROGRESS),
        ("in_progress", ("review:pending",), WorkProgress.IN_REVIEW),
        ("open", ("review:pending",), WorkProgress.IN_REVIEW),
        ("closed", ("review:pending",), WorkProgress.CLOSED),
        ("closed", (), WorkProgress.CLOSED),
    ],
)
def test_work_progress_of_status_and_review_label(status, labels, expected) -> None:
    assert work_progress_of(status, labels) is expected


# -- the delivery rule -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("labels", "edge", "unlinked", "expected"),
    [
        ((), True, "", "pending"),  # first link
        (("delivery:pending",), True, "", None),  # already pending: write nothing
        (("delivery:merged",), True, "", "pending"),  # other work: the milestone is stale
        (("delivery:merged",), True, "w", None),  # the very work it was unlinked from
        (("delivery:released",), True, "other", "pending"),
        (("delivery:reverted",), True, "", "pending"),
        ((), False, "", "pending"),  # an existing edge repairs a missing value
        (("delivery:merged",), False, "", None),  # ... and never rewrites a real one
    ],
)
def test_delivery_at_link(labels, edge, unlinked, expected) -> None:
    assert delivery_at_link(labels, edge_written=edge, unlinked_work=unlinked, work="w") == expected


# -- the unlinked fact in the document ---------------------------------------------------------


def test_the_unlinked_fact_round_trips_and_is_omitted_when_unset() -> None:
    plain = OutboxDocument(seq=1)
    assert "unlinked" not in outbox_to_document(plain)["facts"]  # an old document is unchanged

    doc = OutboxDocument(seq=2, facts=DeliveryFacts(unlinked=UnlinkedFact("acme-9")))
    stored = outbox_to_document(doc)

    assert stored["facts"]["unlinked"] == {"work": "acme-9"}
    assert outbox_from_document(stored) == doc


# -- the disabled port -------------------------------------------------------------------------


def test_null_intake_refuses_link_and_accept_with_work_as_disabled() -> None:
    null = NullIntake()
    calls = (
        lambda: null.link(LinkRequest("a", "b", ACTOR)),
        lambda: null.unlink("a", actor=ACTOR),
        lambda: null.dispose(DispositionRequest("a", Disposition.ACCEPT, ACTOR, new_work=True)),
    )
    for call in calls:
        with pytest.raises(IntakeRefused) as refused:
            call()
        assert refused.value.reason is RefusalReason.INTAKE_DISABLED
    assert null.work_progress("a") is WorkProgress.UNLINKED
