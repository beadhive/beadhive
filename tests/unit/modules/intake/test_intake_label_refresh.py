"""The opt-in label refresh on transitions (``bh-gjb5w``, ``intake.external.labels.on_transition``).

``IntakeService`` wired as the root wires it (thread service, supervised sender, milestone service,
label projection) over ``MemoryReportStore`` and ``MemoryTracker`` / ``MemoryWorld``. With the
switch on, every transition (dispose, needs_info, wake, link, unlink and the milestone triggers)
drafts or refreshes exactly ONE ``labels`` record for the affected report, from stored state and
with no tracker call; a later transition refreshes that record and never stacks a second open
one; ``apply_reviewed`` keeps its own refresh and the verbs it runs draft none. With the switch
off nothing changes. Asserted on record counts and call counts only: no clock, no sleep, no socket.
"""

from __future__ import annotations

from collections import Counter

import pytest

from beadhive.modules.intake.application import (
    IntakeService,
    LabelProjection,
    MilestoneService,
    NullIntake,
    OutboxService,
    StoreOutboxView,
    ThreadService,
    TriageBinding,
)
from beadhive.modules.intake.domain import (
    ApplyRequest,
    CompletionPolicy,
    Disposition,
    DispositionRequest,
    IntakeRefused,
    IssueBinding,
    IssueStatus,
    LabelDefineRequest,
    LabelSettings,
    LabelSyncRequest,
    LeakPolicy,
    LinkRequest,
    MergeFact,
    MilestonePolicy,
    NeedsInfoRequest,
    NoticeFact,
    OpKind,
    OutboxEvent,
    OutboxPolicy,
    OwnIdentity,
    ProposalReviewRequest,
    ProposeRequest,
    RecordStatus,
    RegistryDimension,
    ReleaseFact,
    ReportDraft,
    SourceSnapshot,
    StartFact,
    StorePosture,
    ThreadRequest,
    WakeRequest,
    WorkDraft,
)
from beadhive.modules.tracker.contracts import READ_OPERATIONS, WRITE_OPERATIONS, OwnMarker
from beadhive.testing.intake import (
    SCOPE,
    FakeReleaseVerifier,
    FakeRepoFacts,
    InProcessIntakeLock,
    MemoryReportStore,
    RecordingSanitizer,
    SequentialIdSource,
)
from beadhive.testing.tracker import MemoryTracker, MemoryWorld, at

ME = OwnIdentity("acme/widgets", "inst-1")
LEAKS = LeakPolicy(
    bead_prefixes=frozenset({"acme", "pub"}),
    private_dimensions=frozenset({"intake", "origin", "delivery", "notified", "review"}),
    owned_label_prefixes=("bh:",),
)
OUTBOX = OutboxPolicy(enabled=True, labels_enabled=True)
PRIVATE = StorePosture(published=False)
MILESTONES = MilestonePolicy(
    draft_on_merge=True,
    draft_on_release=True,
    draft_on_start=True,
    draft_on_blocked=True,
    completion=CompletionPolicy.CLOSE_ON_RELEASE,
)
ACTOR = "dev/triage"
MERGE_SHA = "a" * 40
RELEASE_SHA = "b" * 40
QUESTION = "Which version are you running, and does it happen with a fresh config?"
WAITING = "Waiting on an upstream fix in the parser library."


def marker_line(marker: OwnMarker) -> str:
    return f"<!-- bh:v1 hive={marker.hive} install={marker.install} outbox={marker.outbox_id} -->"


class _NoLabels:
    def violations(self, labels) -> tuple[str, ...]:
        return ()


class SilentTracker(MemoryTracker):
    """A tracker that counts every public call, so a test can assert it was never asked."""

    def __getattribute__(self, name):
        attr = super().__getattribute__(name)
        if name.startswith("_") or name == "calls" or not callable(attr):
            return attr
        super().__getattribute__("calls")[name] += 1
        return attr


class SpyProjection(LabelProjection):
    """``LabelProjection`` counting its entry points."""

    calls: Counter[str]

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.calls = Counter()

    def refresh(self, reports, actor):
        self.calls["refresh"] += 1
        return super().refresh(reports, actor)

    def sync(self, request):
        self.calls["sync"] += 1
        return super().sync(request)


class Hive:
    def __init__(self, *, on_transition: bool = True, projection: bool = True) -> None:
        self.world = MemoryWorld()
        self.world.set_write_time(at(3))
        self.tracker = SilentTracker(
            self.world, enabled=frozenset(READ_OPERATIONS | WRITE_OPERATIONS)
        )
        self.tracker.calls = Counter()
        self.store = MemoryReportStore(SCOPE)
        self.lock = InProcessIntakeLock()
        self.ids = SequentialIdSource()
        sanitizer = RecordingSanitizer.of(["s3cr3t"])
        threads = ThreadService(
            self.store,
            self.tracker,
            self.lock,
            posture=PRIVATE,
            identity=ME,
            outbox=StoreOutboxView(self.store),
        )
        sender = OutboxService(
            self.store,
            self.tracker,
            self.lock,
            ids=self.ids,
            identity=ME,
            marker_line=marker_line,
            policy=OUTBOX,
            leak_policy=LEAKS,
            sanitizer=sanitizer,
            posture=PRIVATE,
        )
        milestones = MilestoneService(
            self.store,
            self.lock,
            ids=self.ids,
            leak_policy=LEAKS,
            sanitizer=sanitizer,
            posture=PRIVATE,
            outbox=OUTBOX,
            milestones=MILESTONES,
            verifier=FakeReleaseVerifier(
                tags={"v1": RELEASE_SHA}, contained={RELEASE_SHA: frozenset({MERGE_SHA})}
            ),
        )
        self.projection = SpyProjection(
            self.store,
            self.tracker,
            self.lock,
            ids=self.ids,
            registry={"component": RegistryDimension("component", "Area of the code")},
            settings=LabelSettings(),
            leak_policy=LEAKS,
            sanitizer=sanitizer,
            posture=PRIVATE,
            outbox=OUTBOX,
        )
        self.intake = IntakeService(
            self.store,
            _NoLabels(),
            threads=threads,
            outbox=sender,
            lock=self.lock,
            milestones=milestones,
            projection=self.projection if projection else None,
            refresh_on_transition=on_transition,
            triage=TriageBinding(
                repo=FakeRepoFacts(
                    branches={"main": frozenset()}, releases=(), unchanged_by_default=True
                ),
                leak_policy=LEAKS,
                sanitizer=sanitizer,
            ),
        )
        self.issues: dict[str, object] = {}

    def report(self, title: str = "the app crashes on start", *, intake: str = "untriaged") -> str:
        issue = self.world.add_issue(title, at=at(1))
        report = self.store.create(
            ReportDraft(
                title,
                "bug",
                "origin:github",
                "dev/import",
                binding=IssueBinding("", "memory", self.world.locator, issue.url, issue.identifier),
                snapshot=SourceSnapshot(title, "", IssueStatus.OPEN, "r1"),
            )
        )
        for dimension, value in (("origin", "github"), ("intake", intake)):
            self.store.set_state(report, dimension, value, reason="imported", actor="dev/import")
        self.issues[report] = issue
        return report

    def work(self) -> str:
        return self.store.create_work(WorkDraft("fix it", "bug", "dev/t"))

    def reply(self, report: str) -> None:
        self.world.add_comment(self.issues[report], "version 1.2", at=at(2), author="reporter")
        self.intake.ingest_thread(ThreadRequest(report, ACTOR))

    # -- verbs ---------------------------------------------------------------------------------

    def accept(self, report: str, **options):
        return self.intake.dispose(DispositionRequest(report, Disposition.ACCEPT, ACTOR, **options))

    def link(self, report: str, work: str):
        return self.intake.link(LinkRequest(report, work, ACTOR))

    def merged(self, work: str):
        return self.intake.on_work_merged(MergeFact(work, MERGE_SHA, ACTOR))

    # -- reads ---------------------------------------------------------------------------------

    def records(self, report: str, *, open_only: bool = False):
        doc = self.store.read_outbox(report)
        found = [r for r in (doc.records.values() if doc else ()) if r.event is OutboxEvent.LABELS]
        if open_only:
            found = [r for r in found if r.status is RecordStatus.OPEN]
        return found

    def intent(self, report: str):
        (record,) = self.records(report, open_only=True)
        op = record.op(OpKind.LABELS)
        assert op is not None and op.labels is not None
        return op.labels

    def define(self) -> None:
        """The repository defines the projected labels (a sync refuses an undefined add)."""
        plan = self.intake.define_labels(LabelDefineRequest("alice")).plan
        self.intake.define_labels(LabelDefineRequest("alice", approve=plan.short_digest))
        self.tracker.calls.clear()
        self.writes = self.world.write_requests()

    def untouched_tracker(self) -> bool:
        """No tracker call at all since the last ``define`` (or ever), and no write."""
        return not self.tracker.calls and self.world.write_requests() == getattr(self, "writes", 0)


# -- off by default: nothing changes -----------------------------------------------------------


def test_off_by_default_no_transition_drafts_a_labels_record() -> None:
    hive = Hive(on_transition=False)
    report, other = hive.report(), hive.report("a second one")
    work = hive.work()

    hive.accept(report, work=work)
    hive.merged(work)
    hive.intake.unlink(report, actor=ACTOR)
    hive.intake.needs_info(NeedsInfoRequest(other, QUESTION, ACTOR))

    assert hive.records(report) == [] and hive.records(other) == []
    assert hive.intake.last_label_refresh == ()
    assert hive.projection.calls == Counter()


def test_on_without_a_projection_is_inert() -> None:
    hive = Hive(projection=False)
    report = hive.report()

    hive.accept(report)

    assert hive.records(report) == [] and hive.intake.last_label_refresh == ()


# -- on: one record per transition, from stored state, tracker-free ----------------------------


def test_accept_drafts_one_labels_record_without_reading_the_tracker() -> None:
    hive = Hive()
    report = hive.report()
    hive.tracker.calls.clear()

    hive.accept(report)

    (item,) = hive.intake.last_label_refresh
    assert item.report == report and item.drafted and not item.error
    assert [r.outbox_id for r in hive.records(report)] == [item.drafted]
    assert any("accepted" in label for label in hive.intent(report).add)
    assert hive.projection.calls == Counter(refresh=1)
    assert hive.untouched_tracker()  # no listing, no label read, no write


@pytest.mark.parametrize(
    ("disposition", "options", "needle"),
    [
        (Disposition.REJECT, {"reason": "works as designed"}, "rejected"),
        (Disposition.PROMOTE, {}, "promoted"),
    ],
)
def test_reject_and_promote_each_draft_one_record(disposition, options, needle) -> None:
    hive = Hive()
    report = hive.report()

    hive.intake.dispose(DispositionRequest(report, disposition, ACTOR, **options))

    assert len(hive.records(report)) == 1
    assert any(needle in label for label in hive.intent(report).add)
    assert hive.untouched_tracker()


def test_reroute_elsewhere_drafts_one_record() -> None:
    hive = Hive()
    report = hive.report()

    hive.intake.reroute_elsewhere(
        DispositionRequest(report, Disposition.REROUTE, ACTOR), lambda view: "filed elsewhere"
    )

    assert len(hive.records(report)) == 1
    assert any("rerouted" in label for label in hive.intent(report).add)


def test_needs_info_then_wake_refresh_the_one_record_and_never_stack() -> None:
    hive = Hive()
    report = hive.report()

    hive.intake.needs_info(NeedsInfoRequest(report, QUESTION, ACTOR))
    first = hive.records(report, open_only=True)
    assert len(first) == 1 and any("needs-info" in x for x in hive.intent(report).add)

    hive.reply(report)
    woken = hive.intake.wake(WakeRequest(report, ACTOR))

    assert woken.woken
    assert hive.records(report, open_only=True) != first  # refreshed, not kept
    assert len(hive.records(report, open_only=True)) == 1
    assert hive.projection.calls == Counter(refresh=2)


def test_wake_that_wakes_nothing_refreshes_nothing() -> None:
    hive = Hive()
    report = hive.report(intake="needs-info")

    assert not hive.intake.wake(WakeRequest(report, ACTOR)).woken

    assert hive.projection.calls == Counter() and hive.records(report) == []


def test_wake_all_refreshes_each_woken_report_once() -> None:
    hive = Hive()
    woken, waiting = (
        hive.report("one", intake="needs-info"),
        hive.report("two", intake="needs-info"),
    )
    hive.reply(woken)

    sweep = hive.intake.wake_all(actor=ACTOR)

    assert sweep.woken == (woken,)
    assert hive.projection.calls == Counter(refresh=1)
    assert len(hive.records(woken)) == 1 and hive.records(waiting) == []


def test_link_and_unlink_each_refresh_the_one_record() -> None:
    hive = Hive()
    report, work = hive.report(intake="accepted"), hive.work()

    hive.link(report, work)
    assert len(hive.records(report, open_only=True)) == 1
    assert any("pending" in label for label in hive.intent(report).add)

    hive.intake.unlink(report, actor=ACTOR)  # fields fall back to the report's: a new diff
    assert len(hive.records(report, open_only=True)) == 1
    assert hive.projection.calls == Counter(refresh=2)
    assert hive.untouched_tracker()


def test_accept_to_work_refreshes_once_even_though_it_links() -> None:
    hive = Hive()
    report, work = hive.report(), hive.work()

    hive.accept(report, work=work)

    assert hive.projection.calls == Counter(refresh=1)
    assert len(hive.records(report)) == 1
    labels = hive.intent(report).add
    assert any("accepted" in x for x in labels) and any("pending" in x for x in labels)


def test_a_repeated_transition_finds_the_record_instead_of_drafting_another() -> None:
    hive = Hive()
    report, work = hive.report(intake="accepted"), hive.work()

    hive.link(report, work)
    (first,) = hive.records(report)
    hive.link(report, work)  # already linked: the same desired state, the same diff (O2)

    assert [r.outbox_id for r in hive.records(report)] == [first.outbox_id]
    assert hive.intake.last_label_refresh[0].pending == first.outbox_id


def test_a_refresh_keeps_the_fresh_base_of_the_open_sync_draft() -> None:
    hive = Hive()
    owned = "bh:intake/accepted"
    issue = hive.world.add_issue("drifted", at=at(1), labels=("bug", owned))
    report = hive.store.create(
        ReportDraft(
            "drifted",
            "bug",
            "origin:github",
            "dev/import",
            binding=IssueBinding("", "memory", hive.world.locator, issue.url, issue.identifier),
            snapshot=SourceSnapshot("drifted", "", IssueStatus.OPEN, "r1", labels=("bug",)),
        )
    )
    hive.store.set_state(report, "intake", "untriaged", reason="imported", actor="dev/import")
    hive.define()
    hive.intake.sync_labels(LabelSyncRequest("alice", reports=(report,)))
    (synced,) = hive.records(report, open_only=True)
    assert synced.op(OpKind.LABELS).labels.base_owned == {
        owned
    }  # read FRESH, not from the snapshot
    hive.tracker.calls.clear()

    hive.intake.dispose(DispositionRequest(report, Disposition.PROMOTE, ACTOR))

    (record,) = hive.records(report, open_only=True)
    assert record.outbox_id != synced.outbox_id
    assert record.op(OpKind.LABELS).labels.base_owned == {owned}  # the refresh built on it
    assert owned in hive.intent(report).remove  # and removes the drifted label, not re-adds it
    assert hive.untouched_tracker()


# -- the milestone triggers --------------------------------------------------------------------


def test_merge_and_release_each_refresh_the_one_record() -> None:
    hive = Hive()
    report, work = hive.report(intake="accepted"), hive.work()
    hive.link(report, work)
    before = hive.records(report, open_only=True)

    merged = hive.merged(work)
    assert merged.reports == (report,)
    after_merge = hive.records(report, open_only=True)
    assert len(after_merge) == 1 and after_merge != before
    assert any("merged" in label for label in hive.intent(report).add)

    released = hive.intake.on_release(ReleaseFact("v1", RELEASE_SHA, ACTOR))
    assert released.reports == (report,)
    assert len(hive.records(report, open_only=True)) == 1
    assert any("released" in label for label in hive.intent(report).add)
    assert hive.untouched_tracker()


def test_started_and_delivery_notices_refresh_the_one_record() -> None:
    hive = Hive()
    report, work = hive.report(intake="accepted"), hive.work()
    hive.link(report, work)
    refreshes = hive.projection.calls["refresh"]

    hive.intake.on_work_started(StartFact(work, ACTOR))
    assert len(hive.records(report, open_only=True)) == 1

    hive.intake.delivery_notice(NoticeFact(work, OutboxEvent.BLOCKED, WAITING, "alice"))

    assert len(hive.records(report, open_only=True)) == 1
    assert hive.projection.calls["refresh"] == refreshes + 2
    assert any("blocked" in label for label in hive.intent(report).add)


def test_a_milestone_that_reaches_no_report_refreshes_nothing() -> None:
    hive = Hive()

    outcome = hive.merged(hive.work())

    assert outcome.items == () and hive.projection.calls == Counter()


# -- never fails the transition ----------------------------------------------------------------


def test_a_report_that_cannot_be_refreshed_is_noted_and_the_transition_stands() -> None:
    hive = Hive()
    report = hive.report()
    born_native = hive.store.create(ReportDraft("internal", "bug", "origin:report", "x"))
    hive.store.set_state(born_native, "intake", "untriaged", reason="filed", actor="x")

    hive.accept(born_native)

    (item,) = hive.intake.last_label_refresh
    assert item.report == born_native and "not bound" in item.note and not item.drafted
    assert hive.accept(report) is not None


def test_a_refresh_with_the_outbox_labels_gate_off_is_an_item_error_not_a_failure() -> None:
    hive = Hive()
    report = hive.report()
    off = SpyProjection(
        hive.store,
        hive.tracker,
        hive.lock,
        ids=hive.ids,
        registry={},
        settings=LabelSettings(),
        leak_policy=LEAKS,
        sanitizer=RecordingSanitizer.of([]),
        posture=PRIVATE,
        outbox=OutboxPolicy(enabled=True),
    )
    service = IntakeService(hive.store, _NoLabels(), projection=off, refresh_on_transition=True)

    outcome = service.dispose(DispositionRequest(report, Disposition.PROMOTE, ACTOR))

    assert outcome.state
    (item,) = service.last_label_refresh
    assert item.error and hive.records(report) == []


def test_a_refused_transition_refreshes_nothing() -> None:
    hive = Hive()
    report = hive.report(intake="accepted")

    with pytest.raises(IntakeRefused):
        hive.accept(report)  # not untriaged

    assert hive.projection.calls == Counter()


# -- apply_reviewed keeps its own refresh --------------------------------------------------------


def test_apply_reviewed_does_not_double_refresh_the_reports_it_touches() -> None:
    hive = Hive()
    hive.define()
    touched = hive.report("a crash")
    hive.intake.propose(ProposeRequest(touched, {"disposition": "new", "type": "bug"}, "dev/a"))
    hive.intake.review_proposal(ProposalReviewRequest(touched, {"verdict": "confirmed"}, "dev/v"))

    outcome = hive.intake.apply_reviewed(ApplyRequest(ACTOR))

    assert outcome.applied == (touched,)
    assert hive.projection.calls == Counter(sync=1)  # apply's own fresh refresh, once
    assert len(hive.records(touched)) == 1
    assert hive.world.write_requests() == hive.writes  # drafted, never sent
    # the deferral ends with the call: the next transition refreshes again
    other = hive.report("another")
    hive.accept(other)
    assert hive.projection.calls == Counter(sync=1, refresh=1)


def test_null_intake_is_unchanged() -> None:
    assert not hasattr(NullIntake(), "last_label_refresh")
