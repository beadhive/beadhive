"""The ``investigates`` relation (bh-u3a53): reproduction work never fires a delivery milestone.

``IntakeService`` over ``MemoryReportStore``, the never-waiting ``InProcessIntakeLock`` and
``SequentialIdSource``, with the milestone service wired as the root wires it and EVERY milestone
switch on, so a trigger that could fire does. What is pinned:

* the rules: an investigation is refused for the same reasons as a ``tracks`` link (public id,
  self, unknown or unbound or closed report, foreign or report or untriaged target), plus the work
  the report already tracks (``tracked_work``); several beads may investigate one report;
* ``--new-work`` creates a plain reproduction bead and nothing when refused;
* an investigation writes ONE edge and nothing else: no ``delivery``, no outbox document;
* the key property: for a report that a bead only INVESTIGATES, no trigger (``started`` from the
  claim hook, ``blocked``, ``deprioritised``, ``merged``, ``released``, the link catch-up) drafts a
  record, moves a dimension or closes the report, while a report that TRACKS the very same bead
  hears every one of them;
* the report's relations list both kinds, and each kind unlinks without touching the other.

No tracker request in any case (``write_requests() == 0``); no clock, no sleep.
"""

from __future__ import annotations

import pytest

from beadhive.modules.intake.application import (
    IntakeService,
    MilestoneService,
    NullIntake,
    OutboxService,
)
from beadhive.modules.intake.domain import (
    BeadView,
    CompletionPolicy,
    IntakeRefused,
    InvestigateRequest,
    IssueBinding,
    IssueStatus,
    LeakPolicy,
    LinkKind,
    LinkRequest,
    MergeFact,
    MilestonePolicy,
    NoticeFact,
    OutboxEvent,
    OutboxPolicy,
    OwnIdentity,
    RefusalReason,
    ReleaseFact,
    ReportDraft,
    ReportRelations,
    SourceSnapshot,
    StartFact,
    StorePosture,
    WorkDraft,
    WorkProgress,
    check_investigate,
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
EVERYTHING = MilestonePolicy(
    draft_on_merge=True,
    draft_on_release=True,
    append_release_to_closed=True,
    draft_on_start=True,
    draft_on_blocked=True,
    draft_on_deprioritised=True,
)
PRIVATE = StorePosture(published=False)
MERGE_SHA = "a" * 40
RELEASE_SHA = "d" * 40
TAG = "v1.2.0"
ACTOR = "dev/triage"
ME = OwnIdentity("acme/widgets", "inst-1")
WAITING = "Waiting on an upstream fix in the parser library."


def marker_line(marker: OwnMarker) -> str:
    return f"<!-- bh:v1 hive={marker.hive} install={marker.install} outbox={marker.outbox_id} -->"


class _NoLabels:
    def violations(self, labels) -> tuple[str, ...]:
        return ()


class Scene:
    """A reproduction bead, reports bound to memory issues, and intake wired with the milestone
    service (every switch on, the link catch-up on) and the sender."""

    def __init__(self, *, outbox: bool = True, milestones: MilestonePolicy = EVERYTHING) -> None:
        self.world = MemoryWorld()
        self.world.set_write_time(at(2))
        self.tracker = MemoryTracker(
            self.world, enabled=frozenset(READ_OPERATIONS | WRITE_OPERATIONS)
        )
        self.store = MemoryReportStore(SCOPE)
        self.lock = InProcessIntakeLock()
        self.ids = SequentialIdSource()
        sanitizer = RecordingSanitizer.of(["s3cr3t"])
        policy = OutboxPolicy(enabled=outbox)
        self.verifier = FakeReleaseVerifier(
            tags={TAG: RELEASE_SHA}, contained={RELEASE_SHA: frozenset({MERGE_SHA})}
        )
        self.milestones = MilestoneService(
            self.store,
            self.lock,
            ids=self.ids,
            leak_policy=LEAKS,
            sanitizer=sanitizer,
            posture=PRIVATE,
            outbox=policy,
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
            policy=policy,
            leak_policy=LEAKS,
            sanitizer=sanitizer,
            posture=PRIVATE,
            completion=self.milestones.complete,
        )
        self.intake = IntakeService(
            self.store,
            _NoLabels(),
            outbox=self.sender,
            milestones=self.milestones,
            lock=self.lock,
            catch_up_on_link=True,
        )
        self.repro = self.store.create_work(WorkDraft("Reproduce: a bug", "task", "dev/t"))

    def report(self, title: str = "a bug") -> str:
        issue = self.world.add_issue(title, at=at(1))
        return self.store.create(
            ReportDraft(
                title,
                "bug",
                "origin:github",
                "dev/import",
                binding=IssueBinding("", "memory", self.world.locator, issue.url, issue.identifier),
                snapshot=SourceSnapshot(title, "", IssueStatus.OPEN, "r1"),
            )
        )

    def investigated(self, work: str = "") -> str:
        report = self.report()
        self.intake.investigate(InvestigateRequest(report, ACTOR, work=work or self.repro))
        return report

    def tracking(self, work: str = "") -> str:
        report = self.report()
        self.intake.link(LinkRequest(report, work or self.repro, ACTOR))
        return report

    def view(self, bead: str) -> BeadView:
        view = self.store.get(bead)
        assert view is not None
        return view

    def events(self, report: str) -> list[OutboxEvent]:
        doc = self.store.read_outbox(report)
        found = sorted((doc.records if doc else {}).values(), key=lambda r: r.created_seq)
        return [record.event for record in found]

    def untouched(self, report: str) -> None:
        """Nothing intake owns moved on ``report``: no outbox document, no ``delivery`` or
        ``notified`` value, still open, still held (never in ready work)."""
        view = self.view(report)
        assert self.store.read_outbox(report) is None
        assert not [x for x in view.labels if x.startswith(("delivery:", "notified:"))]
        assert view.status == "open"
        assert report not in self.store.ready_ids()
        assert report not in [bead for bead, _seq in self.store.outbox_writes]

    def requests(self) -> tuple[int, int]:
        return self.world.read_requests(), self.world.write_requests()


# -- the rules ----------------------------------------------------------------------------------


def _report(status: str = "open", bound: bool = True) -> BeadView:
    binding = IssueBinding("acme-1", "memory", "loc", "https://x/1", "1") if bound else None
    return BeadView("acme-1", "a bug", "bug", status=status, binding=binding)


WORK = BeadView("acme-2", "Reproduce: a bug", "task")


def _check(report=None, work=WORK, *, report_id="acme-1", work_id="acme-2", tracked=None, by=()):
    return check_investigate(
        scope=SCOPE,
        report_id=report_id,
        report=_report() if report is None else report,
        work_id=work_id,
        work=work,
        tracked=tracked,
        investigated_by=by,
    )


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"work_id": "pub-abc1"}, RefusalReason.PUBLIC_STORE_ID),
        ({"work_id": "acme-1", "work": _report()}, RefusalReason.SELF_LINK),
        ({"report": _report(bound=False)}, RefusalReason.NOT_A_REPORT),
        ({"report": _report(status="closed")}, RefusalReason.REPORT_CLOSED),
        ({"work_id": "other-2", "work": None}, RefusalReason.CROSS_STORE_LINK),
        ({"work": None}, RefusalReason.CROSS_STORE_LINK),
        ({"work": _report()}, RefusalReason.TARGET_IS_REPORT),
        (
            {"work": BeadView("acme-2", "t", "task", labels=("intake:untriaged",))},
            RefusalReason.TARGET_UNTRIAGED,
        ),
        ({"tracked": "acme-2"}, RefusalReason.TRACKED_WORK),
    ],
)
def test_an_investigation_is_refused_for_the_link_reasons_and_the_tracked_work(
    kwargs, reason
) -> None:
    with pytest.raises(IntakeRefused) as refused:
        _check(**kwargs)

    assert refused.value.reason is reason


def test_an_unknown_report_is_not_found() -> None:
    with pytest.raises(IntakeRefused) as refused:
        check_investigate(
            scope=SCOPE,
            report_id="acme-1",
            report=None,
            work_id="acme-2",
            work=WORK,
            tracked=None,
            investigated_by=(),
        )
    assert refused.value.reason is RefusalReason.NOT_FOUND


def test_a_new_investigation_links_and_a_repeat_is_already_linked() -> None:
    assert _check() is LinkKind.LINKED
    assert _check(by=("acme-3",)) is LinkKind.LINKED  # several beads may investigate one report
    assert _check(by=("acme-2",)) is LinkKind.ALREADY_LINKED
    assert _check(tracked="acme-3") is LinkKind.LINKED  # tracking OTHER work does not matter


# -- the service --------------------------------------------------------------------------------


def test_investigating_writes_one_edge_and_nothing_else() -> None:
    s = Scene()
    report = s.report()
    writes = s.store.writes

    outcome = s.intake.investigate(InvestigateRequest(report, ACTOR, work=s.repro))

    assert (outcome.kind, outcome.work, outcome.created_work) == (LinkKind.LINKED, s.repro, False)
    assert outcome.relations == ReportRelations(report, investigated_by=(s.repro,))
    assert s.store.writes == writes + 1  # the edge, and no delivery stamp or outbox write
    assert s.store.investigations_of(report) == (s.repro,)
    assert s.store.tracked_work(report) is None and s.store.trackers_of(s.repro) == ()
    s.untouched(report)
    assert s.requests() == (0, 0)


def test_investigating_again_writes_nothing() -> None:
    s = Scene()
    report = s.investigated()
    writes = s.store.writes

    outcome = s.intake.investigate(InvestigateRequest(report, ACTOR, work=s.repro))

    assert outcome.kind is LinkKind.ALREADY_LINKED and s.store.writes == writes


def test_the_reports_intake_value_is_untouched() -> None:
    s = Scene()
    report = s.report()
    s.store.set_state(report, "intake", "untriaged", reason="import", actor="dev/import")

    s.intake.investigate(InvestigateRequest(report, ACTOR, work=s.repro))

    assert "intake:untriaged" in s.view(report).labels  # reproduction is a triage step


def test_a_refusal_writes_nothing() -> None:
    s = Scene()
    report = s.tracking()
    writes = s.store.writes

    with pytest.raises(IntakeRefused) as refused:
        s.intake.investigate(InvestigateRequest(report, ACTOR, work=s.repro))

    assert refused.value.reason is RefusalReason.TRACKED_WORK
    assert s.store.writes == writes and s.store.investigations_of(report) == ()


@pytest.mark.parametrize(
    "request_kwargs",
    [{}, {"work": "acme-x", "new_work": True}],
    ids=["neither", "both"],
)
def test_exactly_one_of_work_and_new_work(request_kwargs) -> None:
    s = Scene()
    report = s.report()

    with pytest.raises(IntakeRefused) as refused:
        s.intake.investigate(InvestigateRequest(report, ACTOR, **request_kwargs))

    assert refused.value.reason is RefusalReason.INVALID_REQUEST


def test_new_work_creates_a_plain_reproduction_bead_that_investigates_the_report() -> None:
    s = Scene()
    report = s.report("Login page crashes")

    outcome = s.intake.investigate(InvestigateRequest(report, ACTOR, new_work=True, priority="1"))

    assert outcome.kind is LinkKind.LINKED and outcome.created_work
    work = s.view(outcome.work)
    assert (work.title, work.issue_type, work.priority) == (
        "Reproduce: Login page crashes",
        "task",
        "1",
    )
    assert work.binding is None and work.external_ref == ""  # plain work, never a report
    assert not [x for x in work.labels if x.startswith(("origin:", "intake:", "delivery:"))]
    assert outcome.work in s.store.ready_ids()  # dispatchable while the report stays held
    assert s.store.investigations_of(report) == (outcome.work,)
    assert s.store.tracked_work(report) is None
    s.untouched(report)


def test_new_work_takes_the_requested_type() -> None:
    s = Scene()
    report = s.report()

    outcome = s.intake.investigate(
        InvestigateRequest(report, ACTOR, new_work=True, issue_type="chore")
    )

    assert s.view(outcome.work).issue_type == "chore"


@pytest.mark.parametrize(
    ("make", "reason"),
    [
        (lambda s: "acme-nope", RefusalReason.NOT_FOUND),
        (lambda s: s.repro, RefusalReason.NOT_A_REPORT),  # plain work is no report
        (lambda s: "pub-abc1", RefusalReason.PUBLIC_STORE_ID),
    ],
    ids=["unknown", "unbound", "public"],
)
def test_a_refused_new_work_creates_nothing(make, reason) -> None:
    s = Scene()
    report = make(s)
    writes = s.store.writes

    with pytest.raises(IntakeRefused) as refused:
        s.intake.investigate(InvestigateRequest(report, ACTOR, new_work=True))

    assert refused.value.reason is reason and s.store.writes == writes


def test_a_closed_report_is_refused_before_new_work_is_created() -> None:
    s = Scene()
    report = s.report()
    s.store.close(report, reason="dup", actor=ACTOR)
    writes = s.store.writes

    with pytest.raises(IntakeRefused) as refused:
        s.intake.investigate(InvestigateRequest(report, ACTOR, new_work=True))

    assert refused.value.reason is RefusalReason.REPORT_CLOSED and s.store.writes == writes


def test_new_work_whose_edge_fails_is_closed_again() -> None:
    s = Scene()
    report = s.report()
    created: list[str] = []
    create_work = s.store.create_work

    def spy(draft):
        created.append(create_work(draft))
        return created[-1]

    def broken(report_id, work_id, *, actor):
        raise IntakeRefused(RefusalReason.INTAKE_BUSY, "held elsewhere")

    s.store.create_work = spy  # type: ignore[method-assign]
    s.store.investigate = broken  # type: ignore[method-assign]

    with pytest.raises(IntakeRefused) as refused:
        s.intake.investigate(InvestigateRequest(report, ACTOR, new_work=True))

    assert refused.value.reason is RefusalReason.INTAKE_BUSY
    (work,) = created
    assert s.view(work).status == "closed" and s.store.investigations_of(report) == ()


def test_several_beads_investigate_one_report_and_one_bead_several_reports() -> None:
    s = Scene()
    other = s.store.create_work(WorkDraft("Reproduce on macOS", "task", "dev/t"))
    first = s.investigated()
    s.intake.investigate(InvestigateRequest(first, ACTOR, work=other))
    second = s.investigated()

    assert s.intake.relations(first).investigated_by == tuple(sorted((s.repro, other)))
    assert s.intake.relations(second).investigated_by == (s.repro,)


# -- the report view and unlinking ------------------------------------------------------------


def test_relations_list_the_tracked_work_and_every_investigating_bead() -> None:
    s = Scene()
    fix = s.store.create_work(WorkDraft("fix the login crash", "bug", "dev/t"))
    report = s.tracking(fix)
    s.intake.investigate(InvestigateRequest(report, ACTOR, work=s.repro))
    s.store.mark(fix, status="in_progress")

    relations = s.intake.relations(report)

    assert relations == ReportRelations(
        report, tracks=fix, investigated_by=(s.repro,), progress=WorkProgress.IN_PROGRESS
    )
    assert s.intake.work_progress(report) is WorkProgress.IN_PROGRESS  # tracks only


def test_relations_of_an_unknown_report_is_not_found() -> None:
    with pytest.raises(IntakeRefused) as refused:
        Scene().intake.relations("acme-nope")
    assert refused.value.reason is RefusalReason.NOT_FOUND


def test_uninvestigate_removes_only_that_edge_and_leaves_tracks() -> None:
    s = Scene()
    fix = s.store.create_work(WorkDraft("fix", "bug", "dev/t"))
    other = s.store.create_work(WorkDraft("Reproduce again", "task", "dev/t"))
    report = s.tracking(fix)
    for work in (s.repro, other):
        s.intake.investigate(InvestigateRequest(report, ACTOR, work=work))
    delivery = [x for x in s.view(report).labels if x.startswith("delivery:")]

    outcome = s.intake.uninvestigate(report, s.repro, actor=ACTOR)

    assert (outcome.kind, outcome.work) == (LinkKind.UNLINKED, s.repro)
    assert outcome.relations == ReportRelations(
        report, tracks=fix, investigated_by=(other,), progress=WorkProgress.OPEN
    )
    assert [x for x in s.view(report).labels if x.startswith("delivery:")] == delivery
    assert s.store.read_outbox(report) is None or s.store.read_outbox(report).facts.unlinked is None


def test_unlinking_tracks_leaves_every_investigation() -> None:
    s = Scene()
    fix = s.store.create_work(WorkDraft("fix", "bug", "dev/t"))
    report = s.tracking(fix)
    s.intake.investigate(InvestigateRequest(report, ACTOR, work=s.repro))

    s.intake.unlink(report, actor=ACTOR)

    assert s.intake.relations(report) == ReportRelations(report, investigated_by=(s.repro,))


def test_uninvestigating_a_bead_that_does_not_investigate_is_a_no_op() -> None:
    s = Scene()
    report = s.report()
    writes = s.store.writes

    outcome = s.intake.uninvestigate(report, s.repro, actor=ACTOR)

    assert (outcome.kind, outcome.work) == (LinkKind.UNLINKED, "") and s.store.writes == writes


def test_uninvestigate_works_on_a_closed_report_and_refuses_an_unbound_bead() -> None:
    s = Scene()
    report = s.investigated()
    s.store.close(report, reason="dup", actor=ACTOR)

    assert s.intake.uninvestigate(report, s.repro, actor=ACTOR).work == s.repro
    with pytest.raises(IntakeRefused) as refused:
        s.intake.uninvestigate(s.repro, "acme-x", actor=ACTOR)
    assert refused.value.reason is RefusalReason.NOT_A_REPORT


# -- the key property: no trigger ever reaches an investigated report -------------------------


def _claim(s: Scene):
    return s.intake.on_work_started(StartFact(s.repro, "dev/alice"))


def _blocked(s: Scene):
    return s.intake.delivery_notice(NoticeFact(s.repro, OutboxEvent.BLOCKED, WAITING, "alice"))


def _deprioritised(s: Scene):
    return s.intake.delivery_notice(
        NoticeFact(s.repro, OutboxEvent.DEPRIORITISED, WAITING, "alice")
    )


def _merged(s: Scene):
    s.store.mark(s.repro, status="closed")
    return s.intake.on_work_merged(MergeFact(s.repro, MERGE_SHA, "merger/test"))


def _released(s: Scene):
    _merged(s)
    return s.intake.on_release(ReleaseFact(TAG, RELEASE_SHA, "alice"))


TRIGGERS = [
    pytest.param(_claim, OutboxEvent.STARTED, id="started-claim-hook"),
    pytest.param(_blocked, OutboxEvent.BLOCKED, id="blocked"),
    pytest.param(_deprioritised, OutboxEvent.DEPRIORITISED, id="deprioritised"),
    pytest.param(_merged, OutboxEvent.MERGED, id="merged"),
    pytest.param(_released, OutboxEvent.RELEASED, id="released"),
]


@pytest.mark.parametrize(("trigger", "event"), TRIGGERS)
def test_no_trigger_reaches_a_report_the_work_only_investigates(trigger, event) -> None:
    s = Scene()
    investigated = s.investigated()
    tracking = s.tracking()  # the SAME work, linked as tracks: the control

    outcome = trigger(s)

    assert investigated not in outcome.reports
    s.untouched(investigated)
    assert s.intake.relations(investigated).investigated_by == (s.repro,)
    # the control proves the trigger fired: the tracking report heard it
    assert tracking in outcome.reports and event in s.events(tracking)
    assert s.requests() == (0, 0)


@pytest.mark.parametrize(("trigger", "_event"), TRIGGERS)
def test_a_trigger_on_work_that_only_investigates_drafts_nothing_at_all(trigger, _event) -> None:
    s = Scene()
    investigated = s.investigated()
    writes = len(s.store.outbox_writes)

    outcome = trigger(s)

    assert investigated not in outcome.reports and not outcome.errors
    assert len(s.store.outbox_writes) == writes
    s.untouched(investigated)


def test_closing_investigating_work_at_merge_never_closes_the_report() -> None:
    # outbox off: close-on-merge closes a tracking report AT the merge, so a report wrongly read
    # as tracking would close here too
    s = Scene(outbox=False, milestones=MilestonePolicy(completion=CompletionPolicy.CLOSE_ON_MERGE))
    investigated = s.investigated()
    tracking = s.tracking()

    outcome = _merged(s)

    assert s.view(tracking).status == "closed"  # the control closed
    assert investigated not in outcome.reports
    s.untouched(investigated)


def test_investigating_merged_work_catches_nothing_up() -> None:
    s = Scene()
    tracking = s.tracking()
    _merged(s)  # the merge is recorded on the tracking report; the work is closed
    assert OutboxEvent.MERGED in s.events(tracking)
    report = s.report()

    outcome = s.intake.investigate(InvestigateRequest(report, ACTOR, work=s.repro))

    assert outcome.kind is LinkKind.LINKED
    s.untouched(report)
    # the control: tracking the same merged work with the catch-up on DOES catch up
    late = s.report()
    linked = s.intake.link(LinkRequest(late, s.repro, ACTOR))
    assert linked.milestones and OutboxEvent.MERGED in s.events(late)


def test_an_investigated_report_that_later_tracks_other_work_hears_only_that_work() -> None:
    s = Scene()
    fix = s.store.create_work(WorkDraft("fix", "bug", "dev/t"))
    report = s.investigated()
    s.intake.link(LinkRequest(report, fix, ACTOR))

    _merged(s)  # the reproduction bead lands: nothing for the report
    assert s.events(report) == [] and "delivery:pending" in s.view(report).labels

    s.store.mark(fix, status="closed")
    s.intake.on_work_merged(MergeFact(fix, "b" * 40, "merger/test"))  # the fix lands
    assert s.events(report) == [OutboxEvent.MERGED]


def test_reports_for_work_and_work_progress_ignore_investigations() -> None:
    s = Scene()
    report = s.investigated()

    assert s.intake.reports_for_work(s.repro) == ()
    assert s.intake.work_progress(report) is WorkProgress.UNLINKED


# -- intake off ------------------------------------------------------------------------------


def test_null_intake_refuses_the_writes_and_reads_no_relations() -> None:
    null = NullIntake()

    for call in (
        lambda: null.investigate(InvestigateRequest("acme-1", ACTOR, work="acme-2")),
        lambda: null.uninvestigate("acme-1", "acme-2", actor=ACTOR),
    ):
        with pytest.raises(IntakeRefused) as refused:
            call()
        assert refused.value.reason is RefusalReason.INTAKE_DISABLED
    assert null.relations("acme-1") == ReportRelations("acme-1")
