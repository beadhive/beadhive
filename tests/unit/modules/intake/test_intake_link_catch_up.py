"""Linking a report to work that already merged (bh-61xkc): the report catches up, drafts only.

Without this a duplicate linked after its canonical work landed stays ``delivery:pending``
forever. With ``catch_up_on_link`` (``intake.external.milestones.catch_up_on_link``, off by
default) the link runs the merge trigger's own steps for that one report, using the merge (and the
verified release) another tracking report recorded: the merged fact, ``delivery:merged``, a
``merged`` record when drafting is on, then ``delivery:released`` and a ``released`` record when
the release was verified, then the completion policy. Nothing sends: ``write_requests()`` stays 0.
"""

from __future__ import annotations

from beadhive.modules.intake.application import IntakeService, MilestoneService
from beadhive.modules.intake.domain import (
    CompletionPolicy,
    Disposition,
    DispositionRequest,
    IssueBinding,
    IssueStatus,
    LeakPolicy,
    LinkRequest,
    MergeFact,
    MilestonePolicy,
    OpKind,
    OpState,
    OutboxEvent,
    OutboxPolicy,
    ReleaseFact,
    ReportDraft,
    SourceSnapshot,
    StorePosture,
    Verdict,
    WorkDraft,
    delivery_of,
)
from beadhive.testing.intake import (
    SCOPE,
    FakeReleaseVerifier,
    InProcessIntakeLock,
    MemoryReportStore,
    RecordingSanitizer,
    SequentialIdSource,
)
from beadhive.testing.tracker import MemoryWorld, at

LEAKS = LeakPolicy(bead_prefixes=frozenset({"acme", "pub"}))
MERGE_SHA = "a" * 40
RELEASE_SHA = "d" * 40
C = OpKind.COMMENT
DRAFT_BOTH_KEEP_OPEN = MilestonePolicy(
    draft_on_merge=True, draft_on_release=True, completion=CompletionPolicy.CLOSE_ON_RELEASE
)


class _NoLabels:
    def violations(self, labels) -> tuple[str, ...]:
        return ()


class Scene:
    def __init__(
        self,
        *,
        catch_up: bool = True,
        outbox: OutboxPolicy = OutboxPolicy(enabled=True),  # noqa: B008 - frozen value
        milestones: MilestonePolicy = DRAFT_BOTH_KEEP_OPEN,
    ) -> None:
        self.world = MemoryWorld()
        self.store = MemoryReportStore(SCOPE)
        self.lock = InProcessIntakeLock()
        self.verifier = FakeReleaseVerifier(
            tags={"v1.0.0": RELEASE_SHA}, contained={RELEASE_SHA: frozenset({MERGE_SHA})}
        )
        self.milestones = MilestoneService(
            self.store,
            self.lock,
            ids=SequentialIdSource(),
            leak_policy=LEAKS,
            sanitizer=RecordingSanitizer.of(["s3cr3t"]),
            posture=StorePosture(published=False),
            outbox=outbox,
            milestones=milestones,
            verifier=self.verifier,
        )
        self.intake = IntakeService(
            self.store,
            _NoLabels(),
            lock=self.lock,
            milestones=self.milestones,
            catch_up_on_link=catch_up,
        )
        self.work = self.store.create_work(WorkDraft("fix the crash", "bug", "dev/t"))

    def report(self, *, intake: str = "accepted") -> str:
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
        self.store.set_state(report, "intake", intake, reason="triaged", actor="dev/t")
        return report

    def link(self, report: str):
        return self.intake.link(LinkRequest(report, self.work, "dev/triage"))

    def land(self) -> None:
        """The canonical report is linked, then the work merges (the merge trigger runs)."""
        self.canonical = self.report()
        self.link(self.canonical)
        self.intake.on_work_merged(MergeFact(self.work, MERGE_SHA, "merger/t"))
        self.store.mark(self.work, status="closed")

    def release(self) -> None:
        self.intake.on_release(ReleaseFact("v1.0.0", RELEASE_SHA, "operator"))

    def delivery(self, report: str) -> str | None:
        return delivery_of(self.store.get(report).labels)

    def events(self, report: str) -> list[OutboxEvent]:
        doc = self.store.read_outbox(report)
        return [r.event for r in (doc.records.values() if doc else ())]

    def record(self, report: str, event: OutboxEvent):
        (record,) = [r for r in self.store.read_outbox(report).records.values() if r.event is event]
        return record


def test_a_duplicate_linked_after_the_merge_catches_up_and_drafts_the_merged_milestone() -> None:
    s = Scene()
    s.land()
    late = s.report()
    outcome = s.link(late)
    assert s.delivery(late) == "merged"
    (item,) = outcome.milestones
    assert item.delivery == "merged" and item.drafted and not item.error
    assert s.events(late) == [OutboxEvent.MERGED]
    assert s.record(late, OutboxEvent.MERGED).op(C).state is OpState.DRAFTED
    doc = s.store.read_outbox(late)
    assert (doc.facts.merged.work, doc.facts.merged.commit) == (s.work, MERGE_SHA)
    assert s.store.get(late).status == "open"  # close-on-release: merged but unreleased stays open
    assert s.world.write_requests() == 0
    # a re-link is idempotent: nothing is drafted or written twice
    writes = s.store.writes
    again = s.link(late)
    assert s.events(late) == [OutboxEvent.MERGED] and s.store.writes == writes
    assert all(not i.drafted for i in again.milestones)


def test_a_duplicate_linked_after_a_verified_release_drafts_the_released_milestone() -> None:
    s = Scene()
    s.land()
    s.release()
    assert s.delivery(s.canonical) == "released"
    late = s.report()
    outcome = s.link(late)
    assert s.delivery(late) == "released"
    assert [i.event for i in outcome.milestones] == ["merged", "released"]
    assert outcome.milestones[1].verdict is Verdict.YES
    # the released record supersedes the unsent merged one, as the release trigger's does
    assert s.record(late, OutboxEvent.MERGED).op(C).state is OpState.SUPERSEDED
    assert s.record(late, OutboxEvent.RELEASED).op(C).state is OpState.DRAFTED
    assert s.store.read_outbox(late).facts.released.tag == "v1.0.0"
    assert s.verifier.asked == [(RELEASE_SHA, MERGE_SHA)]  # only the trigger's own question
    assert s.world.write_requests() == 0


def test_an_unverified_release_is_never_assumed() -> None:
    s = Scene()
    s.verifier.contained = {}  # the release cannot be shown to contain the merge
    s.land()
    s.release()
    assert s.delivery(s.canonical) == "merged"
    late = s.report()
    outcome = s.link(late)
    assert s.delivery(late) == "merged" and [i.event for i in outcome.milestones] == ["merged"]


def test_catch_up_is_off_by_default_and_the_report_stays_pending() -> None:
    s = Scene(catch_up=False)
    s.land()
    late = s.report()
    outcome = s.link(late)
    assert s.delivery(late) == "pending" and outcome.milestones == ()
    assert s.events(late) == []


def test_open_work_and_work_closed_without_a_recorded_merge_change_nothing() -> None:
    s = Scene()
    early = s.report()
    assert s.link(early).milestones == () and s.delivery(early) == "pending"
    s.store.mark(s.work, status="closed")  # closed, but no tracking report recorded a merge
    late = s.report()
    (item,) = s.link(late).milestones
    assert "merge commit is unknown" in item.note and not item.delivery
    assert s.delivery(late) == "pending"


def test_drafting_off_still_moves_delivery_and_runs_the_completion_policy() -> None:
    s = Scene(outbox=OutboxPolicy(), milestones=MilestonePolicy())  # close-on-merge, no outbox
    s.land()
    assert s.store.get(s.canonical).status == "closed"
    late = s.report()
    (item,) = s.link(late).milestones
    assert s.delivery(late) == "merged" and item.closed and not item.drafted
    assert s.store.get(late).status == "closed"


def test_accept_with_work_catches_up_after_the_intake_clear() -> None:
    s = Scene(outbox=OutboxPolicy(), milestones=MilestonePolicy())  # close-on-merge, no outbox
    s.land()
    late = s.report(intake="untriaged")
    outcome = s.intake.dispose(
        DispositionRequest(late, Disposition.ACCEPT, "dev/triage", work=s.work)
    )
    assert outcome.state == "accepted" and s.delivery(late) == "merged"
    (item,) = outcome.milestones
    assert item.closed and s.store.get(late).status == "closed"


def test_a_plain_link_of_a_report_still_awaiting_triage_never_closes_it() -> None:
    s = Scene(outbox=OutboxPolicy(), milestones=MilestonePolicy())  # close-on-merge, no outbox
    s.land()
    for intake in ("untriaged", "needs-info"):
        late = s.report(intake=intake)
        (item,) = s.link(late).milestones
        assert s.delivery(late) == "merged" and not item.closed
        assert s.store.get(late).status == "open"
