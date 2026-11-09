"""Perf check of the factory-shaped intake batches (``bh-pc9rf``, ``bh-25fvb``): drafting,
approving and sending the issue type and Priority of 135 issues, and one single send.

``@pytest.mark.perf``: deselected from every gate lane, run on a quiet host with ``-m perf`` (or
``BH_PERF_TESTS=1``); see ``tests/harness/perf_marker.py``. The structural guarantees (one batch
scope, one hold and one report read per report, one approval write per report, one issue listing,
one pass over the outbox documents) are pinned deterministically by
``tests/unit/modules/intake/test_intake_outbox_batch_send.py`` and
``tests/test_intake_api_store.py``; this file adds the wall clock.

The fakes sleep latencies MEASURED on 2026-11-09 (bh-25fvb), scaled by :data:`SCALE` so the run
takes seconds:

* a Beads-service read of one bead, 77 ms (median of 80 ``GET issue`` on the factory's own
  ``github/beadhive/beadhive`` service, read-only); the outbox listing of 135 documents 0.85 s;
* a Beads-service metadata compare-and-set, 124 ms: 66 ms measured on a disposable scratch hive
  (``bd init --server`` + ``bd serve``, bd 1.3.0, 36 writes) scaled by the factory/scratch read
  ratio (77 / 41 ms). It replaces the 150 ms the bh-pc9rf model ASSUMED; the factory's own
  service was not written to (its writes would be real bead history);
* a GitHub request 0.35 s with a fresh client (the adapter opens one per operation), plus 0.07 s
  of ``gh auth token`` per operation unless the opt-in per-invocation token cache keeps the first;
  an ``issue.fields`` write is TWO requests (the org's field definitions, then the write);
* the intake lock's host check and epoch read, now ONE evaluation (``guard.primary_epoch``),
  1.85 s per hold on the factory (13 s before): once per batch with the factory's opt-in
  ``batch_recheck_seconds: 300``, once per hold at the default 0 (computed from the hold count);
* one ``bh`` process before its verb runs, 4.4 s (``bh work outbox show <id>`` took 5.6 s end to
  end on the factory, 1.2 s of it the verb; 8.5 s before this bead).

Before this bead (factory, 0.35.3, 2026-11-06): labels sync draft 745 s, labels approve --batch
1440 s and send-approved 1770 s for the 270 type and Priority operations of 135 issues, and about
13 s of host check in every single send.
"""

from __future__ import annotations

import contextlib
import time
from collections import Counter

import pytest

from beadhive import intake_services
from beadhive.modules.intake.application import IntakeService, LabelProjection, OutboxService
from beadhive.modules.intake.domain import (
    BatchSend,
    IssueBinding,
    IssueStatus,
    LabelBatchApprove,
    LabelDefineRequest,
    LabelSettings,
    LabelSyncRequest,
    LeakPolicy,
    OpKind,
    OpState,
    OutboxPolicy,
    OwnIdentity,
    RegistryDimension,
    ReportDraft,
    SourceSnapshot,
    StorePosture,
    ValueSettings,
    WorkDraft,
)
from beadhive.modules.tracker.contracts import READ_OPERATIONS, WRITE_OPERATIONS
from beadhive.testing.intake import SCOPE, MemoryReportStore, RecordingSanitizer, SequentialIdSource
from beadhive.testing.tracker import MemoryTracker, MemoryWorld, at

SCALE = 0.05
_slept: list[float] = []  # every scaled latency slept, so the in-process time is not scaled up


@pytest.mark.perf  # a perf-only helper: the wall-clock check exempts it like the test
def _sleep(seconds: float) -> None:
    _slept.append(seconds * SCALE)
    time.sleep(seconds * SCALE)


STORE_READ = 0.077
STORE_LISTING = 0.85
SCRATCH_READ, SCRATCH_WRITE = 0.041, 0.066  # measured on the disposable scratch hive
STORE_WRITE = SCRATCH_WRITE * STORE_READ / SCRATCH_READ  # about 124 ms on the factory
CLI_WRITE = 0.4  # a `bd set-state` subprocess (a CLI-only write)
GITHUB_REQUEST = 0.35
GITHUB_TOKEN = 0.07  # one `gh auth token`, per operation unless the per-invocation cache keeps it
HOST_CHECK = 1.85
PROCESS = 4.4
REPORTS = 135
OPS = 2 * REPORTS  # the type and the Priority of every issue, as on the factory

BEFORE = {"draft": 745.0, "approve": 1440.0, "send": 1770.0, "single send": 21.0}
TARGET = {"draft": 120.0, "approve": 120.0, "send": 120.0, "single send": 3.0}
#: The modeled totals with headroom (the opt-in recheck 300): the bar this check holds. Draft
#: (about 57 s) and approve (about 46 s) are under their 2-minute target. The send (about 256 s,
#: 238 s with the token cache) is not: its 270 operations are 405 GitHub requests (an
#: ``issue.fields`` write reads the org's field definitions first), 142 s at 0.35 s each, and 540
#: compare-and-set writes (each operation's write-ahead and receipt), 67 s. The single send (about
#: 8 s) is not under 3 s: 4.4 s of it is the process, 1.85 s the host check. Both remainders are
#: reported on the bead.
BUDGET = {"draft": 90.0, "approve": 60.0, "send": 280.0, "single send": 9.0}

LEAKS = LeakPolicy(
    bead_prefixes=frozenset({"acme"}),
    private_dimensions=frozenset({"intake", "origin", "delivery", "notified", "review"}),
    owned_label_prefixes=("bh:",),
)
POLICY = OutboxPolicy(enabled=True, labels_enabled=True, type_enabled=True, fields_enabled=True)
OPERATOR = "alice"


@pytest.mark.perf
class SlowStore(MemoryReportStore):
    """``MemoryReportStore`` sleeping the Beads-service latencies once ``slow``, with the
    production store's ``ReportSession`` semantics: inside ``session()`` a bead is read once,
    and the ``seq`` re-read before a write is that read."""

    slow = False
    calls: Counter[str]
    _session: set[str] | None = None
    _quiet = False

    @contextlib.contextmanager
    def session(self):
        if self._session is not None:
            yield
            return
        self._session = set()
        try:
            yield
        finally:
            self._session = None

    def _wait(self, name: str, seconds: float) -> None:
        if self.slow and not self._quiet:
            self.calls[name] += 1
            _sleep(seconds)

    def _read(self, bead: str) -> None:
        if self._session is not None:
            if bead in self._session:
                return  # the session's one read of it
            self._session.add(bead)
        self._wait("read", STORE_READ)

    def get(self, bead):
        self._read(bead)
        return super().get(bead)

    def tracked_work(self, report):
        self._read(report)  # the service answers from the report's own row
        return super().tracked_work(report)

    def read_thread(self, bead):
        self._read(bead)
        return super().read_thread(bead)

    def read_outbox(self, bead):
        self._read(bead)
        return super().read_outbox(bead)

    def read_outboxes(self):
        self._quiet = True
        try:
            docs = super().read_outboxes()
        finally:
            self._quiet = False
        self._wait("listing", STORE_LISTING)
        return docs

    def bound_reports(self):
        self._wait("listing", STORE_LISTING)
        return super().bound_reports()

    def write_outbox(self, bead, document, *, expect_seq, actor):
        self._read(bead)  # the seq re-read (free inside a session that read the report)
        self._quiet = True
        try:
            super().write_outbox(bead, document, expect_seq=expect_seq, actor=actor)
        finally:
            self._quiet = False
        self._wait("write", STORE_WRITE)

    def set_state(self, bead, dimension, value, *, reason, actor):
        if self._session is not None:
            self._session.clear()  # any other write drops what the session read
        self._wait("cli", CLI_WRITE)
        return super().set_state(bead, dimension, value, reason=reason, actor=actor)


@pytest.mark.perf
class SlowTracker(MemoryTracker):
    """``MemoryTracker`` sleeping its GitHub requests per call (and per page), plus the token."""

    slow = False
    token_cache = False
    calls: Counter[str]

    def _wait(self, name: str, requests: int = 1) -> None:
        if not self.slow:
            return
        self.calls[name] += 1
        token = 0.0 if self.token_cache and self.calls["token"] else GITHUB_TOKEN
        self.calls["token"] += 1
        _sleep(requests * GITHUB_REQUEST + token)

    def list_issues(self, query):
        self._wait("list_issues")
        return super().list_issues(query)

    def get(self, key):
        self._wait("get")
        return super().get(key)

    def list_label_definitions(self):
        self._wait("list_label_definitions")
        return super().list_label_definitions()

    def probe_capabilities(self):
        self._wait("probe_capabilities")
        return super().probe_capabilities()

    def set_issue_type(self, issue, issue_type, **kwargs):
        self._wait("set_issue_type")
        return super().set_issue_type(issue, issue_type, **kwargs)

    def set_issue_fields(self, issue, values, **kwargs):
        self._wait("set_issue_fields", requests=2)  # the org's field definitions, then the write
        return super().set_issue_fields(issue, values, **kwargs)


class _NoLabels:
    def violations(self, labels) -> tuple[str, ...]:
        return ()


class Factory:
    """135 bound reports, each tracking work whose type and priority its issue lacks, wired as
    the composition root wires them; the lock is the root's ``FlockIntakeLock`` with the
    factory's opt-in ``recheck_seconds=300`` and its holds counted in ``holds``."""

    def __init__(self, tmp_path, holds: list[str], *, token_cache: bool) -> None:
        self.world = world = MemoryWorld()
        world.set_write_time(at(2))
        self.tracker = SlowTracker(world, enabled=READ_OPERATIONS | WRITE_OPERATIONS, page_size=100)
        self.tracker.calls = Counter()
        self.tracker.token_cache = token_cache
        self.store = SlowStore(SCOPE)
        self.store.calls = Counter()
        self.holds = holds
        self.checks = 0

        def admit() -> int:  # guard.primary_epoch: the host check and the epoch, one evaluation
            self.checks += 1
            if self.store.slow:
                _sleep(HOST_CHECK)
            return 1

        lock = intake_services.FlockIntakeLock(tmp_path / "locks", admit=admit, recheck_seconds=300)
        ids = SequentialIdSource()
        sanitizer = RecordingSanitizer.of([])
        private = StorePosture(published=False)
        sender = OutboxService(
            self.store,
            self.tracker,
            lock,
            ids=ids,
            identity=OwnIdentity("acme/widgets", "inst-1"),
            marker_line=lambda marker: f"<!-- bh outbox={marker.outbox_id} -->",
            policy=POLICY,
            leak_policy=LEAKS,
            sanitizer=sanitizer,
            posture=private,
        )
        projection = LabelProjection(
            self.store,
            self.tracker,
            lock,
            ids=ids,
            registry={"component": RegistryDimension("component", "Area")},
            settings=LabelSettings(),
            leak_policy=LEAKS,
            sanitizer=sanitizer,
            posture=private,
            outbox=POLICY,
            values=ValueSettings(),
        )
        self.intake = IntakeService(self.store, _NoLabels(), outbox=sender, projection=projection)
        for i in range(REPORTS):
            key = world.add_issue(f"report {i}", at=at(1), labels=("bug",))
            binding = IssueBinding("", "memory", world.locator, key.url, key.identifier)
            report = self.store.create(
                ReportDraft(
                    f"report {i}",
                    "bug",
                    "origin:github",
                    "dev/import",
                    binding=binding,
                    snapshot=SourceSnapshot("t", "", IssueStatus.OPEN, "r1", labels=("bug",)),
                )
            )
            self.store.set_state(report, "intake", "accepted", reason="triage", actor="import")
            work = self.store.create_work(WorkDraft("fix", "feature", "dev/x", priority="1"))
            self.store.track(report, work, actor="dev/x")
        plan = self.intake.define_labels(LabelDefineRequest(OPERATOR)).plan
        self.intake.define_labels(LabelDefineRequest(OPERATOR, approve=plan.short_digest))

    def timed(self, call):
        """``(result, modeled seconds, holds, host checks)`` of one command: the slept latencies
        unscaled, plus the in-process time at real speed, plus one process."""
        self.store.calls.clear()
        self.tracker.calls.clear()
        self.store.slow = self.tracker.slow = True
        _slept.clear()
        checks, holds = self.checks, len(self.holds)
        started = time.perf_counter()
        try:
            result = call()
        finally:
            elapsed = time.perf_counter() - started
            self.store.slow = self.tracker.slow = False
        slept = sum(_slept)
        modeled = slept / SCALE + (elapsed - slept) + PROCESS
        return result, modeled, len(self.holds) - holds, self.checks - checks


@pytest.fixture
def holds(monkeypatch) -> list[str]:
    """Every hold of the root lock (at the default recheck 0, each is one host check)."""
    taken: list[str] = []
    real = intake_services.FlockIntakeLock.hold

    @contextlib.contextmanager
    def hold(self, binding_key):
        taken.append(binding_key)
        with real(self, binding_key) as epoch:
            yield epoch

    monkeypatch.setattr(intake_services.FlockIntakeLock, "hold", hold)
    return taken


def _line(phase: str, modeled: float, holds: int, checks: int) -> str:
    at_zero = modeled + (holds - checks) * HOST_CHECK
    return (
        f"{phase:8s} modeled {modeled:6.1f}s (recheck 300), {at_zero:6.1f}s (default 0); "
        f"target {TARGET[phase]:.0f}s, before {BEFORE[phase]:.0f}s"
    )


@pytest.mark.perf
@pytest.mark.parametrize("token_cache", [False, True], ids=["no-token-cache", "token-cache"])
def test_135_issues_draft_approve_and_send_fit_their_budgets(tmp_path, holds, token_cache) -> None:
    f = Factory(tmp_path, holds, token_cache=token_cache)

    synced, draft, held, checks = f.timed(lambda: f.intake.sync_labels(LabelSyncRequest(OPERATOR)))
    assert len(synced.drafted) == REPORTS and checks == 1  # one host check for the batch
    lines = [_line("draft", draft, held, checks)]

    batch = f.intake.review_label_batch()
    assert len(batch.items) == 3 * REPORTS  # the labels, type and fields of every issue
    approved, approve, held, checks = f.timed(
        lambda: f.intake.approve_label_batch(LabelBatchApprove(batch.short_digest, OPERATOR))
    )
    assert len(set(approved.grant_ids)) == len(approved.approved) == 3 * REPORTS
    assert held == REPORTS and f.store.calls["write"] == REPORTS  # one write per report
    lines.append(_line("approve", approve, held, checks))

    sent, send, held, checks = f.timed(
        lambda: f.intake.send_approved(BatchSend(OPERATOR, kinds=(OpKind.TYPE, OpKind.FIELDS)))
    )
    assert sent.ok and len(sent.sent) == OPS
    assert all(r.state is OpState.SENT for r in sent.results)
    assert held == REPORTS and checks == 1  # a hold per report, one host check for the batch
    # per report: its binding, then ONE read under its hold for both operations' four writes
    assert f.store.calls["read"] == 2 * REPORTS and f.store.calls["write"] == 2 * OPS
    assert f.tracker.calls["set_issue_type"] == f.tracker.calls["set_issue_fields"] == REPORTS
    lines.append(_line("send", send, held, checks))

    print("\n" + "\n".join(lines))
    assert draft < BUDGET["draft"] <= TARGET["draft"]
    assert approve < BUDGET["approve"] <= TARGET["approve"]
    assert send < BUDGET["send"] < BEFORE["send"] / 6


@pytest.mark.perf
def test_one_send_is_one_process_one_host_check_and_its_requests(tmp_path, holds) -> None:
    f = Factory(tmp_path, holds, token_cache=False)
    f.intake.sync_labels(LabelSyncRequest(OPERATOR))
    batch = f.intake.review_label_batch()
    approved = f.intake.approve_label_batch(LabelBatchApprove(batch.short_digest, OPERATOR))
    typed = next(r for r in approved.approved if r.kind is OpKind.TYPE)

    result, single, held, checks = f.timed(
        lambda: f.intake.send(typed.outbox_id, actor=OPERATOR, kind=OpKind.TYPE)
    )

    assert result.state is OpState.SENT and held == checks == 1
    assert f.store.calls["write"] == 2  # the write-ahead and the receipt
    print(
        f"\nsingle send modeled {single:.1f}s ({PROCESS:.1f}s of it the process); "
        f"target {TARGET['single send']:.0f}s, before {BEFORE['single send']:.0f}s"
    )
    assert single < BUDGET["single send"] < BEFORE["single send"]
