"""The label projection service (``bh-uim9i``) end to end on ``MemoryTracker`` and
``MemoryReportStore``: sync (fresh labels, include-closed backfill, drift, prune), define with
in-command approval, and batch-digest approval.

Wired as the root wires it (``LabelProjection`` and ``OutboxService`` over one store, one tracker
and the never-waiting ``InProcessIntakeLock``). Offline: no clock, no sleep, no socket. Bulk reads
are asserted by call count on the tracker and the store, never by timing.
"""

from __future__ import annotations

from collections import Counter

import pytest

from beadhive.modules.intake.application import (
    IntakeService,
    LabelProjection,
    NullIntake,
    OutboxService,
)
from beadhive.modules.intake.domain import (
    DraftRequest,
    IntakeRefused,
    IssueBinding,
    IssueStatus,
    LabelBatchApprove,
    LabelDefineRequest,
    LabelSettings,
    LabelSyncRequest,
    LeakPolicy,
    Message,
    OpKind,
    OpState,
    OutboxDocument,
    OutboxEvent,
    OutboxPolicy,
    OwnIdentity,
    RefusalReason,
    RegistryDimension,
    RenderContext,
    ReportDraft,
    SourceSnapshot,
    StorePosture,
    WorkDraft,
    progress_facts,
)
from beadhive.modules.intake.domain import outbox as machine
from beadhive.modules.tracker.contracts import (
    READ_OPERATIONS,
    WRITE_OPERATIONS,
    IssueKey,
    IssueState,
    LabelDefinition,
    LabelNotOwned,
    Operation,
    OwnMarker,
    ProjectRef,
    WriteGrant,
)
from beadhive.testing.intake import (
    SCOPE,
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
POLICY = OutboxPolicy(enabled=True, labels_enabled=True)
PRIVATE = StorePosture(published=False)
EVERYTHING = READ_OPERATIONS | WRITE_OPERATIONS
REGISTRY = {"component": RegistryDimension("component", "Area of the code")}
OPERATOR = "alice"
L = OpKind.LABELS


def marker_line(marker: OwnMarker) -> str:
    return f"<!-- bh:v1 hive={marker.hive} install={marker.install} outbox={marker.outbox_id} -->"


class _NoLabels:
    def violations(self, labels) -> tuple[str, ...]:
        return ()


class Audit:
    """A recording ``AuditLog``."""

    def __init__(self) -> None:
        self.notes: list[tuple[str, str]] = []

    def note(self, text: str, *, actor: str) -> None:
        self.notes.append((actor, text))


class CountingTracker(MemoryTracker):
    """``MemoryTracker`` counting the reads a run makes, by method."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.calls: Counter[str] = Counter()

    def list_issues(self, query):
        self.calls["list_issues"] += 1
        return super().list_issues(query)

    def get(self, key):
        self.calls["get"] += 1
        return super().get(key)

    def probe_capabilities(self):
        self.calls["probe_capabilities"] += 1
        return super().probe_capabilities()

    def list_label_definitions(self):
        self.calls["list_label_definitions"] += 1
        return super().list_label_definitions()


class CountingStore(MemoryReportStore):
    """``MemoryReportStore`` counting its outbox reads."""

    def __init__(self, scope) -> None:
        super().__init__(scope)
        self.calls: Counter[str] = Counter()

    def read_outbox(self, bead):
        self.calls["read_outbox"] += 1
        return super().read_outbox(bead)

    def reports_with_outbox(self):
        self.calls["reports_with_outbox"] += 1
        return super().reports_with_outbox()


class Hive:
    """Bound reports on one memory repository, the projection and the sender wired over them."""

    def __init__(self, *, enabled=EVERYTHING, settings: LabelSettings | None = None) -> None:
        self.world = MemoryWorld()
        self.world.set_write_time(at(2))
        self.tracker = CountingTracker(self.world, enabled=frozenset(enabled))
        self.store = CountingStore(SCOPE)
        self.lock = InProcessIntakeLock()
        self.ids = SequentialIdSource()
        self.sanitizer = RecordingSanitizer.of([])
        self.audit = Audit()
        self.sender = OutboxService(
            self.store,
            self.tracker,
            self.lock,
            ids=self.ids,
            identity=ME,
            marker_line=marker_line,
            policy=POLICY,
            leak_policy=LEAKS,
            sanitizer=self.sanitizer,
            posture=PRIVATE,
            holder="host-1:42",
        )
        self.projection = LabelProjection(
            self.store,
            self.tracker,
            self.lock,
            ids=self.ids,
            registry=REGISTRY,
            settings=settings or LabelSettings(),
            leak_policy=LEAKS,
            sanitizer=self.sanitizer,
            posture=PRIVATE,
            outbox=POLICY,
            audit=self.audit,
        )
        self.intake = IntakeService(
            self.store, _NoLabels(), outbox=self.sender, projection=self.projection
        )

    # -- fixtures ------------------------------------------------------------------------------

    def report(
        self,
        title: str,
        *,
        labels: tuple[str, ...] = ("bug",),
        state: IssueState = IssueState.OPEN,
        intake: str = "accepted",
    ) -> tuple[str, IssueKey]:
        key = self.world.add_issue(title, at=at(1), state=state, labels=labels)
        binding = IssueBinding("", "memory", self.world.locator, key.url, key.identifier)
        snapshot = SourceSnapshot(title, "", IssueStatus(state.value), "r1", labels=labels)
        report = self.store.create(
            ReportDraft(
                title, "bug", "origin:github", "dev/import", binding=binding, snapshot=snapshot
            )
        )
        self.store.set_state(report, "intake", intake, reason="triage", actor="dev/import")
        return report, key

    def work(
        self,
        report: str,
        *,
        status: str = "in_progress",
        labels: tuple[str, ...] = ("component:intake",),
        issue_type: str = "feature",
        priority: str = "2",
    ) -> str:
        work = self.store.create_work(WorkDraft("fix", issue_type, "dev/x", priority=priority))
        self.store.track(report, work, actor="dev/x")
        self.store.mark(work, status=status, labels=labels)
        return work

    # -- verbs -----------------------------------------------------------------------------------

    def define(self, *, prune: bool = False, actor: str = OPERATOR):
        plan = self.intake.define_labels(LabelDefineRequest(OPERATOR, prune=prune)).plan
        return self.intake.define_labels(
            LabelDefineRequest(actor, approve=plan.short_digest, prune=prune)
        )

    def sync(self, **options):
        return self.intake.sync_labels(LabelSyncRequest(OPERATOR, **options))

    def approve(self, *, actor: str = OPERATOR):
        batch = self.intake.review_label_batch()
        return self.intake.approve_label_batch(LabelBatchApprove(batch.short_digest, actor))

    def send(self, outcome) -> None:
        for result in outcome.approved:
            assert self.intake.send(result.outbox_id, actor=OPERATOR, kind=L).state is OpState.SENT

    def converge(self, **options) -> None:
        self.sync(**options)
        self.send(self.approve())

    def labels(self, key: IssueKey) -> frozenset[str]:
        return self.world.server_labels(key)

    def op(self, report: str, outbox_id: str):
        doc = self.store.read_outbox(report)
        assert doc is not None
        op = doc.records[outbox_id].op(L)
        assert op is not None
        return op


def refused(reason: RefusalReason, call, *args, **kwargs) -> IntakeRefused:
    with pytest.raises(IntakeRefused) as caught:
        call(*args, **kwargs)
    assert caught.value.reason is reason, caught.value
    return caught.value


# -- idempotence -------------------------------------------------------------------------------


def test_sync_approve_send_sync_drafts_zero_ops() -> None:
    hive = Hive()
    started, first = hive.report("a crash")
    hive.work(started)
    plain, second = hive.report("a typo", intake="untriaged")
    hive.define()

    synced = hive.sync()
    assert len(synced.drafted) == 2
    approved = hive.approve()
    assert len(approved.grant_ids) == 2 and not approved.stale and not approved.failed
    hive.send(approved)

    assert hive.labels(first) == {
        "bug",
        "bh:intake/accepted",
        "bh:progress/started",
        "bh:type/feature",
        "bh:priority/p2",
        "bh:component/intake",
    }
    assert hive.labels(second) == {"bug", "bh:intake/untriaged", "bh:type/bug"}
    writes = list(hive.store.outbox_writes)

    again = hive.sync()
    assert again.drafted == () and not any(i.changed for i in again.items)
    assert hive.store.outbox_writes == writes  # nothing written at all
    assert hive.intake.review_label_batch().items == ()


def test_a_resync_before_approval_refreshes_nothing_and_never_stacks() -> None:
    hive = Hive()
    report, _ = hive.report("a crash")
    hive.define()
    first = hive.sync().items[0]
    again = hive.sync().items[0]

    assert first.drafted and not again.drafted and again.pending == first.drafted  # O2
    open_records = [
        r for r in hive.store.read_outbox(report).records.values() if r.status.value == "open"
    ]
    assert [r.outbox_id for r in open_records] == [first.drafted]


# -- a disposition change ----------------------------------------------------------------------


def test_a_disposition_change_drafts_exactly_the_group_swap() -> None:
    """Accepted to rejected swaps the intake label and, since a rejected report's delivery is not
    its own (bh-5kpwc), also drops the progress label its tracked work had projected."""
    hive = Hive()
    moved, key = hive.report("a crash")
    hive.work(moved)
    still, _ = hive.report("a typo")
    hive.define()
    hive.converge()

    hive.store.set_state(moved, "intake", "rejected", reason="wontfix", actor="alice")
    outcome = hive.sync()

    by_report = {i.report: i for i in outcome.items}
    item = by_report[moved]
    assert item.add == {"bh:intake/rejected"}
    assert item.remove == {"bh:intake/accepted", "bh:progress/started"}
    assert item.swaps == (("intake", "bh:intake/accepted", "bh:intake/rejected"),)
    assert outcome.drafted == (item.drafted,)  # the other report drafts nothing
    assert not by_report[still].changed
    diff = hive.op(moved, item.drafted).labels
    assert (diff.add, diff.remove) == (
        {"bh:intake/rejected"},
        {"bh:intake/accepted", "bh:progress/started"},
    )

    hive.send(hive.approve())
    assert "bh:intake/rejected" in hive.labels(key) and "bh:intake/accepted" not in hive.labels(key)
    assert "bh:progress/started" not in hive.labels(key)


def test_a_new_diff_supersedes_the_unsent_record_instead_of_stacking() -> None:
    hive = Hive()
    report, _ = hive.report("a crash")
    hive.define()
    first = hive.sync().items[0].drafted
    hive.store.set_state(report, "intake", "rejected", reason="wontfix", actor="alice")
    second = hive.sync().items[0]

    assert second.drafted != first and second.superseded == first
    assert hive.op(report, first).state is OpState.SUPERSEDED
    assert [i.outbox_id for i in hive.intake.review_label_batch().items] == [second.drafted]


# -- include closed ----------------------------------------------------------------------------


def test_include_closed_converges_open_and_closed_fixtures_in_one_pass() -> None:
    hive = Hive()
    open_report, open_issue = hive.report("open on open")
    closed_report, closed_issue = hive.report("closed on closed", state=IssueState.CLOSED)
    hive.store.close(closed_report, reason="done", actor="alice")
    stale_report, stale_issue = hive.report("open on closed", state=IssueState.CLOSED)
    hive.define()

    default = {i.report: i for i in hive.sync(dry_run=True).items}
    assert closed_report not in default  # a closed report is out of the default selection
    assert "include_closed" in default[stale_report].note and not default[stale_report].changed

    hive.tracker.calls.clear()
    outcome = hive.sync(include_closed=True)
    assert len(outcome.drafted) == 3
    assert hive.tracker.calls["list_issues"] == 1 and hive.tracker.calls["get"] == 0
    hive.send(hive.approve())

    for key in (open_issue, closed_issue, stale_issue):
        assert {"bh:intake/accepted", "bh:type/bug"} <= hive.labels(key)
    assert hive.sync(include_closed=True).drafted == ()


def test_sync_drafts_the_removal_of_a_closed_duplicates_stale_delivery_label() -> None:
    """bh-0d5th / #75: closed as a duplicate, still ``delivery:pending``, and the label already
    published. Syncing it with ``--include-closed`` drafts that one removal; a send converges."""
    hive = Hive()
    duplicate, issue = hive.report(
        "same crash",
        labels=(
            "bug",
            "bh:intake/rejected",
            "bh:delivery/pending",
            "bh:type/bug",
            "bh:priority/p2",
        ),
        state=IssueState.CLOSED,
        intake="rejected",
    )
    hive.work(duplicate, status="closed", labels=(), issue_type="bug")  # the canonical's work
    hive.store.set_state(duplicate, "delivery", "pending", reason="linked", actor="dev/x")
    hive.store.close(duplicate, reason="duplicate", actor="alice")
    hive.define()

    outcome = hive.sync(include_closed=True)

    (item,) = outcome.items
    assert item.remove == {"bh:delivery/pending"} and not item.add and item.drafted
    hive.send(hive.approve())
    assert "bh:delivery/pending" not in hive.labels(issue)
    assert hive.sync(include_closed=True).drafted == ()


# -- owned labels only; drift ------------------------------------------------------------------


def test_non_owned_labels_are_never_touched_and_drift_is_removed_only_with_prune() -> None:
    hive = Hive()
    report, key = hive.report("a crash", labels=("bug", "needs-love", "bh:legacy/thing"))
    hive.define()

    item = hive.sync().items[0]
    assert item.drift == {"bh:legacy/thing"}
    assert "bh:legacy/thing" not in item.remove and not item.remove & {"bug", "needs-love"}
    hive.send(hive.approve())
    assert {"bug", "needs-love", "bh:legacy/thing"} <= hive.labels(key)

    pruned = hive.sync(prune_unmapped=True).items[0]
    assert pruned.remove == {"bh:legacy/thing"} and pruned.add == frozenset()
    hive.send(hive.approve())
    assert hive.labels(key) == {"bug", "needs-love", "bh:intake/accepted", "bh:type/bug"}
    assert hive.sync(prune_unmapped=True).drafted == ()


def test_a_dry_run_reports_the_diff_and_writes_nothing() -> None:
    hive = Hive()
    hive.report("a crash")
    hive.define()
    writes = hive.store.writes

    item = hive.sync(dry_run=True).items[0]
    assert item.add == {"bh:intake/accepted", "bh:type/bug"} and not item.drafted
    assert hive.store.writes == writes and hive.store.outbox_writes == []


# -- undefined labels --------------------------------------------------------------------------


def test_sync_refuses_to_add_a_label_the_repository_does_not_define() -> None:
    hive = Hive()
    report, _ = hive.report("a crash")

    item = hive.sync().items[0]
    assert item.undefined == {"bh:intake/accepted", "bh:type/bug"}
    assert "bh work labels define" in item.error and not item.drafted
    assert hive.store.outbox_writes == []

    hive.define()
    assert hive.sync().items[0].drafted


def test_a_newly_observed_open_value_needs_define_before_sync_adds_it() -> None:
    hive = Hive()
    report, _ = hive.report("a crash")
    work = hive.work(report, labels=("component:intake",))
    hive.define()
    hive.converge()
    hive.store.mark(work, labels=("component:cli",))  # a component the repository lacks

    item = hive.sync().items[0]
    assert item.undefined == {"bh:component/cli"} and not item.drafted
    plan = hive.intake.define_labels(LabelDefineRequest(OPERATOR)).plan
    assert [s.name for s in plan.create] == ["bh:component/cli"]
    hive.define()
    assert hive.sync().items[0].add == {"bh:component/cli"}


# -- define --------------------------------------------------------------------------------------


def test_define_converges_with_one_grant_and_a_local_audit_note() -> None:
    hive = Hive()
    hive.world.seed_label_definition(LabelDefinition("bh:type/bug", "000000", "stale"))
    hive.world.seed_label_definition(LabelDefinition("bug", "ff0000", "a human's label"))

    dry = hive.intake.define_labels(LabelDefineRequest(OPERATOR))
    assert dry.plan.changed and not dry.applied
    assert [s.name for s in dry.plan.update] == ["bh:type/bug"]
    assert "bh:type/bug" not in {s.name for s in dry.plan.create}
    assert hive.world.write_requests() == 0

    applied = hive.intake.define_labels(LabelDefineRequest(OPERATOR, approve=dry.plan.short_digest))
    assert applied.applied and len(applied.grant_ids) == 1
    assert hive.audit.notes == [(OPERATOR, applied.audit)]
    assert dry.plan.short_digest in applied.audit and applied.grant_ids[0] in applied.audit
    defs = {d.name: d for d in hive.world.label_definitions()}
    assert defs["bh:type/bug"].color == "5319e7" and defs["bug"].color == "ff0000"

    again = hive.intake.define_labels(LabelDefineRequest(OPERATOR))
    assert not again.plan.changed
    writes = hive.world.write_requests()
    converged = hive.intake.define_labels(
        LabelDefineRequest(OPERATOR, approve=again.plan.short_digest)
    )
    assert not converged.applied and converged.grant_ids == ()
    assert hive.world.write_requests() == writes and len(hive.audit.notes) == 1
    refused(
        RefusalReason.STALE_DIGEST,
        hive.intake.define_labels,
        LabelDefineRequest(OPERATOR, approve=dry.plan.short_digest),
    )


def test_define_is_approved_by_an_operator_seat_only() -> None:
    hive = Hive()
    plan = hive.intake.define_labels(LabelDefineRequest(OPERATOR)).plan
    refused(
        RefusalReason.OPERATOR_SEAT_REQUIRED,
        hive.intake.define_labels,
        LabelDefineRequest("dev/agent", approve=plan.short_digest),
    )
    assert hive.world.label_definitions() == ()


def test_define_without_the_adapters_define_enable_refuses_before_any_write() -> None:
    hive = Hive(enabled=READ_OPERATIONS)
    plan = hive.intake.define_labels(LabelDefineRequest(OPERATOR)).plan
    refused(
        RefusalReason.CAPABILITY_UNUSABLE,
        hive.intake.define_labels,
        LabelDefineRequest(OPERATOR, approve=plan.short_digest),
    )
    assert hive.world.write_requests() == 0


def test_prune_deletes_unmapped_owned_labels_but_refuses_one_an_open_issue_carries() -> None:
    hive = Hive()
    for name in ("bh:legacy/carried", "bh:legacy/closed-only", "bh:legacy/unused", "bug"):
        hive.world.seed_label_definition(LabelDefinition(name, "ededed", ""))
    carrier = hive.world.add_issue("open", at=at(1), labels=("bh:legacy/carried",))
    hive.world.add_issue(
        "closed", at=at(1), state=IssueState.CLOSED, labels=("bh:legacy/closed-only",)
    )

    plan = hive.intake.define_labels(LabelDefineRequest(OPERATOR, prune=True)).plan
    assert plan.delete == ("bh:legacy/closed-only", "bh:legacy/unused")
    assert [(k.name, k.carried_by) for k in plan.kept] == [
        ("bh:legacy/carried", (carrier.identifier,))
    ]

    applied = hive.define(prune=True)
    assert applied.applied and len(applied.grant_ids) == 2  # the define write and the delete
    names = {d.name for d in hive.world.label_definitions()}
    assert {"bh:legacy/carried", "bug"} <= names
    assert not names & {"bh:legacy/closed-only", "bh:legacy/unused"}
    assert "bh:legacy/carried" in hive.labels(carrier)
    assert not hive.intake.define_labels(LabelDefineRequest(OPERATOR, prune=True)).plan.changed


def test_the_memory_pruner_deletes_owned_names_only() -> None:
    world = MemoryWorld()
    tracker = MemoryTracker(world, enabled=EVERYTHING)
    grant = WriteGrant("gr-1", Operation.LABELS_DEFINE, ProjectRef("memory", world.locator))
    with pytest.raises(LabelNotOwned):
        tracker.delete_labels(("bh:a", "bug"), grant=grant)
    assert world.write_requests() == 0


# -- the batch ---------------------------------------------------------------------------------


def test_a_batch_with_one_stale_item_mints_n_minus_one_grants_and_reports_it() -> None:
    hive = Hive()
    reports = [hive.report(title)[0] for title in ("one", "two", "three")]
    keys = {r: hive.store.get(r).binding for r in reports}
    hive.define()
    hive.sync()
    batch = hive.intake.review_label_batch()
    assert len(batch.items) == 3 and batch.stale == ()

    stale_item = batch.items[1]
    hive.world.issues[keys[stale_item.report].identifier].labels.add("bh:legacy/human")
    hive.tracker.calls.clear()
    hive.store.calls.clear()
    outcome = hive.intake.approve_label_batch(LabelBatchApprove(batch.short_digest, OPERATOR))
    tracker_calls, store_calls = dict(hive.tracker.calls), dict(hive.store.calls)

    # ONE capability probe, ONE listing and ONE outbox-document index for the whole batch: each
    # document is read once by the index, and an approved one once more under its lock and once by
    # the store's own seq re-check, never a search per item
    assert tracker_calls.get("probe_capabilities") == 1
    assert tracker_calls.get("list_issues") == 1 and "get" not in tracker_calls
    assert store_calls.get("reports_with_outbox") == 1
    assert store_calls.get("read_outbox") == len(batch.items) + 2 * 2
    assert len(outcome.grant_ids) == 2 and len(set(outcome.grant_ids)) == 2
    assert [i.outbox_id for i in outcome.stale] == [stale_item.outbox_id] and not outcome.failed
    assert hive.op(stale_item.report, stale_item.outbox_id).state is OpState.DRAFTED
    for result in outcome.approved:
        assert hive.op(result.report, result.outbox_id).state is OpState.APPROVED

    redrafted = {i.report: i for i in hive.sync().items}[stale_item.report]
    assert redrafted.drafted and redrafted.superseded == stale_item.outbox_id


def test_a_batch_that_moved_since_review_is_refused_whole() -> None:
    hive = Hive()
    first, _ = hive.report("one")
    hive.define()
    hive.sync()
    batch = hive.intake.review_label_batch()
    hive.report("two")
    hive.sync()  # a new item joins the batch after the review

    refused(
        RefusalReason.STALE_DIGEST,
        hive.intake.approve_label_batch,
        LabelBatchApprove(batch.short_digest, OPERATOR),
    )
    assert all(i.outbox_id for i in hive.intake.review_label_batch().items)
    assert not any(
        op.state is OpState.APPROVED
        for doc in (hive.store.read_outbox(first),)
        for r in doc.records.values()
        for op in r.operations
    )


def test_a_batch_needs_an_operator_seat_and_the_labels_flag() -> None:
    hive = Hive()
    hive.report("one")
    hive.define()
    hive.sync()
    batch = hive.intake.review_label_batch()
    refused(
        RefusalReason.OPERATOR_SEAT_REQUIRED,
        hive.intake.approve_label_batch,
        LabelBatchApprove(batch.short_digest, "dev/agent"),
    )
    off = OutboxService(
        hive.store,
        hive.tracker,
        hive.lock,
        ids=hive.ids,
        identity=ME,
        marker_line=marker_line,
        policy=OutboxPolicy(enabled=True),
        leak_policy=LEAKS,
        sanitizer=hive.sanitizer,
        posture=PRIVATE,
    )
    refused(
        RefusalReason.LABELS_DISABLED,
        off.approve_label_batch,
        LabelBatchApprove(batch.short_digest, OPERATOR),
    )


# -- bulk reads --------------------------------------------------------------------------------


def test_one_listing_per_sync_run_never_one_read_per_issue() -> None:
    hive = Hive()
    for title in ("one", "two", "three", "four"):
        hive.report(title)
    hive.define()
    hive.tracker.calls.clear()

    hive.sync()
    assert hive.tracker.calls["list_issues"] == 1
    assert hive.tracker.calls["get"] == 0
    assert hive.tracker.calls["list_label_definitions"] == 1

    hive.tracker.calls.clear()
    hive.intake.review_label_batch()
    assert hive.tracker.calls["list_issues"] == 1 and hive.tracker.calls["get"] == 0


# -- progress notices, the null port, and the switches ---------------------------------------------


def _notice(doc: OutboxDocument, event: OutboxEvent, oid: str) -> OutboxDocument:
    url = "memory://acme/widgets/issues/1"
    binding = IssueBinding("acme-0001", "memory", "acme/widgets", url, "#1")
    ctx = RenderContext(binding.url, LEAKS, RecordingSanitizer.of([]))
    request = DraftRequest(binding, event, f"{event.value}@1", "alice", message=Message("", "x"))
    return machine.draft(doc, request, outbox_id=oid, policy=POLICY, ctx=ctx).document


def test_the_newest_live_operator_notice_sets_progress() -> None:
    ids = SequentialIdSource()
    blocked_id = ids.outbox_id()
    doc = _notice(OutboxDocument(), OutboxEvent.BLOCKED, blocked_id)
    assert progress_facts(doc).blocked_notice and not progress_facts(doc).deprioritised_notice

    doc = _notice(doc, OutboxEvent.DEPRIORITISED, ids.outbox_id())  # supersedes the unsent one
    assert progress_facts(doc).deprioritised_notice and not progress_facts(doc).blocked_notice

    last = max(doc.records.values(), key=lambda r: r.created_seq).outbox_id
    doc = machine.discard(doc, last, reason="withdrawn", actor="alice", policy=POLICY).document
    assert progress_facts(doc) == progress_facts(None)


def test_a_blocked_notice_projects_the_blocked_progress_label() -> None:
    hive = Hive()
    report, _ = hive.report("a crash")
    hive.work(report)
    doc = hive.store.read_outbox(report) or OutboxDocument()
    hive.store.write_outbox(
        report, _notice(doc, OutboxEvent.BLOCKED, hive.ids.outbox_id()), expect_seq=0, actor="a"
    )
    hive.define()

    item = hive.sync(dry_run=True).items[0]
    assert "bh:progress/blocked" in item.add and "bh:progress/started" not in item.add


def test_null_intake_and_an_unbound_projection_refuse_or_read_nothing() -> None:
    null = NullIntake()
    refused(RefusalReason.INTAKE_DISABLED, null.sync_labels, LabelSyncRequest(OPERATOR))
    refused(RefusalReason.INTAKE_DISABLED, null.define_labels, LabelDefineRequest(OPERATOR))
    assert null.review_label_batch().items == ()
    refused(
        RefusalReason.INTAKE_DISABLED,
        null.approve_label_batch,
        LabelBatchApprove("0" * 12, OPERATOR),
    )
    hive = Hive()
    bare = IntakeService(hive.store, _NoLabels(), outbox=hive.sender)
    refused(RefusalReason.INTAKE_DISABLED, bare.sync_labels, LabelSyncRequest(OPERATOR))


def test_a_drafting_sync_needs_the_outbox_and_its_labels_flag() -> None:
    hive = Hive()
    hive.report("a crash")
    projection = LabelProjection(
        hive.store,
        hive.tracker,
        hive.lock,
        ids=hive.ids,
        registry=REGISTRY,
        settings=LabelSettings(),
        leak_policy=LEAKS,
        sanitizer=hive.sanitizer,
        posture=PRIVATE,
        outbox=OutboxPolicy(enabled=True),
    )
    refused(RefusalReason.LABELS_DISABLED, projection.sync, LabelSyncRequest(OPERATOR))
    assert projection.sync(LabelSyncRequest(OPERATOR, dry_run=True)).items[0].changed
