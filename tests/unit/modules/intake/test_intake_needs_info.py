"""Waiting for the reporter (bh-61xkc): ``intake:needs-info`` and wake-on-reply over MemoryWorld.

``IntakeService`` with the thread service and the supervised sender wired as the root wires them,
over ``MemoryReportStore``, ``MemoryTracker`` / ``MemoryWorld``, the never-waiting
``InProcessIntakeLock``, ``SequentialIdSource`` and ``RecordingSanitizer``. It pins the bead's
acceptance: the needs-info -> wake -> untriaged round trip (a reporter's reply wakes the report;
this install's own message, and our edit of it, do not), a woken report back in the untriaged
queue and still never ready or claimable, the refusal for a born-native report, and every
disposition fielding a needs-info report as it fields an untriaged one. The question is only ever
DRAFTED: it posts only after an operator approves and sends it. Ordering is asserted on ``seq``,
store write counters and ``write_requests()`` only: no clock, no sleep, no socket.
"""

from __future__ import annotations

import pytest

from beadhive.modules.intake.application import (
    IntakeService,
    NullIntake,
    OutboxService,
    StoreOutboxView,
    ThreadService,
)
from beadhive.modules.intake.domain import (
    INTAKE_NEEDS_INFO,
    INTAKE_UNTRIAGED,
    ConsiderRequest,
    Disposition,
    DispositionRequest,
    IntakeRefused,
    IssueBinding,
    IssueStatus,
    LeakPolicy,
    NeedsInfoRequest,
    OpKind,
    OpState,
    OutboxApprove,
    OutboxEvent,
    OutboxPolicy,
    OwnIdentity,
    QueueQuery,
    RefusalReason,
    ReportDraft,
    Role,
    SourceSnapshot,
    StorePosture,
    ThreadRequest,
    WakeRequest,
    is_awaiting_triage,
    is_needs_info,
    may_disposition,
    wake_reopen_text,
)
from beadhive.modules.tracker.contracts import (
    READ_OPERATIONS,
    WRITE_OPERATIONS,
    IssueState,
    OwnMarker,
)
from beadhive.testing.intake import (
    SCOPE,
    InProcessIntakeLock,
    MemoryReportStore,
    RecordingSanitizer,
    SequentialIdSource,
)
from beadhive.testing.tracker import MemoryTracker, MemoryWorld, at
from beadhive.testing.tracker.memory import CommentKey

ME = OwnIdentity("acme/widgets", "inst-1")
LEAKS = LeakPolicy(bead_prefixes=frozenset({"acme", "pub"}))
ON = OutboxPolicy(enabled=True)
PRIVATE = StorePosture(published=False)
REOPENING = OutboxPolicy(enabled=True, reopen_enabled=True)
C, R = OpKind.COMMENT, OpKind.REOPEN
QUESTION = "Which version are you running, and does it happen with a fresh config?"


def marker_line(marker: OwnMarker) -> str:
    """The adapter's footer line, as the GitHub adapter encodes it (``bh:v1``)."""
    return f"<!-- bh:v1 hive={marker.hive} install={marker.install} outbox={marker.outbox_id} -->"


class _NoLabels:
    def violations(self, labels) -> tuple[str, ...]:
        return ()


class Scene:
    """Bound reports on memory issues with comment ingestion and the sender wired."""

    def __init__(
        self, *, outbox: bool = True, threads: bool = True, policy: OutboxPolicy = ON
    ) -> None:
        self.world = MemoryWorld()
        self.world.set_write_time(at(3))
        self.tracker = MemoryTracker(
            self.world, enabled=frozenset(READ_OPERATIONS | WRITE_OPERATIONS)
        )
        self.store = MemoryReportStore(SCOPE)
        self.lock = InProcessIntakeLock()
        self.ids = SequentialIdSource()
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
            policy=policy,
            leak_policy=LEAKS,
            sanitizer=RecordingSanitizer.of(["s3cr3t"]),
            posture=PRIVATE,
        )
        self.intake = IntakeService(
            self.store,
            _NoLabels(),
            threads=self.threads if threads else None,
            outbox=self.sender if outbox else None,
        )
        self.issues: dict[str, object] = {}

    def report(self, title: str = "the app crashes on start") -> str:
        """An imported report: bound, held, ``origin:github`` and ``intake:untriaged``."""
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
        for dimension, value in (("origin", "github"), ("intake", "untriaged")):
            self.store.set_state(report, dimension, value, reason="imported", actor="dev/import")
        self.issues[report] = issue
        return report

    def native(self) -> str:
        """A born-native report (``bh report``): no binding, no hold."""
        report = self.store.create(ReportDraft("an internal report", "bug", "origin:report", "x"))
        for dimension, value in (("origin", "report"), ("intake", "untriaged")):
            self.store.set_state(report, dimension, value, reason="filed", actor="x")
        return report

    def comment(self, report: str, body: str, *, hour: int, day: int = 2, author: str = "reporter"):
        return self.world.add_comment(self.issues[report], body, at=at(day, hour), author=author)

    def ingest(self, report: str) -> None:
        self.intake.ingest_thread(ThreadRequest(report, "dev/ingest"))

    def read_and_consider(self, report: str) -> None:
        """A triage pass: read the thread, then record the read at the seq it saw."""
        view = self.intake.thread_of(report)
        assert view is not None
        self.intake.mark_considered(ConsiderRequest(report, "dev/triage", view.seq))

    def ask(self, report: str, text: str = QUESTION):
        return self.intake.needs_info(NeedsInfoRequest(report, text, "dev/triage"))

    def wake(self, report: str):
        return self.intake.wake(WakeRequest(report, "dev/triage"))

    def send(self, outbox_id: str) -> str:
        """The operator's half: render, approve the digest, send. Returns the posted comment."""
        rendered = self.intake.render(outbox_id).of(C)
        assert rendered is not None
        self.intake.approve(OutboxApprove(outbox_id, rendered.short_digest, "alice"))
        result = self.intake.send(outbox_id, actor="alice")
        assert result.state is OpState.SENT, result
        return result.comment_id

    def labels(self, report: str) -> tuple[str, ...]:
        view = self.store.get(report)
        assert view is not None
        return view.labels

    def queue(self) -> list[str]:
        return [item.id for item in self.intake.queue(QueueQuery()).items]


def refused(expected: RefusalReason, call, /, *args, **kwargs) -> IntakeRefused:
    with pytest.raises(IntakeRefused) as caught:
        call(*args, **kwargs)
    assert caught.value.reason is expected, caught.value
    return caught.value


# -- the vocabulary ----------------------------------------------------------------------------


def test_needs_info_is_awaiting_triage_and_may_be_disposed() -> None:
    assert is_needs_info([INTAKE_NEEDS_INFO]) and not is_needs_info([INTAKE_UNTRIAGED])
    for labels in ([INTAKE_UNTRIAGED], [INTAKE_NEEDS_INFO]):
        assert is_awaiting_triage(labels) and may_disposition(labels)
    for value in ("accepted", "rejected", "rerouted", "promoted"):
        assert not is_awaiting_triage([f"intake:{value}"])
        assert not may_disposition([f"intake:{value}"])


# -- the round trip ----------------------------------------------------------------------------


def test_needs_info_wake_untriaged_round_trip_wakes_only_on_a_reporter_reply() -> None:
    s = Scene()
    report = s.report()
    s.comment(report, "It crashes when I start it.", hour=1)
    s.ingest(report)
    s.read_and_consider(report)
    assert s.queue() == [report]

    asked = s.ask(report)
    assert asked.state_written and not asked.held and asked.outbox_id
    assert INTAKE_NEEDS_INFO in s.labels(report) and INTAKE_UNTRIAGED not in s.labels(report)
    assert s.queue() == []  # waiting for the reporter is no longer a fresh untriaged report
    (record,) = s.store.read_outbox(report).records.values()
    assert record.event is OutboxEvent.REPLY and record.message.text == QUESTION
    assert record.op(C).state is OpState.DRAFTED
    assert s.world.write_requests() == 0  # drafted only: nothing posts without the operator

    # our own question, posted by the operator, never wakes it
    posted = s.send(asked.outbox_id)
    s.ingest(report)
    thread = s.intake.thread_of(report)
    assert thread.thread.records[posted].role is Role.OWN and thread.new_evidence == ()
    assert not s.wake(report).woken and is_needs_info(s.labels(report))

    # nor does our edit of it
    # (our post is stamped at the world's write time, day 3; later activity comes after it)
    s.world.edit_comment(CommentKey(posted), QUESTION + " (edited)", at=at(4, 1))
    s.ingest(report)
    edited = s.intake.thread_of(report)
    assert edited.thread.records[posted].rev == 2 and edited.new_evidence == ()
    idle = s.wake(report)
    assert not idle.woken and "no new reply" in idle.detail
    assert is_needs_info(s.labels(report))

    # the reporter's reply does
    reply = s.comment(report, "v2.3.1, and yes, with a fresh config too.", day=4, hour=2)
    s.ingest(report)
    woken = s.wake(report)
    assert woken.woken and woken.evidence == (reply.id,)
    assert INTAKE_UNTRIAGED in s.labels(report) and INTAKE_NEEDS_INFO not in s.labels(report)
    assert s.queue() == [report]
    # the reply stays new evidence for triage: waking never marks anything considered
    assert s.intake.thread_of(report).new_evidence == (reply.id,)
    # ... and the report is still held: never ready, so never claimable or dispatched
    assert report not in s.store.ready_ids()
    assert any(report in hold.blocks for hold in s.store.holds())
    assert [v for d, v, _ in s.store.history(report) if d == "intake"] == [
        "untriaged",
        "needs-info",
        "untriaged",
    ]
    assert s.world.write_requests() == 1  # only the operator-approved question was ever posted
    refused(RefusalReason.NOT_NEEDS_INFO, s.wake, report)


def test_a_report_is_never_ready_while_it_waits() -> None:
    s = Scene()
    report = s.report()
    assert report not in s.store.ready_ids()
    s.ask(report)
    assert report not in s.store.ready_ids()


def test_a_tombstoned_reporter_comment_is_evidence_that_wakes_too() -> None:
    s = Scene()
    report = s.report()
    first = s.comment(report, "steps: open it", hour=1)
    s.ingest(report)
    s.read_and_consider(report)
    s.ask(report)
    s.world.delete_comment(first)
    for _ in range(2):  # two agreeing complete snapshots tombstone it (the ADR's default)
        s.intake.reconcile_thread(ThreadRequest(report, "dev/ingest"))
    assert s.wake(report).evidence == (first.id,)


# -- asking ------------------------------------------------------------------------------------


def test_asking_a_born_native_report_is_refused_clearly_with_nothing_written() -> None:
    s = Scene()
    report = s.native()
    writes = s.store.writes
    error = refused(RefusalReason.NOT_A_REPORT, s.ask, report)
    assert "not bound to an external issue" in error.detail and "intake-hold" in error.detail
    assert s.store.writes == writes and s.store.read_outbox(report) is None
    refused(RefusalReason.NOT_A_REPORT, s.wake, report)


def test_asking_while_the_thread_holds_unread_evidence_is_refused() -> None:
    s = Scene()
    report = s.report()
    unread = s.comment(report, "here is a stack trace", hour=1)
    s.ingest(report)
    writes = s.store.writes
    error = refused(RefusalReason.UNREAD_EVIDENCE, s.ask, report)
    assert unread.id in error.detail and "thread considered" in error.detail
    assert s.store.writes == writes and is_awaiting_triage(s.labels(report))
    s.read_and_consider(report)
    assert s.ask(report).state_written


def test_asking_needs_the_outbox_and_comment_ingestion_and_a_question() -> None:
    for outbox, threads, reason in (
        (False, True, RefusalReason.OUTBOX_DISABLED),
        (True, False, RefusalReason.INTAKE_DISABLED),
    ):
        s = Scene(outbox=outbox, threads=threads)
        report = s.report()
        writes = s.store.writes
        refused(reason, s.ask, report)
        assert s.store.writes == writes
    s = Scene()
    report = s.report()
    refused(RefusalReason.INVALID_REQUEST, s.ask, report, "   ")
    refused(RefusalReason.NOT_FOUND, s.ask, "acme-404")


def test_asking_a_fielded_or_closed_report_is_refused() -> None:
    s = Scene()
    accepted = s.report("one")
    s.intake.dispose(DispositionRequest(accepted, Disposition.ACCEPT, "dev/triage"))
    refused(RefusalReason.NOT_UNTRIAGED, s.ask, accepted)
    rejected = s.report("two")
    s.intake.dispose(DispositionRequest(rejected, Disposition.REJECT, "x", reason="spam"))
    refused(RefusalReason.REPORT_CLOSED, s.ask, rejected)


def test_asking_again_while_waiting_drafts_another_question_and_keeps_the_state() -> None:
    s = Scene()
    report = s.report()
    first = s.ask(report)
    again = s.ask(report, "Also: which operating system?")
    assert again.outbox_id != first.outbox_id and not again.state_written
    assert [v for d, v, _ in s.store.history(report) if d == "intake"] == [
        "untriaged",
        "needs-info",
    ]
    # the same question against the same thread seq is the same draft (O2), so a re-run after a
    # crash between the draft and the state write drafts nothing twice
    assert s.ask(report, "Also: which operating system?").outbox_id == again.outbox_id


def test_a_draft_held_by_a_leak_finding_still_waits_and_says_so() -> None:
    s = Scene()
    report = s.report()
    asked = s.ask(report, "Is this the same as acme-12?")
    assert asked.held and is_needs_info(s.labels(report))


# -- dispositions from needs-info --------------------------------------------------------------


@pytest.mark.parametrize(
    ("disposition", "kwargs", "value", "closed"),
    [
        (Disposition.ACCEPT, {}, "accepted", False),
        (Disposition.REJECT, {"reason": "cannot reproduce"}, "rejected", True),
        (Disposition.PROMOTE, {}, "promoted", False),
        (Disposition.REROUTE, {"reason": "belongs elsewhere"}, "rerouted", True),
    ],
)
def test_every_disposition_fields_a_needs_info_report_as_an_untriaged_one(
    disposition, kwargs, value, closed
) -> None:
    s = Scene()
    report = s.report()
    s.ask(report)
    outcome = s.intake.dispose(DispositionRequest(report, disposition, "dev/triage", **kwargs))
    assert outcome.state == value and f"intake:{value}" in s.labels(report)
    assert not is_awaiting_triage(s.labels(report))
    assert (s.store.get(report).status == "closed") is closed
    refused(
        RefusalReason.NOT_NEEDS_INFO if not closed else RefusalReason.REPORT_CLOSED, s.wake, report
    )


def test_reject_from_needs_info_still_needs_a_reason_and_reroute_to_a_seat_keeps_it_waiting() -> (
    None
):
    s = Scene()
    report = s.report()
    s.ask(report)
    refused(
        RefusalReason.REASON_REQUIRED,
        s.intake.dispose,
        DispositionRequest(report, Disposition.REJECT, "dev/triage"),
    )
    s.intake.dispose(DispositionRequest(report, Disposition.REROUTE, "x", seat="super/ops"))
    assert is_needs_info(s.labels(report))


# -- wake --------------------------------------------------------------------------------------


def test_waking_without_an_ingested_thread_leaves_it_waiting() -> None:
    s = Scene()
    report = s.report()
    s.ask(report)
    outcome = s.wake(report)
    assert not outcome.woken and "thread ingest" in outcome.detail


def test_waking_needs_comment_ingestion_and_a_waiting_report() -> None:
    s = Scene(threads=False)
    report = s.report()
    refused(RefusalReason.INTAKE_DISABLED, s.wake, report)
    refused(RefusalReason.INTAKE_DISABLED, s.intake.wake_all, actor="x")
    s = Scene()
    report = s.report()
    refused(RefusalReason.NOT_NEEDS_INFO, s.wake, report)
    refused(RefusalReason.NOT_FOUND, s.wake, "acme-404")


def test_the_sweep_wakes_exactly_the_answered_reports() -> None:
    s = Scene()
    answered, silent, untriaged = s.report("one"), s.report("two"), s.report("three")
    s.ask(answered)
    s.ask(silent)
    reply = s.comment(answered, "here you go", hour=4)
    for report in (answered, silent, untriaged):
        s.ingest(report)
    sweep = s.intake.wake_all(actor="dev/triage")
    assert [item.report for item in sweep.items] == [answered, silent]  # waiting reports only
    assert sweep.woken == (answered,) and sweep.errors == ()
    assert sweep.items[0].evidence == (reply.id,)
    assert is_needs_info(s.labels(silent)) and INTAKE_UNTRIAGED in s.labels(answered)
    assert s.intake.wake_all(actor="dev/triage").woken == ()  # idempotent


def test_the_sweep_reports_one_failing_report_and_handles_the_rest() -> None:
    s = Scene()
    broken, answered = s.report("one"), s.report("two")
    s.ask(broken)
    s.ask(answered)
    s.comment(answered, "reply", hour=4)
    s.ingest(answered)
    s.store.put_metadata(broken, "intake.thread", {"schema": 999})  # unreadable: a store error
    sweep = s.intake.wake_all(actor="dev/triage")
    assert [item.report for item in sweep.errors] == [broken]
    assert sweep.woken == (answered,)


def test_null_intake_refuses_waiting_for_the_reporter() -> None:
    null = NullIntake()
    for call in (
        lambda: null.needs_info(NeedsInfoRequest("acme-1", QUESTION, "x")),
        lambda: null.wake(WakeRequest("acme-1", "x")),
        lambda: null.wake_all(actor="x"),
    ):
        refused(RefusalReason.INTAKE_DISABLED, call)


# -- a wake on a closed issue drafts the reopen (bh-ih484) --------------------------------------


def _answered_on(s: Scene, *, closed: bool) -> str:
    """A report that asked its reporter, whose issue is (or is not) closed, and who then replied."""
    report = s.report()
    s.comment(report, "It crashes when I start it.", hour=1)
    s.ingest(report)
    s.read_and_consider(report)
    s.ask(report)
    if closed:
        s.world.update_issue(s.issues[report], at=at(3), state=IssueState.CLOSED)
    s.comment(report, "v2.3.1, and yes, with a fresh config too.", day=4, hour=2)
    s.ingest(report)
    return report


def test_a_wake_on_a_closed_issue_drafts_the_reopen_and_sends_nothing() -> None:
    s = Scene(policy=REOPENING)
    report = _answered_on(s, closed=True)

    woken = s.wake(report)

    assert woken.woken and woken.reopen and woken.detail == ""
    record = s.store.read_outbox(report).records[woken.reopen]
    assert record.message.text == wake_reopen_text()
    assert record.op(C).state is OpState.DRAFTED and record.op(R).state is OpState.DRAFTED
    assert INTAKE_UNTRIAGED in s.labels(report)  # the wake itself is unchanged
    assert s.world.write_requests() == 0  # nothing is sent without an operator's approval


def test_a_wake_on_an_open_issue_drafts_no_reopen() -> None:
    s = Scene(policy=REOPENING)
    report = _answered_on(s, closed=False)

    woken = s.wake(report)

    assert woken.woken and woken.reopen == ""
    assert [r for r in s.store.read_outbox(report).records.values() if r.op(R)] == []


def test_with_the_reopen_gate_off_a_wake_on_a_closed_issue_drafts_nothing() -> None:
    s = Scene()  # reopen_enabled is off by default
    report = _answered_on(s, closed=True)

    woken = s.wake(report)

    assert woken.woken and woken.reopen == "" and woken.detail == ""
    assert [r for r in s.store.read_outbox(report).records.values() if r.op(R)] == []


def test_a_refused_reopen_draft_is_a_detail_and_never_undoes_the_wake(monkeypatch) -> None:
    s = Scene(policy=REOPENING)
    report = _answered_on(s, closed=True)

    def refuse(*_args):
        raise IntakeRefused(RefusalReason.UNRESOLVED_FINDING, "the reply would leak")

    monkeypatch.setattr(s.sender, "draft_reopen", refuse)
    woken = s.wake(report)

    assert woken.woken and woken.reopen == "" and "the reopen was not drafted" in woken.detail
    assert INTAKE_UNTRIAGED in s.labels(report)


def test_the_sweep_drafts_the_reopen_of_each_woken_report_on_a_closed_issue() -> None:
    s = Scene(policy=REOPENING)
    closed = _answered_on(s, closed=True)

    sweep = s.intake.wake_all(actor="dev/triage")

    (item,) = sweep.items
    assert item.report == closed and item.woken and item.reopen
