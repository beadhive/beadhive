"""``IntakePort.discover``: opt-in, pull-only discovery (I6) on ``MemoryTracker``.

Discovery runs the import upsert for each listed issue, checkpoints through ``CheckpointStore``
only after a page is durably upserted, and on a full pass reconciles closed issues and
``source_missing``. Every case runs offline: explicit ``at=`` tracker timestamps, an in-process
lock, a recording checkpoint store and a read-only tracker facade (a call to any writer would raise
``AttributeError``), so "no tracker write" is asserted twice, structurally and by the world's write
counter. No clock, no sleep, no socket.
"""

from __future__ import annotations

import dataclasses

import pytest

from beadhive.modules.intake import (
    INTAKE_UNTRIAGED,
    ORIGIN_GITHUB,
    Checkpoint,
    DiscoveryRequest,
    ImportRequest,
    IntakeRefused,
    IntakeService,
    IntakeStoreError,
    IssueDiscoverer,
    IssueImporter,
    IssueStatus,
    RefusalReason,
    ReportDraft,
    checkpoint_key,
)
from beadhive.modules.intake.domain import IssueBinding
from beadhive.modules.tracker.contracts import (
    IssueState,
    Operation,
    TransientFailure,
)
from beadhive.testing.intake import (
    InProcessIntakeLock,
    MemoryCheckpointStore,
    MemoryReportStore,
    memory_store,
)
from beadhive.testing.tracker import MemoryTracker, MemoryWorld, at

ACTOR = "dev/discoverer"
BINDING = IssueBinding("", "memory", "acme/widgets", "", "")
BINDING_KEY = "memory:acme/widgets"
CHECKPOINT_KEY = checkpoint_key(BINDING)


class _Labels:
    def violations(self, labels):
        return ()


class _ReadOnly:
    """A tracker that exposes ONLY the read half of the port and records every issue query: a
    discovery that reached a writer (or anything else) would raise ``AttributeError``."""

    def __init__(self, inner, *, ids_fail_after: int | None = None) -> None:
        self._inner = inner
        self.queries: list = []
        self._ids_fail_after = ids_fail_after

    def describe(self):
        return self._inner.describe()

    def list_issues(self, query):
        self.queries.append(query)
        return self._inner.list_issues(query)

    def list_ids(self, query):
        if self._ids_fail_after is not None:
            if self._ids_fail_after <= 0:
                raise TransientFailure("the id listing failed part-way")
            self._ids_fail_after -= 1
        return self._inner.list_ids(query)


@dataclasses.dataclass
class Rig:
    world: MemoryWorld
    tracker: _ReadOnly
    store: MemoryReportStore
    lock: InProcessIntakeLock
    marks: MemoryCheckpointStore
    service: IntakeService

    def issue(self, title: str = "Login is broken", *, day: int = 1, **kwargs):
        return self.world.add_issue(title, at=at(day), **kwargs)

    def discover(self, *, full: bool = False):
        return self.service.discover(DiscoveryRequest(ACTOR, full_reconcile=full))

    def view_of(self, key):
        found = self.store.find_by_external_ref(key.url)
        assert len(found) == 1
        return found[0]

    def mark(self) -> str | None:
        stored = self.marks.get(CHECKPOINT_KEY)
        return stored.token if stored else None


def _rig(*, page_size: int = 50, enabled=None, ids_fail_after: int | None = None) -> Rig:
    world = MemoryWorld()
    memory = MemoryTracker(
        world, page_size=page_size, **({} if enabled is None else {"enabled": enabled})
    )
    tracker = _ReadOnly(memory, ids_fail_after=ids_fail_after)
    store, lock, marks = memory_store(), InProcessIntakeLock(), MemoryCheckpointStore()
    importer = IssueImporter(store, _Labels(), tracker, lock, origin=ORIGIN_GITHUB)
    discoverer = IssueDiscoverer(store, tracker, importer, lock, marks)
    service = IntakeService(store, _Labels(), importer=importer, discoverer=discoverer, lock=lock)
    return Rig(world, tracker, store, lock, marks, service)


@pytest.fixture
def rig() -> Rig:
    return _rig()


# -- the happy path: the import upsert, for every issue the tracker lists --


def test_discovery_lands_held_untriaged_reports_for_open_and_closed_issues(rig: Rig) -> None:
    opened = rig.issue("Login is broken", day=1)
    closed = rig.issue("Old crash", day=2, state=IssueState.CLOSED)

    outcome = rig.discover()

    assert (outcome.created, outcome.updated, outcome.unchanged) == (2, 0, 0)
    assert outcome.checkpoint_advanced and not outcome.reconciled
    for key, state in ((opened, IssueStatus.OPEN), (closed, IssueStatus.CLOSED)):
        view = rig.view_of(key)
        assert {INTAKE_UNTRIAGED, ORIGIN_GITHUB} <= set(view.labels)
        assert view.snapshot is not None and view.snapshot.state is state
        assert not view.snapshot.source_missing
    assert rig.store.ready_ids() == ()  # every report is held, none is work
    assert {h.blocks[0] for h in rig.store.holds()} == set(outcome.reports)


def test_a_discovered_report_is_exactly_an_imported_one(rig: Rig) -> None:
    key = rig.issue()
    rig.discover()
    discovered = rig.view_of(key)
    writes = rig.store.writes

    again = rig.service.import_issue(ImportRequest(key.url, ACTOR))

    assert again.kind.value == "unchanged" and again.binding.report == discovered.id
    assert rig.store.writes == writes  # import found nothing to do: discovery had done it all


def test_discovery_with_nothing_to_find_is_a_quiet_success(rig: Rig) -> None:
    outcome = rig.discover()

    assert (outcome.created, outcome.updated, outcome.unchanged) == (0, 0, 0)
    assert not outcome.checkpoint_advanced and rig.mark() is None
    assert rig.store.writes == 0


# -- idempotence and the checkpoint --


def test_a_rerun_is_idempotent_and_writes_nothing(rig: Rig) -> None:
    keys = [rig.issue(f"Issue {n}", day=n) for n in (1, 2, 3)]
    first = rig.discover()
    writes, token = rig.store.writes, rig.mark()

    second = rig.discover()

    assert first.created == 3
    assert (second.created, second.updated) == (0, 0)
    assert second.unchanged == 1  # the inclusive boundary issue repeats and is a no-op
    assert not second.checkpoint_advanced and rig.mark() == token
    assert rig.store.writes == writes
    assert len(rig.store.bound_reports()) == len(keys)


def test_the_checkpoint_is_the_trackers_own_mark_and_the_next_run_resumes_from_it(
    rig: Rig,
) -> None:
    rig.issue("One", day=1)
    last = rig.issue("Two", day=2)
    rig.discover()
    assert rig.mark() == "memory:" + at(2).isoformat()  # the tracker's own stamp
    first_query = rig.tracker.queries[0]
    assert first_query.since is None and first_query.include_closed is True

    fresh = rig.issue("Three", day=3)
    outcome = rig.discover()

    resumed = rig.tracker.queries[-1]
    assert resumed.since is not None and resumed.since.token == rig.marks.puts[0][1]
    assert (outcome.created, outcome.unchanged) == (1, 1)  # Three is new, Two is the boundary
    assert rig.view_of(fresh) and rig.view_of(last)
    assert [key for key, _ in rig.marks.puts] == [CHECKPOINT_KEY, CHECKPOINT_KEY]


def test_a_newly_closed_or_edited_issue_is_picked_up_incrementally(rig: Rig) -> None:
    key = rig.issue("Flaky test", day=1)
    rig.discover()
    before = rig.view_of(key).snapshot
    rig.world.update_issue(key, at=at(5), state=IssueState.CLOSED)

    outcome = rig.discover()

    assert (outcome.created, outcome.updated) == (0, 1)
    after = rig.view_of(key).snapshot
    assert before is not None and after is not None
    assert (before.state, after.state) == (IssueStatus.OPEN, IssueStatus.CLOSED)
    assert INTAKE_UNTRIAGED in rig.view_of(key).labels  # triage is never reset by a refresh


def test_the_checkpoint_moves_only_after_a_page_is_durable_and_a_crash_resumes(
    monkeypatch,
) -> None:
    rig = _rig(page_size=2)
    keys = [rig.issue(f"Issue {n}", day=n) for n in (1, 2, 3, 4, 5)]
    creates = {"n": 0}
    real_create = rig.store.create

    def create(draft):
        creates["n"] += 1
        if creates["n"] == 3:  # the first create of page two
            raise IntakeStoreError("injected create failure", operation="create")
        return real_create(draft)

    monkeypatch.setattr(rig.store, "create", create)

    with pytest.raises(IntakeStoreError):
        rig.discover()

    assert len(rig.marks.puts) == 1  # only page one was durable, so only its mark was stored
    assert len(rig.store.bound_reports()) == 2
    monkeypatch.setattr(rig.store, "create", real_create)

    outcome = rig.discover()  # resumes from page one's mark and finishes the pull

    assert outcome.created == 3 and outcome.unchanged == 1  # the boundary issue repeats
    assert {rig.view_of(key).id for key in keys}  # exactly one report per issue
    assert len(rig.store.bound_reports()) == len(keys)
    assert rig.tracker.queries[-1].since is not None


def test_a_refusal_on_one_issue_stops_before_the_checkpoint_passes_it() -> None:
    rig = _rig(page_size=1)
    first, second = rig.issue("One", day=1), rig.issue("Two", day=2)
    rig.store.create(  # two rows already carry the second URL: R8, refused until resolved
        _bound_draft(second.url, "dup-a")
    )
    rig.store.create(_bound_draft(second.url, "dup-b"))

    with pytest.raises(IntakeRefused) as refused:
        rig.discover()

    assert refused.value.reason is RefusalReason.DUPLICATE_BINDING
    assert rig.view_of(first).snapshot is not None  # what came before it was ingested
    assert len(rig.marks.puts) == 1  # and only that far did the mark move


def _bound_draft(url: str, title: str) -> ReportDraft:
    return ReportDraft(
        title=title,
        issue_type="bug",
        origin=ORIGIN_GITHUB,
        actor=ACTOR,
        binding=IssueBinding("", "memory", "acme/widgets", url, "#2"),
    )


# -- --full: closed and newly closed issues, and source_missing --


def test_a_full_pass_ignores_the_checkpoint_and_reconciles_what_it_would_skip(
    rig: Rig,
) -> None:
    key = rig.issue("Old crash", day=1, state=IssueState.CLOSED)
    rig.discover()
    rig.world.update_issue(key, at=at(3), state=IssueState.OPEN)  # reopened upstream
    gone = rig.issue("Reported late", day=2, state=IssueState.CLOSED)
    rig.marks.put(CHECKPOINT_KEY, Checkpoint("memory:" + at(9).isoformat()))  # a mark ahead of both

    incremental = rig.discover()
    full = rig.discover(full=True)

    assert (incremental.created, incremental.updated) == (0, 0)  # the stale mark hides them
    assert rig.store.find_by_external_ref(gone.url) != ()  # the full pass found the closed one
    assert full.created == 1 and full.updated == 1 and full.reconciled
    reopened = rig.view_of(key).snapshot
    assert reopened is not None and reopened.state is IssueStatus.OPEN
    assert rig.tracker.queries[-1].since is None and rig.tracker.queries[-1].include_closed


def test_a_full_pass_marks_a_vanished_issue_source_missing_and_never_closes_it(rig: Rig) -> None:
    kept, vanished = rig.issue("Kept", day=1), rig.issue("Vanished", day=2)
    rig.discover()
    rig.world.delete_issue(vanished)

    incremental = rig.discover()
    assert incremental.source_missing == 0 and not rig.view_of(vanished).snapshot.source_missing
    full = rig.discover(full=True)

    assert (full.source_missing, full.reconciled) == (1, True)
    view = rig.view_of(vanished)
    assert view.snapshot is not None and view.snapshot.source_missing
    assert view.status == "open" and INTAKE_UNTRIAGED in view.labels  # never deleted or closed
    assert not rig.view_of(kept).snapshot.source_missing
    writes = rig.store.writes

    again = rig.discover(full=True)

    assert again.source_missing == 0 and rig.store.writes == writes  # idempotent


def test_an_issue_that_is_back_clears_source_missing(rig: Rig) -> None:
    key = rig.issue("Flapping", day=1)
    rig.discover()
    view = rig.view_of(key)
    assert view.snapshot is not None
    rig.store.record_snapshot(
        view.id, dataclasses.replace(view.snapshot, source_missing=True), actor=ACTOR
    )

    full = rig.discover(full=True)

    assert full.updated == 1 and not rig.view_of(key).snapshot.source_missing


def test_an_incomplete_id_listing_never_marks_anything_missing() -> None:
    rig = _rig(page_size=1, ids_fail_after=1)  # the second page of ids fails
    rig.issue("One", day=1)
    rig.issue("Two", day=2)
    gone = rig.issue("Three", day=3)
    rig.discover()
    rig.world.delete_issue(gone)

    full = rig.discover(full=True)

    assert (full.source_missing, full.reconciled) == (0, False)
    assert not rig.view_of(gone).snapshot.source_missing


def test_another_projects_report_is_never_marked_missing(rig: Rig) -> None:
    key = rig.issue("Ours", day=1)
    rig.discover()
    foreign = rig.store.create(
        dataclasses.replace(
            _bound_draft("memory://elsewhere/issues/1", "foreign"),
            binding=IssueBinding(
                "", "memory", "other/project", "memory://elsewhere/issues/1", "#1"
            ),
            snapshot=rig.view_of(key).snapshot,
        )
    )

    full = rig.discover(full=True)

    assert full.source_missing == 0
    foreign_view = rig.store.get(foreign)
    assert foreign_view is not None and foreign_view.snapshot is not None
    assert not foreign_view.snapshot.source_missing


# -- pull-only: no tracker write, one lock, off without a tracker --


def test_discovery_writes_nothing_to_the_tracker(rig: Rig) -> None:
    rig.issue("One", day=1)
    rig.issue("Two", day=2, state=IssueState.CLOSED)
    snapshot_of_world = {k: (i.title, i.state, set(i.labels)) for k, i in rig.world.issues.items()}

    rig.discover()
    rig.discover(full=True)

    assert rig.world.write_requests() == 0 and rig.world.comments == {}
    assert snapshot_of_world == {
        k: (i.title, i.state, set(i.labels)) for k, i in rig.world.issues.items()
    }
    assert rig.world.read_requests() > 0


def test_a_tracker_without_issues_read_is_a_clean_refusal_that_writes_nothing() -> None:
    rig = _rig(enabled=frozenset({Operation.COMMENTS_READ}))
    rig.issue("One", day=1)

    with pytest.raises(IntakeRefused) as refused:
        rig.discover()

    assert refused.value.reason is RefusalReason.INTAKE_DISABLED
    assert rig.store.writes == 0 and rig.marks.puts == []


def test_discovery_holds_the_binding_lock_for_the_run_and_a_busy_binding_is_refused(
    rig: Rig,
) -> None:
    rig.issue("One", day=1)

    with rig.lock.hold(BINDING_KEY):  # an import (or another discovery) is in flight
        with pytest.raises(IntakeRefused) as refused:
            rig.discover()
    assert refused.value.reason is RefusalReason.INTAKE_BUSY
    assert rig.store.writes == 0 and rig.marks.puts == []
    rig.lock.holds.clear()

    rig.discover()

    assert rig.lock.holds == [BINDING_KEY]  # one hold, released after the run
    assert not rig.lock.held(BINDING_KEY)


def test_a_tracker_failure_is_a_refusal_naming_it_and_moves_nothing() -> None:
    rig = _rig()
    rig.issue("One", day=1)
    rig.world.throttle(requests=99, retry_after_seconds=10_000)

    with pytest.raises(IntakeRefused) as refused:
        rig.discover()

    assert refused.value.reason is RefusalReason.TRACKER_ERROR
    assert rig.store.writes == 0 and rig.marks.puts == []
