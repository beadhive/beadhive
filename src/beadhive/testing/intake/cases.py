"""Runner-neutral contract cases for the intake :class:`ReportStore` port.

The shared definition of done for a ``ReportStore`` implementation: it conforms when it passes
:data:`REPORT_STORE_CASES`. Each case is a :class:`~beadhive.testing.PortCase` with a stable
lowercase id that receives a factory and builds a FRESH store from it, so no scenario leaks into
the next. The in-memory fake runs them directly; the bd-backed store runs the same cases against
a scratch bd store::

    @pytest.mark.parametrize("case", REPORT_STORE_CASES, ids=lambda case: case.case_id)
    def test_my_store_conforms(case):
        run_report_store_case(my_factory, case)

No case reads a clock, sleeps or opens a socket.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from beadhive.modules.intake.contracts import (
    HiveTriplet,
    IntakeStoreError,
    IssueBinding,
    IssueStatus,
    ReportDraft,
    ReportStore,
    SourceSnapshot,
    StoreScope,
)

from ..conformance import PortCase, assert_port_conforms
from .memory_store import MemoryReportStore

ReportStoreFactory = Callable[[], ReportStore]

SCOPE = StoreScope(
    operational_prefix="acme",
    hive=HiveTriplet("github", "acme", "widgets"),
    public_prefixes=frozenset({"pub"}),
)
URL = "https://tracker.example/acme/widgets/issues/7"


class ReportStoreCase(PortCase[ReportStoreFactory]):
    """A semantic case shared by every ``ReportStore`` implementation."""


def draft(title: str = "login is broken", *, bound: bool = False, kind: str = "bug") -> ReportDraft:
    binding = IssueBinding("", "memory", "acme/widgets", URL, "#7") if bound else None
    snapshot = SourceSnapshot("t", "b", IssueStatus.OPEN, "r1") if bound else None
    return ReportDraft(
        title,
        kind,
        "origin:report",
        "dev/test",
        description="steps",
        binding=binding,
        snapshot=snapshot,
    )


def _expect_store_error(call: Callable[[], object]) -> None:
    try:
        call()
    except IntakeStoreError:
        return
    raise AssertionError("expected IntakeStoreError for an unknown bead")


def _scope_is_stable(factory: ReportStoreFactory) -> None:
    store = factory()
    assert store.scope() == store.scope()
    assert store.scope().hive.labels() == ("provider:github", "org:acme", "repo:widgets")


def _create_reads_back(factory: ReportStoreFactory) -> None:
    store = factory()
    first, second = store.create(draft()), store.create(draft("another"))
    assert first != second and first.startswith(store.scope().operational_prefix + "-")
    view = store.get(first)
    assert view is not None and view.id == first
    assert (view.title, view.issue_type, view.status) == ("login is broken", "bug", "open")
    assert set(store.scope().hive.labels()) <= set(view.labels)
    # filing stamps the channel and queue state separately: create alone adds neither
    assert not [label for label in view.labels if label.startswith(("origin:", "intake:"))]
    assert store.get("acme-nope") is None


def _set_state_replaces_its_dimension(factory: ReportStoreFactory) -> None:
    store = factory()
    bead = store.create(draft())
    store.set_state(bead, "origin", "report", reason="filed", actor="dev/test")
    store.set_state(bead, "intake", "untriaged", reason="filed", actor="dev/test")
    store.set_state(bead, "intake", "accepted", reason="ok", actor="dev/test")
    view = store.get(bead)
    assert view is not None
    assert "intake:accepted" in view.labels and "intake:untriaged" not in view.labels
    assert "origin:report" in view.labels  # another dimension is untouched


def _queue_key_is_open_labeled(factory: ReportStoreFactory) -> None:
    store = factory()
    open_one, closed_one, other = (
        store.create(draft()),
        store.create(draft("b")),
        store.create(draft("c")),
    )
    for bead in (open_one, closed_one):
        store.set_state(bead, "intake", "untriaged", reason="filed", actor="dev/test")
    store.close(closed_one, reason="dup", actor="dev/test")
    ids = [v.id for v in store.list_open_labeled("intake:untriaged")]
    assert ids == [open_one] and other not in ids


def _closed_stays_readable_and_findable(factory: ReportStoreFactory) -> None:
    store = factory()
    bead = store.create(draft(bound=True))
    store.close(bead, reason="done", actor="dev/test")
    view = store.get(bead)
    assert view is not None and view.status == "closed"
    assert [v.id for v in store.find_by_external_ref(URL)] == [bead]  # every status


def _external_ref_lookup(factory: ReportStoreFactory) -> None:
    store = factory()
    native = store.create(draft("born native"))
    bound = store.create(draft("bound", bound=True))
    assert [v.id for v in store.find_by_external_ref(URL)] == [bound]
    assert store.find_by_external_ref("https://tracker.example/acme/widgets/issues/8") == ()
    assert store.find_by_external_ref("") == ()  # a native bead has no ref to match
    view = store.get(bound)
    assert view is not None and view.external_ref == URL
    assert view.binding is not None and view.binding.report == bound and view.binding.url == URL
    native_view = store.get(native)
    assert native_view is not None and native_view.binding is None


def _update_fields_leaves_empty_unchanged(factory: ReportStoreFactory) -> None:
    store = factory()
    bead = store.create(draft(kind="bug"))
    store.update_fields(bead, issue_type="", priority="1", actor="dev/test")
    store.update_fields(bead, issue_type="feature", priority="", actor="dev/test")
    view = store.get(bead)
    assert view is not None and (view.issue_type, view.priority) == ("feature", "1")


def _assign(factory: ReportStoreFactory) -> None:
    store = factory()
    bead = store.create(draft())
    store.assign(bead, "super/one", actor="dev/test")
    view = store.get(bead)
    assert view is not None and view.assignee == "super/one"


def _record_snapshot(factory: ReportStoreFactory) -> None:
    store = factory()
    bead = store.create(draft(bound=True))
    newer = SourceSnapshot("t2", "b2", IssueStatus.CLOSED, "r2", closed_reason="completed")
    store.record_snapshot(bead, newer, actor="dev/test")
    view = store.get(bead)
    assert view is not None and view.snapshot == newer


def _duplicate_pairs(factory: ReportStoreFactory) -> None:
    store = factory()
    one, two = store.create(draft("login page crashes")), store.create(draft("login page crashes"))
    store.create(draft("add dark mode"))
    pairs = store.duplicate_pairs(threshold=0.9)
    assert [(p.first, p.second) for p in pairs] == [(one, two)]
    assert pairs[0].similarity >= 0.9
    assert store.duplicate_pairs(threshold=1.1) == ()


def _tracks_many_reports_to_one_work(factory: ReportStoreFactory) -> None:
    store = factory()
    a, b, work = store.create(draft("a")), store.create(draft("b")), store.create(draft("work"))
    assert store.tracked_work(a) is None and store.trackers_of(work) == ()
    store.track(a, work, actor="dev/test")
    store.track(b, work, actor="dev/test")
    assert store.tracked_work(a) == work
    assert store.trackers_of(work) == tuple(sorted((a, b)))
    store.close(a, reason="dup", actor="dev/test")
    assert a in store.trackers_of(work)  # the link survives the report's closure
    store.untrack(a, work, actor="dev/test")
    assert store.tracked_work(a) is None and store.trackers_of(work) == (b,)


def _unknown_bead_is_a_store_error(factory: ReportStoreFactory) -> None:
    store = factory()
    ghost = "acme-ghost"
    _expect_store_error(lambda: store.set_state(ghost, "intake", "x", reason="r", actor="a"))
    _expect_store_error(lambda: store.close(ghost, reason="r", actor="a"))
    _expect_store_error(lambda: store.assign(ghost, "s", actor="a"))
    _expect_store_error(
        lambda: store.update_fields(ghost, issue_type="bug", priority="", actor="a")
    )


REPORT_STORE_CASES: tuple[ReportStoreCase, ...] = (
    ReportStoreCase("store.scope-is-stable", _scope_is_stable),
    ReportStoreCase("store.create-reads-back", _create_reads_back),
    ReportStoreCase("store.set-state-replaces-its-dimension", _set_state_replaces_its_dimension),
    ReportStoreCase("store.queue-key-is-open-labeled", _queue_key_is_open_labeled),
    ReportStoreCase(
        "store.closed-stays-readable-and-findable", _closed_stays_readable_and_findable
    ),
    ReportStoreCase("store.external-ref-lookup", _external_ref_lookup),
    ReportStoreCase(
        "store.update-fields-leaves-empty-unchanged", _update_fields_leaves_empty_unchanged
    ),
    ReportStoreCase("store.assign", _assign),
    ReportStoreCase("store.record-snapshot", _record_snapshot),
    ReportStoreCase("store.duplicate-pairs", _duplicate_pairs),
    ReportStoreCase("store.tracks-many-reports-to-one-work", _tracks_many_reports_to_one_work),
    ReportStoreCase("store.unknown-bead-is-a-store-error", _unknown_bead_is_a_store_error),
)


def memory_store(scope: StoreScope = SCOPE) -> MemoryReportStore:
    """A fresh :class:`MemoryReportStore`. This is a :data:`ReportStoreFactory`."""
    return MemoryReportStore(scope)


def run_report_store_case(
    factory: ReportStoreFactory, case: ReportStoreCase, *, subject: str = "report store"
) -> None:
    """Run one case; a failure raises ``ConformanceFailure`` naming the case id."""
    assert_port_conforms(lambda: factory, (case,), subject=subject)


def assert_report_store_conforms(
    factory: ReportStoreFactory,
    cases: Sequence[ReportStoreCase] = REPORT_STORE_CASES,
    *,
    subject: str = "report store",
) -> None:
    """Run every case against fresh stores from ``factory``."""
    assert_port_conforms(lambda: factory, cases, subject=subject)


__all__ = [
    "REPORT_STORE_CASES",
    "SCOPE",
    "URL",
    "ReportStoreCase",
    "ReportStoreFactory",
    "assert_report_store_conforms",
    "draft",
    "memory_store",
    "run_report_store_case",
]
