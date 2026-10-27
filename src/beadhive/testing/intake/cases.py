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

import dataclasses
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from beadhive.modules.intake.contracts import (
    CheckpointScope,
    CommentRecord,
    CommentRevision,
    Evidence,
    HiveTriplet,
    IntakeRefused,
    IntakeStoreError,
    IssueBinding,
    IssueStatus,
    OutboxDocument,
    RefusalReason,
    ReportDraft,
    ReportStore,
    Role,
    SourceSnapshot,
    StoreScope,
    Thread,
    ThreadActor,
    ThreadCheckpoint,
    ThreadIssue,
    mint_outbox_id,
    outbox_from_document,
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


def thread(seq: int = 3, *, comments: Sequence[str] = ("c1", "c2"), url: str = URL) -> Thread:
    """A small ``intake.thread`` value for store cases: ``comments`` live records and a stored
    issue-scope checkpoint, at ``seq``. Explicit timestamps; never the clock."""
    when = datetime(2026, 1, 1, tzinfo=UTC)
    records = {
        cid: CommentRecord(
            id=cid,
            global_id=f"g-{cid}",
            author=ThreadActor("human-alice", "human", "alice"),
            via=None,
            marker=None,
            role=Role.EXTERNAL,
            evidence=Evidence.NONE,
            created_at=when,
            body=f"body of {cid}",
            revisions=(CommentRevision(1, when, f"sha256:{cid}", n + 1),),
            first_seen_seq=n + 1,
            last_seen_seq=n + 1,
        )
        for n, cid in enumerate(comments)
    }
    checkpoint = ThreadCheckpoint(CheckpointScope.ISSUE, url, "token-1", seq)
    return Thread(
        "memory",
        "acme/widgets",
        ThreadIssue("#7", url),
        seq=seq,
        checkpoint=checkpoint,
        records=records,
    )


def outbox(seq: int = 2, *, texts: Sequence[str] = ("thanks",), url: str = URL) -> OutboxDocument:
    """A small ``intake.outbox`` value for store cases: one drafted reply per text, at ``seq``.
    Decoded through the module's own codec, so a store round-trips the real format."""
    records = {}
    for n, text in enumerate(texts, start=1):
        oid = mint_outbox_id(n.to_bytes(16, "big"))
        step = {"seq": n, "rule": "O1", "event": "draft", "from": None, "to": "drafted"}
        records[oid] = {
            "draft_key": {"binding_url": url, "event": "reply", "source_revision": f"r{n}"},
            "binding": {
                "report": "",
                "tracker": "memory",
                "project": "acme/widgets",
                "url": url,
                "identifier": "#7",
            },
            "event": "reply",
            "group": f"reply:{n}",
            "facts": {"tag": "", "version": "", "commit_url": "", "progress": ""},
            "message": {"template": "reply", "text": text},
            "operations": [
                {
                    "kind": "comment.create",
                    "state": "drafted",
                    "labels": None,
                    "findings": [],
                    "overrides": [],
                    "approvals": [],
                    "attempts": [],
                    "receipt": None,
                    "fail_reason": "",
                    "absent_confirmations": 0,
                    "last_absent_seq": 0,
                    "outcome_unknown": False,
                    "duplicates": [],
                    "history": [{**step, "actor": "dev/test", "detail": ""}],
                }
            ],
            "created_seq": n,
            "supersedes": None,
        }
    return outbox_from_document(
        {
            "schema": 1,
            "seq": seq,
            "facts": {"merged": None, "released": None},
            "records": records,
        }
    )


def _expect_store_error(call: Callable[[], object]) -> None:
    try:
        call()
    except IntakeStoreError:
        return
    raise AssertionError("expected IntakeStoreError")


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


def _set_state_replaces_but_never_clears(factory: ReportStoreFactory) -> None:
    """``bd set-state`` can replace a dimension's value but not clear it (measured on bd 1.3.0):
    an empty value is refused and the previous value stays. Unlinking a report therefore keeps its
    ``delivery`` value and records an ``unlinked`` fact elsewhere (external-intake-module-adr.md,
    acceptance item 3)."""
    store = factory()
    bead = store.create(draft(bound=True))
    store.set_state(bead, "delivery", "pending", reason="linked", actor="dev/test")
    for dimension, value in (("delivery", ""), ("", "pending")):
        _expect_store_error(
            lambda dimension=dimension, value=value: store.set_state(
                bead, dimension, value, reason="clear", actor="dev/test"
            )
        )
    view = store.get(bead)
    assert view is not None and "delivery:pending" in view.labels
    store.set_state(bead, "delivery", "merged", reason="merged", actor="dev/test")
    view = store.get(bead)
    assert view is not None
    assert "delivery:merged" in view.labels and "delivery:pending" not in view.labels


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
    # a pair is unordered: a store may name either side first (bd orders by its own ids)
    assert [frozenset((p.first, p.second)) for p in pairs] == [frozenset((one, two))]
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


def _expect_thread_moved(call: Callable[[], object]) -> None:
    try:
        call()
    except IntakeRefused as exc:
        assert exc.reason is RefusalReason.THREAD_MOVED, exc.reason
        return
    raise AssertionError("expected IntakeRefused(THREAD_MOVED) for a stale expect_seq")


def _thread_absent_reads_none(factory: ReportStoreFactory) -> None:
    store = factory()
    bead = store.create(draft(bound=True))
    assert store.read_thread(bead) is None
    assert store.read_thread("acme-ghost") is None


def _thread_round_trips_whole(factory: ReportStoreFactory) -> None:
    store = factory()
    bead = store.create(draft(bound=True))
    first = thread(3)
    store.write_thread(bead, first, expect_seq=0, actor="dev/test")
    assert store.read_thread(bead) == first
    second = thread(5, comments=("c2",))  # the whole document is replaced, never merged
    store.write_thread(bead, second, expect_seq=3, actor="dev/test")
    assert store.read_thread(bead) == second


def _thread_write_rechecks_seq(factory: ReportStoreFactory) -> None:
    store = factory()
    bead = store.create(draft(bound=True))
    store.write_thread(bead, thread(3), expect_seq=0, actor="dev/test")
    _expect_thread_moved(
        lambda: store.write_thread(bead, thread(9), expect_seq=0, actor="dev/other")
    )
    _expect_thread_moved(
        lambda: store.write_thread(bead, thread(9), expect_seq=4, actor="dev/other")
    )
    stored = store.read_thread(bead)
    assert stored is not None and stored.seq == 3  # a refused write wrote nothing
    try:
        store.write_thread(bead, thread(3), expect_seq=3, actor="dev/test")
    except IntakeStoreError:
        pass
    else:
        raise AssertionError("a thread write that does not advance seq must be refused")


def _thread_keeps_other_documents(factory: ReportStoreFactory) -> None:
    store = factory()
    bead = store.create(draft(bound=True))
    before = store.get(bead)
    assert before is not None
    store.write_thread(bead, thread(3), expect_seq=0, actor="dev/test")
    after = store.get(bead)
    assert after is not None
    assert (after.snapshot, after.binding, after.external_ref) == (
        before.snapshot,
        before.binding,
        before.external_ref,
    )
    newer = SourceSnapshot("t2", "b2", IssueStatus.CLOSED, "r2")
    store.record_snapshot(bead, newer, actor="dev/test")  # the other writer leaves the thread
    assert store.read_thread(bead) == thread(3)
    view = store.get(bead)
    assert view is not None and view.snapshot == newer


def _thread_on_a_closed_report(factory: ReportStoreFactory) -> None:
    store = factory()
    bead = store.create(draft(bound=True))
    store.close(bead, reason="done", actor="dev/test")
    store.write_thread(bead, thread(3), expect_seq=0, actor="dev/test")  # closed issues in scope
    assert store.read_thread(bead) == thread(3)


def _thread_write_needs_its_bound_report(factory: ReportStoreFactory) -> None:
    store = factory()
    bead = store.create(draft(bound=True))
    native = store.create(draft("born native"))
    _expect_store_error(
        lambda: store.write_thread("acme-ghost", thread(3), expect_seq=0, actor="dev/test")
    )
    other = dataclasses.replace(thread(3), issue=ThreadIssue("#8", URL[:-1] + "8"))
    _expect_store_error(lambda: store.write_thread(bead, other, expect_seq=0, actor="dev/test"))
    _expect_store_error(lambda: store.write_thread(native, thread(3), expect_seq=0, actor="a"))
    assert store.read_thread(bead) is None and store.read_thread(native) is None


def _expect_outbox_moved(call: Callable[[], object]) -> None:
    try:
        call()
    except IntakeRefused as exc:
        assert exc.reason is RefusalReason.OUTBOX_MOVED, exc.reason
        return
    raise AssertionError("expected IntakeRefused(OUTBOX_MOVED) for a stale expect_seq")


def _outbox_absent_reads_none(factory: ReportStoreFactory) -> None:
    store = factory()
    bead = store.create(draft(bound=True))
    assert store.read_outbox(bead) is None
    assert store.read_outbox("acme-ghost") is None
    assert store.reports_with_outbox() == ()


def _outbox_round_trips_whole(factory: ReportStoreFactory) -> None:
    store = factory()
    bead = store.create(draft(bound=True))
    first = outbox(2, texts=("one", "two"))
    store.write_outbox(bead, first, expect_seq=0, actor="dev/test")
    assert store.read_outbox(bead) == first
    second = outbox(5, texts=("three",))  # the whole document is replaced, never merged
    store.write_outbox(bead, second, expect_seq=2, actor="dev/test")
    assert store.read_outbox(bead) == second


def _outbox_write_rechecks_seq(factory: ReportStoreFactory) -> None:
    store = factory()
    bead = store.create(draft(bound=True))
    store.write_outbox(bead, outbox(2), expect_seq=0, actor="dev/test")
    _expect_outbox_moved(lambda: store.write_outbox(bead, outbox(9), expect_seq=0, actor="a"))
    _expect_outbox_moved(lambda: store.write_outbox(bead, outbox(9), expect_seq=3, actor="a"))
    stored = store.read_outbox(bead)
    assert stored is not None and stored.seq == 2  # a refused write wrote nothing
    try:
        store.write_outbox(bead, outbox(2), expect_seq=2, actor="dev/test")
    except IntakeStoreError:
        pass
    else:
        raise AssertionError("an outbox write that does not advance seq must be refused")


def _outbox_keeps_other_documents(factory: ReportStoreFactory) -> None:
    store = factory()
    bead = store.create(draft(bound=True))
    before = store.get(bead)
    assert before is not None
    store.write_thread(bead, thread(3), expect_seq=0, actor="dev/test")
    store.write_outbox(bead, outbox(2), expect_seq=0, actor="dev/test")
    after = store.get(bead)
    assert after is not None
    assert (after.snapshot, after.binding, after.external_ref) == (
        before.snapshot,
        before.binding,
        before.external_ref,
    )
    assert store.read_thread(bead) == thread(3)  # each writer rewrites only its own key
    store.write_thread(bead, thread(4), expect_seq=3, actor="dev/test")
    assert store.read_outbox(bead) == outbox(2)


def _outbox_on_every_status_and_bound_only(factory: ReportStoreFactory) -> None:
    store = factory()
    open_report = store.create(draft(bound=True))
    closed = store.create(draft("another", bound=True))
    native = store.create(draft("born native"))
    store.write_outbox(open_report, outbox(2), expect_seq=0, actor="dev/test")
    store.close(closed, reason="done", actor="dev/test")
    store.write_outbox(closed, outbox(2), expect_seq=0, actor="dev/test")  # closed in scope
    _expect_store_error(lambda: store.write_outbox(native, outbox(2), expect_seq=0, actor="a"))
    ghost = "acme-ghost"
    _expect_store_error(lambda: store.write_outbox(ghost, outbox(2), expect_seq=0, actor="a"))
    assert sorted(v.id for v in store.reports_with_outbox()) == sorted([open_report, closed])
    assert store.read_outbox(native) is None


def _bound_create_opens_a_hold_and_a_native_one_does_not(factory: ReportStoreFactory) -> None:
    store = factory()
    native = store.create(draft("born native"))
    assert store.holds() == () and store.bound_reports() == ()
    bound = store.create(draft(bound=True))
    (hold,) = store.holds()
    assert hold.blocks == (bound,) and hold.gate not in (native, bound)
    assert [v.id for v in store.bound_reports()] == [bound]  # the native report is not bound


def _close_is_the_held_close(factory: ReportStoreFactory) -> None:
    """A bound report closes through ``close`` even though its own hold blocks a plain close, and
    its hold is resolved afterwards; a native report closes as it always did."""
    store = factory()
    bound, native = store.create(draft(bound=True)), store.create(draft("born native"))
    store.close(bound, reason="done", actor="dev/test")
    store.close(native, reason="done", actor="dev/test")
    for bead in (bound, native):
        view = store.get(bead)
        assert view is not None and view.status == "closed"
    assert store.holds() == ()  # the hold is resolved as bookkeeping
    assert [v.id for v in store.bound_reports()] == [bound]  # still findable, closed


def _holds_open_and_resolve_one_at_a_time(factory: ReportStoreFactory) -> None:
    store = factory()
    bead = store.create(draft(bound=True))
    store.open_hold(bead, actor="dev/test")
    first, second = store.holds()
    assert first.blocks == second.blocks == (bead,) and first.gate != second.gate
    store.resolve_hold(first.gate, actor="dev/test")
    assert [h.gate for h in store.holds()] == [second.gate]
    _expect_store_error(lambda: store.resolve_hold("acme-ghost", actor="dev/test"))
    _expect_store_error(lambda: store.open_hold("acme-ghost", actor="dev/test"))
    store.close(bead, reason="done", actor="dev/test")  # closes through its one remaining hold
    assert store.holds() == ()


REPORT_STORE_CASES: tuple[ReportStoreCase, ...] = (
    ReportStoreCase("store.scope-is-stable", _scope_is_stable),
    ReportStoreCase("store.create-reads-back", _create_reads_back),
    ReportStoreCase("store.set-state-replaces-its-dimension", _set_state_replaces_its_dimension),
    ReportStoreCase(
        "store.set-state-replaces-but-never-clears", _set_state_replaces_but_never_clears
    ),
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
    ReportStoreCase("store.thread-absent-reads-none", _thread_absent_reads_none),
    ReportStoreCase("store.thread-round-trips-whole", _thread_round_trips_whole),
    ReportStoreCase("store.thread-write-rechecks-seq", _thread_write_rechecks_seq),
    ReportStoreCase("store.thread-keeps-other-documents", _thread_keeps_other_documents),
    ReportStoreCase("store.thread-on-a-closed-report", _thread_on_a_closed_report),
    ReportStoreCase(
        "store.thread-write-needs-its-bound-report", _thread_write_needs_its_bound_report
    ),
    ReportStoreCase(
        "store.bound-create-opens-a-hold", _bound_create_opens_a_hold_and_a_native_one_does_not
    ),
    ReportStoreCase("store.close-is-the-held-close", _close_is_the_held_close),
    ReportStoreCase("store.outbox-absent-reads-none", _outbox_absent_reads_none),
    ReportStoreCase("store.outbox-round-trips-whole", _outbox_round_trips_whole),
    ReportStoreCase("store.outbox-write-rechecks-seq", _outbox_write_rechecks_seq),
    ReportStoreCase("store.outbox-keeps-other-documents", _outbox_keeps_other_documents),
    ReportStoreCase(
        "store.outbox-on-every-status-and-bound-only", _outbox_on_every_status_and_bound_only
    ),
    ReportStoreCase(
        "store.holds-open-and-resolve-one-at-a-time", _holds_open_and_resolve_one_at_a_time
    ),
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
    "outbox",
    "run_report_store_case",
    "thread",
]
