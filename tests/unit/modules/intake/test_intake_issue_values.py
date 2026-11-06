"""The issue type and Priority field projection (``bh-poqy0``), beside the labels.

The pure rules (the maps, the work-then-report fallback, never guessing) and the end-to-end path
on ``MemoryTracker`` and ``MemoryReportStore``: ``labels sync`` drafts an ``issue.type`` and an
``issue.fields`` operation on the SAME ``labels`` record as the label change, the batch review
approves each as its own operation with its own grant, and ``send --kind type|fields`` sets the
issue's type and Priority with the labels unchanged. Each value is gated by the bound adapter's
own switch (``OutboxPolicy.type_enabled`` / ``fields_enabled``); off, nothing changes.

Offline: no clock, no sleep, no socket.
"""

from __future__ import annotations

import pytest

from beadhive.modules.intake.application import IntakeService, LabelProjection, OutboxService
from beadhive.modules.intake.domain import (
    DraftRequest,
    IntakeRefused,
    IssueBinding,
    IssueStatus,
    LabelBatchApprove,
    LabelDefineRequest,
    LabelIntent,
    LabelSettings,
    LabelSyncRequest,
    LeakPolicy,
    OpKind,
    OpState,
    OutboxDocument,
    OutboxDocumentError,
    OutboxEvent,
    OutboxPolicy,
    OwnIdentity,
    RefusalReason,
    RegistryDimension,
    RenderContext,
    ReportDraft,
    ReportFacts,
    SourceSnapshot,
    StorePosture,
    ValueChange,
    ValueIntent,
    ValueSettings,
    WorkDraft,
    WorkFacts,
    desired_values,
    field_changes,
    outbox_from_document,
    outbox_to_document,
    type_change,
)
from beadhive.modules.intake.domain import outbox as machine
from beadhive.modules.tracker.contracts import (
    READ_OPERATIONS,
    WRITE_OPERATIONS,
    IssueKey,
    Operation,
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

ME = OwnIdentity("acme/widgets", "inst-1")
LEAKS = LeakPolicy(
    bead_prefixes=frozenset({"acme", "pub"}),
    private_dimensions=frozenset({"intake", "origin", "delivery", "notified", "review"}),
    owned_label_prefixes=("bh:",),
)
GATED = OutboxPolicy(enabled=True, labels_enabled=True, type_enabled=True, fields_enabled=True)
PRIVATE = StorePosture(published=False)
EVERYTHING = READ_OPERATIONS | WRITE_OPERATIONS
OPERATOR = "alice"
T, F, L = OpKind.TYPE, OpKind.FIELDS, OpKind.LABELS


def marker_line(marker: OwnMarker) -> str:
    return f"<!-- bh:v1 hive={marker.hive} install={marker.install} outbox={marker.outbox_id} -->"


class _NoLabels:
    def violations(self, labels) -> tuple[str, ...]:
        return ()


class Hive:
    """Bound reports on one memory repository with the projection and the sender wired."""

    def __init__(
        self,
        *,
        policy: OutboxPolicy = GATED,
        enabled=EVERYTHING,
        values: ValueSettings | None = None,
    ) -> None:
        self.world = MemoryWorld()
        self.world.set_write_time(at(2))
        self.tracker = MemoryTracker(self.world, enabled=frozenset(enabled))
        self.store = MemoryReportStore(SCOPE)
        self.lock = InProcessIntakeLock()
        self.ids = SequentialIdSource()
        sanitizer = RecordingSanitizer.of([])
        self.sender = OutboxService(
            self.store,
            self.tracker,
            self.lock,
            ids=self.ids,
            identity=ME,
            marker_line=marker_line,
            policy=policy,
            leak_policy=LEAKS,
            sanitizer=sanitizer,
            posture=PRIVATE,
            holder="host-1:42",
        )
        self.projection = LabelProjection(
            self.store,
            self.tracker,
            self.lock,
            ids=self.ids,
            registry={"component": RegistryDimension("component", "Area")},
            settings=LabelSettings(),
            leak_policy=LEAKS,
            sanitizer=sanitizer,
            posture=PRIVATE,
            outbox=policy,
            values=values if values is not None else ValueSettings(),
        )
        self.intake = IntakeService(
            self.store, _NoLabels(), outbox=self.sender, projection=self.projection
        )

    def report(self, title: str, *, issue_type: str = "bug") -> tuple[str, IssueKey]:
        key = self.world.add_issue(title, at=at(1), labels=("bug",))
        binding = IssueBinding("", "memory", self.world.locator, key.url, key.identifier)
        snapshot = SourceSnapshot(title, "", IssueStatus("open"), "r1", labels=("bug",))
        report = self.store.create(
            ReportDraft(
                title,
                issue_type,
                "origin:github",
                "dev/import",
                binding=binding,
                snapshot=snapshot,
            )
        )
        self.store.set_state(report, "intake", "accepted", reason="triage", actor="dev/import")
        return report, key

    def work(self, report: str, *, issue_type: str = "feature", priority: str = "1") -> str:
        work = self.store.create_work(WorkDraft("fix", issue_type, "dev/x", priority=priority))
        self.store.track(report, work, actor="dev/x")
        self.store.mark(work, status="in_progress", labels=("component:intake",))
        return work

    def define(self) -> None:
        plan = self.intake.define_labels(LabelDefineRequest(OPERATOR)).plan
        self.intake.define_labels(LabelDefineRequest(OPERATOR, approve=plan.short_digest))

    def sync(self, **options):
        return self.intake.sync_labels(LabelSyncRequest(OPERATOR, **options))

    def approve(self):
        batch = self.intake.review_label_batch()
        return batch, self.intake.approve_label_batch(
            LabelBatchApprove(batch.short_digest, OPERATOR)
        )

    def send_all(self, outcome) -> None:
        for result in outcome.approved:
            sent = self.intake.send(result.outbox_id, actor=OPERATOR, kind=result.kind)
            assert sent.state is OpState.SENT, sent

    def record(self, report: str, outbox_id: str):
        doc = self.store.read_outbox(report)
        assert doc is not None
        return doc.records[outbox_id]


# -- the pure rules ------------------------------------------------------------------------------


def test_the_default_maps_are_githubs_org_defaults_and_chore_is_a_task() -> None:
    settings = ValueSettings()
    for bead, issue in (
        ("bug", "Bug"),
        ("feature", "Feature"),
        ("task", "Task"),
        ("chore", "Task"),
    ):
        assert desired_values(settings, ReportFacts(type=bead)).issue_type == issue
    for rank, option in (("0", "Urgent"), ("P1", "High"), (2, "Medium"), ("p3", "Low"), (4, "Low")):
        assert desired_values(settings, ReportFacts(priority=rank)).fields == (
            ("Priority", option),
        )


def test_the_work_wins_and_the_report_is_the_fallback() -> None:
    settings = ValueSettings()
    work = WorkFacts("in_progress", type="feature", priority="0")
    desired = desired_values(settings, ReportFacts(type="bug", priority="3"), work)
    assert (desired.issue_type, desired.fields) == ("Feature", (("Priority", "Urgent"),))
    fallback = desired_values(settings, ReportFacts(type="bug", priority="3"), WorkFacts("open"))
    assert (fallback.issue_type, fallback.fields) == ("Bug", (("Priority", "Low"),))


def test_an_unmapped_value_is_skipped_with_a_note_and_never_guessed() -> None:
    settings = ValueSettings(issue_types={"bug": "Bug"}, priorities={"P0": "Urgent"})
    desired = desired_values(settings, ReportFacts(type="epic", priority="2"))
    assert desired.issue_type is None and desired.fields == ()
    assert any("type 'epic' is not mapped" in note for note in desired.notes)
    assert any("priority P2 is not mapped" in note for note in desired.notes)
    # an unmapped work type falls back to the report's mapped one, like the type label
    fallback = desired_values(settings, ReportFacts(type="bug"), WorkFacts("open", type="chore"))
    assert fallback.issue_type == "Bug" and fallback.notes == ()


def test_a_priority_map_key_must_be_a_priority() -> None:
    with pytest.raises(ValueError, match="P0 to P4"):
        ValueSettings(priorities={"urgent": "Urgent"})


def test_a_value_not_read_is_never_assumed_unset() -> None:
    desired = desired_values(ValueSettings(), ReportFacts(type="bug", priority="1"))
    assert type_change(desired, None) == (
        None,
        "the issue's type was not read (the tracker reports none); skipped",
    )
    assert field_changes(desired, None)[0] == ()
    assert type_change(desired, "bug") == (None, "")  # an issue type compares case-insensitively
    assert type_change(desired, "") == (ValueChange("type", "", "Bug"), "")
    assert field_changes(desired, (("Priority", "Low"),)) == (
        (ValueChange("Priority", "Low", "High"),),
        "",
    )


# -- sync, batch review, send ----------------------------------------------------------------------


def test_sync_drafts_type_and_priority_with_the_labels_and_the_batch_sends_each_kind() -> None:
    hive = Hive()
    report, key = hive.report("a crash")
    hive.work(report, issue_type="feature", priority="1")
    hive.define()

    outcome = hive.sync()
    (item,) = outcome.items
    assert item.values == (
        ValueChange("type", "", "Feature"),
        ValueChange("Priority", "", "High"),
    )
    assert outcome.value_drift == {report: item.values}  # reported like drift
    record = hive.record(report, item.drafted)
    assert [op.kind for op in record.operations] == [L, T, F]  # ONE labels record
    assert record.event is OutboxEvent.LABELS

    batch, approved = hive.approve()
    assert [i.kind for i in batch.items] == [L, T, F]
    assert [i.values for i in batch.items[1:]] == [(("type", "Feature"),), (("Priority", "High"),)]
    assert [r.kind for r in approved.approved] == [L, T, F]
    assert len(set(approved.grant_ids)) == 3  # each operation its own single-use grant
    labels, typed, fielded = approved.approved
    assert hive.intake.send(labels.outbox_id, actor=OPERATOR, kind=L).state is OpState.SENT
    labelled = hive.world.server_labels(key)
    assert "bug" in labelled and "bh:type/feature" in labelled
    for result in (typed, fielded):
        assert hive.intake.send(result.outbox_id, actor=OPERATOR, kind=result.kind).state is (
            OpState.SENT
        )

    assert hive.world.server_issue_type(key) == "Feature"
    assert hive.world.server_issue_fields(key) == {"Priority": "High"}
    assert hive.world.server_labels(key) == labelled  # the value writes touch no label
    record = hive.record(report, item.drafted)
    receipts = {op.kind: op.receipt for op in record.operations}
    assert receipts[T].values_after == (("type", "Feature"),)
    assert receipts[F].values_after == (("Priority", "High"),)

    again = hive.sync()
    assert again.drafted == () and not again.items[0].changed and not again.value_drift


def test_with_the_gates_off_nothing_but_the_labels_is_drafted() -> None:
    hive = Hive(policy=OutboxPolicy(enabled=True, labels_enabled=True))
    report, key = hive.report("a crash")
    hive.define()
    (item,) = hive.sync().items
    assert item.values == () and item.value_notes == ()
    assert [op.kind for op in hive.record(report, item.drafted).operations] == [L]
    batch, _ = hive.approve()
    assert [i.kind for i in batch.items] == [L]
    assert hive.world.server_issue_type(key) == ""


def test_labels_converged_but_the_type_moved_drafts_a_values_only_record() -> None:
    hive = Hive()
    report, key = hive.report("a crash", issue_type="bug")
    hive.define()
    hive.sync()
    _, approved = hive.approve()
    hive.send_all(approved)
    assert hive.world.server_issue_type(key) == "Bug"

    hive.world.issues[key.identifier].issue_type = "Task"  # a human retyped it
    (item,) = hive.sync().items
    assert (item.add, item.remove) == (frozenset(), frozenset())
    assert item.values == (ValueChange("type", "Task", "Bug"),)
    record = hive.record(report, item.drafted)
    assert [op.kind for op in record.operations] == [T]
    assert record.op(T).values.base == (("type", "Task"),)
    _, approved = hive.approve()
    hive.send_all(approved)
    assert hive.world.server_issue_type(key) == "Bug"


def test_an_unmapped_type_is_noted_and_only_the_mapped_value_is_drafted() -> None:
    hive = Hive(values=ValueSettings(issue_types={"bug": "Bug"}))
    report, _ = hive.report("an idea", issue_type="feature")
    hive.define()
    (item,) = hive.sync().items
    assert item.values == ()  # the report has no priority, and its type is unmapped
    assert any("type 'feature' is not mapped" in note for note in item.value_notes)
    kinds = [op.kind for op in hive.record(report, item.drafted).operations]
    assert T not in kinds


def test_a_value_the_tracker_does_not_report_is_skipped_with_a_note() -> None:
    # the outbox gates are on, but the adapter does not read the type or fields (switch off)
    hive = Hive(enabled=READ_OPERATIONS | {Operation.LABELS_CHANGE, Operation.LABELS_DEFINE})
    report, _ = hive.report("a crash")
    hive.work(report, priority="2")
    hive.define()
    (item,) = hive.sync().items
    assert item.values == ()
    assert any("type was not read" in note for note in item.value_notes)
    assert any("field values were not read" in note for note in item.value_notes)


def test_a_dry_run_reports_the_values_and_writes_nothing() -> None:
    hive = Hive()
    report, _ = hive.report("a crash")
    hive.define()
    (item,) = hive.sync(dry_run=True).items
    assert item.values and not item.drafted
    assert hive.store.read_outbox(report) is None


def test_a_stale_value_operation_is_skipped_by_the_batch() -> None:
    hive = Hive()
    report, key = hive.report("a crash")
    hive.work(report, priority="2")
    hive.define()
    hive.sync()
    hive.world.issues[key.identifier].issue_type = "Task"  # moved after the draft
    batch, approved = hive.approve()
    stale = [i for i in batch.items if i.stale]
    assert [i.kind for i in stale] == [T] and "type moved" in stale[0].note
    assert [r.kind for r in approved.approved] == [L, F]


def test_sending_a_value_needs_its_own_gate() -> None:
    hive = Hive()
    report, key = hive.report("a crash")
    hive.define()
    hive.sync()
    _, approved = hive.approve()
    typed = next(r for r in approved.approved if r.kind is T)
    writes = hive.world.write_requests()
    no_type = OutboxService(
        hive.store,
        hive.tracker,
        hive.lock,
        ids=hive.ids,
        identity=ME,
        marker_line=marker_line,
        policy=OutboxPolicy(enabled=True, labels_enabled=True, fields_enabled=True),
        leak_policy=LEAKS,
        sanitizer=RecordingSanitizer.of([]),
        posture=PRIVATE,
    )
    with pytest.raises(IntakeRefused) as caught:
        no_type.send(typed.outbox_id, actor=OPERATOR, kind=T)
    assert caught.value.reason is RefusalReason.TYPE_DISABLED
    assert hive.world.server_issue_type(key) == "" and hive.world.write_requests() == writes


def test_an_ambiguous_type_write_is_reconciled_by_reading_the_type_back() -> None:
    hive = Hive()
    report, key = hive.report("a crash")
    hive.define()
    hive.sync()
    _, approved = hive.approve()
    typed = next(r for r in approved.approved if r.kind is T)
    hive.world.fail_next_write_ambiguously()
    sent = hive.intake.send(typed.outbox_id, actor=OPERATOR, kind=T)
    assert sent.state is OpState.UNKNOWN
    (result,) = [r for r in hive.intake.reconcile(typed.outbox_id, actor=OPERATOR) if r.kind is T]
    assert result.state is OpState.SENT and result.complete
    assert hive.world.server_issue_type(key) == "Bug"


# -- the record and its document --------------------------------------------------------------


def _ctx(url: str) -> RenderContext:
    return RenderContext(url, LEAKS, RecordingSanitizer.of([]))


def test_a_value_operation_rides_only_on_a_labels_record_and_round_trips() -> None:
    binding = IssueBinding(
        "acme-1", "memory", "acme/widgets", "memory://acme/widgets/issues/1", "#1"
    )
    typed = ValueIntent((("type", "Bug"),), (("type", "Task"),))
    fields = ValueIntent((("Priority", "High"),))
    applied = machine.draft(
        OutboxDocument(),
        DraftRequest(
            binding,
            OutboxEvent.LABELS,
            "v1",
            OPERATOR,
            comment=False,
            issue_type=typed,
            fields=fields,
        ),
        outbox_id="ob1_" + "a" * 26,
        policy=GATED,
        ctx=_ctx(binding.url),
    )
    record = applied.record
    assert [op.kind for op in record.operations] == [T, F]
    assert machine.render(record, T, _ctx(binding.url)).payload == "type: Bug"
    decoded = outbox_from_document(outbox_to_document(applied.document))
    assert decoded.records[record.outbox_id].op(T).values == typed
    assert decoded.records[record.outbox_id].op(F).values == fields

    with pytest.raises(IntakeRefused) as caught:  # never on a reply
        machine.draft(
            OutboxDocument(),
            DraftRequest(binding, OutboxEvent.REPLY, "r1", OPERATOR, issue_type=typed),
            outbox_id="ob1_" + "b" * 26,
            policy=GATED,
            ctx=_ctx(binding.url),
        )
    assert caught.value.reason is RefusalReason.INVALID_DRAFT
    with pytest.raises(IntakeRefused) as gate:  # the type gate off: refused at draft
        machine.draft(
            OutboxDocument(),
            DraftRequest(
                binding, OutboxEvent.LABELS, "v2", OPERATOR, comment=False, issue_type=typed
            ),
            outbox_id="ob1_" + "c" * 26,
            policy=OutboxPolicy(enabled=True, labels_enabled=True),
            ctx=_ctx(binding.url),
        )
    assert gate.value.reason is RefusalReason.TYPE_DISABLED


def test_a_labels_only_document_is_unchanged_and_a_malformed_value_fails_closed() -> None:
    binding = IssueBinding(
        "acme-1", "memory", "acme/widgets", "memory://acme/widgets/issues/1", "#1"
    )
    from beadhive.modules.intake.domain import LabelBase

    ctx = RenderContext(
        binding.url, LEAKS, RecordingSanitizer.of([]), labels=LabelBase("r1", frozenset())
    )
    labels_only = machine.draft(
        OutboxDocument(),
        DraftRequest(
            binding,
            OutboxEvent.LABELS,
            "v1",
            OPERATOR,
            comment=False,
            labels=LabelIntent(frozenset({"bh:type/bug"})),
        ),
        outbox_id="ob1_" + "d" * 26,
        policy=GATED,
        ctx=ctx,
    )
    document = outbox_to_document(labels_only.document)
    (row,) = next(iter(document["records"].values()))["operations"]
    assert "values" not in row  # a document without a value op is byte-identical to 0.35.2's
    row_with = dict(row, kind="issue.type")  # a type op without its values
    broken = dict(document)
    record = dict(next(iter(document["records"].values())), operations=[row_with])
    broken["records"] = {"ob1_" + "d" * 26: record}
    with pytest.raises(OutboxDocumentError):
        outbox_from_document(broken)


# -- the transition refresh (on_transition): tracker-free ---------------------------------------


def test_a_transition_drafts_the_values_from_the_outbox_belief_never_the_tracker() -> None:
    hive = Hive()
    report, key = hive.report("a crash")
    hive.work(report, priority="0")
    hive.define()
    hive.sync()
    _, approved = hive.approve()
    hive.send_all(approved)
    assert hive.world.server_issue_fields(key) == {"Priority": "Urgent"}
    reads = hive.world.read_requests()

    (item,) = hive.projection.refresh([report], OPERATOR)  # what every transition runs
    assert item.values == ()  # believed from the sent operations: nothing to set again
    assert hive.world.read_requests() == reads  # no tracker read at all

    hive.store.update_fields(report, issue_type="chore", priority="", actor=OPERATOR)
    work = hive.store.tracked_work(report)
    hive.store.update_fields(work, issue_type="chore", priority="3", actor=OPERATOR)
    (item,) = hive.projection.refresh([report], OPERATOR)
    assert item.values == (
        ValueChange("type", "Feature", "Task"),
        ValueChange("Priority", "Urgent", "Low"),
    )
    record = hive.record(report, item.drafted)
    assert record.op(T).values.base == (("type", "Feature"),)
    assert hive.world.read_requests() == reads


def test_a_lowercase_type_map_value_matches_githubs_type_case_insensitively() -> None:
    assert ValueIntent((("type", "bug"),)).held_by({"type": "Bug"})
    assert not ValueIntent((("Priority", "high"),)).held_by({"Priority": "High"})  # options exact
    hive = Hive(values=ValueSettings(issue_types={"bug": "bug"}))
    report, key = hive.report("a crash")
    hive.define()
    hive.world.issues[key.identifier].issue_type = "Bug"
    (item,) = hive.sync().items
    assert item.values == ()  # "bug" is GitHub's "Bug": no drift, nothing drafted

    hive.world.issues[key.identifier].issue_type = "Task"
    (item,) = hive.sync().items
    assert item.values == (ValueChange("type", "Task", "bug"),)
    hive.world.issues[key.identifier].issue_type = "TASK"  # the same type, another spelling
    batch = hive.intake.review_label_batch()
    typed = next(i for i in batch.items if i.kind is T)
    assert not typed.stale
