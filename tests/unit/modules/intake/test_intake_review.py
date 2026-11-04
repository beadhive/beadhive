"""Review packets (bh-hrx2r) over MemoryWorld: ``review_packets`` and ``review_metrics``.

``IntakeService`` with the thread service wired as the root wires it, over ``MemoryReportStore``,
``MemoryTracker`` / ``MemoryWorld`` and the never-waiting ``InProcessIntakeLock``. It pins the
bead's acceptance: three duplicates spread across the number range land in ONE batch; every packet
carries the full thread (ingested first) and the global index; with comment ingestion off review
refuses and names both switches, reading nothing; the metrics come from the stored reviews and are
deterministic. Asserted on store write counters and tracker read counters only: no clock, no
sleep, no socket.
"""

from __future__ import annotations

import pytest

from beadhive.modules.intake.application import IntakeService, NullIntake, ThreadService
from beadhive.modules.intake.domain import (
    COMMENTS_ENABLED_KEY,
    COMMENTS_READ_KEY,
    INTAKE_UNTRIAGED,
    IntakeRefused,
    IssueBinding,
    IssueStatus,
    OwnIdentity,
    PassMetrics,
    Proposal,
    ProposalDisposition,
    ProposalTarget,
    RefusalReason,
    ReportDraft,
    ReviewMetrics,
    ReviewPolicy,
    ReviewRequest,
    ReviewVerdict,
    SourceSnapshot,
    StorePosture,
    WorkDraft,
    add_review,
    mark_applied,
    new_proposal,
)
from beadhive.modules.tracker.contracts import READ_OPERATIONS
from beadhive.testing.intake import SCOPE, InProcessIntakeLock, MemoryReportStore
from beadhive.testing.tracker import MemoryTracker, MemoryWorld, at

ME = OwnIdentity("acme/widgets", "inst-1")
ON = ReviewPolicy(comments_enabled=True, comments_read=True)
SEAT = "dev/verifier"

#: Unrelated titles: no two share a word, so neither the store nor text similarity joins them.
WORDS = (
    "apple bridge candle dragon engine falcon garden harbor island jungle kettle lantern "
    "meadow needle orchid pepper quartz river saddle tunnel umbrella violin walnut xylophone "
    "yacht zipper anchor blanket canyon dolphin ember fossil glacier helmet igloo jasmine "
    "kayak lemon marble nectar oyster pillow quiver rocket summit thistle unicorn velvet "
    "willow yarrow zenith acorn beacon cactus desert eagle fabric goblet hammock iris jigsaw"
).split()
#: A family filed three times, far apart in the number range.
FAMILY = {
    2: "Export writes an empty archive",
    15: "export writes empty archive on windows",
    29: "Empty archive when export writes",
}


class _NoLabels:
    def violations(self, labels) -> tuple[str, ...]:
        return ()


class Scene:
    """Bound reports on memory issues, with comment ingestion wired as ``policy`` says."""

    def __init__(self, *, policy: ReviewPolicy = ON, threads: bool = True) -> None:
        self.world = MemoryWorld()
        self.tracker = MemoryTracker(self.world, enabled=frozenset(READ_OPERATIONS))
        self.store = MemoryReportStore(SCOPE)
        self.threads = ThreadService(
            self.store,
            self.tracker,
            InProcessIntakeLock(),
            posture=StorePosture(published=False),
            identity=ME,
        )
        self.intake = IntakeService(
            self.store, _NoLabels(), threads=self.threads if threads else None, review=policy
        )
        self.issues: dict[str, object] = {}

    def report(self, title: str, body: str = "") -> str:
        issue = self.world.add_issue(title, at=at(1))
        report = self.store.create(
            ReportDraft(
                title,
                "bug",
                "origin:github",
                "dev/import",
                binding=IssueBinding("", "memory", self.world.locator, issue.url, issue.identifier),
                snapshot=SourceSnapshot(title, body, IssueStatus.OPEN, "r1"),
            )
        )
        for dimension, value in (("origin", "github"), ("intake", "untriaged")):
            self.store.set_state(report, dimension, value, reason="imported", actor="dev/import")
        self.issues[report] = issue
        return report

    def native(self, title: str) -> str:
        report = self.store.create(ReportDraft(title, "bug", "origin:report", "dev/x"))
        self.store.set_state(report, "intake", "untriaged", reason="filed", actor="dev/x")
        return report

    def comment(self, report: str, body: str, *, day: int = 2, author: str = "alice") -> None:
        self.world.add_comment(self.issues[report], body, at=at(day), author=author)

    def propose(self, report: str, proposal: Proposal) -> None:
        self.store.write_proposal(report, proposal, expect_seq=0, actor=SEAT)

    def review(self, batch_size: int = 10):
        return self.intake.review_packets(ReviewRequest("dev/triage", batch_size=batch_size))


def thirty(scene: Scene) -> list[str]:
    """Thirty bound reports, issues #1 to #30; the FAMILY's three sit at #3, #16 and #30."""
    return [
        scene.report(FAMILY.get(n, f"{WORDS[2 * n]} {WORDS[2 * n + 1]}"), body=f"body {n}")
        for n in range(30)
    ]


def reviewed(disposition: ProposalDisposition, *verdicts: ReviewVerdict, **fields) -> Proposal:
    proposal = new_proposal(disposition, **fields)
    for verdict in verdicts:
        corrections = ("the claim is wrong",) if verdict is ReviewVerdict.CORRECTED else ()
        proposal = add_review(proposal, SEAT, verdict, corrections)
    return proposal


# -- the acceptance --------------------------------------------------------------------------


def test_three_duplicates_spread_across_the_number_range_land_in_one_batch() -> None:
    s = Scene()
    reports = thirty(s)
    family = {reports[n] for n in FAMILY}

    run = s.review(batch_size=10)

    holding = [b.number for b in run.batches if family & set(b.reports)]
    assert len(holding) == 1
    assert family <= set(run.batches[holding[0] - 1].reports)
    assert all(len(b.reports) <= 10 for b in run.batches)
    assert sorted(run.reports) == sorted(reports)  # every pending report in exactly one batch
    (joined,) = [c for c in run.clusters if c.size > 1]
    assert set(joined.members) == family and joined.key == reports[2]
    packet = next(p for b in run.batches for p in b.packets if p.report == reports[29])
    assert packet.cluster == reports[2] and set(packet.cluster_members) == family
    assert packet.links and all(reports[29] in (k.first, k.second) for k in packet.links)


def test_review_ingests_every_thread_first_and_every_packet_carries_it_and_the_index() -> None:
    s = Scene()
    reports = thirty(s)
    s.comment(reports[3], "still broken on v0.31", author="reporter")
    s.comment(reports[3], "and on v0.32 too", day=3, author="reporter")
    assert all(s.store.read_thread(r) is None for r in reports)  # nothing ingested yet

    run = s.review()

    assert run.ingested == tuple(reports)  # issue-number order
    packets = [p for b in run.batches for p in b.packets]
    assert len(packets) == 30 and len(run.index) == 30
    for packet in packets:
        assert packet.thread is not None and packet.thread_complete
        assert packet.index == run.index
        assert packet.body == f"body {reports.index(packet.report)}"
    thread = next(p.thread for p in packets if p.report == reports[3])
    bodies = sorted(r.body for r in thread.thread.records.values())
    assert bodies == ["and on v0.32 too", "still broken on v0.31"]
    assert thread.new_evidence  # the reporter's comments are evidence triage has not read
    assert s.store.read_thread(reports[3]) == thread.thread  # stored, not just packed
    assert [e.report for e in run.index] == list(reports)
    assert {e.reference for e in run.index} == {f"#{n}" for n in range(1, 31)}

    writes = len(s.store.thread_writes)
    again = s.review()  # nothing new on the tracker: the second ingest writes nothing
    assert len(s.store.thread_writes) == writes
    assert again.index == run.index


def test_packets_carry_the_proposal_and_review_history_and_the_index_its_disposition() -> None:
    s = Scene()
    first, second = s.report("crash on start"), s.report("dark mode colours wrong")
    s.propose(
        first,
        reviewed(
            ProposalDisposition.TRACKED,
            ReviewVerdict.CORRECTED,
            ReviewVerdict.CONFIRMED,
            targets=(ProposalTarget("acme-0099"),),
        ),
    )

    run = s.review()

    packet = next(p for p in run.batches[0].packets if p.report == first)
    assert packet.proposal is not None
    assert [r.verdict for r in packet.proposal.reviews] == [
        ReviewVerdict.CORRECTED,
        ReviewVerdict.CONFIRMED,
    ]
    entries = {e.report: e for e in run.index}
    assert (entries[first].disposition, entries[first].status) == ("tracked", "verified")
    assert (entries[second].disposition, entries[second].status) == ("", "")
    assert next(p for p in run.batches[0].packets if p.report == second).proposal is None


@pytest.mark.parametrize(
    ("policy", "threads"),
    [
        (ReviewPolicy(), False),  # the default: both off
        (ReviewPolicy(comments_enabled=True, comments_read=False), True),  # comments.read off
        (ReviewPolicy(comments_enabled=False, comments_read=True), True),
        (ON, False),  # the policy says on, but no thread service is wired
    ],
)
def test_with_comments_off_review_refuses_naming_both_keys_and_reads_nothing(
    policy: ReviewPolicy, threads: bool
) -> None:
    s = Scene(policy=policy, threads=threads)
    report = s.report("crash on start")
    s.comment(report, "more detail")
    reads, writes = s.world.reads, s.store.writes

    with pytest.raises(IntakeRefused) as refused:
        s.review()

    assert refused.value.reason is RefusalReason.INTAKE_DISABLED
    assert COMMENTS_ENABLED_KEY in refused.value.detail
    assert COMMENTS_READ_KEY in refused.value.detail
    assert COMMENTS_READ_KEY == "plugins.tracker-github.read.comments.enabled"
    assert (s.world.reads, s.store.writes) == (reads, writes)
    assert s.store.read_thread(report) is None


def test_the_pending_set_is_untriaged_or_woken_without_an_applied_proposal() -> None:
    s = Scene()
    untriaged = s.report("crash on start")
    woken = s.report("dark mode colours wrong")
    waiting = s.report("slow sync over vpn")
    applied = s.report("typo in the readme")
    closed = s.report("login button misaligned")
    native = s.native("escalated: hook loops forever")
    work = s.store.create_work(WorkDraft("fix the crash", "bug", "dev/x"))
    for report in (woken, waiting):
        s.store.set_state(report, "intake", "needs-info", reason="asked", actor="dev/x")
    s.comment(woken, "here is the log you asked for", author="reporter")
    done = mark_applied(
        reviewed(
            ProposalDisposition.TRACKED,
            ReviewVerdict.CONFIRMED,
            targets=(ProposalTarget(work),),
        )
    )
    s.propose(applied, done)
    s.store.close(closed, reason="gone", actor="dev/x")

    run = s.review()

    assert run.reports == (untriaged, woken, native)
    assert run.ingested == (untriaged, woken, waiting)  # every bound candidate, applied excluded
    packets = {p.report: p for p in run.batches[0].packets}
    assert packets[native].thread is None and packets[native].thread_complete
    assert packets[woken].thread is not None and packets[woken].thread.new_evidence
    assert INTAKE_UNTRIAGED not in packets[woken].labels  # review never wakes: it only reads


# -- metrics -----------------------------------------------------------------------------------


def test_metrics_are_computed_from_stored_reviews_and_are_deterministic() -> None:
    s = Scene()
    a, b, c, d, e = (s.report(t) for t in ("crash", "colours", "sync", "readme", "login"))
    s.propose(
        a,
        reviewed(
            ProposalDisposition.FIXED,
            ReviewVerdict.CORRECTED,
            ReviewVerdict.CONFIRMED,
            fix_commit="abc1234",
        ),
    )
    s.propose(b, reviewed(ProposalDisposition.NEW, ReviewVerdict.CONFIRMED))
    s.propose(
        c,
        reviewed(ProposalDisposition.REJECT, ReviewVerdict.CONFIRMED, reporter_text="By design."),
    )
    s.propose(d, reviewed(ProposalDisposition.NEEDS_INFO, ReviewVerdict.UNSURE))
    s.propose(e, reviewed(ProposalDisposition.TRACKED))  # never reviewed

    reads = s.world.reads
    metrics = s.intake.review_metrics()

    assert s.world.reads == reads  # metrics read the store only
    assert metrics == ReviewMetrics(
        proposals=5,
        reviewed=4,
        passes=(
            PassMetrics(1, reviewed=4, confirmed=2, corrected=1, unsure=1),
            PassMetrics(2, reviewed=1, confirmed=1),
        ),
        reporter_visible=3,
        reporter_visible_errors=1,
    )
    assert [p.correction_rate for p in metrics.passes] == [0.25, 0.0]
    assert metrics.reporter_visible_error_rate == pytest.approx(1 / 3)
    assert s.intake.review_metrics() == metrics  # the same stored reviews, the same numbers
    assert s.review().metrics == metrics
    assert ReviewMetrics().reporter_visible_error_rate == 0.0
    assert PassMetrics(1).correction_rate == 0.0


def test_metrics_need_no_comment_switch_and_null_intake_reads_nothing() -> None:
    s = Scene(policy=ReviewPolicy(), threads=False)
    report = s.report("crash")
    s.propose(report, reviewed(ProposalDisposition.FIXED, ReviewVerdict.CORRECTED))
    assert s.intake.review_metrics().reporter_visible_errors == 1

    null = NullIntake()
    assert null.review_metrics() == ReviewMetrics()
    with pytest.raises(IntakeRefused) as refused:
        null.review_packets(ReviewRequest("dev/x"))
    assert refused.value.reason is RefusalReason.INTAKE_DISABLED


def test_a_batch_size_below_one_is_refused() -> None:
    s = Scene()
    with pytest.raises(IntakeRefused) as refused:
        s.review(batch_size=0)
    assert refused.value.reason is RefusalReason.INVALID_REQUEST
