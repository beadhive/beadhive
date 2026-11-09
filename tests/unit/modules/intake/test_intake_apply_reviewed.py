"""Propose, review and apply-reviewed (bh-9aqmv) end to end on memory fakes.

``IntakeService`` wired as the root wires it (thread service, supervised sender, the never-waiting
``InProcessIntakeLock``) over ``MemoryReportStore`` and ``MemoryTracker`` / ``MemoryWorld``, with a
scripted ``FakeRepoFacts`` and ``FakeLandedCommits``. The centre is a RECORDED fixture: the
2026-11-04 single-pass triage of beadhive's own imported reports, replayed. Its unverified
proposals, the five reporter-visible errors (#3, #11, #14, #25, #110) among them, cannot reach
apply; after the verifier pass the verified set applies canonicals first, drafts (and never
sends) every reply and question, lists the decision, and a second apply changes nothing.
Asserted on store write counters and ``write_requests()`` only: no clock, no sleep, no socket.
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from beadhive.modules.intake.application import (
    IntakeService,
    LabelProjection,
    MilestoneService,
    NullIntake,
    OutboxService,
    StoreOutboxView,
    ThreadService,
    TrackerProjectIssues,
    TriageBinding,
)
from beadhive.modules.intake.domain import (
    CLOSE_REASON_LIMIT,
    INTAKE_NEEDS_INFO,
    INTAKE_UNTRIAGED,
    ApplyOutcome,
    ApplyRequest,
    ApplyStatus,
    ApplyStep,
    CanonicalIssue,
    CheckKind,
    CloseBackfill,
    CloseReason,
    CompletionPolicy,
    ConsiderRequest,
    Disposition,
    DispositionRequest,
    IntakeRefused,
    IssueBinding,
    IssueStatus,
    LabelSettings,
    LeakPolicy,
    LinkRequest,
    MergeFact,
    MilestonePolicy,
    OpKind,
    OpState,
    OutboxEvent,
    OutboxPolicy,
    OwnIdentity,
    Proposal,
    ProposalDisposition,
    ProposalReviewRequest,
    ProposalStatus,
    ProposeKind,
    ProposeRequest,
    RefusalReason,
    RegistryDimension,
    ReplyDraft,
    ReportDraft,
    ReviewPolicy,
    ReviewRequest,
    SourceSnapshot,
    StorePosture,
    ThreadRequest,
    WakeRequest,
    WorkDraft,
    mark_applied,
    reopen_text,
    version_key,
)
from beadhive.modules.tracker.contracts import (
    READ_OPERATIONS,
    WRITE_OPERATIONS,
    IssueState,
    OwnMarker,
)
from beadhive.testing.intake import (
    SCOPE,
    FakeLandedCommits,
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
ANALYST = "dev/analyst"
VERIFIER = "dev/verifier"
TRIAGE = "dev/triage"
D = ProposalDisposition

# -- the recorded fixture: the 2026-11-04 first pass and its verification --------------------

#: The first pass as recorded (the triage rows of 2026-11-04): issue number -> (disposition,
#: targets, claimed fixed_in, type, priority). ``unclear`` was recorded for #30 and #40 and is
#: replayed as ``needs-info`` with no question (the pass wrote none); ``reroute-upstream`` (#11)
#: as ``reroute``. A tracked or fixed row's targets are owners, any other row's are related.
FIRST_PASS: dict[int, tuple[str, tuple[str, ...], str, str, str]] = {
    3: ("fixed", ("3nuo", "c3nf"), "v0.8.0", "", ""),
    7: ("tracked", ("j9jkq.4", "gss7m.4", "z9g3r"), "", "", ""),
    11: ("reroute", (), "", "", ""),
    14: ("reject", ("yzwts",), "", "", ""),
    22: ("new", ("iuqxu",), "", "bug", "2"),
    25: ("fixed", ("a80nx", "j9jkq.4"), "v0.28.1", "", ""),
    30: ("needs-info", (), "", "", ""),
    35: ("new", (), "", "bug", "1"),
    40: ("needs-info", ("gj0v9.6",), "", "", ""),
    44: ("new", (), "", "bug", "2"),
    50: ("new", (), "", "feature", "2"),
    56: ("new", (), "", "bug", "2"),
    87: ("new", ("r8el", "2642a"), "", "bug", "1"),
    90: ("new", ("iuqxu",), "", "feature", "2"),
    93: ("tracked", ("e5kex",), "", "", ""),
    110: ("fixed", ("jbhhe", "0qazu"), "v0.24.3", "", ""),
    118: ("new", (), "", "bug", "2"),
    132: ("new", (), "", "bug", "2"),
}
#: The five reporter-visible errors of the first pass: false "fixed" on #3, #25 and #110, a false
#: reject on #14, and #11 sent upstream when the right answer was a reasoned reject.
REPORTER_VISIBLE_ERRORS = (3, 11, 14, 25, 110)

#: Every work bead the rows name, as it stood that day: status and, for a closed one, the commits
#: that landed it.
WORK: dict[str, tuple[str, tuple[str, ...]]] = {
    "3nuo": ("closed", ("9b1324f8",)),
    "c3nf": ("closed", ("c3f0e1a2",)),
    "j9jkq.4": ("open", ()),
    "gss7m.4": ("open", ()),
    "z9g3r": ("open", ()),
    "yzwts": ("closed", ()),
    "iuqxu": ("open", ()),
    "a80nx": ("closed", ("84b01887",)),
    "gj0v9.6": ("closed", ()),
    "r8el": ("closed", ()),
    "2642a": ("open", ()),
    "e5kex": ("open", ()),
    "jbhhe": ("closed", ("67dcd7dd",)),
    "0qazu": ("open", ()),
}
#: The release that first contained each landed commit, as the repository computes it.
RELEASED_IN = {
    "9b1324f8": "v0.7.0",
    "c3f0e1a2": "v0.7.0",
    "67dcd7dd": "v0.24.3",
    "84b01887": "v0.28.1",
}

REPLY_11 = (
    "Thanks for the clear write-up. This was a bug in the bd tool itself rather than in bh. In bd "
    "1.3.0, `bd gate list <issue-id>` lists only the gates that block that issue, so the unscoped "
    "listing you saw no longer occurs. Please upgrade bd. There is nothing to change in bh."
)
QUESTION_30 = (
    "Thanks for the report. We couldn't reproduce the hang from the code. Could you retry on bh "
    "0.34.0 with the loop's output redirected to a file, and let us know whether it still hangs "
    "and which bd or dolt processes are running when it does?"
)


def _member(canonical: int, kind: str, priority: str) -> dict:
    return {"disposition": "duplicate-of", "share": canonical, "type": kind, "priority": priority}


#: The verifier pass (corrections of 2026-11-04): issue -> (verdict, correction, adopted content).
#: ``share`` names the canonical by issue, ``owner``/``related`` the work by name; ``read_thread``
#: records the thread seq the verifier read. #30's question is recorded as the adopted correction
#: (the pass wrote the question beside its confirmation); #110's correction carries its crash-fix
#: version as evidence, since a tracked proposal claims no fix.
VERIFIER_PASS: dict[int, tuple[str, str, dict | None]] = {
    3: (
        "corrected",
        "Only the false-success half was fixed (in v0.7.0); a subset group merge still leaves "
        "members open.",
        {"disposition": "new", "type": "bug", "priority": "2"},
    ),
    7: (
        "corrected",
        "The retry and remote-behind marker gap is the same as #25: one shared work bead.",
        {**_member(25, "task", "3"), "related": ("j9jkq.4",)},
    ),
    11: (
        "corrected",
        "Fixed upstream in bd 1.3.0: a reasoned reject, not a reroute.",
        {"disposition": "reject", "reporter_text": REPLY_11},
    ),
    14: (
        "corrected",
        "Do NOT reject: the reporter's later comment shows hosts whose local main is behind.",
        {
            "disposition": "new",
            "related": ("yzwts",),
            "type": "feature",
            "priority": "2",
            "read_thread": True,
        },
    ),
    22: (
        "corrected",
        "Canonical work for #22 and #90; the named bead is unrelated.",
        {"disposition": "new", "type": "bug", "priority": "2"},
    ),
    25: (
        "corrected",
        "Partly fixed in v0.28.1; a bounded retry and a remote-behind marker are still missing. "
        "Canonical for #7.",
        {"disposition": "new", "related": ("j9jkq.4", "a80nx"), "type": "task", "priority": "3"},
    ),
    30: (
        "corrected",
        "Needs the reporter: ask them to retry on 0.34.0.",
        {"disposition": "needs-info", "reporter_text": QUESTION_30},
    ),
    35: ("confirmed", "", None),
    40: ("unsure", "", None),
    44: ("corrected", "Same close-ladder root cause as #87.", _member(87, "bug", "1")),
    50: ("corrected", "Same defect as #35: shares its work bead.", _member(35, "bug", "1")),
    56: ("confirmed", "", None),
    87: ("confirmed", "", None),
    90: ("corrected", "Shares #22's work bead.", _member(22, "feature", "2")),
    93: ("confirmed", "", None),
    110: (
        "corrected",
        "The crash was fixed in v0.24.3; the resumability ask lives in open work.",
        {
            "disposition": "tracked",
            "owner": ("0qazu",),
            "evidence": ("the crash fix shipped in v0.24.3",),
        },
    ),
    118: ("corrected", "Shares #87's close-ladder work bead.", _member(87, "bug", "1")),
    132: ("corrected", "Shares #87's close-ladder work bead.", _member(87, "bug", "1")),
}
#: The canonicals of the verified set and their members.
FAMILIES = {25: (7,), 22: (90,), 35: (50,), 87: (44, 118, 132)}


def marker_line(marker: OwnMarker) -> str:
    return f"<!-- bh:v1 hive={marker.hive} install={marker.install} outbox={marker.outbox_id} -->"


class _NoLabels:
    def violations(self, labels) -> tuple[str, ...]:
        return ()


def _repo(commits: Mapping[str, str]) -> FakeRepoFacts:
    """Every landed commit on ``main``; each release contains its own and every earlier one."""
    tags = sorted(set(commits.values()), key=version_key)
    releases = tuple(
        (tag, frozenset(c for c, t in commits.items() if version_key(t) <= version_key(tag)))
        for tag in tags
    )
    return FakeRepoFacts(
        branches={"main": frozenset(commits)}, releases=releases, unchanged_by_default=True
    )


class _CountingIssues:
    """``ProjectIssues`` that counts its listings (one per run, never one per mention)."""

    def __init__(self, inner) -> None:
        self.inner, self.calls = inner, 0

    def identifiers(self):
        self.calls += 1
        return self.inner.identifiers()


class Hive:
    """One hive's intake wired over memory fakes, with propose/review/apply bound."""

    def __init__(
        self,
        *,
        commits: Mapping[str, str] = RELEASED_IN,
        triage: bool = True,
        refile=None,
        labels: bool = False,
        milestones: bool = False,
        outbox: OutboxPolicy = OUTBOX,
        sender: bool = True,
        issues: bool = True,
    ) -> None:
        self.world = MemoryWorld()
        self.world.set_write_time(at(3))
        self.tracker = MemoryTracker(
            self.world, enabled=frozenset(READ_OPERATIONS | WRITE_OPERATIONS)
        )
        self.store = MemoryReportStore(SCOPE)
        self.lock = InProcessIntakeLock()
        self.ids = SequentialIdSource()
        self.sanitizer = RecordingSanitizer.of(["s3cr3t"])
        self.threads = ThreadService(
            self.store,
            self.tracker,
            self.lock,
            posture=PRIVATE,
            identity=ME,
            outbox=StoreOutboxView(self.store),
        )
        self.sender = OutboxService(
            self.store,
            self.tracker,
            self.lock,
            ids=self.ids,
            identity=ME,
            marker_line=marker_line,
            policy=outbox,
            leak_policy=LEAKS,
            sanitizer=self.sanitizer,
            posture=PRIVATE,
        )
        self.landed = FakeLandedCommits({})
        self.repo = _repo(commits)
        projection = (
            LabelProjection(
                self.store,
                self.tracker,
                self.lock,
                ids=self.ids,
                registry={"component": RegistryDimension("component", "Area of the code")},
                settings=LabelSettings(),
                leak_policy=LEAKS,
                sanitizer=self.sanitizer,
                posture=PRIVATE,
                outbox=OUTBOX,
            )
            if labels
            else None
        )
        catch_up = (
            MilestoneService(
                self.store,
                self.lock,
                ids=self.ids,
                leak_policy=LEAKS,
                sanitizer=self.sanitizer,
                posture=PRIVATE,
                outbox=OUTBOX,
                milestones=MilestonePolicy(
                    draft_on_merge=True,
                    draft_on_release=True,
                    completion=CompletionPolicy.CLOSE_ON_RELEASE,
                ),
                verifier=FakeReleaseVerifier(tags={}, contained={}),
            )
            if milestones
            else None
        )
        binding = TriageBinding(
            repo=self.repo,
            leak_policy=LEAKS,
            sanitizer=self.sanitizer,
            branch="main",
            landed=self.landed,
            refile=refile,
            issues=_CountingIssues(TrackerProjectIssues(self.tracker)) if issues else None,
        )
        self.listings = binding.issues
        self.intake = IntakeService(
            self.store,
            _NoLabels(),
            threads=self.threads,
            outbox=self.sender if sender else None,
            lock=self.lock,
            review=ReviewPolicy(comments_enabled=True, comments_read=True),
            projection=projection,
            triage=binding if triage else None,
            milestones=catch_up,
            catch_up_on_link=milestones,
        )
        self.issues: dict[str, object] = {}

    # -- fixtures ------------------------------------------------------------------------------

    def report(self, title: str, *, intake: str = "untriaged") -> str:
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

    def work(self, title: str, *, status: str = "open", commits: tuple[str, ...] = ()) -> str:
        bead = self.store.create_work(WorkDraft(title, "bug", "dev/plan"))
        if status != "open":
            self.store.mark(bead, status=status)
        if commits:
            self.landed.commits = {**self.landed.commits, bead: commits}
        return bead

    def reply(self, report: str, body: str, *, day: int = 2) -> None:
        self.world.add_comment(self.issues[report], body, at=at(day), author="reporter")
        self.intake.ingest_thread(ThreadRequest(report, TRIAGE))

    # -- the verbs -----------------------------------------------------------------------------

    def propose(self, report: str, document: dict):
        return self.intake.propose(ProposeRequest(report, document, ANALYST))

    def review(self, report: str, document: dict):
        return self.intake.review_proposal(ProposalReviewRequest(report, document, VERIFIER))

    def apply(self, *, dry_run: bool = False) -> ApplyOutcome:
        return self.intake.apply_reviewed(ApplyRequest(TRIAGE, dry_run=dry_run))

    def proposal(self, report: str) -> Proposal:
        stored = self.store.read_proposal(report)
        assert stored is not None
        return stored

    def records(self, report: str):
        doc = self.store.read_outbox(report)
        return list(doc.records.values()) if doc is not None else []

    def writes(self) -> tuple[int, int, int]:
        return (
            self.store.writes,
            len(self.store.proposal_writes),
            len(self.store.outbox_writes),
        )


class Replay:
    """The recorded first pass replayed onto a hive: one report per row (in issue order, so
    report ids sort by issue), one work bead per named work, and #14's later reporter comment."""

    def __init__(self, hive: Hive) -> None:
        self.hive = hive
        self.reports = {n: hive.report(f"issue {n}") for n in sorted(FIRST_PASS)}
        self.works = {
            name: hive.work(name, status=status, commits=commits)
            for name, (status, commits) in WORK.items()
        }
        hive.reply(self.reports[14], "Our executors fork from a local main 41 commits behind.")

    def issue_of(self, report: str) -> int:
        return next(n for n, r in self.reports.items() if r == report)

    def first_pass(self) -> None:
        for n, (disposition, targets, fixed_in, kind, priority) in sorted(FIRST_PASS.items()):
            role = "owner" if disposition in ("tracked", "fixed") else "related"
            document = {
                "disposition": disposition,
                "targets": [{"id": self.works[t], "role": role} for t in targets],
                "fixed_in": fixed_in,
                "type": kind,
                "priority": priority,
            }
            outcome = self.hive.propose(self.reports[n], document)
            assert outcome.kind is ProposeKind.CREATED

    def adopted(self, n: int, content: dict) -> dict:
        content = dict(content)
        document: dict = {"disposition": content.pop("disposition")}
        targets = [{"id": self.works[w], "role": "owner"} for w in content.pop("owner", ())]
        targets += [{"id": self.works[w], "role": "related"} for w in content.pop("related", ())]
        document["targets"] = targets
        if "share" in content:
            document["share_with"] = self.reports[content.pop("share")]
        if content.pop("read_thread", False):
            thread = self.hive.store.read_thread(self.reports[n])
            document["thread_seq"] = thread.seq if thread is not None else 0
        return {**document, **content}

    def verifier_pass(self) -> None:
        for n, (verdict, correction, content) in sorted(VERIFIER_PASS.items()):
            document: dict = {"verdict": verdict, "pass": 1}
            if verdict == "corrected":
                document["corrections"] = [correction]
            if content is not None:
                document["adopt"] = self.adopted(n, content)
            self.hive.review(self.reports[n], document)
        # #40: the verifier was unsure and pointed at an operator decision; the analyst
        # re-proposes it as one (a decision is listed and never applied, reviewed or not)
        self.hive.propose(self.reports[40], {"disposition": "decision"})


def item(outcome: ApplyOutcome, report: str):
    (found,) = [i for i in outcome.items if i.report == report]
    return found


# -- the recorded fixture ---------------------------------------------------------------------


def test_the_first_pass_cannot_reach_apply() -> None:
    """Unverified, every proposal of the first pass is blocked; the five reporter-visible
    errors are each blocked by a missing verification, and four of them by a check too."""
    hive = Hive()
    replay = Replay(hive)
    replay.first_pass()
    before = hive.writes()

    outcome = hive.apply()

    assert outcome.applied == () and outcome.decisions == ()
    assert set(outcome.blocked) == set(replay.reports.values())
    assert all("not reviewed" in i.detail for i in outcome.items)
    kinds = {replay.issue_of(i.report): {f.kind for f in i.findings} for i in outcome.items}
    assert CheckKind.FIX_VERSION in kinds[3]  # claimed v0.8.0, computed v0.7.0
    assert item(outcome, replay.reports[3]).fixed_in == "v0.7.0"
    assert {CheckKind.STALE_THREAD, CheckKind.INCONSISTENT_FIELDS} <= kinds[14]
    assert CheckKind.FIX_OPEN in kinds[25] and CheckKind.FIX_OPEN in kinds[110]
    assert kinds[11] == set()  # only the missing verification stops the false reroute
    assert hive.writes() == before  # nothing written
    assert hive.world.write_requests() == 0


def test_a_rubber_stamped_first_pass_is_still_stopped_by_the_checks() -> None:
    """Confirming the first pass without reading it does not get its false outcomes through:
    the false fixed claims and the false reject fail their checks."""
    hive = Hive()
    replay = Replay(hive)
    replay.first_pass()
    for n in REPORTER_VISIBLE_ERRORS:
        hive.review(replay.reports[n], {"verdict": "confirmed"})

    outcome = hive.apply(dry_run=True)

    for n in (3, 14, 25, 110):
        stopped = item(outcome, replay.reports[n])
        assert stopped.status is ApplyStatus.BLOCKED and stopped.findings, n
    # the reroute passes its checks, and still cannot be applied without the root's re-filer
    rerouted = item(outcome, replay.reports[11])
    assert rerouted.status is ApplyStatus.BLOCKED and "re-filer" in rerouted.detail


def test_the_verified_set_applies_canonicals_first_and_drafts_without_sending() -> None:
    hive = Hive()
    replay = Replay(hive)
    replay.first_pass()
    replay.verifier_pass()

    outcome = hive.apply()

    reports = replay.reports
    order = [i.report for i in outcome.items]
    for canonical, members in FAMILIES.items():
        for member in members:
            assert order.index(reports[canonical]) < order.index(reports[member])
            # the member is told (the reply drafted first), shares the canonical's work, and is
            # rejected
            work = hive.store.tracked_work(reports[canonical])
            assert work and hive.store.tracked_work(reports[member]) == work
            assert item(outcome, reports[member]).steps == (
                ApplyStep.DRAFT_REPLY,
                ApplyStep.LINK,
                ApplyStep.REJECT,
            )
            assert hive.store.get(reports[member]).status == "closed"
    applied = {replay.issue_of(r) for r in outcome.applied}
    assert applied == set(FIRST_PASS) - {40}
    assert outcome.decisions == (reports[40],)
    assert hive.proposal(reports[40]).status is ProposalStatus.PROPOSED  # listed, never applied
    for n in applied:
        assert hive.proposal(reports[n]).status is ProposalStatus.APPLIED, n
    # the five errors now carry their verified outcome
    assert item(outcome, reports[3]).steps == (ApplyStep.ACCEPT_NEW_WORK,)
    assert item(outcome, reports[11]).steps == (ApplyStep.DRAFT_REPLY, ApplyStep.REJECT)
    assert item(outcome, reports[14]).steps == (ApplyStep.ACCEPT_NEW_WORK,)
    assert item(outcome, reports[25]).steps == (ApplyStep.ACCEPT_NEW_WORK,)
    assert item(outcome, reports[110]).work == replay.works["0qazu"]
    assert hive.store.get(reports[30]).labels.count(INTAKE_NEEDS_INFO) == 1
    # the replies and the question are drafted, held for an operator, never sent
    drafted = outcome.drafted
    members = [reports[m] for family in FAMILIES.values() for m in family]
    assert len(drafted) == 2 + len(members)
    for report in (reports[11], reports[30], *members):
        (record,) = hive.records(report)
        assert record.event is OutboxEvent.REPLY and record.outbox_id in drafted
        assert all(op.state is not OpState.SENT for op in record.operations)
    assert hive.world.write_requests() == 0


def test_a_second_apply_changes_nothing() -> None:
    hive = Hive()
    replay = Replay(hive)
    replay.first_pass()
    replay.verifier_pass()
    first = hive.apply()
    before = hive.writes()

    second = hive.apply()

    assert second.applied == () and set(second.already_applied) == set(first.applied)
    assert second.decisions == first.decisions  # still listed, still not applied
    assert hive.writes() == before
    assert hive.world.write_requests() == 0


def test_a_dry_run_plans_the_verified_set_and_writes_nothing() -> None:
    hive = Hive()
    replay = Replay(hive)
    replay.first_pass()
    replay.verifier_pass()
    before = hive.writes()

    outcome = hive.apply(dry_run=True)

    planned = {replay.issue_of(r) for r in outcome.by_status(ApplyStatus.PLANNED)}
    assert planned == set(FIRST_PASS) - {40} and outcome.dry_run
    member = item(outcome, replay.reports[44])
    # its canonical is only planned
    assert member.steps == (ApplyStep.DRAFT_REPLY, ApplyStep.LINK, ApplyStep.REJECT)
    assert hive.writes() == before and outcome.labels is None


# -- propose and review ---------------------------------------------------------------------


def test_propose_creates_amends_and_keeps_the_reviews() -> None:
    hive = Hive()
    report = hive.report("a crash")
    created = hive.propose(report, {"disposition": "new", "type": "bug"})
    assert created.kind is ProposeKind.CREATED and created.proposal.seq == 1
    hive.review(report, {"verdict": "confirmed"})
    amended = hive.propose(report, {"disposition": "new", "type": "bug", "priority": "1"})
    assert amended.kind is ProposeKind.AMENDED
    assert amended.proposal.status is ProposalStatus.PROPOSED  # un-verified
    assert len(amended.proposal.reviews) == 1 and amended.proposal.priority == "1"


LEAK = RefusalReason.UNRESOLVED_FINDING


@pytest.mark.parametrize(
    ("document", "reason"),
    [
        (
            {"disposition": "reject", "reporter_text": "see acme-0042"},
            RefusalReason.UNRESOLVED_FINDING,
        ),
        (
            {"disposition": "reject", "reporter_text": "token s3cr3t"},
            RefusalReason.UNRESOLVED_FINDING,
        ),
        ({"disposition": "new", "status": "verified"}, RefusalReason.PROPOSAL_INVALID),
        ({"disposition": "sideways"}, RefusalReason.PROPOSAL_INVALID),
        ({"disposition": "new", "targets": ["w", "w"]}, RefusalReason.PROPOSAL_INVALID),
    ],
)
def test_a_refused_proposal_writes_nothing(document, reason) -> None:
    hive = Hive()
    report = hive.report("a crash")
    before = hive.writes()
    with pytest.raises(IntakeRefused) as refused:
        hive.propose(report, document)
    assert refused.value.reason is reason
    assert hive.writes() == before and hive.store.read_proposal(report) is None


def test_a_verdict_is_recorded_once_and_its_adopted_text_is_linted() -> None:
    hive = Hive()
    report = hive.report("a crash")
    hive.propose(report, {"disposition": "new"})
    outcome = hive.review(
        report,
        {
            "verdict": "corrected",
            "corrections": ["it is a duplicate question"],
            "pass": 1,
            "adopt": {"disposition": "needs-info", "reporter_text": "Which version?"},
        },
    )
    assert outcome.adopted and outcome.verified and outcome.proposal.reviews[0].seat == VERIFIER
    before = hive.writes()
    with pytest.raises(IntakeRefused) as again:
        hive.review(report, {"verdict": "confirmed", "pass": 1})
    assert again.value.reason is RefusalReason.PROPOSAL_MOVED
    with pytest.raises(IntakeRefused) as leaky:
        hive.review(
            report,
            {
                "verdict": "corrected",
                "corrections": ["x"],
                "adopt": {"disposition": "reject", "reporter_text": "see acme-0001"},
            },
        )
    assert leaky.value.reason is RefusalReason.UNRESOLVED_FINDING
    assert hive.writes() == before


def test_without_a_triage_binding_every_verb_refuses() -> None:
    hive = Hive(triage=False)
    report = hive.report("a crash")
    for call in (
        lambda: hive.propose(report, {"disposition": "new"}),
        lambda: hive.review(report, {"verdict": "confirmed"}),
        lambda: hive.apply(dry_run=True),
    ):
        with pytest.raises(IntakeRefused) as refused:
            call()
        assert refused.value.reason is RefusalReason.INTAKE_DISABLED
    null = NullIntake()
    for call in (
        lambda: null.propose(ProposeRequest(report, {}, ANALYST)),
        lambda: null.review_proposal(ProposalReviewRequest(report, {}, VERIFIER)),
        lambda: null.apply_reviewed(ApplyRequest(TRIAGE, dry_run=True)),
    ):
        with pytest.raises(IntakeRefused):
            call()


# -- the needs-info new round (dispatcher decision on bh-9aqmv) -----------------------------


def test_a_woken_needs_info_report_starts_a_new_round_with_the_old_one_in_history() -> None:
    hive = Hive()
    report = hive.report("it hangs")
    settled = hive.report("it crashes")
    hive.propose(report, {"disposition": "needs-info", "reporter_text": "Which version?"})
    hive.review(report, {"verdict": "confirmed"})
    hive.propose(settled, {"disposition": "new", "type": "bug"})
    hive.review(settled, {"verdict": "confirmed"})
    hive.apply()
    asked = hive.proposal(report)
    assert asked.status is ProposalStatus.APPLIED
    assert INTAKE_NEEDS_INFO in hive.store.get(report).labels

    def pending() -> tuple[str, ...]:
        return hive.intake.review_packets(ReviewRequest(TRIAGE)).reports

    # waiting on the reporter: not pending, and no new round yet
    assert report not in pending()
    with pytest.raises(IntakeRefused) as early:
        hive.propose(report, {"disposition": "new"})
    assert early.value.reason is RefusalReason.PROPOSAL_TRANSITION

    hive.reply(report, "Version 0.34.0, and it still hangs.")
    hive.intake.wake(WakeRequest(report, TRIAGE))
    assert INTAKE_UNTRIAGED in hive.store.get(report).labels
    assert report in pending() and settled not in pending()

    thread = hive.store.read_thread(report)
    following = hive.propose(
        report, {"disposition": "new", "type": "bug", "thread_seq": thread.seq}
    )
    assert following.kind is ProposeKind.NEW_ROUND
    stored = hive.proposal(report)
    assert stored.status is ProposalStatus.PROPOSED and stored.history == (asked,)
    assert stored.seq == asked.seq + 1
    # every other applied outcome stays final
    with pytest.raises(IntakeRefused) as final:
        hive.propose(settled, {"disposition": "reject", "reporter_text": "No."})
    assert final.value.reason is RefusalReason.PROPOSAL_IMMUTABLE
    # the new round is reviewed and applied like any other; the history survives it
    hive.review(report, {"verdict": "confirmed"})
    assert report in hive.apply().applied
    assert hive.proposal(report).history == (asked,)


# -- the remaining mappings -------------------------------------------------------------------


def test_fixed_without_a_fixing_bead_rejects_with_the_computed_version() -> None:
    hive = Hive(commits={"abc1234": "v1.2.0"})
    report = hive.report("export is empty")
    hive.propose(report, {"disposition": "fixed", "fix_commit": "abc1234"})
    hive.review(report, {"verdict": "confirmed"})

    outcome = hive.apply()

    done = item(outcome, report)
    assert done.status is ApplyStatus.APPLIED and done.fixed_in == "v1.2.0"
    assert done.steps == (ApplyStep.DRAFT_REPLY, ApplyStep.REJECT)
    (record,) = hive.records(report)
    assert "v1.2.0" in record.message.text
    assert hive.store.get(report).status == "closed"


def test_a_claimed_version_that_is_not_the_computed_one_is_blocked() -> None:
    hive = Hive(commits={"abc1234": "v1.2.0"})
    report = hive.report("export is empty")
    hive.propose(report, {"disposition": "fixed", "fix_commit": "abc1234", "fixed_in": "v1.1.0"})
    hive.review(report, {"verdict": "confirmed"})

    stopped = item(hive.apply(), report)

    assert stopped.status is ApplyStatus.BLOCKED
    assert [f.kind for f in stopped.findings] == [CheckKind.FIX_VERSION]


def test_fixed_by_a_closed_bead_links_late_and_drafts_the_merged_milestone() -> None:
    hive = Hive(commits={"a" * 40: "v1.0.0"}, milestones=True)
    fix = hive.work("fix the crash", commits=("a" * 40,))
    first = hive.report("it crashes", intake="accepted")
    hive.intake.link(LinkRequest(first, fix, TRIAGE))
    hive.intake.on_work_merged(MergeFact(fix, "a" * 40, "merger/t"))
    hive.store.mark(fix, status="closed")
    late = hive.report("it crashes too")
    hive.propose(late, {"disposition": "fixed", "targets": [fix]})
    hive.review(late, {"verdict": "confirmed"})

    done = item(hive.apply(), late)

    assert done.status is ApplyStatus.APPLIED and done.steps == (ApplyStep.ACCEPT_WORK,)
    assert done.work == fix and done.fixed_in == "v1.0.0"
    assert done.milestones  # the late link caught up on the merge
    assert OutboxEvent.MERGED in [r.event for r in hive.records(late)]
    assert hive.world.write_requests() == 0


def test_a_reroute_runs_the_roots_refiler_with_the_proposal() -> None:
    seen: list[tuple[str, D]] = []

    def refile(view, proposal) -> str:
        seen.append((view.id, proposal.disposition))
        return "rerouted to the right hive"

    hive = Hive(refile=refile)
    report = hive.report("wrong project")
    hive.propose(report, {"disposition": "reroute"})
    hive.review(report, {"verdict": "confirmed"})

    done = item(hive.apply(), report)

    assert done.status is ApplyStatus.APPLIED and seen == [(report, D.REROUTE)]
    assert hive.store.get(report).status == "closed"


def test_a_report_no_longer_awaiting_triage_is_blocked_before_any_write() -> None:
    hive = Hive()
    report = hive.report("a crash")
    owner = hive.work("the fix")
    hive.propose(report, {"disposition": "tracked", "targets": [owner]})
    hive.review(report, {"verdict": "confirmed"})
    hive.store.mark(report, status="closed")  # closed after the review: nothing to field
    before = hive.writes()

    blocked = item(hive.apply(), report)

    assert blocked.status is ApplyStatus.BLOCKED and "awaiting triage" in blocked.detail
    assert hive.proposal(report).status is ProposalStatus.VERIFIED
    assert hive.writes() == before


def test_a_refused_verb_fails_the_item_and_leaves_the_proposal_verified() -> None:
    """A verb that fails after every check passed fails its item and applies nothing: here the
    question's draft cannot be written."""
    hive = Hive()
    report = hive.report("a hang")
    hive.propose(report, {"disposition": "needs-info", "reporter_text": "Which version is it?"})
    hive.review(report, {"verdict": "confirmed"})
    before = hive.writes()
    hive.store.fail_next_outbox_write()

    failed = item(hive.apply(), report)

    assert failed.status is ApplyStatus.FAILED and failed.steps == ()
    assert failed.detail.startswith("needs-info failed")
    assert hive.proposal(report).status is ProposalStatus.VERIFIED
    assert hive.writes() == before


def test_an_owner_that_is_a_report_is_blocked_by_the_checks() -> None:
    """Tracking another REPORT is refused by the link, so the checks catch it before apply
    writes anything, and the dry run shows it (bh-osh0a)."""
    hive = Hive()
    report = hive.report("a crash")
    other = hive.report("another report")
    hive.propose(report, {"disposition": "tracked", "targets": [other]})
    hive.review(report, {"verdict": "confirmed"})
    before = hive.writes()

    planned = item(hive.apply(dry_run=True), report)
    blocked = item(hive.apply(), report)

    for outcome in (planned, blocked):
        assert outcome.status is ApplyStatus.BLOCKED
        assert [f.kind for f in outcome.findings] == [CheckKind.OWNER_NOT_WORK]
    assert hive.proposal(report).status is ProposalStatus.VERIFIED
    assert hive.writes() == before


def test_apply_refreshes_the_labels_of_the_touched_reports_when_labels_are_on() -> None:
    hive = Hive(labels=True)
    touched = hive.report("a crash")
    untouched = hive.report("still thinking")
    hive.propose(touched, {"disposition": "new", "type": "bug"})
    hive.review(touched, {"verdict": "confirmed"})
    hive.propose(untouched, {"disposition": "new", "type": "bug"})

    outcome = hive.apply()

    assert outcome.applied == (touched,)
    assert outcome.labels is not None and outcome.labels_error == ""
    assert [i.report for i in outcome.labels.items] == [touched]
    assert hive.world.write_requests() == 0  # label changes are drafted, never sent


# -- the 0.35.0 dog-food apply (bh-4kwet): closes, resumes and the thread watermark ----------

LONG_REPLY = (
    "Thanks for the detailed report. "
    + "We traced the behaviour you describe through the close ladder and the retry path. " * 8
    + "Please upgrade and let us know if it comes back."
)


def test_a_reject_whose_reply_is_longer_than_a_close_reason_still_closes() -> None:
    """Beads stores at most 255 characters of close reason; the whole reply goes to the outbox
    and the intake state change, and the close takes its start (bh-mqv12)."""
    assert len(LONG_REPLY) > CLOSE_REASON_LIMIT
    hive = Hive()
    report = hive.report("works as intended?")
    hive.propose(report, {"disposition": "reject", "reporter_text": LONG_REPLY})
    hive.review(report, {"verdict": "confirmed"})

    done = item(hive.apply(), report)

    assert done.status is ApplyStatus.APPLIED
    assert done.steps == (ApplyStep.DRAFT_REPLY, ApplyStep.REJECT)
    assert hive.store.get(report).status == "closed"
    history = hive.store.history(report)
    (closed,) = [reason for kind, value, reason in history if (kind, value) == ("status", "closed")]
    assert len(closed) <= CLOSE_REASON_LIMIT and closed.endswith("\u2026")
    assert LONG_REPLY.startswith(closed[:-1])
    assert any(LONG_REPLY in reason for _, _, reason in history)  # the state change keeps it all
    (record,) = hive.records(report)
    assert record.event is OutboxEvent.REPLY


def test_apply_finishes_a_half_rejected_report_without_drafting_again() -> None:
    """An earlier run drafted the reply, set intake:rejected and failed to close: apply only
    closes it now, and marks the proposal applied (bh-mqv12)."""
    hive = Hive()
    report = hive.report("works as intended?")
    hive.propose(report, {"disposition": "reject", "reporter_text": "Not a bug: see the docs."})
    hive.review(report, {"verdict": "confirmed"})
    hive.store.set_state(report, "intake", "rejected", reason="rejected", actor=TRIAGE)

    planned = item(hive.apply(dry_run=True), report)
    done = item(hive.apply(), report)

    assert planned.status is ApplyStatus.PLANNED and planned.steps == (ApplyStep.REJECT,)
    assert done.status is ApplyStatus.APPLIED and done.steps == (ApplyStep.REJECT,)
    assert hive.store.get(report).status == "closed"
    assert hive.proposal(report).status is ProposalStatus.APPLIED
    assert hive.records(report) == []  # nothing drafted again


def test_a_half_rejected_report_whose_proposal_does_not_reject_it_is_blocked() -> None:
    hive = Hive()
    report = hive.report("a crash")
    hive.propose(report, {"disposition": "new", "type": "bug"})
    hive.review(report, {"verdict": "confirmed"})
    hive.store.set_state(report, "intake", "rejected", reason="rejected", actor=TRIAGE)
    before = hive.writes()

    blocked = item(hive.apply(), report)

    assert blocked.status is ApplyStatus.BLOCKED and "does not reject" in blocked.detail
    assert hive.writes() == before


def test_reject_closes_a_half_rejected_report() -> None:
    """``bh work reject`` on a report a failed reject left open finishes it (bh-mqv12)."""
    hive = Hive()
    report = hive.report("works as intended?")
    hive.store.set_state(report, "intake", "rejected", reason="rejected", actor=TRIAGE)

    outcome = hive.intake.dispose(
        DispositionRequest(report, Disposition.REJECT, TRIAGE, reason=LONG_REPLY)
    )

    assert outcome.state == "rejected"
    assert hive.store.get(report).status == "closed"


def test_needs_info_applies_when_the_analysis_read_the_thread() -> None:
    """The verified analysis read the reporter's comment (its thread_seq covers it): apply
    records that read and asks, and the comment is no longer new evidence (bh-yu6sy)."""
    hive = Hive()
    report = hive.report("a hang")
    hive.reply(report, "It hangs on the second run.")
    seq = hive.store.read_thread(report).seq
    hive.propose(
        report,
        {"disposition": "needs-info", "reporter_text": "Which version is it?", "thread_seq": seq},
    )
    hive.review(report, {"verdict": "confirmed"})

    done = item(hive.apply(), report)

    assert done.status is ApplyStatus.APPLIED and done.steps == (ApplyStep.NEEDS_INFO,)
    assert INTAKE_NEEDS_INFO in hive.store.get(report).labels
    assert hive.intake.thread_of(report).new_evidence == ()


def test_a_triage_read_does_not_make_a_proposal_stale() -> None:
    """Recording a read moves the thread's seq but no comment, so a proposal that read the thread
    is not stale; a comment after it still is (bh-yu6sy)."""
    hive = Hive()
    report = hive.report("a hang")
    hive.reply(report, "It hangs on the second run.")
    seq = hive.store.read_thread(report).seq
    hive.propose(
        report,
        {"disposition": "needs-info", "reporter_text": "Which version is it?", "thread_seq": seq},
    )
    hive.review(report, {"verdict": "confirmed"})
    hive.intake.mark_considered(ConsiderRequest(report, TRIAGE, through_seq=seq))
    assert hive.store.read_thread(report).seq > seq

    fresh = item(hive.apply(dry_run=True), report)
    hive.reply(report, "Also on the third.", day=3)
    stale = item(hive.apply(dry_run=True), report)

    assert fresh.status is ApplyStatus.PLANNED
    assert stale.status is ApplyStatus.BLOCKED
    assert [f.kind for f in stale.findings] == [CheckKind.STALE_THREAD]


def test_a_needs_info_report_whose_analysis_read_a_comment_waits_for_a_new_one() -> None:
    """Apply records the read (moving the thread's seq) and asks; the report is not woken until
    the reporter writes again (bh-yu6sy review)."""
    hive = Hive()
    report = hive.report("a hang")
    hive.reply(report, "It hangs on the second run.")
    seq = hive.store.read_thread(report).seq
    hive.propose(
        report,
        {"disposition": "needs-info", "reporter_text": "Which version is it?", "thread_seq": seq},
    )
    hive.review(report, {"verdict": "confirmed"})
    hive.apply()
    assert hive.store.read_thread(report).seq > seq  # the recorded read

    with pytest.raises(IntakeRefused) as early:
        hive.propose(report, {"disposition": "new"})
    assert early.value.reason is RefusalReason.PROPOSAL_TRANSITION

    hive.reply(report, "Version 0.34.0.", day=3)
    thread = hive.store.read_thread(report)
    following = hive.propose(
        report, {"disposition": "new", "type": "bug", "thread_seq": thread.seq}
    )
    assert following.kind is ProposeKind.NEW_ROUND


def test_ordinary_open_work_is_a_valid_owner() -> None:
    hive = Hive()
    report = hive.report("a crash")
    owner = hive.work("the fix")
    hive.propose(report, {"disposition": "tracked", "targets": [owner]})
    hive.review(report, {"verdict": "confirmed"})

    planned = item(hive.apply(dry_run=True), report)

    assert planned.status is ApplyStatus.PLANNED and planned.findings == ()


# -- closing the issue with the reply (bh-pr5ll) ----------------------------------------------

CLOSING = OutboxPolicy(enabled=True, labels_enabled=True, close_enabled=True)


def _close_of(hive: Hive, report: str):
    (record,) = [r for r in hive.records(report) if r.event is OutboxEvent.REPLY]
    return record, record.op(OpKind.CLOSE)


def test_apply_drafts_the_close_with_the_reply_for_each_thread_ending_outcome() -> None:
    hive = Hive(outbox=CLOSING)
    replay = Replay(hive)
    replay.first_pass()
    replay.verifier_pass()

    outcome = hive.apply()

    reports = replay.reports
    expected = {
        reports[11]: CloseReason.NOT_PLANNED,  # a reasoned reject
        **{reports[m]: CloseReason.DUPLICATE for f in FAMILIES.values() for m in f},
    }
    for report, reason in expected.items():
        record, close = _close_of(hive, report)
        assert close is not None and close.close is reason, report
        assert close.state is OpState.DRAFTED and record.op(OpKind.COMMENT) is not None
    # a needs-info question waits for the reporter: it never closes anything
    (question,) = hive.records(reports[30])
    assert question.op(OpKind.CLOSE) is None
    assert len(outcome.drafted) == 2 + len(expected) - 1
    assert hive.world.write_requests() == 0  # drafted, never sent


def test_a_fixed_reply_closes_as_completed() -> None:
    hive = Hive(commits={"abc1234": "v1.2.0"}, outbox=CLOSING)
    report = hive.report("export is empty")
    hive.propose(report, {"disposition": "fixed", "fix_commit": "abc1234"})
    hive.review(report, {"verdict": "confirmed"})

    done = item(hive.apply(), report)

    assert done.status is ApplyStatus.APPLIED
    _, close = _close_of(hive, report)
    assert close is not None and close.close is CloseReason.COMPLETED


def test_with_the_close_gate_off_apply_drafts_the_reply_alone() -> None:
    hive = Hive(commits={"abc1234": "v1.2.0"})  # OUTBOX: close_enabled is off by default
    report = hive.report("export is empty")
    hive.propose(report, {"disposition": "fixed", "fix_commit": "abc1234"})
    hive.review(report, {"verdict": "confirmed"})

    assert item(hive.apply(), report).status is ApplyStatus.APPLIED
    record, close = _close_of(hive, report)
    assert close is None and [op.kind for op in record.operations] == [OpKind.COMMENT]


# -- the reply goes first; the outbox off keeps 0.35.2's duplicate (bh-pr5ll review B1) --------


def _family(hive: Hive) -> tuple[str, str]:
    """A canonical (new work) and its duplicate-of member, both reviewed."""
    canonical, member = hive.report("it crashes"), hive.report("it crashes for me too")
    hive.propose(canonical, {"disposition": "new", "type": "bug", "priority": "2"})
    hive.propose(member, {"disposition": "duplicate-of", "share_with": canonical})
    for report in (canonical, member):
        hive.review(report, {"verdict": "confirmed"})
    return canonical, member


def test_with_the_outbox_off_a_duplicate_is_linked_and_rejected_as_before() -> None:
    hive = Hive(sender=False)
    canonical, member = _family(hive)

    done = item(hive.apply(), member)

    assert done.status is ApplyStatus.APPLIED
    assert done.steps == (ApplyStep.LINK, ApplyStep.REJECT) and done.drafted == ()
    assert hive.store.get(member).status == "closed" and hive.records(member) == []
    assert hive.store.tracked_work(member) == hive.store.tracked_work(canonical)
    assert hive.proposal(member).status is ProposalStatus.APPLIED


def _deliveries(hive: Hive, report: str) -> list[str]:
    return [label for label in hive.store.get(report).labels if label.startswith("delivery:")]


@pytest.mark.parametrize("outbox_on", [True, False])
def test_a_duplicate_of_leaves_no_delivery_while_its_canonical_keeps_pending(outbox_on) -> None:
    """bh-5kpwc: a duplicate shares its canonical's work but never its delivery."""
    hive = Hive(outbox=CLOSING) if outbox_on else Hive(sender=False)
    canonical, member = _family(hive)

    assert item(hive.apply(), member).status is ApplyStatus.APPLIED

    assert hive.store.tracked_work(member) == hive.store.tracked_work(canonical)
    assert _deliveries(hive, member) == []  # no delivery:* on the duplicate
    assert _deliveries(hive, canonical) == ["delivery:pending"]  # a tracks link is as before


def test_a_duplicate_of_clears_a_delivery_the_report_already_carries() -> None:
    hive = Hive(sender=False)
    canonical, member = _family(hive)
    hive.store.set_state(member, "delivery", "pending", reason="linked earlier", actor=TRIAGE)

    assert item(hive.apply(), member).status is ApplyStatus.APPLIED

    assert _deliveries(hive, member) == [] and _deliveries(hive, canonical) == ["delivery:pending"]


def test_with_the_outbox_off_a_reject_fails_cleanly_with_nothing_written() -> None:
    hive = Hive(sender=False)
    report = hive.report("not a bug")
    hive.propose(report, {"disposition": "reject", "reporter_text": "Works as designed."})
    hive.review(report, {"verdict": "confirmed"})

    failed = item(hive.apply(), report)

    assert failed.status is ApplyStatus.FAILED and failed.steps == ()
    assert "draft reply" in failed.detail
    assert hive.store.get(report).status != "closed" and hive.records(report) == []
    assert hive.proposal(report).status is not ProposalStatus.APPLIED


def test_a_duplicates_refused_reply_leaves_it_unlinked_and_open(monkeypatch) -> None:
    hive = Hive(outbox=CLOSING)
    _canonical, member = _family(hive)

    def refuse(request):
        raise IntakeRefused(RefusalReason.UNRESOLVED_FINDING, "the reply would leak")

    monkeypatch.setattr(hive.sender, "draft_reply", refuse)
    failed = item(hive.apply(), member)

    assert failed.status is ApplyStatus.FAILED and failed.steps == ()
    assert hive.store.tracked_work(member) is None  # the link never ran
    assert hive.store.get(member).status != "closed"
    assert hive.proposal(member).status is not ProposalStatus.APPLIED


def test_a_rerun_after_a_later_failure_drafts_the_reply_once() -> None:
    hive = Hive(outbox=CLOSING)
    _canonical, member = _family(hive)
    hive.apply()
    (record,) = hive.records(member)

    # the same text answering the same thread seq is the same draft key: O2, no second record
    again = hive.intake.draft_reply(ReplyDraft(member, record.message.text, TRIAGE))

    assert again.outbox_id == record.outbox_id and len(hive.records(member)) == 1


def test_a_duplicates_close_names_its_canonicals_issue() -> None:
    """bh-iamru: the member closes as a duplicate OF the canonical's bound issue."""
    hive = Hive(outbox=CLOSING)
    canonical, member = _family(hive)

    assert item(hive.apply(), member).status is ApplyStatus.APPLIED

    _, close = _close_of(hive, member)
    issue = hive.issues[canonical]
    assert close.close is CloseReason.DUPLICATE
    assert close.duplicate_of == CanonicalIssue(issue.identifier, issue.url)


def test_the_close_backfill_composes_a_fixed_reply_from_the_computed_version() -> None:
    """B2: a fixed report closed before closes existed, with no reply and no reviewed text, gets
    the reply apply would have drafted (the version computed now), its close attached."""
    hive = Hive(commits={"abc1234": "v1.2.0"}, outbox=CLOSING)
    report = hive.report("export is empty")
    hive.propose(report, {"disposition": "fixed", "fix_commit": "abc1234"})
    hive.review(report, {"verdict": "confirmed"})
    hive.store.write_proposal(
        report,
        mark_applied(hive.proposal(report)),
        expect_seq=hive.proposal(report).seq,
        actor=TRIAGE,
    )
    hive.store.close(report, reason="fixed", actor=TRIAGE)

    (item,) = hive.intake.backfill_closes(CloseBackfill(TRIAGE)).drafted

    assert item.reason is CloseReason.COMPLETED and "v1.2.0" in item.reply_text
    record, close = _close_of(hive, report)
    assert record.message.text == item.reply_text and close.close is CloseReason.COMPLETED


# -- the #N a reply mentions: resolved at check, related at apply (bh-iamru) ---------------------


def _reject(hive: Hive, title: str, text: str) -> str:
    report = hive.report(title)
    hive.propose(report, {"disposition": "reject", "reporter_text": text})
    hive.review(report, {"verdict": "confirmed"})
    return report


def test_a_reply_naming_a_missing_issue_is_refused_at_check_and_writes_nothing() -> None:
    hive = Hive()
    report = _reject(hive, "not a bug", "Works as designed; see #999.")
    before = hive.writes()

    outcome = hive.apply()

    (check,) = outcome.checks
    assert check.kinds() == {CheckKind.MENTION_MISSING} and "#999" in check.findings[0].detail
    blocked = item(outcome, report)
    assert blocked.status is ApplyStatus.BLOCKED and "mention_missing" in blocked.detail
    assert hive.writes() == before and hive.records(report) == []


def test_a_reply_naming_another_bound_report_relates_the_two_once() -> None:
    hive = Hive()
    other = hive.report("the original thread")
    named = hive.issues[other].identifier
    report = _reject(hive, "asked again", f"Answered in {named}; it works as designed.")
    plain = _reject(hive, "a third", f"Also see {named}.")

    planned = hive.apply(dry_run=True)

    assert item(planned, report).relates == (other,) and item(planned, plain).relates == (other,)
    assert hive.store.related_reports(other) == ()  # a dry run records nothing
    assert hive.listings.calls == 1  # one listing for the whole run, not one per mention

    done = hive.apply()

    assert item(done, report).status is ApplyStatus.APPLIED
    assert hive.store.related_reports(other) == tuple(sorted((report, plain)))
    assert hive.store.related_reports(report) == (other,)
    assert hive.listings.calls == 2  # and one more for the second run


def test_a_reply_naming_an_issue_without_a_listing_is_unverified() -> None:
    hive = Hive(issues=False)
    report = _reject(hive, "not a bug", "Works as designed; see #1.")

    blocked = item(hive.apply(), report)

    assert blocked.status is ApplyStatus.BLOCKED and "mention_unverified" in blocked.detail
    assert hive.records(report) == []


def test_a_run_that_mentions_nothing_never_lists_the_issues() -> None:
    hive = Hive()
    _reject(hive, "not a bug", "Works as designed.")
    hive.apply()
    assert hive.listings.calls == 0


# -- a reversed outcome drafts the reopen (bh-ih484) ---------------------------------------------

REOPENING = OutboxPolicy(enabled=True, labels_enabled=True, close_enabled=True, reopen_enabled=True)


def _closed_report(hive: Hive, title: str = "a hang") -> str:
    """A bound report whose GitHub issue was closed (by a human, or an earlier close)."""
    report = hive.report(title)
    hive.world.update_issue(hive.issues[report], at=at(2), state=IssueState.CLOSED)
    return report


def _reopens(hive: Hive, report: str):
    return [r for r in hive.records(report) if r.op(OpKind.REOPEN) is not None]


def test_an_outcome_that_keeps_a_closed_issue_tracked_drafts_the_reopen_first() -> None:
    hive = Hive(outbox=REOPENING)
    report = _closed_report(hive)
    hive.propose(report, {"disposition": "new", "type": "bug", "priority": "2"})
    hive.review(report, {"verdict": "confirmed"})

    done = item(hive.apply(), report)

    assert done.status is ApplyStatus.APPLIED and done.steps == (ApplyStep.ACCEPT_NEW_WORK,)
    (record,) = _reopens(hive, report)
    assert done.drafted == (record.outbox_id,)
    assert record.event is OutboxEvent.REPLY and record.message.text == reopen_text()
    assert [op.kind for op in record.operations] == [OpKind.COMMENT, OpKind.REOPEN]
    assert all(op.state is OpState.DRAFTED for op in record.operations)  # drafted, never sent
    assert hive.world.write_requests() == 0
    assert hive.store.get(report).status != "closed"  # the report is accepted into the backlog


def test_a_needs_info_question_on_a_closed_issue_reopens_it_first() -> None:
    hive = Hive(outbox=REOPENING)
    report = _closed_report(hive)
    hive.reply(report, "It hangs on the second run.")
    seq = hive.store.read_thread(report).seq
    hive.propose(
        report,
        {"disposition": "needs-info", "reporter_text": "Which version is it?", "thread_seq": seq},
    )
    hive.review(report, {"verdict": "confirmed"})

    done = item(hive.apply(), report)

    assert done.status is ApplyStatus.APPLIED and done.steps == (ApplyStep.NEEDS_INFO,)
    (reopen,) = _reopens(hive, report)
    assert len(done.drafted) == 2 and done.drafted[0] == reopen.outbox_id  # reopen, then the ask
    assert hive.world.write_requests() == 0


def test_a_thread_ending_outcome_never_reopens_and_nor_does_an_open_issue() -> None:
    hive = Hive(outbox=REOPENING)
    rejected = _closed_report(hive, "not a bug")
    hive.propose(rejected, {"disposition": "reject", "reporter_text": "Works as designed."})
    hive.review(rejected, {"verdict": "confirmed"})
    tracked = hive.report("an open one")  # its issue is open: nothing contradicts the outcome
    hive.propose(tracked, {"disposition": "new", "type": "bug", "priority": "2"})
    hive.review(tracked, {"verdict": "confirmed"})

    outcome = hive.apply()

    assert item(outcome, rejected).status is ApplyStatus.APPLIED
    assert item(outcome, tracked).status is ApplyStatus.APPLIED
    assert _reopens(hive, rejected) == [] and _reopens(hive, tracked) == []


def test_with_the_reopen_gate_off_apply_drafts_no_reopen() -> None:
    hive = Hive(outbox=CLOSING)  # close on, reopen off (the default)
    report = _closed_report(hive)
    hive.propose(report, {"disposition": "new", "type": "bug", "priority": "2"})
    hive.review(report, {"verdict": "confirmed"})

    done = item(hive.apply(), report)

    assert done.status is ApplyStatus.APPLIED and done.drafted == ()
    assert hive.records(report) == []


def test_a_refused_reopen_draft_fails_the_item_before_any_verb_runs(monkeypatch) -> None:
    hive = Hive(outbox=REOPENING)
    report = _closed_report(hive)
    hive.propose(report, {"disposition": "new", "type": "bug", "priority": "2"})
    hive.review(report, {"verdict": "confirmed"})

    def refuse(*_args):
        raise IntakeRefused(RefusalReason.UNRESOLVED_FINDING, "the reply would leak")

    monkeypatch.setattr(hive.sender, "draft_reopen", refuse)
    failed = item(hive.apply(), report)

    assert failed.status is ApplyStatus.FAILED and failed.steps == ()
    assert "drafting the reopen failed" in failed.detail
    assert INTAKE_UNTRIAGED in hive.store.get(report).labels  # nothing else ran
    assert hive.proposal(report).status is not ProposalStatus.APPLIED


def test_a_rerun_after_a_later_failure_drafts_the_reopen_once() -> None:
    hive = Hive(outbox=REOPENING)
    report = _closed_report(hive)
    first = hive.sender.draft_reopen(report, reopen_text(), TRIAGE)
    second = hive.sender.draft_reopen(report, reopen_text(), TRIAGE)  # O2: the same key
    assert first is not None and second is not None and first.outbox_id == second.outbox_id
    assert len(_reopens(hive, report)) == 1
