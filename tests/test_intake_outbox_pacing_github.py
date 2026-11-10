"""The paced outbox through the REAL GitHub adapter over ``httpx.MockTransport`` (``bh-eqtxv``).

intake-outbox-paced-send-design.md tests 5 to 8 and 15: the pacer is wired into the adapter's
transport as its write gate (before every attempt of a write) and its wait observer (whenever the
retry decision returns a wait), exactly as the composition root wires it, and the outbox sender
runs over the adapter. The adapter's ``sleep``, the transport's clock and the pacer's clocks are
all the fake world's one clock, which moves only when something waits: no test here sleeps.
"""

from __future__ import annotations

import importlib.util
import sys
from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from beadhive.bootstrap.tracker import narrow
from beadhive.modules.intake.application import IntakeService, OutboxService, WritePacer
from beadhive.modules.intake.domain import (
    BatchSend,
    DraftRequest,
    IssueBinding,
    IssueStatus,
    LabelBase,
    LabelIntent,
    LeakPolicy,
    OpKind,
    OpState,
    OutboxApprove,
    OutboxDocument,
    OutboxEvent,
    OutboxPolicy,
    OwnIdentity,
    RenderContext,
    ReplyDraft,
    ReportDraft,
    SendStop,
    SourceSnapshot,
    StopReason,
    StorePosture,
    labels_revision,
)
from beadhive.modules.intake.domain import outbox as machine
from beadhive.modules.tracker.contracts import (
    READ_OPERATIONS,
    Operation,
    RateLimited,
    RetryBudget,
    TransientFailure,
    WriteGrant,
    join_marker_line,
)
from beadhive.testing.intake import (
    SCOPE,
    InProcessIntakeLock,
    MemoryOutboxIndex,
    MemoryReportStore,
    MemoryWriteLedger,
    RecordingSanitizer,
    SequentialIdSource,
)

pytest.importorskip("githubkit")

_FAKE = Path(__file__).resolve().parents[1] / "packages/beadhive-tracker-github/tests"


def _load_fake():
    spec = importlib.util.spec_from_file_location(
        "tracker_github_fake_for_pacing", _FAKE / "tracker_github_fake.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # the fake's dataclasses resolve their module by name
    spec.loader.exec_module(module)
    return module


_fake = _load_fake()
FakeGitHub, adapter_for = _fake.FakeGitHub, _fake.adapter_for

WHEN = datetime(2026, 1, 2, tzinfo=UTC)
ENABLED = READ_OPERATIONS | {
    Operation.COMMENT_CREATE,
    Operation.LABELS_CHANGE,
    Operation.ISSUE_FIELDS,
}
LEAKS = LeakPolicy(bead_prefixes=frozenset({"acme"}), owned_label_prefixes=("bh:",))
POLICY = OutboxPolicy(enabled=True, labels_enabled=True, fields_enabled=True)
C, L = OpKind.COMMENT, OpKind.LABELS
SPACING = 0.75


class _NoLabels:
    def violations(self, labels) -> tuple[str, ...]:
        return ()


def _limited(seconds: int) -> httpx.Response:
    return httpx.Response(
        429,
        json={"message": "You have exceeded a secondary rate limit."},
        headers={"retry-after": str(seconds)},
    )


class Rig:
    """Bound reports on the fake GitHub, the sender over the real adapter, and the pacer wired
    into that adapter's transport. ``on_gate(n)`` runs at the n-th write gate, before the pacer,
    so a test can script the answer to exactly that request."""

    def __init__(self) -> None:
        self.world = world = FakeGitHub()
        world.set_write_time(WHEN)
        world.clock.moment = WHEN
        self.ledger = MemoryWriteLedger()
        self.pacer_waits: list[float] = []
        self.gates: list[str] = []
        self.observed: list[tuple[str, float]] = []
        self.on_gate = None
        self.adapter = adapter_for(
            world,
            enabled=ENABLED,
            label_prefixes=("bh:",),
            command_scoped=True,
            budget=RetryBudget(),
            write_gate=self._gate,
            observe_wait=self._observe,
        )
        self.store = MemoryReportStore(SCOPE)
        self.ids = SequentialIdSource()
        self.sanitizer = RecordingSanitizer.of([])
        self.service = OutboxService(
            self.store,
            narrow(self.adapter, ENABLED),
            InProcessIntakeLock(),
            ids=self.ids,
            identity=OwnIdentity("acme/widgets", "inst-1"),
            marker_line=lambda m: f"<!-- bh:v1 outbox={m.outbox_id} -->",
            policy=POLICY,
            leak_policy=LEAKS,
            sanitizer=self.sanitizer,
            posture=StorePosture(published=False),
            index=MemoryOutboxIndex(),
        )
        self.intake = IntakeService(self.store, _NoLabels(), outbox=self.service)
        self.new_pacer()

    # -- the pacer on the world's one clock ----------------------------------------------------

    def now(self) -> float:
        return self.world.clock.moment.timestamp()

    def _wait(self, seconds: float, stop) -> None:
        self.pacer_waits.append(seconds)
        if stop is not None and stop.is_set():
            return
        self.world.clock.moment += timedelta(seconds=max(0.0, seconds))

    def new_pacer(self) -> WritePacer:
        self.pacer = WritePacer(
            self.ledger,
            per_minute=80,
            per_hour=500,
            monotonic=self.now,
            wall=self.now,
            wait=self._wait,
        )
        self.service._pacer = self.pacer
        return self.pacer

    def _gate(self, method: str) -> None:
        self.gates.append(method)
        if callable(self.on_gate):
            self.on_gate(len(self.gates))
        self.pacer.gate(method)

    def _observe(self, error, seconds: float) -> None:
        self.observed.append((type(error).__name__, seconds))
        self.pacer.observe(error, seconds)

    def batch(self, **kwargs):
        self.new_pacer()
        return self.intake.send_approved(BatchSend("alice", **kwargs))

    # -- reports and records -------------------------------------------------------------------

    def report(self, title: str, labels: tuple[str, ...] = ("bug",)):
        key = self.world.add_issue(title, at=WHEN, labels=labels)
        binding = IssueBinding("", "github", "acme/widgets", key.url, key.identifier)
        snapshot = SourceSnapshot(title, "", IssueStatus.OPEN, "r1", labels=labels)
        report = self.store.create(
            ReportDraft(
                title, "bug", "origin:github", "dev/import", binding=binding, snapshot=snapshot
            )
        )
        return report, key

    def reply(self, report: str) -> str:
        oid = self.intake.draft_reply(ReplyDraft(report, "Thanks, on it.", "alice")).outbox_id
        self.approve(oid, C)
        return oid

    def labels(self, report: str, add, remove, current) -> str:
        doc = self.store.read_outbox(report) or OutboxDocument()
        oid = self.ids.outbox_id()
        intent = LabelIntent(frozenset(add), frozenset(remove))
        binding = self.store.get(report).binding
        ctx = RenderContext(
            binding.url,
            LEAKS,
            self.sanitizer,
            labels=LabelBase("r1", frozenset(current)),
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
        applied = machine.draft(doc, request, outbox_id=oid, policy=POLICY, ctx=ctx)
        self.store.write_outbox(report, applied.document, expect_seq=doc.seq, actor="outbox")
        self.approve(oid, L)
        return oid

    def approve(self, oid: str, kind: OpKind) -> None:
        rendered = self.intake.render(oid).of(kind)
        self.intake.approve(OutboxApprove(oid, rendered.short_digest, "alice", kind=kind))

    def op(self, report: str, oid: str, kind: OpKind):
        return self.store.read_outbox(report).records[oid].op(kind)

    def comments(self, key) -> int:
        return self.world.comment_count(key)


@pytest.fixture
def rig() -> Rig:
    return Rig()


def _three_replies(rig: Rig):
    out = []
    for n in range(3):
        report, key = rig.report(f"report {n}")
        out.append((report, key, rig.reply(report)))
    return out


def _script_at(rig: Rig, gate: int, *responses: httpx.Response) -> None:
    """Answer the request that the ``gate``-th write gate lets through with ``responses``."""

    def script(n: int) -> None:
        if n == gate:
            rig.world.scripted = deque(responses)

    rig.on_gate = script


# -- 5, 6, 15: the stop on the first rate limit ---------------------------------------------------


def test_a_rate_limit_stops_at_its_first_wait_and_the_resend_takes_its_own_slot(rig) -> None:
    """Test 5: a 429 with ``retry-after`` on operation j's request: the observer stops the batch
    with ``rate_limited`` and moves the next slot; the adapter's resend takes a gate slot and
    succeeds, so j is sent; no later write-ahead starts, and a re-run sends the rest."""
    (r0, k0, o0), (r1, k1, o1), (r2, k2, o2) = _three_replies(rig)
    _script_at(rig, 2, _limited(5))  # operation j = the second reply

    outcome = rig.batch()

    assert outcome.stopped == "rate_limited" and "asked for 5 s" in outcome.stopped_detail
    assert [(r.outbox_id, r.state) for r in outcome.results] == [
        (o0, OpState.SENT),
        (o1, OpState.SENT),  # its own retry went through
    ]
    assert [(f.outbox_id, f.reason) for f in outcome.failed] == [(o2, "rate_limited")]
    assert rig.observed == [("RateLimited", 5.0)]
    assert rig.world.sleeps == [5.0]  # the adapter slept as GitHub asked
    assert rig.gates == ["POST", "POST", "POST"]  # j's resend went through the gate
    assert rig.ledger.taken == 3 and outcome.pacing.requests == 3
    assert rig.op(r2, o2, C).state is OpState.APPROVED and rig.comments(k2) == 0
    assert rig.comments(k1) == 1

    again = rig.batch()

    assert again.ok and [r.outbox_id for r in again.results] == [o2]
    assert rig.comments(k2) == 1


def test_a_wait_longer_than_the_budget_is_the_o11_backstop(rig) -> None:
    """Test 6: GitHub asks for more than the budget's single wait: the adapter raises without
    sleeping (so the observer never sees it), j is O11 ``refused:RateLimited`` and the batch
    stops with ``rate_limited``."""
    (r0, _, o0), (r1, k1, o1), (r2, _, o2) = _three_replies(rig)
    _script_at(rig, 2, _limited(600))

    outcome = rig.batch()

    assert rig.observed == [] and rig.world.sleeps == []
    assert [(r.outbox_id, r.state) for r in outcome.results] == [
        (o0, OpState.SENT),
        (o1, OpState.FAILED),
    ]
    assert rig.op(r1, o1, C).fail_reason == "refused:RateLimited" and rig.comments(k1) == 0
    assert outcome.stopped == "rate_limited"
    assert [(f.outbox_id, f.reason) for f in outcome.failed] == [(o2, "rate_limited")]


def test_a_retry_after_of_zero_still_reaches_the_observer_and_stops(rig) -> None:
    """Test 15: ``retry-after: 0`` is a wait of 0: the observer sees it (with no sleep) and the
    batch stops."""
    (_, _, o0), (_, _, o1), (r2, _, o2) = _three_replies(rig)
    _script_at(rig, 1, _limited(0))

    outcome = rig.batch()

    assert rig.observed == [("RateLimited", 0.0)] and rig.world.sleeps == []
    assert outcome.stopped == "rate_limited"
    assert [(r.outbox_id, r.state) for r in outcome.results] == [(o0, OpState.SENT)]
    assert [f.outbox_id for f in outcome.failed] == [o1, o2]
    assert rig.op(r2, o2, C).state is OpState.APPROVED


# -- 8: per-request counting -------------------------------------------------------------------


def test_a_labels_change_takes_a_slot_per_request(rig) -> None:
    """One addition and two removals: three requests, three ledger entries, the second and the
    third each after its own gate slot (the spacing)."""
    current = ("bug", "bh:a", "bh:b")
    report, key = rig.report("labelled", current)
    oid = rig.labels(report, {"bh:c"}, {"bh:a", "bh:b"}, current)

    outcome = rig.batch()

    assert outcome.ok and rig.op(report, oid, L).state is OpState.SENT
    assert rig.gates == ["POST", "DELETE", "DELETE"]
    assert rig.ledger.taken == 3 == outcome.pacing.requests
    assert rig.pacer_waits == [pytest.approx(SPACING)] * 2
    assert rig.world.server_labels(key) == frozenset({"bug", "bh:c"})


def test_a_request_over_the_spent_hour_is_recorded_and_stops_the_next_operation(rig) -> None:
    """With the hour spent while an operation is mid-write, its further request is not refused:
    it is recorded (an overdraft), the operation completes, and ``hourly_budget`` keeps the next
    operation from starting."""
    current = ("bug", "bh:a", "bh:b")
    report, key = rig.report("labelled", current)
    oid = rig.labels(report, {"bh:c"}, {"bh:a", "bh:b"}, current)
    other, other_key = rig.report("next")
    reply = rig.reply(other)
    now = rig.now()
    rig.ledger.entries.extend(now - 600 for _ in range(497))  # three fit: the op is admitted

    def another_command(n: int) -> None:
        if n == 1:  # meanwhile another command on the host spends the rest of the hour
            rig.ledger.entries.extend(rig.now() - 300 for _ in range(2))

    rig.on_gate = another_command

    outcome = rig.batch()

    assert rig.op(report, oid, L).state is OpState.SENT  # completed, never half-applied
    assert rig.ledger.overdrafts == 2 and rig.ledger.taken == 3
    assert outcome.stopped == "hourly_budget"
    assert [(f.outbox_id, f.reason) for f in outcome.failed] == [(reply, "hourly_budget")]
    assert rig.op(other, reply, C).state is OpState.APPROVED and rig.comments(other_key) == 0


def test_a_field_write_retried_on_stale_ids_counts_two_requests(rig) -> None:
    """An ``issue.fields`` whose first ``POST`` answers 422 on memoized ids re-reads the
    definitions and posts once more on the same grant: two ledger entries for that operation."""
    first, second = (rig.world.add_issue(t, at=WHEN) for t in ("one", "two"))
    rig.new_pacer().begin(SendStop())
    rig.pacer.admit(1)
    rig.adapter.set_issue_fields(first, {"Priority": "High"}, grant=_grant(first, "g-1"))
    assert rig.ledger.taken == 1  # one write; the definitions were read (not paced)

    def stale(n: int) -> None:
        if n == 2:  # the second operation's first POST meets ids GitHub no longer has
            rig.world.scripted = deque([httpx.Response(422, json={"message": "Validation"})])

    rig.on_gate = stale
    rig.pacer.admit(1)
    receipt = rig.adapter.set_issue_fields(second, {"Priority": "High"}, grant=_grant(second))

    assert receipt.fields_after == (("Priority", "High"),)
    assert rig.gates == ["POST", "POST", "POST"] and rig.ledger.taken == 3
    assert rig.pacer_waits == [pytest.approx(SPACING)] * 2  # each POST after the first waited


def test_one_command_goes_over_the_hour_by_at_most_its_operations_unexpected_requests(
    rig,
) -> None:
    """The worst one operation can do past its admission, alone on the host: an
    ``issue.fields`` admitted with 1 of 500 left sends 8 requests (3 rate-limit resends, a 422 on
    memoized ids, the re-read, then 3 more resends): 7 over the hour (1.4%), each recorded, and
    the command stops. A ``labels.change`` is bounded by 3 resends per request, others by 3."""
    first, second = (rig.world.add_issue(t, at=WHEN) for t in ("one", "two"))
    stop = SendStop()
    rig.new_pacer().begin(stop)
    rig.pacer.admit(1)
    rig.adapter.set_issue_fields(first, {"Priority": "High"}, grant=_grant(first, "g-1"))
    rig.ledger.entries.extend(rig.now() - 600 for _ in range(498))  # 499 of 500 now

    def worst(n: int) -> None:
        if n == 2:  # the second operation's first POST: three limits, then stale ids
            rig.world.scripted = deque([_limited(0)] * 3 + [httpx.Response(422, json={})])
        if n == 6:  # its retry after the re-read: three more limits, then it goes
            rig.world.scripted = deque([_limited(0)] * 3)

    rig.on_gate = worst
    rig.pacer.admit(1)
    rig.adapter.set_issue_fields(second, {"Priority": "High"}, grant=_grant(second))

    assert rig.gates.count("POST") == 9  # one for the first operation, eight for the second
    assert len(rig.ledger.entries) == 507  # 7 over 500, every one recorded
    assert stop.reason is StopReason.RATE_LIMITED  # its first rate limit stopped the command
    assert rig.world.server_issue_fields(second) == {"Priority": "High"}


def _grant(issue, grant_id: str = "g-2") -> WriteGrant:
    return WriteGrant(grant_id, Operation.ISSUE_FIELDS, issue)


# -- 7: a read's backoff inside a write operation -----------------------------------------------


def test_a_server_error_on_the_definitions_read_delays_and_never_stops(rig) -> None:
    """Test 7: a 503 on the field-definitions read of an ``issue.fields`` reaches the observer
    as ``TransientFailure``, moves the next slot and does not stop; the write is then sent."""
    issue = rig.world.add_issue("one", at=WHEN)
    stop = SendStop()
    rig.new_pacer().begin(stop)
    rig.pacer.admit(1)
    rig.world.scripted = deque([httpx.Response(503, json={"message": "unavailable"})])

    receipt = rig.adapter.set_issue_fields(issue, {"Priority": "High"}, grant=_grant(issue))

    assert rig.observed == [("TransientFailure", 1.0)] and rig.world.sleeps == [1.0]
    assert not stop.is_set()
    assert receipt.fields_after == (("Priority", "High"),)
    assert rig.gates == ["POST"] and rig.ledger.taken == 1  # the admitted slot, used once
    assert rig.world.server_issue_fields(issue) == {"Priority": "High"}


def test_the_observer_classifies_by_the_error_it_is_given() -> None:
    rig = Rig()
    stop = SendStop()
    rig.new_pacer().begin(stop)
    rig.pacer.observe(TransientFailure("503"), 2.0)
    assert not stop.is_set()
    rig.pacer.observe(RateLimited("x", retry_after=None, kind="quota"), 60.0)
    assert stop.reason is StopReason.RATE_LIMITED


# -- crash after the first of two label requests -----------------------------------------------


class _Crash(BaseException):
    pass


def test_a_crash_between_two_label_requests_never_writes_twice(rig) -> None:
    """Row 6: a crash in the gate before the second request leaves the operation ``sending``
    with its first request applied; a new process recovers it (O13) and reconcile reads the
    labels back, never sending it again without an operator."""
    current = ("bug", "bh:a")
    report, key = rig.report("labelled", current)
    oid = rig.labels(report, {"bh:c"}, {"bh:a"}, current)

    def crash(n: int) -> None:
        if n == 2:
            raise _Crash

    rig.on_gate = crash
    with pytest.raises(_Crash):
        rig.batch()
    assert rig.op(report, oid, L).state is OpState.SENDING
    assert rig.world.write_requests() == 1 and "bh:c" in rig.world.server_labels(key)

    rig.on_gate = None
    successor = OutboxService(
        rig.store,
        narrow(rig.adapter, ENABLED),
        InProcessIntakeLock(epoch=2),
        ids=rig.ids,
        identity=OwnIdentity("acme/widgets", "inst-1"),
        marker_line=lambda m: f"<!-- bh:v1 outbox={m.outbox_id} -->",
        policy=POLICY,
        leak_policy=LEAKS,
        sanitizer=rig.sanitizer,
        posture=StorePosture(published=False),
    )
    successor._pacer = rig.new_pacer()
    rerun = successor.send_approved(BatchSend("alice"))
    assert rerun.results == () and rig.world.write_requests() == 1  # not sendable: never again
    for _ in range(3):
        successor.reconcile(actor="alice")
    op = rig.op(report, oid, L)
    assert op.state in {OpState.FAILED, OpState.UNKNOWN} and rig.world.write_requests() == 1
