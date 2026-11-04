"""The label projection's pure domain (bh-tppoh): the registry-derived catalog, the desired state,
the owned-only diff, and the ``labels`` outbox record type with its own supersede group.

No tracker, no store, no clock: the registry and settings are passed in, live labels are plain
sets, and the outbox is driven directly through its pure machine with a deterministic id source.
"""

from __future__ import annotations

import dataclasses

import pytest

from beadhive.modules.intake.domain import (
    DEFAULT_PROJECTED,
    LABELS_GROUP,
    MILESTONE_GROUP,
    NOTIFIED_BY_EVENT,
    ApprovalRequest,
    Capabilities,
    DraftRequest,
    IntakeRefused,
    IssueBinding,
    LabelBase,
    LabelCatalog,
    LabelIntent,
    LabelOverride,
    LabelSettings,
    LeakKind,
    LeakPolicy,
    Message,
    OpKind,
    OpState,
    OutboxDocument,
    OutboxDocumentError,
    OutboxEvent,
    OutboxPolicy,
    ProgressFacts,
    Receipt,
    RefusalReason,
    RegistryDimension,
    RenderContext,
    ReportFacts,
    UnmappedReason,
    WorkFacts,
    derive_progress,
    desired_labels,
    diff_labels,
    group_of,
    labels_revision,
    outbox_from_document,
    outbox_to_document,
)
from beadhive.modules.intake.domain import outbox as ob
from beadhive.modules.intake.domain.labels import (
    MAX_DESCRIPTION_CHARS,
    PRIORITY_VALUES,
    PROGRESS_VALUES,
    TYPE_VALUES,
    Openness,
    normalize_priority,
)
from beadhive.modules.intake.domain.vocabulary import INTAKE_DIMENSIONS
from beadhive.testing.intake import RecordingSanitizer, SequentialIdSource

REGISTRY = {
    "intake": RegistryDimension("intake", "Where triage left this report"),
    "size": RegistryDimension("size", "Rough size", frozenset({"s", "m", "l"})),
    "component": RegistryDimension("component", "Area of the code"),
}
POLICY = LeakPolicy(
    bead_prefixes=frozenset({"bh", "acme"}),
    private_dimensions=frozenset({"intake", "origin", "delivery", "notified", "review"}),
    owned_label_prefixes=("bh:",),
)


def catalog(**settings) -> LabelCatalog:
    return LabelCatalog.build(REGISTRY, LabelSettings(**settings))


# -- the catalog: closed sets, open sets, names, descriptions, colors -----------------------


def test_closed_sets_are_complete_up_front() -> None:
    cat = catalog()

    assert cat.values("intake") == INTAKE_DIMENSIONS["intake"]
    assert cat.values("delivery") == INTAKE_DIMENSIONS["delivery"]
    assert cat.values("progress") == PROGRESS_VALUES == {"started", "blocked", "deprioritised"}
    assert cat.values("type") == TYPE_VALUES == {"bug", "feature", "chore", "task"}
    assert cat.values("priority") == PRIORITY_VALUES == {f"p{n}" for n in range(5)}
    closed = 6 + 4 + 3 + 4 + 5
    assert len(cat.specs) == closed  # component is open and nothing is observed yet
    assert {s.name for s in cat.specs} >= {
        "bh:intake/untriaged",
        "bh:intake/needs-info",
        "bh:delivery/released",
        "bh:progress/deprioritised",
        "bh:type/chore",
        "bh:priority/p0",
    }
    assert [s.dimension for s in cat.specs] == sorted(
        (s.dimension for s in cat.specs), key=DEFAULT_PROJECTED.index
    )


def test_configured_values_close_a_dimension_and_extend_a_built_in_one() -> None:
    registry = {
        **REGISTRY,
        "delivery": RegistryDimension("delivery", "", frozenset({"backported"})),
        "type": RegistryDimension("type", "", frozenset({"epic"})),  # fixed: not extensible
    }
    cat = LabelCatalog.build(registry, LabelSettings(project=(*DEFAULT_PROJECTED, "size")))

    assert cat.openness("size") is Openness.CLOSED and cat.values("size") == {"s", "m", "l"}
    assert "backported" in cat.values("delivery")
    assert cat.values("type") == TYPE_VALUES
    assert cat.openness("component") is Openness.OPEN
    assert cat.openness("origin") is None and cat.values("size") and not cat.values("origin")


def test_descriptions_come_from_the_registry_with_the_value() -> None:
    cat = catalog()

    assert cat.spec("intake", "accepted").description == "Where triage left this report: accepted"
    assert cat.spec("delivery", "merged").description == "Delivery of the fix: merged"  # built-in
    long = {"intake": RegistryDimension("intake", "x" * 200)}
    spec = LabelCatalog.build(long).spec("intake", "rejected")
    assert len(spec.description) == MAX_DESCRIPTION_CHARS


def test_colors_are_per_dimension_with_a_built_in_default() -> None:
    cat = catalog(colors={"intake": "ABCDEF"})

    assert {s.color for s in cat.specs if s.dimension == "intake"} == {"abcdef"}
    assert {s.color for s in cat.specs if s.dimension == "type"} == {"5319e7"}


def test_aliases_rename_the_group_and_parse_back() -> None:
    cat = catalog(aliases={"intake": "triage"})

    assert cat.spec("intake", "accepted").name == "bh:triage/accepted"
    assert cat.by_name("bh:intake/accepted") is None
    assert cat.parse("bh:triage/accepted") == ("intake", "accepted")
    assert cat.parse("bh:triage/gone") == ("intake", "gone")  # by the naming rule


def test_overrides_apply_per_value() -> None:
    cat = catalog(
        prefix="bh:",
        overrides={
            "type/bug": LabelOverride(name="bh:bug", description="Something is broken"),
            "priority/p0": LabelOverride(color="B60205"),
        },
    )

    bug = cat.spec("type", "bug")
    assert (bug.name, bug.description) == ("bh:bug", "Something is broken")
    assert cat.by_name("bh:bug") is bug and cat.parse("bh:bug") == ("type", "bug")
    assert cat.spec("priority", "p0").color == "b60205"
    assert cat.spec("priority", "p1").color == "d93f0b"


@pytest.mark.parametrize(
    ("settings", "message"),
    [
        ({"prefix": ""}, "owned prefix"),
        ({"colors": {"intake": "red"}}, "colors.intake"),
        ({"aliases": {"intake": "Bad Group"}}, "aliases.intake"),
        ({"aliases": {"intake": "type"}}, "share the label group"),
        ({"overrides": {"bug": LabelOverride(name="bh:bug")}}, "<dimension>/<value>"),
        ({"overrides": {"type/bug": LabelOverride(name="bug")}}, "not under"),
        ({"overrides": {"type/bug": LabelOverride(color="#fff")}}, "6-hex"),
    ],
)
def test_bad_settings_are_refused(settings, message) -> None:
    with pytest.raises(ValueError, match=message):
        LabelSettings(**settings)


def test_two_values_overridden_onto_one_name_are_refused() -> None:
    clash = {"type/bug": LabelOverride(name="bh:x"), "type/task": LabelOverride(name="bh:x")}
    with pytest.raises(ValueError, match="both 'bh:x'"):
        catalog(overrides=clash)


def test_open_sets_are_filled_as_values_are_observed() -> None:
    cat = catalog(project=(*DEFAULT_PROJECTED, "tag"))
    assert cat.values("component") == frozenset() and cat.values("tag") == frozenset()

    seen = cat.observe({"component": ["intake", "cli"], "tag": ["regression"], "type": ["epic"]})

    assert seen.values("component") == {"intake", "cli"}
    assert seen.spec("component", "cli").openness is Openness.OPEN
    assert seen.spec("tag", "regression").name == "bh:tag/regression"
    assert seen.values("type") == TYPE_VALUES  # a closed set ignores observations
    assert cat.values("component") == frozenset()  # never mutated
    assert seen.observe({"component": ["tracker"]}).values("component") == {
        "intake",
        "cli",
        "tracker",
    }
    # an unobserved open value still has a name (defined once it is observed)
    assert cat.name_for("component", "docs") == "bh:component/docs"
    assert cat.name_for("type", "epic") is None


def test_a_value_removed_from_the_registry_is_reported_for_prune() -> None:
    project = (*DEFAULT_PROJECTED, "size")
    cat = LabelCatalog.build(REGISTRY, LabelSettings(project=project), {"component": ["cli"]})
    live = {
        "bh:size/m",  # still defined
        "bh:size/xl",  # removed from the registry's values
        "bh:origin/report",  # dimension not projected
        "bh:component/old",  # open value no projected bead carries
        "bh:stray",  # not <group>/<value>
        "good first issue",  # not owned: never reported
    }

    unmapped = {u.name: u for u in cat.unmapped(live)}

    assert set(unmapped) == {"bh:size/xl", "bh:origin/report", "bh:component/old", "bh:stray"}
    assert unmapped["bh:size/xl"].reason is UnmappedReason.VALUE_REMOVED
    assert (unmapped["bh:size/xl"].dimension, unmapped["bh:size/xl"].value) == ("size", "xl")
    assert unmapped["bh:origin/report"].reason is UnmappedReason.NOT_PROJECTED
    assert unmapped["bh:component/old"].reason is UnmappedReason.NOT_OBSERVED
    assert unmapped["bh:stray"].reason is UnmappedReason.UNPARSEABLE


def test_a_name_over_the_tracker_limit_is_a_problem() -> None:
    cat = catalog().observe({"component": ["c" * 60]})

    (problem,) = cat.problems()
    assert problem.name == "bh:component/" + "c" * 60


def test_leak_validation_flags_internal_text_in_names_and_descriptions() -> None:
    sanitizer = RecordingSanitizer.of(["hunter2"])
    clean = catalog().observe({"component": ["intake"]})
    assert clean.findings(POLICY, sanitizer=sanitizer) == {}

    leaky = clean.observe({"component": ["bh-abc12", "hunter2"]})
    found = leaky.findings(POLICY, sanitizer=sanitizer)

    assert set(found) == {"bh:component/bh-abc12", "bh:component/hunter2"}
    assert {f.kind for f in found["bh:component/bh-abc12"]} == {LeakKind.BEAD_ID}
    assert {f.kind for f in found["bh:component/hunter2"]} == {LeakKind.SECRET}
    registry = {"intake": RegistryDimension("intake", "see delivery:merged")}
    private = LabelCatalog.build(registry).findings(POLICY, sanitizer=sanitizer)
    assert {f.kind for fs in private.values() for f in fs} == {LeakKind.PRIVATE_LABEL}
    unowned = LeakPolicy(bead_prefixes=frozenset({"bh"}), owned_label_prefixes=("other:",))
    assert all(
        {f.kind for f in fs} == {LeakKind.LABEL_NOT_OWNED}
        for fs in clean.findings(unowned, sanitizer=sanitizer).values()
    )
    no_scan = clean.findings(POLICY, sanitizer=None)
    assert set(no_scan) == clean.names  # fail closed without a sanitizer


# -- the desired state -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "labels", "notices", "delivery", "expected"),
    [
        ("open", (), ProgressFacts(), "pending", None),
        ("in_progress", (), ProgressFacts(), "pending", "started"),
        ("open", ("review:pending",), ProgressFacts(), "pending", "started"),
        ("blocked", (), ProgressFacts(), "pending", "blocked"),
        ("in_progress", (), ProgressFacts(blocked_notice=True), "pending", "blocked"),
        ("blocked", (), ProgressFacts(deprioritised_notice=True), "pending", "deprioritised"),
        ("open", (), ProgressFacts(deprioritised_notice=True), "reverted", "deprioritised"),
        ("in_progress", (), ProgressFacts(blocked_notice=True), "merged", None),
        ("closed", (), ProgressFacts(), "released", None),
    ],
)
def test_progress_is_derived_on_each_sync(status, labels, notices, delivery, expected) -> None:
    assert derive_progress(WorkFacts(status, labels), notices, delivery) == expected


def test_an_operator_notice_counts_without_tracked_work() -> None:
    assert derive_progress(None, ProgressFacts(blocked_notice=True)) == "blocked"
    assert derive_progress(None, ProgressFacts()) is None


def test_desired_labels_take_work_values_falling_back_to_the_report() -> None:
    cat = catalog().observe({"component": ["intake", "cli"]})
    report = ReportFacts.from_bead(
        ["intake:accepted", "delivery:pending", "component:cli", "origin:github"], "bug", 3
    )
    work = WorkFacts.from_bead("in_progress", ["component:intake"], "feature", "P1")

    with_work = desired_labels(cat, report, work)
    alone = desired_labels(cat, report)

    assert with_work.labels == {
        "bh:intake/accepted",
        "bh:delivery/pending",
        "bh:progress/started",
        "bh:type/feature",
        "bh:priority/p1",
        "bh:component/intake",
    }
    assert alone.labels == {
        "bh:intake/accepted",
        "bh:delivery/pending",
        "bh:type/bug",
        "bh:priority/p3",
        "bh:component/cli",
    }
    assert with_work.by_dimension["type"] == ("bh:type/feature",)


def test_an_unprojectable_work_value_falls_back_and_is_never_invented() -> None:
    cat = catalog()
    report = ReportFacts(intake="bogus", type="bug", priority=9)
    work = WorkFacts("open", type="epic", priority=None)

    desired = desired_labels(cat, report, work)

    assert desired.labels == {"bh:type/bug"}
    assert set(desired.skipped) == {("intake", "bogus")}
    assert desired_labels(cat, ReportFacts(type="epic")).skipped == (("type", "epic"),)


def test_every_closed_dimension_is_an_exclusive_group() -> None:
    cat = catalog()
    report = ReportFacts.from_bead(["intake:untriaged", "intake:accepted"])

    desired = desired_labels(cat, report)

    assert desired.by_dimension["intake"] == ("bh:intake/untriaged",)
    assert all(cat.by_name(n).exclusive for n in desired.labels)


def test_open_dimensions_project_every_value_and_name_undefined_ones() -> None:
    cat = catalog(project=(*DEFAULT_PROJECTED, "tag")).observe({"component": ["cli"]})
    report = ReportFacts(components=("cli", "docs"), tags=("regression",))

    desired = desired_labels(cat, report)

    assert desired.by_dimension["component"] == ("bh:component/cli", "bh:component/docs")
    assert desired.undefined(cat) == {"bh:component/docs", "bh:tag/regression"}


def test_an_unprojected_dimension_is_never_desired() -> None:
    cat = catalog(project=("intake",))

    desired = desired_labels(cat, ReportFacts(intake="accepted", type="bug", priority=1))

    assert desired.labels == {"bh:intake/accepted"}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(0, "p0"), ("4", "p4"), ("P2", "p2"), (" p1 ", "p1"), (5, None), (None, None), (True, None)],
)
def test_priority_is_normalised(raw, expected) -> None:
    assert normalize_priority(raw) == expected


# -- the diff: owned only, exclusive swaps, drift --------------------------------------------


def test_a_disposition_change_is_an_exclusive_group_swap() -> None:
    cat = catalog()
    live = {"bh:intake/untriaged", "bh:type/bug", "help wanted"}
    desired = desired_labels(cat, ReportFacts(intake="accepted", type="bug"))

    diff = diff_labels(cat, desired, live)

    assert (diff.add, diff.remove, diff.drift) == (
        {"bh:intake/accepted"},
        {"bh:intake/untriaged"},
        frozenset(),
    )
    assert dict(diff.swaps(cat)) == {"intake": ("bh:intake/untriaged", "bh:intake/accepted")}


def test_the_diff_never_touches_labels_outside_the_prefix_and_reports_drift() -> None:
    cat = catalog()
    live = {"bug", "priority: high", "bh-legacy", "bh:size/xl", "bh:intake/accepted"}
    desired = desired_labels(cat, ReportFacts(intake="accepted"))

    diff = diff_labels(cat, desired, live)

    assert not diff.changed
    assert diff.drift == {"bh:size/xl"}  # owned, not produced: reported, not removed
    assert not (diff.add | diff.remove | diff.drift) & {"bug", "priority: high", "bh-legacy"}


def test_a_stale_open_value_is_removed_not_left_as_drift() -> None:
    cat = catalog().observe({"component": ["intake"]})
    desired = desired_labels(cat, ReportFacts(components=("intake",)))

    diff = diff_labels(cat, desired, {"bh:component/old"})

    assert (diff.add, diff.remove, diff.drift) == (
        {"bh:component/intake"},
        {"bh:component/old"},
        frozenset(),
    )


def test_a_converged_issue_diffs_to_nothing() -> None:
    cat = catalog()
    desired = desired_labels(cat, ReportFacts(intake="accepted", delivery="merged", priority=2))

    diff = diff_labels(cat, desired, desired.labels | {"bug"})

    assert not diff.changed and not diff.drift


def test_a_desired_label_outside_the_prefix_is_refused() -> None:
    with pytest.raises(ValueError, match="outside the owned prefix"):
        diff_labels(catalog(), {"bug"}, set())


# -- the labels outbox record type -----------------------------------------------------------

URL = "https://tracker.example/acme/widgets/issues/7"
BINDING = IssueBinding("acme-r1", "memory", "acme/widgets", URL, "#7")
ON = OutboxPolicy(enabled=True, labels_enabled=True)
ALL = Capabilities(frozenset({"comment.create", "labels.change", "comments.read", "issues.read"}))


class Issue:
    """One bound report: its outbox document, its live labels, and the sync -> send loop."""

    def __init__(self, live: set[str]) -> None:
        self.doc = OutboxDocument()
        self.ids = SequentialIdSource()
        self.live = set(live)
        self.revision = 0
        self.cat = catalog()

    def ctx(self) -> RenderContext:
        return RenderContext(
            URL,
            POLICY,
            RecordingSanitizer(),
            labels=LabelBase(f"r{self.revision}", frozenset(self.live)),
        )

    def sync(self, report: ReportFacts) -> str | None:
        diff = diff_labels(self.cat, desired_labels(self.cat, report), self.live)
        if not diff.changed:
            return None
        intent = LabelIntent(diff.add, diff.remove)
        request = DraftRequest(
            BINDING,
            OutboxEvent.LABELS,
            labels_revision(self.doc, URL, intent),
            "dev/sync",
            comment=False,
            labels=intent,
        )
        applied = ob.draft(
            self.doc, request, outbox_id=self.ids.outbox_id(), policy=ON, ctx=self.ctx()
        )
        self.doc = applied.document
        return applied.outbox_id

    def send(self, oid: str) -> None:
        digest = ob.render(self.doc.record(oid), OpKind.LABELS, self.ctx()).short_digest
        request = ApprovalRequest(oid, OpKind.LABELS, digest, "alice")
        self.doc = ob.approve(
            self.doc,
            request,
            grant_id=self.ids.grant_id(),
            policy=ON,
            ctx=self.ctx(),
            capabilities=ALL,
        ).document
        applied = ob.begin_send(
            self.doc,
            oid,
            OpKind.LABELS,
            actor="alice",
            policy=ON,
            ctx=self.ctx(),
            epoch=1,
            holder="h",
        )
        plan = applied.plan
        self.live = (self.live | plan.add) - plan.remove
        self.revision += 1
        self.doc = ob.record_receipt(
            applied.document,
            oid,
            OpKind.LABELS,
            Receipt(labels_after=frozenset(self.live)),
            actor="s",
        ).document

    def key(self, oid: str):
        return self.doc.record(oid).draft_key


def test_sync_send_change_sync_send_keeps_distinct_draft_keys() -> None:
    issue = Issue({"bug"})
    a = issue.sync(ReportFacts(intake="untriaged"))
    assert issue.sync(ReportFacts(intake="untriaged")) == a  # a re-sync is O2: no new record
    issue.send(a)
    assert "bh:intake/untriaged" in issue.live
    assert issue.sync(ReportFacts(intake="untriaged")) is None  # converged: nothing to draft

    b = issue.sync(ReportFacts(intake="accepted"))
    issue.send(b)
    c = issue.sync(ReportFacts(intake="untriaged"))  # A -> B -> A
    issue.send(c)

    keys = [issue.key(oid) for oid in (a, b, c)]
    assert len(set(keys)) == 3 and len({a, b, c}) == 3
    assert all(k.event is OutboxEvent.LABELS for k in keys)
    assert issue.live == {"bug", "bh:intake/untriaged"}
    assert all(issue.doc.record(o).op(OpKind.LABELS).state is OpState.SENT for o in (a, b, c))


def test_an_unsent_a_b_a_flip_refreshes_instead_of_finding_the_retired_record() -> None:
    issue = Issue(set())
    a = issue.sync(ReportFacts(intake="accepted"))
    b = issue.sync(ReportFacts(intake="rejected"))
    a2 = issue.sync(ReportFacts(intake="accepted"))  # same intent as a, which b superseded

    assert len({a, b, a2}) == 3 and issue.key(a) != issue.key(a2)
    states = [issue.doc.record(o).op(OpKind.LABELS).state for o in (a, b, a2)]
    assert states == [OpState.SUPERSEDED, OpState.SUPERSEDED, OpState.DRAFTED]
    open_records = [r for r in issue.doc.records.values() if r.status.value == "open"]
    assert [r.outbox_id for r in open_records] == [a2]  # refreshed, never stacked


def test_labels_records_have_their_own_supersede_group() -> None:
    issue = Issue(set())
    milestone = ob.draft(
        issue.doc,
        DraftRequest(BINDING, OutboxEvent.MERGED, "c1", "outbox", message=Message("m", "Merged.")),
        outbox_id=issue.ids.outbox_id(),
        policy=ON,
        ctx=issue.ctx(),
    )
    issue.doc = milestone.document
    labels = issue.sync(ReportFacts(intake="accepted"))
    refreshed = issue.sync(ReportFacts(intake="rejected"))

    merged = issue.doc.record(milestone.outbox_id)
    assert merged.group == MILESTONE_GROUP and merged.op(OpKind.COMMENT).state is OpState.DRAFTED
    assert issue.doc.record(labels).group == issue.doc.record(refreshed).group == LABELS_GROUP
    assert issue.doc.record(refreshed).supersedes == labels

    later = ob.draft(
        issue.doc,
        DraftRequest(BINDING, OutboxEvent.RELEASED, "v1", "outbox", message=Message("r", "Out.")),
        outbox_id=issue.ids.outbox_id(),
        policy=ON,
        ctx=issue.ctx(),
    ).document
    assert later.record(refreshed).op(OpKind.LABELS).state is OpState.DRAFTED  # untouched
    assert later.record(milestone.outbox_id).op(OpKind.COMMENT).state is OpState.SUPERSEDED


@pytest.mark.parametrize(
    ("event", "group"),
    [
        (OutboxEvent.LABELS, LABELS_GROUP),
        (OutboxEvent.MERGED, MILESTONE_GROUP),
        (OutboxEvent.BLOCKED, MILESTONE_GROUP),
        (OutboxEvent.REPLY, "reply:4"),
    ],
)
def test_group_of(event, group) -> None:
    assert group_of(event, 4) == group


@pytest.mark.parametrize(
    ("comment", "labels"),
    [(True, LabelIntent(add=frozenset({"bh:intake/accepted"}))), (False, None)],
)
def test_a_labels_record_carries_one_label_operation_and_no_comment(comment, labels) -> None:
    request = DraftRequest(
        BINDING,
        OutboxEvent.LABELS,
        "x@0",
        "dev/sync",
        message=Message("m", "hi"),
        comment=comment,
        labels=labels,
    )
    with pytest.raises(IntakeRefused) as caught:
        ob.draft(
            OutboxDocument(),
            request,
            outbox_id=SequentialIdSource().outbox_id(),
            policy=ON,
            ctx=Issue(set()).ctx(),
        )
    assert caught.value.reason is RefusalReason.INVALID_DRAFT


def test_a_sent_labels_record_never_moves_notified() -> None:
    issue = Issue(set())
    oid = issue.sync(ReportFacts(intake="accepted"))
    issue.send(oid)
    assert issue.doc.record(oid).status.value == "delivered"
    assert NOTIFIED_BY_EVENT["labels"] is None
    assert set(NOTIFIED_BY_EVENT) == {e.value for e in OutboxEvent}


# -- the codec -------------------------------------------------------------------------------


def test_the_labels_event_round_trips_through_the_document() -> None:
    issue = Issue(set())
    sent = issue.sync(ReportFacts(intake="accepted"))
    issue.send(sent)
    issue.sync(ReportFacts(intake="rejected"))

    decoded = outbox_from_document(outbox_to_document(issue.doc))

    assert dataclasses.replace(decoded) == issue.doc
    assert decoded.record(sent).event is OutboxEvent.LABELS


def _encoded_labels_doc() -> tuple[dict, str]:
    issue = Issue(set())
    oid = issue.sync(ReportFacts(intake="accepted"))
    return outbox_to_document(issue.doc), oid


def test_an_unknown_event_fails_closed() -> None:
    encoded, oid = _encoded_labels_doc()
    record = encoded["records"][oid]
    unknown = {**record, "event": "labels.v2", "draft_key": {**record["draft_key"], "event": "x"}}

    with pytest.raises(OutboxDocumentError):
        outbox_from_document({**encoded, "records": {oid: unknown}})


@pytest.mark.parametrize(
    "mangle",
    [
        lambda r: {**r, "group": MILESTONE_GROUP},
        lambda r: {**r, "operations": [{**r["operations"][0], "kind": "comment.create"}]},
    ],
)
def test_an_inconsistent_labels_record_fails_closed(mangle) -> None:
    encoded, oid = _encoded_labels_doc()

    with pytest.raises(OutboxDocumentError, match="labels record"):
        outbox_from_document({**encoded, "records": {oid: mangle(encoded["records"][oid])}})
