"""The paced serial outbox write path (``bh-eqtxv``; intake-outbox-paced-send-design.md).

On the memory world, the memory store and the never-waiting lock, with the pacer over a memory
ledger and a fake clock whose time moves only when the pacer waits: no test here sleeps or reads
a real clock. The design's unit tests 1 to 4 and 9 to 13 and its crash injection live here;
the requests counted at the GitHub adapter's transport (tests 5 to 8 and 15) are in
``tests/test_intake_outbox_pacing_github.py`` and the file ledger (14, and 12's two processes) in
``tests/test_intake_write_ledger.py``.
"""

from __future__ import annotations

from collections import Counter
from datetime import timedelta

import pytest
from tests.unit.modules.intake.test_intake_outbox_batch_send import (
    EVERY,
    EVERYTHING,
    LEAKS,
    ME,
    PRIVATE,
    C,
    L,
    Scene,
    X,
    _NoLabels,
    marker_line,
    refused,
)

from beadhive.modules.intake.application import IntakeService, OutboxService, WritePacer, check_pace
from beadhive.modules.intake.application import outbox as translate
from beadhive.modules.intake.domain import (
    MAX_WRITES_PER_HOUR,
    MAX_WRITES_PER_MINUTE,
    BatchSend,
    DraftRequest,
    GrantStatus,
    HourFull,
    IntakeStoreError,
    IssueBinding,
    IssueStatus,
    LabelBase,
    LabelIntent,
    MinuteFull,
    OpState,
    OutboxDocument,
    OutboxEvent,
    RefusalReason,
    RenderContext,
    ReportDraft,
    SendStop,
    SendStopped,
    SlotGranted,
    SourceSnapshot,
    StopReason,
    labels_revision,
    ledger_step,
)
from beadhive.modules.intake.domain import outbox as machine
from beadhive.modules.tracker.contracts import RateLimited, TransientFailure, join_marker_line
from beadhive.testing.intake import FakePacerClock, InProcessIntakeLock, MemoryWriteLedger
from beadhive.testing.tracker import MemoryTracker, at

SPACING = 60 / MAX_WRITES_PER_MINUTE  # 0.75 s at the default 80 a minute


class Paced(Scene):
    """The batch scene with the root's pacer wired in: a memory ledger (shareable) and a fake
    clock. ``new_pacer`` builds the command's pacer, as the composition root does per command,
    and every writer method of the memory tracker passes the pacer's gate first (the memory
    tracker sends one request per write, where the GitHub adapter's transport gates each)."""

    def __init__(self, *, ledger=None, clock=None, per_minute=80, per_hour=500, **kwargs):
        super().__init__(**kwargs)
        self.ledger = ledger if ledger is not None else MemoryWriteLedger()
        self.clock = clock if clock is not None else FakePacerClock()
        self.per_minute, self.per_hour = per_minute, per_hour
        self.starts: list[float] = []  # the wall time each write request started
        self.gate_writes(self.tracker)
        self.pacer = self.new_pacer()

    def new_pacer(self) -> WritePacer:
        pacer = WritePacer(
            self.ledger,
            per_minute=self.per_minute,
            per_hour=self.per_hour,
            monotonic=self.clock.monotonic,
            wall=self.clock.wall,
            wait=self.clock.wait,
        )
        self.service._pacer = self.pacer = pacer
        return pacer

    def gate_writes(self, tracker) -> None:
        for name in ("post_comment", "change_labels", "close_issue"):
            real = getattr(tracker, name)

            def gated(*args, _real=real, **kwargs):
                self.pacer.gate("POST")
                self.starts.append(self.clock.now)  # the request starts after its gate
                return _real(*args, **kwargs)

            setattr(tracker, name, gated)

    def batch(self, **kwargs):
        self.new_pacer()  # one pacer per command
        return self.intake.send_approved(BatchSend("alice", **kwargs))

    def seed(self, count: int, *, age: float) -> None:
        """``count`` ledger entries ``age`` seconds old (another command's writes)."""
        self.ledger.entries.extend(self.clock.now - age for _ in range(count))

    def labelled_report(self, title: str, labels: tuple[str, ...]):
        issue = self.world.add_issue(title, at=at(1), labels=labels)
        binding = IssueBinding("", "memory", self.world.locator, issue.url, issue.identifier)
        snapshot = SourceSnapshot(title, "", IssueStatus.OPEN, "r1", labels=labels)
        draft = ReportDraft(title, "bug", "origin:github", "dev/import", binding=binding)
        report = self.store.create(dataclasses_replace(draft, snapshot=snapshot))
        return report, issue

    def draft_label_change(self, report: str, add, remove, labels) -> str:
        doc = self.store.read_outbox(report) or OutboxDocument()
        oid = self.ids.outbox_id()
        intent = LabelIntent(frozenset(add), frozenset(remove))
        binding = self.binding(report)
        ctx = RenderContext(
            binding.url,
            LEAKS,
            self.sanitizer,
            labels=LabelBase("r1", frozenset(labels)),
            join_marker_line=join_marker_line,
        )
        request = DraftRequest(
            binding,
            OutboxEvent.LABELS,
            labels_revision(doc, binding.url, intent),
            "outbox",
            comment=False,
            labels=intent,
        )
        applied = machine.draft(doc, request, outbox_id=oid, policy=self.service._policy, ctx=ctx)
        self.store.write_outbox(report, applied.document, expect_seq=doc.seq, actor="outbox")
        return oid


def dataclasses_replace(value, **changes):
    import dataclasses

    return dataclasses.replace(value, **changes)


def successor(s: Paced) -> Paced:
    """A new process on the same hive after ``s`` crashed or stopped: the same world, store,
    index, ledger and clock; a new tracker client, a new lock holder (epoch 2) and a new pacer.
    Anything the crash injection patched onto the shared objects is dropped first."""
    for name in ("write_outbox",):
        s.store.__dict__.pop(name, None)
    s.clock.on_wait = None
    fresh = Paced.__new__(Paced)
    fresh.__dict__.update(s.__dict__)
    fresh.lock = InProcessIntakeLock(epoch=2)
    fresh.tracker = MemoryTracker(s.world, enabled=frozenset(EVERYTHING))
    fresh.service = OutboxService(
        s.store,
        fresh.tracker,
        fresh.lock,
        ids=s.ids,
        identity=ME,
        marker_line=marker_line,
        policy=EVERY,
        leak_policy=LEAKS,
        sanitizer=s.sanitizer,
        posture=PRIVATE,
        holder="host-1:43",
        index=s.index,
    )
    fresh.intake = IntakeService(s.store, _NoLabels(), outbox=fresh.service)
    fresh.gate_writes(fresh.tracker)
    fresh.new_pacer()
    return fresh


def _states(s: Scene, items) -> list[OpState]:
    return [s.op(report, oid, kind).state for report, oid, kind in items]


# -- 1, 2: the pacer in the batch ----------------------------------------------------------------


def test_a_paced_batch_keeps_its_structure_and_spaces_every_write_request() -> None:
    """Test 1: the existing batch, paced on the fake clock. The hold count, the two store writes
    per operation and the order are unchanged; each request took one ledger slot, the slots
    are ``60 / per_minute`` apart, and only the spacing was waited for."""
    s = Paced()
    first, _, reply1, labels1 = s.closing_report("one")
    second, _, reply2, labels2 = s.closing_report("two")
    writes: list[str] = []
    real = s.store.write_outbox

    def counted(bead, document, *, expect_seq, actor):
        writes.append(bead)
        return real(bead, document, expect_seq=expect_seq, actor=actor)

    s.store.write_outbox = counted

    outcome = s.batch()

    assert outcome.ok and not outcome.failed and outcome.stopped == ""
    assert [(r.report, r.outbox_id, r.kind) for r in outcome.results] == [
        (first, reply1, C),
        (first, labels1, L),
        (first, reply1, X),
        (second, reply2, C),
        (second, labels2, L),
        (second, reply2, X),
    ]
    assert len(writes) == 2 * 6  # a write-ahead and an outcome per operation
    assert s.lock.batches == 1 and len(s.lock.batched) == 2  # a hold per report
    assert s.world.write_requests() == 6 == s.ledger.taken
    gaps = [b - a for a, b in zip(s.ledger.entries, s.ledger.entries[1:], strict=False)]
    assert gaps == [pytest.approx(SPACING)] * 5
    assert [w for w, _ in s.clock.waits] == [pytest.approx(SPACING)] * 5
    assert outcome.pacing is not None
    assert (outcome.pacing.writes_per_minute, outcome.pacing.writes_per_hour) == (80, 500)
    assert outcome.pacing.requests == 6
    assert outcome.pacing.waited_seconds == pytest.approx(5 * SPACING)


def test_a_slow_write_ahead_never_puts_more_than_80_request_starts_in_a_minute() -> None:
    """The ledger records each request as it STARTS, at the gate, not at the hook: however long
    and uneven the time between the hook and the first request (here a write-ahead that takes 0
    to 3 s), no 60 s window holds more than 80 request starts, counted from the tracker's own
    request timestamps rather than from the ledger."""
    s = Paced()
    for n in range(200):
        report, _ = s.new_report(f"report {n}")
        s.approve(s.reply(report), C)
    lags = [3.0, 0.0, 0.0, 1.7, 0.0, 0.2, 2.9]
    real = s.store.write_outbox
    calls = Counter()

    def slow(bead, document, *, expect_seq, actor):
        calls["n"] += 1
        s.clock.advance(lags[calls["n"] % len(lags)])  # an uneven compare-and-set
        return real(bead, document, expect_seq=expect_seq, actor=actor)

    s.store.write_outbox = slow
    s.clock.advance(3600)  # past the drafts' own writes

    outcome = s.batch()

    assert outcome.ok and len(s.starts) == 200 == s.ledger.taken
    assert max(_window(s.starts, 60)) <= 80
    assert sorted(s.ledger.entries) == sorted(s.starts)  # the entries ARE the request starts


def test_the_hook_runs_after_the_open_and_the_o7_recheck_and_before_the_write_ahead() -> None:
    """Test 2: when the pacer admits an operation its document is read (O13 applied), the O7
    re-check is done, and it is still ``approved`` with its grant minted: no write-ahead yet."""
    s = Paced()
    report, _ = s.new_report("one")
    reply = s.reply(report)
    s.approve(reply, C)
    seen = []
    pacer = s.new_pacer()
    real = pacer.admit

    def admit(requests: int) -> None:
        op = s.op(report, reply, C)
        seen.append((op.state, op.approval.grant, requests, s.world.write_requests()))
        real(requests)

    pacer.admit = admit

    outcome = s.intake.send_approved(BatchSend("alice"))

    assert seen == [(OpState.APPROVED, GrantStatus.MINTED, 1, 0)]
    assert outcome.ok and s.op(report, reply, C).state is OpState.SENT


def test_an_approval_the_o7_recheck_invalidates_takes_no_slot() -> None:
    s = Paced()
    report, _ = s.new_report("one")
    oid = s.draft_labels(report)
    s.approve(oid, L)
    issue = s.world.issues[s.binding(report).identifier].key
    s.world.add_human_label(issue, "beadhive:rejected")  # the render moved since the approval

    outcome = s.batch()

    (result,) = outcome.results
    assert result.state is OpState.DRAFTED and s.ledger.taken == 0


def test_a_stop_during_a_pacer_wait_gives_up_the_slot_and_the_operation_stays_approved() -> None:
    s = Paced()
    first, _, reply1, labels1 = s.closing_report("one")
    stop = SendStop()

    def interrupt(_seconds: float) -> None:  # the first SIGINT arrives during the spacing wait
        stop.set(StopReason.INTERRUPTED, "interrupted")

    s.clock.on_wait = interrupt

    outcome = s.batch(stop=stop)

    assert [(r.outbox_id, r.kind, r.state) for r in outcome.results] == [(reply1, C, OpState.SENT)]
    assert [(f.outbox_id, f.kind, f.reason) for f in outcome.failed] == [
        (labels1, L, "interrupted"),
        (reply1, X, "interrupted"),
    ]
    assert outcome.stopped == "interrupted" and not outcome.ok
    assert s.ledger.taken == 1  # the slot the wait was for was never taken
    assert _states(s, [(first, labels1, L), (first, reply1, X)]) == [OpState.APPROVED] * 2


# -- 3: the hourly budget ------------------------------------------------------------------------


def test_the_hourly_budget_stops_the_batch_and_a_rerun_after_the_hour_continues() -> None:
    s = Paced()
    first, _, reply1, labels1 = s.closing_report("one")
    second, _, reply2, labels2 = s.closing_report("two")
    s.seed(499, age=600)  # another command's writes, ten minutes ago

    outcome = s.batch()

    assert [(r.outbox_id, r.kind, r.state) for r in outcome.results] == [(reply1, C, OpState.SENT)]
    assert outcome.stopped == "hourly_budget" and "frees at" in outcome.stopped_detail
    not_started = [(labels1, L), (reply1, X), (reply2, C), (labels2, L), (reply2, X)]
    assert [(f.outbox_id, f.kind) for f in outcome.failed] == not_started
    assert {f.reason for f in outcome.failed} == {"hourly_budget"}
    assert outcome.pacing.budget_frees_at == pytest.approx(s.clock.now - 600 + 3600)
    grants = {(oid, k): s.op(r, oid, k).approval.grant_id for r, oid, k in _all(s, first, second)}
    assert s.world.write_requests() == 1

    s.clock.advance(3600)  # the hour passes: the re-run continues with the same approvals
    again = s.batch()

    assert again.ok and [(r.outbox_id, r.kind) for r in again.results] == not_started
    for report, oid, kind in _all(s, first, second):
        op = s.op(report, oid, kind)
        assert op.state is OpState.SENT and op.approval.grant_id == grants[(oid, kind)]


def _all(s, first, second):
    out = []
    for report in (first, second):
        doc = s.store.read_outbox(report)
        for record in doc.records.values():
            for op in record.operations:
                out.append((report, record.outbox_id, op.kind))
    return out


def test_a_full_minute_in_the_ledger_delays_the_first_request() -> None:
    s = Paced()
    report, _ = s.new_report("one")
    s.approve(s.reply(report), C)
    s.seed(80, age=20)  # 80 writes 20 s ago: the minute is full for 40 s more

    outcome = s.batch()

    assert outcome.ok and s.clock.waits == [(pytest.approx(40.0), True)]  # at the hook


def test_a_single_send_with_the_hour_spent_is_refused_hourly_budget() -> None:
    s = Paced()
    report, _ = s.new_report("one")
    reply = s.reply(report)
    s.approve(reply, C)
    s.seed(500, age=60)

    err = refused(RefusalReason.HOURLY_BUDGET, s.intake.send, reply, actor="alice")

    assert "frees at" in err.detail
    assert s.op(report, reply, C).state is OpState.APPROVED and s.world.write_requests() == 0


def test_a_labels_change_is_admitted_only_when_all_its_requests_fit() -> None:
    """One addition and two removals are three requests: not admitted when only two fit."""
    s = Paced()
    labels = ("bug", "beadhive:a", "beadhive:b")
    report, _ = s.labelled_report("labelled", labels)
    oid = s.draft_label_change(report, {"beadhive:c"}, {"beadhive:a", "beadhive:b"}, labels)
    s.approve(oid, L)
    s.seed(498, age=600)

    outcome = s.batch()

    (failure,) = outcome.failed
    assert (failure.outbox_id, failure.reason) == (oid, "hourly_budget")
    assert s.op(report, oid, L).state is OpState.APPROVED

    s.ledger.entries.pop()  # one more fits: three of 500
    assert s.batch().ok and s.op(report, oid, L).state is OpState.SENT


# -- a ledger that cannot be used -------------------------------------------------------------


class _BrokenLedger:
    def take(self, now, **kwargs):
        raise IntakeStoreError("read-only file system", operation="write_ledger")


def test_a_ledger_that_cannot_be_used_stops_the_batch_once_with_error() -> None:
    s = Paced(ledger=_BrokenLedger())
    first, _, reply1, labels1 = s.closing_report("one")
    second, _, reply2, _ = s.closing_report("two")

    outcome = s.batch()

    assert outcome.stopped == "error" and "write ledger cannot be used" in outcome.stopped_detail
    assert outcome.results == () and s.world.write_requests() == 0
    assert {f.reason for f in outcome.failed} == {"error"} and len(outcome.failed) == 6
    assert _states(s, [(first, reply1, C), (second, reply2, C)]) == [OpState.APPROVED] * 2


def test_a_single_send_with_a_broken_ledger_is_a_store_error() -> None:
    s = Paced(ledger=_BrokenLedger())
    report, _ = s.new_report("one")
    reply = s.reply(report)
    s.approve(reply, C)
    with pytest.raises(IntakeStoreError, match="read-only"):
        s.intake.send(reply, actor="alice")
    assert s.op(report, reply, C).state is OpState.APPROVED


# -- 4: the bounds ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("per_minute", "per_hour", "named"),
    [(81, 500, "80"), (80, 501, "500"), (0, 500, "80"), (80, 0, "500"), (True, 500, "80")],
)
def test_the_pacer_refuses_a_pace_outside_githubs_limits(per_minute, per_hour, named) -> None:
    with pytest.raises(ValueError, match=named):
        check_pace(per_minute, per_hour)
    assert check_pace(MAX_WRITES_PER_MINUTE, MAX_WRITES_PER_HOUR) == (80, 500)


# -- 9: drain after an interrupt ------------------------------------------------------------------


def test_an_interrupt_between_the_write_ahead_and_the_outcome_drains_that_operation() -> None:
    """Row 12: the stop is set (as the root's handler would) while operation j is between W1 and
    W2. j writes its outcome; no later write-ahead starts; the rest are listed, approved."""
    s = Paced()
    first, _, reply1, labels1 = s.closing_report("one")
    second, _, reply2, labels2 = s.closing_report("two")
    stop = SendStop()
    real = s.tracker.change_labels

    def labels_then_interrupt(*args, **kwargs):
        receipt = real(*args, **kwargs)
        stop.set(StopReason.INTERRUPTED, "interrupted")
        return receipt

    s.tracker.change_labels = labels_then_interrupt

    outcome = s.batch(stop=stop)

    assert [(r.outbox_id, r.kind, r.state) for r in outcome.results] == [
        (reply1, C, OpState.SENT),
        (labels1, L, OpState.SENT),  # drained: its receipt was written
    ]
    assert [(f.outbox_id, f.kind, f.reason) for f in outcome.failed] == [
        (reply1, X, "interrupted"),
        (reply2, C, "interrupted"),
        (labels2, L, "interrupted"),
        (reply2, X, "interrupted"),
    ]
    assert outcome.stopped == "interrupted" and s.world.write_requests() == 2
    assert s.op(first, labels1, L).receipt is not None
    assert _states(s, [(second, reply2, C), (first, reply1, X)]) == [OpState.APPROVED] * 2


# -- 10: the error stop ----------------------------------------------------------------------------


@pytest.mark.parametrize("where", ["receipt", "record_receipt", "outcome_write"])
def test_an_unexpected_error_after_the_request_stops_the_batch_and_is_recovered(
    monkeypatch, where
) -> None:
    s = Paced()
    first, _, reply1, labels1 = s.closing_report("one")
    second, _, reply2, _labels2 = s.closing_report("two")
    if where == "receipt":
        real = translate.receipt
        monkeypatch.setattr(translate, "receipt", _fail_once(real))
    elif where == "record_receipt":
        monkeypatch.setattr(machine, "record_receipt", _fail_once(machine.record_receipt))
    else:
        real_write = s.store.write_outbox
        count = Counter()

        def write(bead, document, *, expect_seq, actor):
            count["n"] += 1
            if count["n"] == 2:  # the first operation's outcome (W2)
                raise RuntimeError("the store broke in an unexpected way")
            return real_write(bead, document, expect_seq=expect_seq, actor=actor)

        s.store.write_outbox = write

    outcome = s.batch()

    assert outcome.stopped == "error" and not outcome.ok
    assert outcome.results == () and outcome.failed[0].reason == "error"
    assert "RuntimeError" in outcome.failed[0].detail
    assert {f.reason for f in outcome.failed[1:]} == {"error"}
    assert s.op(first, reply1, C).state is OpState.SENDING  # the comment WAS posted
    assert s.world.write_requests() == 1
    assert s.op(second, reply2, C).state is OpState.APPROVED
    monkeypatch.undo()

    # a fresh process: its open recovers the operation (O13) and reconcile finds the comment
    fresh = successor(s)
    rerun = fresh.batch()
    assert s.op(first, reply1, C).state is OpState.UNKNOWN
    fresh.intake.reconcile(actor="alice")
    assert s.op(first, reply1, C).state is OpState.SENT
    assert rerun.stopped == "" and s.op(second, reply2, C).state is OpState.SENT
    assert s.world.comment_count(s.world.issues[_issue_id(s, first)].key) == 1  # never twice
    assert s.op(first, labels1, L).state is OpState.SENT


def _fail_once(real):
    calls = Counter()

    def wrapped(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("a bug after the request")
        return real(*args, **kwargs)

    return wrapped


def _issue_id(s: Scene, report: str) -> str:
    return s.binding(report).identifier


# -- 11: a lost compare-and-set mid-batch ---------------------------------------------------------


def test_a_lost_compare_and_set_is_reported_and_the_batch_goes_on() -> None:
    s = Paced()
    first, _, reply1, labels1 = s.closing_report("one")
    second, _, reply2, labels2 = s.closing_report("two")
    real = s.store.write_outbox
    calls = Counter()

    def write(bead, document, *, expect_seq, actor):
        calls["n"] += 1
        if calls["n"] == 1:  # the first comment's W1: nothing written
            raise IntakeStoreError("changed since it was read, nothing was written")
        if calls["n"] == 3:  # the labels op's W2 (after its own W1 and its request)
            raise IntakeStoreError("changed since it was read, nothing was written")
        return real(bead, document, expect_seq=expect_seq, actor=actor)

    s.store.write_outbox = write

    outcome = s.batch()

    assert outcome.stopped == ""
    assert [(f.outbox_id, f.kind, f.reason) for f in outcome.failed] == [
        (reply1, C, "store_error"),
        (labels1, L, "store_error"),
        (reply1, X, "close_not_ready"),  # its comment never went: refused, as before
    ]
    assert s.op(first, reply1, C).state is OpState.APPROVED  # W1 lost: never begun
    # W2 lost: left sending, then recovered by the close's own open in the same hold (O13)
    assert s.op(first, labels1, L).state is OpState.UNKNOWN
    assert all(s.op(second, oid, k).state is OpState.SENT for oid, k in ((reply2, C), (labels2, L)))


# -- 12, 13: shared ledger, clocks ---------------------------------------------------------------


def test_two_pacers_on_one_ledger_never_exceed_the_minute_or_the_hour() -> None:
    """Two commands on one host, interleaved by a scripted fake clock: between them never more
    than 80 starts in any minute or 500 in any hour."""
    ledger, clock = MemoryWriteLedger(), FakePacerClock()
    starts: list[float] = []

    class Recording:
        def take(self, now, **kwargs):
            answer = ledger.take(now, **kwargs)
            if kwargs.get("record", True) and isinstance(answer, SlotGranted):
                starts.append(now)  # a request started (the hook only peeks)
            return answer

    shared = Recording()
    pacers = [
        WritePacer(
            shared,
            per_minute=80,
            per_hour=500,
            monotonic=clock.monotonic,
            wall=clock.wall,
            wait=clock.wait,
        )
        for _ in range(2)
    ]
    live = [True, True]
    for turn in range(2000):
        index = turn % 2
        if not live[index]:
            continue
        try:
            pacers[index].admit(1)
            pacers[index].gate("POST")
        except SendStopped:  # the hourly budget stops that command
            live[index] = False
        if turn % 7 == 0:
            clock.advance(0.1)  # the other command's own work
    assert not any(live)
    assert len(starts) == 500
    assert max(_window(starts, 60)) <= 80 and max(_window(starts, 3600)) <= 500


def _window(starts: list[float], width: float) -> list[int]:
    ordered = sorted(starts)
    return [sum(1 for t in ordered if s <= t < s + width) for s in ordered]


# -- the stated bounds (operator rule, 2026-11-10: every exception is a pinned number) ---------


def _pacers(ledger, clock, count: int) -> list[WritePacer]:
    return [
        WritePacer(
            ledger,
            per_minute=80,
            per_hour=500,
            monotonic=clock.monotonic,
            wall=clock.wall,
            wait=clock.wait,
        )
        for _ in range(count)
    ]


def test_racing_commands_never_start_more_than_80_in_any_minute() -> None:
    """Three commands on one host, every admission peeked before any of their requests starts,
    with uneven lags between: the minute stays strict (each start is one locked ledger step)."""
    ledger, clock = MemoryWriteLedger(), FakePacerClock()
    starts: list[float] = []
    pacers = _pacers(ledger, clock, 3)
    lags = [0.0, 2.0, 0.1, 0.7, 0.0, 1.3]
    for round_ in range(150):
        for pacer in pacers:
            pacer.admit(1)
        for n, pacer in enumerate(pacers):
            clock.advance(lags[(round_ + n) % len(lags)])
            pacer.gate("POST")
            starts.append(clock.now)
    assert len(starts) == 450 and max(_window(starts, 60)) <= 80


def test_racing_commands_go_over_the_hour_by_at_most_their_in_flight_requests() -> None:
    """Three commands each peek 499 of 500 and admit their one-request operation; all three
    start it (never refused mid-operation): 502, the bound being the other commands' in-flight
    requests (here 2 of 500, 0.4% over), and each one over stops its command."""
    ledger, clock = MemoryWriteLedger(), FakePacerClock()
    ledger.entries.extend(clock.now - 600 for _ in range(499))
    stops = [SendStop() for _ in range(3)]
    pacers = _pacers(ledger, clock, 3)
    for pacer, stop in zip(pacers, stops, strict=True):
        pacer.begin(stop)
        pacer.admit(1)
    for pacer in pacers:
        pacer.gate("POST")
    assert len(ledger.entries) == 502 and ledger.overdrafts == 2
    assert [s.reason for s in stops] == [None, StopReason.HOURLY_BUDGET, StopReason.HOURLY_BUDGET]


def test_a_forward_wall_clock_step_lets_at_most_80_more_start_once() -> None:
    """The ledger dates entries by the wall clock: a step forward of 60 s makes the last minute's
    80 starts look old, so 80 more may start in the same real minute (at most 80 extra, 100% over,
    once per step); the 81st after the step waits."""
    ledger, now = MemoryWriteLedger(), 1_800_000_000.0
    for i in range(80):
        assert ledger.take(now + i * 0.5, per_minute=80, per_hour=500) == SlotGranted()
    real = now + 40.0  # 40 real seconds after the first start
    stepped = real + 60.0  # the wall clock jumps a minute ahead
    granted = [ledger.take(stepped, per_minute=80, per_hour=500) for _ in range(81)]
    assert granted.count(SlotGranted()) == 80 and isinstance(granted[-1], MinuteFull)


def test_a_ledger_failing_mid_operation_lets_that_operation_finish_and_stops() -> None:
    """Never refused mid-write: a request whose ledger step fails still goes (unrecorded), the
    command stops with ``error``, and the next operation is not admitted, so the unrecorded
    requests are at most the in-flight operation's."""

    class FailsToRecord(MemoryWriteLedger):
        def take(self, now, **kwargs):
            if kwargs.get("record", True):
                raise IntakeStoreError("disk full", operation="write_ledger")
            return super().take(now, **kwargs)

    clock, stop = FakePacerClock(), SendStop()
    (pacer,) = _pacers(FailsToRecord(), clock, 1)
    pacer.begin(stop)
    pacer.admit(1)
    pacer.gate("POST")  # returns: the request goes

    assert stop.reason is StopReason.ERROR and pacer.summary().requests == 1
    with pytest.raises(SendStopped, match="error"):
        pacer.admit(1)


def test_a_wall_clock_step_back_keeps_future_entries_and_not_the_monotonic_spacing() -> None:
    """Test 13: after the wall clock steps back an hour every entry is in the future; they are
    kept and counted (the minute is full), while the spacing inside the command stays on the
    monotonic clock."""
    now = 1_800_000_000.0
    future = [now + 3600 - i for i in range(80)]
    answer, kept = ledger_step(future, now, per_minute=80, per_hour=500)
    assert isinstance(answer, HourFull) is False and isinstance(answer, MinuteFull)
    assert len(kept) == 80  # nothing pruned
    assert ledger_step(future, now, per_minute=80, per_hour=80)[0] == HourFull(
        now + 3600 - 79 + 3600
    )

    s = Paced()
    report, _ = s.new_report("one")
    s.approve(s.reply(report), C)
    s.approve(s.draft_labels(report), L)
    s.clock.on_wait = lambda _seconds: s.clock.step_wall(-1800)  # a step back inside the wait

    assert s.batch().ok
    assert [w for w, _ in s.clock.waits] == [pytest.approx(SPACING)]  # monotonic spacing only


def test_the_ledger_rule_prunes_the_hour_and_says_when_it_frees() -> None:
    now = 10_000.0
    entries = [now - 4000, now - 3000, now - 10]
    answer, kept = ledger_step(entries, now, per_minute=2, per_hour=3)
    assert answer == SlotGranted() and kept == (now - 3000, now - 10, now)
    answer, kept = ledger_step(kept, now, per_minute=2, per_hour=3)
    assert answer == HourFull(now - 3000 + 3600) and len(kept) == 3
    assert ledger_step(kept, now, per_minute=2, per_hour=9)[0] == MinuteFull(now - 10 + 60)
    over = ledger_step(kept, now, per_minute=9, per_hour=3, overdraw=True)[0]
    assert over == SlotGranted(overdraft=True)


# -- stops from the adapter's seams ---------------------------------------------------------------


def test_the_observer_stops_on_a_rate_limit_and_only_delays_on_a_transient_failure() -> None:
    clock = FakePacerClock()
    pacer = WritePacer(
        MemoryWriteLedger(),
        per_minute=80,
        per_hour=500,
        monotonic=clock.monotonic,
        wall=clock.wall,
        wait=clock.wait,
    )
    stop = SendStop()
    pacer.begin(stop)
    pacer.admit(1)
    pacer.gate("POST")

    pacer.observe(TransientFailure("GitHub answered 503"), 4.0)
    assert not stop.is_set()
    pacer.gate("POST")  # the next slot moved past the wait
    assert clock.waits[-1] == (pytest.approx(4.0), False)

    pacer.observe(RateLimited("secondary", retry_after=timedelta(0), kind="throttle"), 0.0)
    assert stop.reason is StopReason.RATE_LIMITED
    with pytest.raises(SendStopped, match="rate_limited"):
        pacer.admit(1)


def test_an_o11_rate_limit_is_the_backstop_stop() -> None:
    s = Paced()
    first, _, reply1, labels1 = s.closing_report("one")
    second, _, reply2, _ = s.closing_report("two")

    def limited(*_args, **_kwargs):
        raise RateLimited("over the limit", retry_after=timedelta(seconds=600), kind="throttle")

    s.tracker.post_comment = limited

    outcome = s.batch()

    assert outcome.stopped == "rate_limited"
    (result,) = outcome.results
    assert result.state is OpState.FAILED and s.op(first, reply1, C).fail_reason.endswith(
        "RateLimited"
    )
    assert {f.reason for f in outcome.failed} == {"rate_limited"}
    assert s.op(second, reply2, C).state is OpState.APPROVED


# -- crash injection ---------------------------------------------------------------------------


class _Crash(BaseException):
    """A process crash at a named point (never caught by the sender)."""


CRASH_POINTS = ["pacer_wait", "after_w1", "after_q_applied", "before_q", "after_w2", "between"]


@pytest.mark.parametrize("point", CRASH_POINTS)
def test_a_crash_at_any_point_writes_no_issue_twice_and_the_rerun_sends_the_rest(point) -> None:
    s = Paced()
    first, _, reply1, labels1 = s.closing_report("one")
    second, _, reply2, labels2 = s.closing_report("two")
    grants: Counter[str] = Counter()
    _count_grants(s.tracker, grants)
    _arm(s, point)

    with pytest.raises(_Crash):
        s.batch()

    began = {
        (oid, k)
        for r, oid, k in _all(s, first, second)
        if s.op(r, oid, k).state is not OpState.APPROVED
    }
    fresh = successor(s)
    _count_grants(fresh.tracker, grants)
    rerun = fresh.batch()
    for _ in range(3):  # reconcile: a found receipt is sent, two complete absences fail it
        fresh.intake.reconcile(actor="alice")

    assert all(n == 1 for n in grants.values()), grants  # no request of a grant ran twice
    rerun_sent = {(r.outbox_id, r.kind) for r in rerun.results}
    assert not rerun_sent & began  # the re-run sent exactly the operations that never began
    for report, oid, kind in _all(s, first, second):
        op = s.op(report, oid, kind)
        assert op.state in {OpState.SENT, OpState.FAILED, OpState.APPROVED}, (oid, kind, op.state)
        if op.state is OpState.APPROVED:  # never re-approved: a close blocked by its comment
            assert op.approval.grant is GrantStatus.MINTED
        if op.state is OpState.FAILED:
            assert op.fail_reason == machine.ABSENT or op.fail_reason.startswith("refused")


def _count_grants(tracker, grants: Counter[str]) -> None:
    for name in ("post_comment", "change_labels", "close_issue"):
        real = getattr(tracker, name)

        def counted(*args, _real=real, **kwargs):
            grants[kwargs["grant"].grant_id] += 1
            return _real(*args, **kwargs)

        setattr(tracker, name, counted)


def _arm(s: Paced, point: str) -> None:
    calls = Counter()
    if point == "pacer_wait":

        def crash_in_wait(_seconds: float) -> None:
            raise _Crash

        s.clock.on_wait = crash_in_wait
        return
    if point in {"after_w1", "after_w2"}:
        real = s.store.write_outbox
        target = 3 if point == "after_w1" else 4  # the second operation's W1, or its W2

        def write(bead, document, *, expect_seq, actor):
            real(bead, document, expect_seq=expect_seq, actor=actor)
            calls["n"] += 1
            if calls["n"] == target:
                raise _Crash

        s.store.write_outbox = write
        return
    if point in {"after_q_applied", "before_q"}:
        real_labels = s.tracker.change_labels

        def labels(*args, **kwargs):
            if point == "before_q":
                raise _Crash
            real_labels(*args, **kwargs)
            raise _Crash

        s.tracker.change_labels = labels
        return
    real_hold = s.lock.hold

    def hold(binding_key):
        calls["n"] += 1
        if calls["n"] == 2:  # the second report's hold: the first report is done
            raise _Crash
        return real_hold(binding_key)

    s.lock.hold = hold
