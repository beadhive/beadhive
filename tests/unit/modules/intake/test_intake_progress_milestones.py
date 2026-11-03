"""The progress milestones (bh-dev42): ``started``, ``blocked`` and ``deprioritised`` only DRAFT.

``MilestoneService`` over ``MemoryReportStore``, the never-waiting ``InProcessIntakeLock`` and
``SequentialIdSource``; the sender and ``MemoryTracker`` / ``MemoryWorld`` join only where a record
has to settle. What is pinned:

* the claim hook's ``on_work_started`` drafts ONE ``started`` record per tracking report on the
  first claim of the work; a re-claim, a resume after a bounce and a claim after an abandon all
  call it again and draft nothing new (the record is keyed on the work);
* ``delivery_notice`` drafts the operator's public reason under fixed wording; a reason naming a
  bead id is held by the leak check; the three are in the milestone group, so a newer one
  supersedes an unsent older one;
* every switch is off by default, the hook is free when off, and nothing ever reaches a tracker
  (``write_requests() == 0`` in every trigger case).

Ordering is asserted on ``seq``, store write counters and ``write_requests()`` only: no clock,
no sleep, no socket.
"""

from __future__ import annotations

import pytest

from beadhive.modules.intake.application import (
    IntakeService,
    MilestoneService,
    NullIntake,
    OutboxService,
)
from beadhive.modules.intake.application.threads import binding_key
from beadhive.modules.intake.domain import (
    STARTED_MESSAGE,
    IntakeRefused,
    IssueBinding,
    IssueStatus,
    LeakPolicy,
    MergeFact,
    MilestonePolicy,
    NoticeFact,
    OpKind,
    OpState,
    OutboxApprove,
    OutboxEvent,
    OutboxPolicy,
    OwnIdentity,
    RefusalReason,
    ReportDraft,
    SourceSnapshot,
    StartFact,
    StorePosture,
    WorkDraft,
    notified_of,
)
from beadhive.modules.intake.domain.milestones import (
    BLOCKED_LEAD,
    DEPRIORITISED_LEAD,
    MAX_REASON_CHARS,
)
from beadhive.modules.tracker.contracts import READ_OPERATIONS, WRITE_OPERATIONS, OwnMarker
from beadhive.testing.intake import (
    SCOPE,
    InProcessIntakeLock,
    MemoryReportStore,
    RecordingSanitizer,
    SequentialIdSource,
)
from beadhive.testing.tracker import MemoryTracker, MemoryWorld, at

LEAKS = LeakPolicy(bead_prefixes=frozenset({"acme", "pub"}))
ON = OutboxPolicy(enabled=True)
ALL_PROGRESS = MilestonePolicy(
    draft_on_merge=True,
    draft_on_start=True,
    draft_on_blocked=True,
    draft_on_deprioritised=True,
)
PRIVATE = StorePosture(published=False)
C = OpKind.COMMENT
MERGE_SHA = "a" * 40
CLAIMER = "dev/alice"
OPERATOR = "alice"
ME = OwnIdentity("acme/widgets", "inst-1")
WAITING = "Waiting on an upstream fix in the parser library."


def marker_line(marker: OwnMarker) -> str:
    return f"<!-- bh:v1 hive={marker.hive} install={marker.install} outbox={marker.outbox_id} -->"


class _NoLabels:
    def violations(self, labels) -> tuple[str, ...]:
        return ()


class Scene:
    """One work bead, the reports tracking it, and the milestone service wired as the root wires
    it, with the sender (and a memory tracker) for the cases that settle a record."""

    def __init__(
        self,
        *,
        outbox: OutboxPolicy = ON,
        milestones: MilestonePolicy = ALL_PROGRESS,
        posture: StorePosture = PRIVATE,
    ) -> None:
        self.world = MemoryWorld()
        self.world.set_write_time(at(2))
        self.tracker = MemoryTracker(
            self.world, enabled=frozenset(READ_OPERATIONS | WRITE_OPERATIONS)
        )
        self.store = MemoryReportStore(SCOPE)
        self.lock = InProcessIntakeLock()
        self.ids = SequentialIdSource()
        sanitizer = RecordingSanitizer.of(["s3cr3t"])
        self.service = MilestoneService(
            self.store,
            self.lock,
            ids=self.ids,
            leak_policy=LEAKS,
            sanitizer=sanitizer,
            posture=posture,
            outbox=outbox,
            milestones=milestones,
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
            sanitizer=sanitizer,
            posture=posture,
            completion=self.service.complete,
        )
        self.intake = IntakeService(
            self.store, _NoLabels(), outbox=self.sender, milestones=self.service
        )
        self.work = self.store.create_work(WorkDraft("fix the login crash", "bug", "dev/t"))

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

    # -- the triggers ----------------------------------------------------------------------

    def claim(self, actor: str = CLAIMER, work: str = ""):
        return self.intake.on_work_started(StartFact(work or self.work, actor))

    def notice(self, event: OutboxEvent, reason: str = WAITING, work: str = ""):
        return self.intake.delivery_notice(NoticeFact(work or self.work, event, reason, OPERATOR))

    # -- reads -------------------------------------------------------------------------------

    def labels(self, report: str) -> tuple[str, ...]:
        view = self.store.get(report)
        assert view is not None
        return view.labels

    def records(self, report: str, event: OutboxEvent | None = None):
        doc = self.store.read_outbox(report)
        found = sorted((doc.records if doc else {}).values(), key=lambda r: r.created_seq)
        return [r for r in found if event is None or r.event is event]

    def state(self, record) -> OpState:
        doc = self.store.read_outbox(self._report_of(record))
        assert doc is not None
        op = doc.records[record.outbox_id].op(C)
        assert op is not None
        return op.state

    def _report_of(self, record) -> str:
        for report in self.store.trackers_of(self.work):
            doc = self.store.read_outbox(report)
            if doc is not None and record.outbox_id in doc.records:
                return report
        raise AssertionError(f"no report holds {record.outbox_id}")

    def payload(self, record) -> str:
        rendered = self.intake.render(record.outbox_id).of(C)
        assert rendered is not None
        return rendered.payload

    def requests(self) -> tuple[int, int]:
        return self.world.read_requests(), self.world.write_requests()


def _send(s: Scene, record) -> None:
    digest = s.intake.render(record.outbox_id).of(C).short_digest
    s.intake.approve(OutboxApprove(record.outbox_id, digest, OPERATOR))
    assert s.intake.send(record.outbox_id, actor=OPERATOR).state is OpState.SENT


# -- started: the first claim drafts one record per tracking report ---------------------------


def test_the_first_claim_drafts_one_started_record_for_every_tracking_report() -> None:
    s = Scene()
    first, second = s.report(), s.report()
    duplicate = s.report(status="closed")  # closed early, still tracks the work
    elsewhere = s.report(work=s.store.create_work(WorkDraft("other", "bug", "dev/t")))

    outcome = s.claim()

    assert outcome.reports == (first, second, duplicate) and not outcome.errors
    for report in (first, second, duplicate):
        (record,) = s.records(report)
        assert record.event is OutboxEvent.STARTED and s.state(record) is OpState.DRAFTED
        assert record.message == STARTED_MESSAGE
        assert "delivery:pending" in s.labels(report)  # a progress milestone moves no dimension
    assert all(item.drafted and not item.held for item in outcome.items)
    assert s.records(elsewhere) == []
    assert s.requests() == (0, 0)  # nothing read or written on a tracker, ever


def test_the_started_message_is_fixed_wording_with_no_bead_id_or_internal_content() -> None:
    s = Scene()
    report = s.report()
    s.claim()
    (record,) = s.records(report)

    rendered = s.intake.render(record.outbox_id).of(C)

    assert rendered is not None and not rendered.findings
    assert rendered.payload.startswith(STARTED_MESSAGE.text)
    for internal in (s.work, report, CLAIMER, "wt/bead", "acme-"):
        assert internal not in rendered.payload


@pytest.mark.parametrize(
    "again",
    [
        pytest.param(lambda s: s.claim(), id="re-claim"),
        pytest.param(lambda s: s.claim(), id="resume-after-bounce"),
        pytest.param(lambda s: s.claim(actor="dev/bob"), id="claim-after-abandon"),
    ],
)
def test_a_later_claim_of_the_same_work_drafts_nothing_new_and_writes_nothing(again) -> None:
    s = Scene()
    report = s.report()
    s.claim()
    writes, outbox_writes = s.store.writes, list(s.store.outbox_writes)

    outcome = again(s)

    assert len(s.records(report)) == 1
    assert s.store.writes == writes and s.store.outbox_writes == outbox_writes
    (item,) = outcome.items
    assert item.drafted == "" and not item.error and "already exists" in item.note
    assert s.requests() == (0, 0)


def test_a_re_claim_after_a_blocked_notice_superseded_the_started_record_drafts_nothing() -> None:
    s = Scene()
    report = s.report()
    s.claim()
    s.notice(OutboxEvent.BLOCKED)

    again = s.claim(actor="dev/bob")  # the work was abandoned and picked up again

    assert again.items[0].drafted == ""
    assert [r.event for r in s.records(report)] == [OutboxEvent.STARTED, OutboxEvent.BLOCKED]


def test_with_draft_on_start_off_the_hook_is_free_and_reads_nothing(monkeypatch) -> None:
    s = Scene(milestones=MilestonePolicy(draft_on_merge=True))
    report = s.report()
    writes = s.store.writes

    def refuse(*_args, **_kwargs):
        raise AssertionError("the claim hook read the store with draft_on_start off")

    with monkeypatch.context() as patched:
        for name in ("trackers_of", "get", "read_outbox"):
            patched.setattr(s.store, name, refuse)
        assert s.claim().items == ()

    assert s.store.writes == writes and s.records(report) == []


def test_with_the_outbox_off_the_hook_drafts_nothing_even_with_draft_on_start() -> None:
    s = Scene(outbox=OutboxPolicy())
    report = s.report()

    assert s.claim().items == () and s.records(report) == []


def test_with_intake_off_every_progress_trigger_is_free_or_refused() -> None:
    null = NullIntake()

    assert null.on_work_started(StartFact("w", CLAIMER)).items == ()
    with pytest.raises(IntakeRefused) as refused:
        null.delivery_notice(NoticeFact("w", OutboxEvent.BLOCKED, WAITING, OPERATOR))
    assert refused.value.reason is RefusalReason.INTAKE_DISABLED


def test_a_started_note_after_the_work_merged_would_be_stale_and_is_not_drafted() -> None:
    s = Scene()
    report = s.report()
    s.intake.on_work_merged(MergeFact(s.work, MERGE_SHA, "merger/t"))  # reopened, then claimed

    outcome = s.claim()

    assert outcome.items[0].drafted == "" and "delivery:merged" in outcome.items[0].note
    assert [r.event for r in s.records(report)] == [OutboxEvent.MERGED]
    assert s.state(s.records(report)[0]) is OpState.DRAFTED  # the merged draft is untouched


def test_an_unlinked_or_unbound_tracker_gets_no_started_record() -> None:
    s = Scene()
    report = s.report(linked=False)
    s.store.track(report, s.work, actor="dev/t")  # tracked, but `link` never set delivery

    outcome = s.claim()

    assert outcome.items[0].drafted == "" and "no delivery state" in outcome.items[0].note
    assert s.records(report) == []


def test_a_held_binding_is_reported_on_its_item_and_never_raised() -> None:
    s = Scene()
    report = s.report()
    view = s.store.get(report)
    assert view is not None and view.binding is not None

    with s.lock.hold(binding_key(view.binding)):  # another intake operation is running
        outcome = s.claim()

    assert "held by another intake operation" in outcome.items[0].error
    assert outcome.items[0].drafted == "" and s.records(report) == []
    assert s.claim().items[0].drafted  # the next fresh claim drafts what was missed


def test_a_published_store_is_reported_per_report_not_raised() -> None:
    s = Scene(posture=StorePosture(published=True))
    report = s.report()

    outcome = s.claim()

    assert "no started record drafted" in outcome.items[0].error
    assert s.records(report) == []


# -- blocked and deprioritised: explicit operator verbs ---------------------------------------


@pytest.mark.parametrize(
    ("event", "lead"),
    [(OutboxEvent.BLOCKED, BLOCKED_LEAD), (OutboxEvent.DEPRIORITISED, DEPRIORITISED_LEAD)],
)
def test_a_notice_drafts_the_public_reason_under_fixed_wording(event, lead) -> None:
    s = Scene()
    first, second = s.report(), s.report()

    outcome = s.notice(event)

    assert outcome.reports == (first, second) and not outcome.errors
    for report in (first, second):
        (record,) = s.records(report)
        assert record.event is event and s.state(record) is OpState.DRAFTED
        assert record.message.template == f"milestone.{event.value}"
        payload = s.payload(record)
        assert payload.startswith(lead) and WAITING in payload
    assert s.requests() == (0, 0)


def test_a_reason_containing_a_bead_id_is_held_by_the_leak_check_never_sent() -> None:
    s = Scene()
    report = s.report()

    outcome = s.notice(OutboxEvent.BLOCKED, reason="Waiting on acme-1234 to land first.")

    (record,) = s.records(report)
    assert s.state(record) is OpState.HELD and outcome.items[0].held
    assert any(f.kind.value == "bead_id" for f in record.operations[0].findings)
    with pytest.raises(IntakeRefused):  # cannot be approved while the finding stands
        digest = s.intake.render(record.outbox_id).of(C).short_digest
        s.intake.approve(OutboxApprove(record.outbox_id, digest, OPERATOR))
    assert s.requests() == (0, 0)


def test_a_newer_milestone_supersedes_an_unsent_older_one_in_the_group() -> None:
    s = Scene()
    report = s.report()
    s.claim()
    s.notice(OutboxEvent.BLOCKED)
    s.notice(OutboxEvent.DEPRIORITISED, reason="Other fixes come first this cycle.")
    s.intake.on_work_merged(MergeFact(s.work, MERGE_SHA, "merger/t"))

    started, blocked, deprioritised, merged = s.records(report)

    assert [r.event for r in (started, blocked, deprioritised, merged)] == [
        OutboxEvent.STARTED,
        OutboxEvent.BLOCKED,
        OutboxEvent.DEPRIORITISED,
        OutboxEvent.MERGED,
    ]
    assert [s.state(r) for r in (started, blocked, deprioritised)] == [OpState.SUPERSEDED] * 3
    assert s.state(merged) is OpState.DRAFTED
    assert s.requests() == (0, 0)


def test_the_same_reason_twice_drafts_nothing_and_new_words_supersede_the_old() -> None:
    s = Scene()
    report = s.report()
    s.notice(OutboxEvent.BLOCKED)
    writes = s.store.writes

    again = s.notice(OutboxEvent.BLOCKED, reason=f"  {WAITING}  ")  # the same words, re-run

    assert again.items[0].drafted == "" and s.store.writes == writes
    newer = s.notice(OutboxEvent.BLOCKED, reason="Still waiting; the fix slipped a release.")
    old, new = s.records(report)
    assert newer.items[0].drafted == new.outbox_id
    assert s.state(old) is OpState.SUPERSEDED and s.state(new) is OpState.DRAFTED


def test_a_notice_after_the_work_merged_is_not_drafted_over_the_merged_record() -> None:
    s = Scene()
    report = s.report()
    s.intake.on_work_merged(MergeFact(s.work, MERGE_SHA, "merger/t"))

    outcome = s.notice(OutboxEvent.BLOCKED)

    assert outcome.items[0].drafted == "" and "stale" in outcome.items[0].note
    (merged,) = s.records(report)
    assert s.state(merged) is OpState.DRAFTED


def test_a_notice_on_work_no_report_tracks_drafts_nothing() -> None:
    s = Scene()

    assert s.notice(OutboxEvent.BLOCKED).items == ()


@pytest.mark.parametrize(
    ("event", "reason", "code"),
    [
        (OutboxEvent.BLOCKED, "   ", RefusalReason.REASON_REQUIRED),
        (OutboxEvent.BLOCKED, "x" * (MAX_REASON_CHARS + 1), RefusalReason.INVALID_DRAFT),
        (OutboxEvent.DEPRIORITISED, "Planned for {tag}.", RefusalReason.INVALID_DRAFT),
        (OutboxEvent.STARTED, WAITING, RefusalReason.INVALID_REQUEST),
        (OutboxEvent.MERGED, WAITING, RefusalReason.INVALID_REQUEST),
    ],
)
def test_a_notice_that_cannot_be_posted_is_refused_before_any_write(event, reason, code) -> None:
    s = Scene()
    report = s.report()
    writes = s.store.writes

    with pytest.raises(IntakeRefused) as refused:
        s.notice(event, reason=reason)

    assert refused.value.reason is code
    assert s.store.writes == writes and s.records(report) == []


@pytest.mark.parametrize(
    ("outbox", "milestones", "code", "key"),
    [
        (OutboxPolicy(), ALL_PROGRESS, RefusalReason.OUTBOX_DISABLED, "outbox.enabled"),
        (ON, MilestonePolicy(draft_on_deprioritised=True), RefusalReason.INTAKE_DISABLED, ""),
    ],
)
def test_a_notice_with_its_switch_off_is_refused_naming_it(outbox, milestones, code, key) -> None:
    s = Scene(outbox=outbox, milestones=milestones)
    report = s.report()

    with pytest.raises(IntakeRefused) as refused:
        s.notice(OutboxEvent.BLOCKED)

    assert refused.value.reason is code
    assert (key or "draft_on_blocked") in refused.value.detail
    assert s.records(report) == []


def test_a_notice_on_unknown_work_is_refused() -> None:
    s = Scene()

    with pytest.raises(IntakeRefused) as refused:
        s.notice(OutboxEvent.BLOCKED, work="acme-nope")

    assert refused.value.reason is RefusalReason.NOT_FOUND


# -- sending: notified and the completion policy ----------------------------------------------


@pytest.mark.parametrize(
    "event", [OutboxEvent.STARTED, OutboxEvent.BLOCKED, OutboxEvent.DEPRIORITISED]
)
def test_a_sent_progress_comment_records_notified_and_never_completes_the_report(event) -> None:
    s = Scene()
    report = s.report()
    if event is OutboxEvent.STARTED:
        s.claim()
    else:
        s.notice(event)
    (record,) = s.records(report)

    _send(s, record)

    assert notified_of(s.labels(report)) == event.value
    view = s.store.get(report)
    assert view is not None and view.status == "open"  # progress never completes a report
    assert s.world.write_requests() == 1  # the operator's one approved send, nothing else
