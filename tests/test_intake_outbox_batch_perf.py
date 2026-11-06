"""Perf check of the factory-shaped outbox batch (``bh-pc9rf``): 135 label sends in one process.

``@pytest.mark.perf``: deselected from every gate lane, run on a quiet host with ``-m perf`` (or
``BH_PERF_TESTS=1``); see ``tests/harness/perf_marker.py``. The structural guarantees (one host
check, one issue listing, one pass over the outbox documents) are pinned deterministically by
``tests/unit/modules/intake/test_intake_outbox_batch_send.py``; this file adds the wall clock.

The fakes sleep the latencies measured on the factory on 2026-11-06, scaled by :data:`SCALE` so the
run takes seconds:

* a Beads-service read 65 ms (``bh-10h4y`` and nine more ``read_outbox``), the outbox listing with
  every document 0.8 s;
* a Beads-service metadata write 150 ms (ASSUMED: writes were not measured, the real hive was
  read-only for this bead);
* a GitHub request 0.37 s plus 0.07 s of ``gh auth token`` per operation (the ``gh auth status``
  probe now runs only when that fails; it took the derivation from 0.46 s to 0.07 s);
* the intake lock's host check and epoch read 13 s (``guard.guard_primary`` + ``live_epoch``),
  reused across the batch as with the factory's opt-in ``batch_recheck_seconds: 300``.

The 135 records are add-only, as on the factory (106 add 4 labels, 21 add 3, 7 add 5, 1 adds 6), so
each is ONE GitHub request. Before this bead each send was its own process at 21.7 s (2904 s for
135); the budget below is the batch's modeled total with headroom, and the target the operator set
(135 in under 2 minutes) is recorded next to it: the remainder is the per-request token derivation
and the GitHub round trip, see the bead's measurement comment.
"""

from __future__ import annotations

import time
from collections import Counter

import pytest

from beadhive import intake_services
from beadhive.modules.intake.application import OutboxService
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
    ReportDraft,
    SourceSnapshot,
    StorePosture,
    labels_revision,
)
from beadhive.modules.intake.domain import outbox as machine
from beadhive.modules.tracker.contracts import READ_OPERATIONS, WRITE_OPERATIONS, join_marker_line
from beadhive.testing.intake import (
    SCOPE,
    MemoryOutboxIndex,
    MemoryReportStore,
    RecordingSanitizer,
    SequentialIdSource,
)
from beadhive.testing.tracker import MemoryTracker, MemoryWorld, at

SCALE = 0.05
_slept: list[float] = []  # every scaled latency slept, so the in-process time is not scaled up


@pytest.mark.perf  # a perf-only helper: the wall-clock check exempts it like the test
def _sleep(seconds: float) -> None:
    _slept.append(seconds * SCALE)
    time.sleep(seconds * SCALE)


STORE_READ = 0.065
STORE_LISTING = 0.8
STORE_WRITE = 0.15  # assumed; see the module docstring
GITHUB_OPERATION = 0.37 + 0.07  # the request plus the per-operation `gh auth token` (one gh)
HOST_CHECK = 13.0
REPORTS = 135
BEFORE_SECONDS = 2904.0  # measured on the factory: 135 single-process sends
BUDGET_SECONDS = 200.0  # the modeled latencies (about 160 s) plus in-process work, with headroom
TARGET_SECONDS = 120.0  # the operator's target, moved to follow-up bh-waiaj

LEAKS = LeakPolicy(bead_prefixes=frozenset({"acme"}), owned_label_prefixes=("bh:",))
POLICY = OutboxPolicy(enabled=True, labels_enabled=True)
ADDED = frozenset({"bh:intake:accepted", "bh:type:bug", "bh:priority:p2", "bh:delivery:pending"})


@pytest.mark.perf
class SlowStore(MemoryReportStore):
    """``MemoryReportStore`` sleeping the factory's Beads-service latencies once ``slow``."""

    slow = False
    calls: Counter[str]

    def _wait(self, name: str, seconds: float) -> None:
        if self.slow:
            self.calls[name] += 1
            _sleep(seconds)

    def get(self, bead):
        self._wait("read", STORE_READ)
        return super().get(bead)

    def read_thread(self, bead):
        self._wait("read", STORE_READ)
        return super().read_thread(bead)

    def read_outbox(self, bead):
        self._wait("read", STORE_READ)
        return super().read_outbox(bead)

    def read_outboxes(self):
        slow, self.slow = self.slow, False
        try:
            docs = super().read_outboxes()
        finally:
            self.slow = slow
        self._wait("listing", STORE_LISTING)
        return docs

    def write_outbox(self, bead, document, *, expect_seq, actor):
        self._wait("write", STORE_WRITE)  # the seq re-read is the read_outbox super() makes
        return super().write_outbox(bead, document, expect_seq=expect_seq, actor=actor)


@pytest.mark.perf
class SlowTracker(MemoryTracker):
    """``MemoryTracker`` sleeping one GitHub operation (request plus token) per call."""

    slow = False
    calls: Counter[str]

    def _wait(self, name: str) -> None:
        if self.slow:
            self.calls[name] += 1
            _sleep(GITHUB_OPERATION)

    def list_issues(self, query):
        self._wait("list_issues")
        return super().list_issues(query)

    def get(self, key):
        self._wait("get")
        return super().get(key)

    def change_labels(self, issue, **kwargs):
        self._wait("change_labels")
        return super().change_labels(issue, **kwargs)


@pytest.mark.perf
def test_135_factory_shaped_label_sends_fit_the_batch_budget(tmp_path) -> None:
    world = MemoryWorld()
    world.set_write_time(at(2))
    tracker = SlowTracker(world, enabled=READ_OPERATIONS | WRITE_OPERATIONS, page_size=100)
    tracker.calls = Counter()
    store = SlowStore(SCOPE)
    store.calls = Counter()
    checks: Counter[str] = Counter()

    def host_check() -> None:
        checks["host"] += 1
        if store.slow:
            _sleep(HOST_CHECK / 2)

    def epoch() -> int:
        checks["epoch"] += 1
        if store.slow:
            _sleep(HOST_CHECK / 2)
        return 1

    lock = intake_services.FlockIntakeLock(
        tmp_path / "locks",
        primary=host_check,
        epoch=epoch,
        recheck_seconds=300,  # the factory opt-in (batch_recheck_seconds: 300; default 0)
    )
    sanitizer = RecordingSanitizer.of([])
    service = OutboxService(
        store,
        tracker,
        lock,
        ids=SequentialIdSource(),
        identity=OwnIdentity("acme/widgets", "inst-1"),
        marker_line=lambda marker: f"<!-- bh outbox={marker.outbox_id} -->",
        policy=POLICY,
        leak_policy=LEAKS,
        sanitizer=sanitizer,
        posture=StorePosture(published=False),
        index=MemoryOutboxIndex(),
    )
    for i in range(REPORTS):  # the labels sync's drafts, then their approvals (not timed)
        issue = world.add_issue(f"report {i}", at=at(1), labels=("bug",))
        binding = IssueBinding("", "memory", world.locator, issue.url, issue.identifier)
        report = store.create(
            ReportDraft(
                f"report {i}",
                "bug",
                "origin:github",
                "dev/import",
                binding=binding,
                snapshot=SourceSnapshot("t", "", IssueStatus.OPEN, "r1", labels=("bug",)),
            )
        )
        doc = OutboxDocument()
        intent = LabelIntent(ADDED, frozenset())
        ctx = RenderContext(
            binding.url,
            LEAKS,
            sanitizer,
            labels=LabelBase("r1", frozenset({"bug"})),
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
        oid = SequentialIdSource(start=10_000 + i).outbox_id()
        applied = machine.draft(doc, request, outbox_id=oid, policy=POLICY, ctx=ctx)
        store.write_outbox(report, applied.document, expect_seq=0, actor="outbox")
        rendered = service.render(oid).of(OpKind.LABELS)
        assert rendered is not None
        service.approve(OutboxApprove(oid, rendered.short_digest, "alice", kind=OpKind.LABELS))
    checks.clear()
    _slept.clear()
    store.slow = tracker.slow = True

    started = time.perf_counter()
    outcome = service.send_approved(BatchSend("alice", kinds=(OpKind.LABELS,)))
    elapsed = time.perf_counter() - started

    slept = sum(_slept)
    in_process = elapsed - slept  # the batch's own work, at real speed
    modeled = slept / SCALE + in_process
    print(f"135 label sends: modeled {modeled:.0f}s ({in_process:.2f}s in process)")
    assert outcome.ok and len(outcome.sent) == REPORTS
    assert all(r.state is OpState.SENT for r in outcome.results)
    assert (checks["host"], checks["epoch"]) == (1, 1)  # one host check for the whole batch
    assert tracker.calls["change_labels"] == REPORTS and tracker.calls["get"] == 0
    assert tracker.calls["list_issues"] <= 2 and store.calls["listing"] == 1
    assert store.calls["read"] == 5 * REPORTS and store.calls["write"] == 2 * REPORTS
    assert in_process < 10.0, f"the batch's own work took {in_process:.2f}s for 135 sends"
    assert modeled < BUDGET_SECONDS < BEFORE_SECONDS / 10, (
        f"135 sends modeled at {modeled:.0f}s (budget {BUDGET_SECONDS:.0f}s, "
        f"target {TARGET_SECONDS:.0f}s, before {BEFORE_SECONDS:.0f}s)"
    )
