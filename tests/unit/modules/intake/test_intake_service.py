"""``IntakeService`` and ``NullIntake`` over ``MemoryReportStore``: no bd, no CLI, no clock."""

from __future__ import annotations

import dataclasses

import pytest

from beadhive.modules.intake import (
    DiscoveryRequest,
    Disposition,
    DispositionRequest,
    ImportRequest,
    IntakePort,
    IntakeRefused,
    IntakeService,
    IntakeStoreError,
    IssueBinding,
    LabelPolicy,
    LinkRequest,
    NullIntake,
    QueueQuery,
    RefusalReason,
    ReportDraft,
    ReportStore,
    bind_intake,
)
from beadhive.testing.intake import URL, MemoryReportStore, draft, memory_store


class _Labels:
    """A LabelPolicy that rejects exactly the labels it is told to."""

    def __init__(self, *bad: str) -> None:
        self.bad = set(bad)
        self.seen: list[tuple[str, ...]] = []

    def violations(self, labels):
        self.seen.append(tuple(labels))
        return tuple(f"unknown label {label}" for label in labels if label in self.bad)


def _service(*bad: str) -> tuple[IntakeService, MemoryReportStore, _Labels]:
    store, labels = memory_store(), _Labels(*bad)
    return IntakeService(store, labels), store, labels


def _imported(store: MemoryReportStore, title: str) -> str:
    """A bead with a native ``source_system`` of github and no ``origin`` label."""
    binding = IssueBinding("", "github", "acme/widgets", URL, "#7")
    bead = store.create(ReportDraft(title, "bug", "origin:github", "dev/test", binding=binding))
    store.set_state(bead, "intake", "untriaged", reason="imported", actor="dev/test")
    return bead


def _filed(service: IntakeService, title: str = "login is broken", origin="origin:report") -> str:
    return service.file_report(
        ReportDraft(title, "bug", origin, "dev/test", description="steps to reproduce")
    )


def test_shapes_satisfy_the_ports() -> None:
    service, store, labels = _service()
    assert isinstance(service, IntakePort) and isinstance(NullIntake(), IntakePort)
    assert isinstance(store, ReportStore) and isinstance(labels, LabelPolicy)


# -- file_report ----------------------------------------------------------------------------


def test_file_report_creates_then_stamps_the_channel_and_queue_state() -> None:
    service, store, labels = _service()

    bead = _filed(service)

    view = store.get(bead)
    assert view is not None and view.external_ref == "" and view.binding is None
    assert {"origin:report", "intake:untriaged", "org:acme"} <= set(view.labels)
    assert store.history(bead) == (
        ("origin", "report", "filed via bh report"),
        ("intake", "untriaged", "filed via bh report"),
    )
    # only the NEW bead's own labels are checked, before the create
    assert labels.seen == [
        ("provider:github", "org:acme", "repo:widgets", "origin:report", "intake:untriaged")
    ]


def test_file_report_event_reason_names_the_configured_binary() -> None:
    store = memory_store()
    service = IntakeService(store, _Labels(), binary="ws")

    bead = _filed(service)

    assert {reason for _, _, reason in store.history(bead)} == {"filed via ws report"}


def test_file_report_escalation_channel() -> None:
    service, store, _ = _service()

    bead = _filed(service, origin="origin:escalation")

    view = store.get(bead)
    assert view is not None and "origin:escalation" in view.labels


@pytest.mark.parametrize("kind", ["epic", "task", "", "Bug"])
def test_file_report_refuses_a_type_outside_the_report_types_and_writes_nothing(kind) -> None:
    service, store, _ = _service()

    with pytest.raises(IntakeRefused) as refused:
        service.file_report(ReportDraft("t", kind, "origin:report", "dev/test"))

    assert refused.value.reason is RefusalReason.INVALID_TYPE
    assert "bug, chore, feature" in refused.value.detail
    assert store.writes == 0


def test_file_report_refuses_invalid_labels_before_any_write() -> None:
    service, store, _ = _service("origin:report")

    with pytest.raises(IntakeRefused) as refused:
        _filed(service)

    assert refused.value.reason is RefusalReason.INVALID_LABELS
    assert "unknown label origin:report" in refused.value.detail
    assert store.writes == 0


# -- queue ----------------------------------------------------------------------------------


def test_queue_is_source_agnostic_and_narrows_by_resolved_channel() -> None:
    service, store, _ = _service()
    report = _filed(service, "a report")
    escalation = _filed(service, "an escalation", origin="origin:escalation")
    # an imported bead has a native source_system and no origin label (derived on read)
    imported = _imported(store, "an import")
    accepted = _filed(service, "already triaged")
    service.dispose(DispositionRequest(accepted, Disposition.ACCEPT, "dev/test"))

    everything = service.queue(QueueQuery(include_duplicates=False))
    only_reports = service.queue(QueueQuery(channel="report", include_duplicates=False))
    only_github = service.queue(QueueQuery(channel="github", include_duplicates=False))

    assert [i.id for i in everything.items] == [report, escalation, imported]
    assert {i.id: i.channel for i in everything.items} == {
        report: "report",
        escalation: "escalation",
        imported: "github",
    }
    assert [i.id for i in only_reports.items] == [report]
    assert [i.id for i in only_github.items] == [imported]
    assert accepted not in [i.id for i in everything.items]


def test_queue_surfaces_only_duplicates_that_touch_a_queued_bead() -> None:
    service, store, _ = _service()
    one, two = _filed(service, "login page crashes"), _filed(service, "login page crashes")
    _filed(service, "something else entirely")
    backlog_a = store.create(draft("billing export slow"))
    backlog_b = store.create(draft("billing export slow"))  # a dupe pair with no queued member

    view = service.queue(QueueQuery(threshold=0.9))

    assert [(d.first, d.second) for d in view.duplicates] == [(one, two)]
    assert backlog_a not in {d.first for d in view.duplicates} | {backlog_b}
    assert service.queue(QueueQuery(include_duplicates=False)).duplicates == ()


def test_queue_of_an_empty_store_is_empty() -> None:
    service, store, _ = _service()

    view = service.queue(QueueQuery())

    assert view.items == () and view.duplicates == () and store.writes == 0


# -- dispositions ---------------------------------------------------------------------------


def test_accept_sets_type_and_priority_then_clears_intake() -> None:
    service, store, _ = _service()
    bead = _filed(service)

    outcome = service.dispose(
        DispositionRequest(bead, Disposition.ACCEPT, "dev/test", issue_type="feature", priority="1")
    )

    view = store.get(bead)
    assert view is not None and (view.issue_type, view.priority, view.status) == (
        "feature",
        "1",
        "open",
    )
    assert "intake:accepted" in view.labels and "intake:untriaged" not in view.labels
    assert (outcome.state, outcome.issue_type, outcome.priority) == ("accepted", "feature", "1")
    assert store.history(bead)[-1] == ("intake", "accepted", "accepted into backlog")


def test_accept_without_type_or_priority_updates_no_fields() -> None:
    service, store, _ = _service()
    bead = _filed(service)
    before = store.writes

    service.dispose(DispositionRequest(bead, Disposition.ACCEPT, "dev/test"))

    assert store.writes == before + 1  # the state write only
    view = store.get(bead)
    assert view is not None and view.issue_type == "bug"


def test_reject_needs_a_reporter_visible_reason_and_checks_it_first() -> None:
    service, store, _ = _service()
    bead = _filed(service)
    before = store.writes

    with pytest.raises(IntakeRefused) as refused:
        service.dispose(DispositionRequest("ghost", Disposition.REJECT, "dev/test"))

    assert refused.value.reason is RefusalReason.REASON_REQUIRED  # before the existence lookup
    assert store.writes == before

    outcome = service.dispose(
        DispositionRequest(bead, Disposition.REJECT, "dev/test", reason="not a bug")
    )

    view = store.get(bead)
    assert view is not None and view.status == "closed" and "intake:rejected" in view.labels
    assert outcome.state == "rejected"
    assert store.history(bead)[-2:] == (
        ("intake", "rejected", "rejected: not a bug"),
        ("status", "closed", "not a bug"),
    )


def test_promote_hands_off_without_closing() -> None:
    service, store, _ = _service()
    bead = _filed(service)

    outcome = service.dispose(DispositionRequest(bead, Disposition.PROMOTE, "dev/test"))

    view = store.get(bead)
    assert view is not None and view.status == "open" and "intake:promoted" in view.labels
    assert outcome.state == "promoted"
    assert store.history(bead)[-1] == ("intake", "promoted", "handed to the planner for adoption")


def test_reroute_to_a_seat_reassigns_and_leaves_the_report_queued() -> None:
    service, store, _ = _service()
    bead = _filed(service)

    outcome = service.dispose(
        DispositionRequest(bead, Disposition.REROUTE, "dev/test", seat="super/one")
    )

    view = store.get(bead)
    assert view is not None and view.assignee == "super/one"
    assert "intake:untriaged" in view.labels and view.status == "open"
    assert (outcome.state, outcome.seat) == ("", "super/one")


def test_reroute_without_a_seat_marks_it_rerouted_and_closes_it() -> None:
    service, store, _ = _service()
    bead = _filed(service)

    outcome = service.dispose(
        DispositionRequest(
            bead, Disposition.REROUTE, "dev/test", reason="rerouted to other as other-0001"
        )
    )

    view = store.get(bead)
    assert view is not None and view.status == "closed" and "intake:rerouted" in view.labels
    assert outcome.state == "rerouted"
    assert store.history(bead)[-1] == ("status", "closed", "rerouted to other as other-0001")


@pytest.mark.parametrize(
    "disposition", [Disposition.ACCEPT, Disposition.PROMOTE, Disposition.REROUTE]
)
def test_a_disposition_never_re_triages_a_fielded_bead(disposition) -> None:
    service, store, _ = _service()
    bead = _filed(service)
    service.dispose(DispositionRequest(bead, Disposition.PROMOTE, "dev/test"))
    before = store.writes

    with pytest.raises(IntakeRefused) as refused:
        service.dispose(DispositionRequest(bead, disposition, "dev/test", seat="s"))

    assert refused.value.reason is RefusalReason.NOT_UNTRIAGED and bead in refused.value.detail
    assert store.writes == before


def test_a_disposition_of_an_unknown_bead_is_not_found() -> None:
    service, store, _ = _service()

    with pytest.raises(IntakeRefused) as refused:
        service.dispose(DispositionRequest("acme-ghost", Disposition.ACCEPT, "dev/test"))

    assert refused.value.reason is RefusalReason.NOT_FOUND and store.writes == 0


# -- bindings -------------------------------------------------------------------------------


def test_binding_of_and_reports_for_work() -> None:
    service, store, _ = _service()
    native = _filed(service)
    bound_a = store.create(draft("a", bound=True))
    bound_b = store.create(draft("b", bound=True))
    work = store.create(draft("work"))
    for report in (native, bound_a):
        store.track(report, work, actor="dev/test")
    store.track(bound_b, store.create(draft("other work")), actor="dev/test")

    binding = service.binding_of(bound_a)

    assert binding == IssueBinding(bound_a, "memory", "acme/widgets", URL, "#7")
    assert service.binding_of(native) is None and service.binding_of("acme-ghost") is None
    # a born-native report that tracks the work has no external binding to report
    assert service.reports_for_work(work) == (binding,)
    assert service.reports_for_work("acme-nothing") == ()


# -- owned by later beads -------------------------------------------------------------------


def test_operations_no_bead_has_implemented_yet_refuse_without_writing() -> None:
    service, store, _ = _service()

    calls = (
        lambda: service.import_issue(ImportRequest(URL, "dev/test")),
        lambda: service.link(LinkRequest("a", "b", "dev/test")),
        lambda: service.unlink("a", actor="dev/test"),
        lambda: service.discover(DiscoveryRequest("dev/test")),
    )
    for call in calls:
        with pytest.raises(IntakeRefused) as refused:
            call()
        assert refused.value.reason is RefusalReason.NOT_AVAILABLE
    assert store.writes == 0


# -- NullIntake and the binding rule --------------------------------------------------------


def test_null_intake_reads_are_empty_and_free() -> None:
    null = NullIntake()

    assert null.queue(QueueQuery()).items == () and null.queue(QueueQuery()).duplicates == ()
    assert null.reports_for_work("any-work") == ()
    assert null.binding_of("any-bead") is None


def test_null_intake_refuses_every_write_as_disabled() -> None:
    null = NullIntake()
    calls = (
        lambda: null.file_report(draft()),
        lambda: null.import_issue(ImportRequest(URL, "dev/test")),
        lambda: null.link(LinkRequest("a", "b", "dev/test")),
        lambda: null.unlink("a", actor="dev/test"),
        lambda: null.dispose(DispositionRequest("a", Disposition.ACCEPT, "dev/test")),
        lambda: null.discover(DiscoveryRequest("dev/test")),
    )
    for call in calls:
        with pytest.raises(IntakeRefused) as refused:
            call()
        assert refused.value.reason is RefusalReason.INTAKE_DISABLED
        assert "opt-in" in refused.value.detail


def test_bind_intake_is_null_when_off_and_never_builds_the_store() -> None:
    built: list[int] = []

    def build() -> IntakePort:
        built.append(1)
        return IntakeService(memory_store(), _Labels())

    assert isinstance(bind_intake(False, build), NullIntake) and built == []
    assert isinstance(bind_intake(True, build), IntakeService) and built == [1]


# -- structured store failures and the raw-row passthrough ----------------------------------


class _FailsOn(MemoryReportStore):
    """A store whose ``set_state`` of ``dimension`` fails with a non-default exit status."""

    def __init__(self, scope, dimension: str) -> None:
        super().__init__(scope)
        self._dimension = dimension

    def set_state(self, bead, dimension, value, *, reason, actor):
        if dimension == self._dimension:
            raise IntakeStoreError("boom", bead=bead, operation="set_state", returncode=7)
        super().set_state(bead, dimension, value, reason=reason, actor=actor)


@pytest.mark.parametrize("dimension", ["origin", "intake"])
def test_a_failed_stamp_names_the_half_filed_bead_and_the_step(dimension) -> None:
    """The bead exists once ``create`` returns: a failed stamp must say which bead, which step and
    keep the store's exit status, so a transport can word "filed X but could not set ..."."""
    store = _FailsOn(memory_store().scope(), dimension)
    service = IntakeService(store, _Labels())

    with pytest.raises(IntakeStoreError) as raised:
        _filed(service)

    error = raised.value
    assert (error.operation, error.returncode, error.detail) == (f"stamp:{dimension}", 7, "boom")
    assert error.bead and store.get(error.bead) is not None


def test_a_failed_disposition_stamp_is_tagged_clear_intake() -> None:
    store = _FailsOn(memory_store().scope(), "intake")
    service = IntakeService(store, _Labels())
    bead = store.create(draft())
    # seed the queue label directly, bypassing the failing write
    MemoryReportStore.set_state(store, bead, "intake", "untriaged", reason="seed", actor="a")

    with pytest.raises(IntakeStoreError) as raised:
        service.dispose(DispositionRequest(bead, Disposition.PROMOTE, "disp/a"))

    assert (raised.value.operation, raised.value.bead) == ("clear_intake", bead)


def test_queue_items_and_duplicate_pairs_carry_the_stores_own_rows_untouched() -> None:
    """``raw`` is opaque store payload: a facade can re-emit the native shape byte for byte, and
    the module never decides on it (it is excluded from equality)."""

    class Rows(MemoryReportStore):
        def list_open_labeled(self, label):
            return tuple(
                dataclasses.replace(v, raw={"id": v.id, "extra": {"nested": [1, 2]}})
                for v in super().list_open_labeled(label)
            )

    store = Rows(memory_store().scope())
    service = IntakeService(store, _Labels())
    bead = _filed(service)

    view = service.queue(QueueQuery(include_duplicates=False))

    assert view.items[0].raw == {"id": bead, "extra": {"nested": [1, 2]}}
    plain = dataclasses.replace(view.items[0], raw={})
    assert plain == view.items[0]  # raw never takes part in equality
