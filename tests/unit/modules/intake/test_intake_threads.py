"""Comment ingestion on ``MemoryTracker``: the ``bh-fptob`` test plan T1 to T19.

Every case drives :class:`ThreadService` against ``MemoryTracker`` / ``MemoryWorld`` with explicit
``at=`` tracker timestamps, the in-memory report store, the never-waiting in-process lock and an
outbox view over plain sets. No case reads the host clock, sleeps or opens a socket; ordering is
asserted on ``seq``, ``rev``, tracker revisions and request counters only. The pure rules have
their own direct cases in ``test_intake_thread_rules.py``.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable

import pytest

from beadhive.modules.intake import (
    THREAD_KEY,
    ConsiderRequest,
    IntakeRefused,
    IntakeService,
    IntakeStoreError,
    IssueBinding,
    NullIntake,
    OwnIdentity,
    PresenceRequest,
    RefusalReason,
    ReportDraft,
    StorePosture,
    ThreadPolicy,
    ThreadRequest,
    ThreadService,
)
from beadhive.modules.intake.contracts import (
    CheckpointScope,
    Evidence,
    Flag,
    OwnMark,
    RecordState,
    Role,
    ThreadCheckpoint,
    ThreadState,
)
from beadhive.modules.intake.domain import marker_bead_ids
from beadhive.modules.tracker.contracts import (
    READ_OPERATIONS,
    CommentKey,
    CommentQuery,
    IdPage,
    IdQuery,
    IdSubject,
    IssueKey,
    IssueState,
    Operation,
    OwnMarker,
    Page,
    TransientFailure,
    WriteGrant,
    WriteOutcomeUnknown,
)
from beadhive.testing.intake import (
    InProcessIntakeLock,
    MemoryOutboxView,
    MemoryReportStore,
    memory_store,
)
from beadhive.testing.tracker import MemoryTracker, MemoryWorld, at

IDENTITY = OwnIdentity("acme/widgets", "8c1f2d4e-0000-4000-8000-000000000001")
OUTBOX_ID = "ob1_" + "q" * 26
FOREIGN_URL = "memory://someone/else/issues/1"
ACTOR = "intake/ingest"
PRIVATE = StorePosture(published=False)


class _Delegate:
    """A tracker wrapper base: forwards every port call to the wrapped ``MemoryTracker``."""

    def __init__(self, inner: MemoryTracker) -> None:
        self.inner = inner

    def describe(self):
        return self.inner.describe()

    def list_issues(self, query):
        return self.inner.list_issues(query)

    def list_comments(self, query: CommentQuery) -> Page:
        return self.inner.list_comments(query)

    def list_ids(self, query: IdQuery) -> IdPage:
        return self.inner.list_ids(query)

    def whoami(self):
        return self.inner.whoami()

    def probe_capabilities(self):
        return self.inner.probe_capabilities()


@dataclasses.dataclass
class Rig:
    world: MemoryWorld
    tracker: MemoryTracker
    store: MemoryReportStore
    lock: InProcessIntakeLock
    outbox: MemoryOutboxView
    service: ThreadService
    report: str
    issue: IssueKey

    def ingest(self):
        return self.service.ingest_thread(ThreadRequest(self.report, ACTOR))

    def reconcile(self):
        return self.service.reconcile_thread(ThreadRequest(self.report, ACTOR))

    def thread(self):
        thread = self.store.read_thread(self.report)
        assert thread is not None
        return thread

    def record(self, key: CommentKey):
        return self.thread().records[key.id]

    def comment(self, body: str, *, day: int, author: str = "alice", **kwargs) -> CommentKey:
        return self.world.add_comment(self.issue, body, at=at(day), author=author, **kwargs)

    def document(self):
        return self.store.metadata(self.report)[THREAD_KEY]


def rig(
    *,
    state: IssueState = IssueState.OPEN,
    policy: ThreadPolicy | None = None,
    posture: StorePosture = PRIVATE,
    wrap: Callable[[MemoryTracker], object] | None = None,
    enabled: frozenset[Operation] = READ_OPERATIONS,
    page_size: int = 50,
    integrations: frozenset[str] = frozenset(),
) -> Rig:
    world = MemoryWorld()
    issue = world.add_issue("login is broken", at=at(1), state=state)
    tracker = MemoryTracker(world, enabled=enabled, page_size=page_size)
    store = memory_store()
    binding = IssueBinding("", "memory", world.locator, issue.url, issue.identifier)
    report = store.create(
        ReportDraft("login is broken", "bug", "origin:github", "importer/a", binding=binding)
    )
    store.set_state(report, "intake", "untriaged", reason="imported", actor="importer/a")
    lock, outbox = InProcessIntakeLock(), MemoryOutboxView()
    service = ThreadService(
        store,
        wrap(tracker) if wrap else tracker,  # type: ignore[arg-type]
        lock,
        posture=posture,
        identity=IDENTITY,
        outbox=outbox,
        policy=policy,
        integrations=integrations,
    )
    return Rig(world, tracker, store, lock, outbox, service, report, issue)


def _mark(world: MemoryWorld, key: CommentKey, marker: OwnMarker) -> None:
    """A marker decoded from a comment's last line (the adapter's job; set on the server row)."""
    world.comments[key.id].marker = marker


# -- T1, T2: first ingest and the inclusive boundary repeat ----------------------------------


def test_t1_first_ingest_stores_three_live_records_and_the_checkpoint_in_one_write() -> None:
    r = rig()
    keys = [r.comment(f"comment {n}", day=n) for n in (2, 3, 4)]

    outcome = r.ingest()

    thread = r.thread()
    assert [r.record(k).state for k in keys] == [RecordState.LIVE] * 3
    assert [r.record(k).rev for k in keys] == [1, 1, 1]
    assert [r.record(k).body for k in keys] == ["comment 2", "comment 3", "comment 4"]
    assert thread.checkpoint is not None
    assert (thread.checkpoint.scope, thread.checkpoint.subject) == (
        CheckpointScope.ISSUE,
        r.issue.url,
    )
    assert r.store.thread_writes == [(r.report, thread.seq)]  # records + checkpoint: ONE write
    assert outcome.written and outcome.complete and outcome.checkpoint_advanced
    assert thread.seq == 4  # three records and the checkpoint, one bump each


def test_t2_a_boundary_repeat_writes_nothing_and_keeps_seq() -> None:
    r = rig()
    for n in (2, 3, 4):
        r.comment(f"comment {n}", day=n)
    r.ingest()
    seq, writes, reads = r.thread().seq, r.store.writes, r.world.read_requests()

    outcome = r.ingest()

    assert r.world.read_requests() > reads  # it did read (the boundary item repeats)
    assert not outcome.written and outcome.events == ()
    assert r.store.writes == writes and r.store.thread_writes == [(r.report, seq)]
    assert r.thread().seq == seq


# -- T3 to T5: revisions ---------------------------------------------------------------------


def test_t3_an_edit_appends_rev_2_and_is_new_evidence_past_the_triage_watermark() -> None:
    r = rig()
    edited, other = r.comment("first words", day=2), r.comment("unrelated", day=3)
    r.ingest()
    r.service.mark_considered(ConsiderRequest(r.report, "triage/a"))
    assert r.service.thread_of(r.report).new_evidence == ()

    r.world.edit_comment(edited, "second thoughts", at=at(5))
    r.ingest()

    record = r.record(edited)
    assert [rv.rev for rv in record.revisions] == [1, 2] and record.body == "second thoughts"
    assert record.considered_rev == 1
    assert r.service.thread_of(r.report).new_evidence == (edited.id,)
    assert r.record(other).rev == 1


def test_t4_an_equal_tracker_revision_with_a_different_body_is_a_new_rev_then_a_no_op() -> None:
    r = rig()
    key = r.comment("draft", day=2)
    r.ingest()
    r.world.edit_comment(key, "final", at=at(2))  # a coarse clock: same timestamp, new content

    r.ingest()
    seq = r.thread().seq

    record = r.record(key)
    assert [rv.rev for rv in record.revisions] == [1, 2] and record.body == "final"
    assert record.revisions[0].tracker_revision == record.revisions[1].tracker_revision
    assert not r.ingest().written and r.thread().seq == seq  # a later identical read: no-op


def test_t5_a_stale_observation_is_ignored() -> None:
    replay: list[Page] = []

    class LaggingReplica(_Delegate):
        """Serves a page recorded earlier, as a lagging read replica would."""

        def list_comments(self, query):
            if replay:
                return replay.pop()
            return self.inner.list_comments(query)

    r = rig(wrap=LaggingReplica)
    key = r.comment("old words", day=2)
    old_page = r.tracker.list_comments(CommentQuery(issue=r.issue))
    r.ingest()
    r.world.edit_comment(key, "new words", at=at(5))
    r.ingest()
    seq = r.thread().seq
    replay.append(dataclasses.replace(old_page, checkpoint=None))

    outcome = r.ingest()

    assert not outcome.written and r.thread().seq == seq
    record = r.record(key)
    assert record.rev == 2 and record.body == "new words"
    assert record.latest.tracker_revision == at(5)


# -- T6, T7: the checkpoint advances only with a durable write -------------------------------


def _scripted(r: Rig) -> None:
    for n in (2, 3, 4):
        r.comment(f"comment {n}", day=n)


def test_t6_a_failed_store_write_leaves_the_checkpoint_and_a_rerun_converges() -> None:
    interrupted, clean = rig(), rig()
    _scripted(interrupted)
    _scripted(clean)
    interrupted.store.fail_next_thread_write()

    with pytest.raises(IntakeStoreError):
        interrupted.ingest()
    assert interrupted.store.read_thread(interrupted.report) is None  # no checkpoint, no records

    interrupted.ingest()
    clean.ingest()
    assert interrupted.document() == clean.document()


def test_t6_a_failed_write_after_an_earlier_pass_keeps_the_earlier_checkpoint() -> None:
    r = rig()
    r.comment("first", day=2)
    r.ingest()
    before = r.thread()
    r.comment("second", day=3)
    r.store.fail_next_thread_write()

    with pytest.raises(IntakeStoreError):
        r.ingest()

    assert r.thread() == before
    r.ingest()
    assert len(r.thread().records) == 2 and r.thread().checkpoint != before.checkpoint


class _WithheldUntilLastPage(_Delegate):
    """A per-issue listing in creation order: no checkpoint until the final page (E4)."""

    crash_on_page: int | None = None

    def __init__(self, inner: MemoryTracker) -> None:
        super().__init__(inner)
        self.pages = 0

    def list_comments(self, query):
        self.pages += 1
        if self.crash_on_page == self.pages:
            raise TransientFailure("connection dropped")
        page = self.inner.list_comments(query)
        if page.next_cursor is not None:
            return dataclasses.replace(page, checkpoint=None)
        return page


def test_t7_a_crash_mid_thread_never_advances_a_withheld_checkpoint_and_replays_cleanly() -> None:
    wrappers: list[_WithheldUntilLastPage] = []

    def wrap(inner):
        wrappers.append(_WithheldUntilLastPage(inner))
        return wrappers[-1]

    crashed, clean = rig(wrap=wrap, page_size=2), rig(wrap=wrap, page_size=2)
    for target in (crashed, clean):
        for n in (2, 3, 4, 5, 6):
            target.comment(f"comment {n}", day=n)
    wrappers[0].crash_on_page = 2

    first = crashed.ingest()

    assert not first.complete and first.written and "dropped" in first.detail
    assert len(crashed.thread().records) == 2  # page 1 may be written...
    assert crashed.thread().checkpoint is None  # ...but the withheld checkpoint never advanced

    wrappers[0].crash_on_page = None
    assert crashed.ingest().complete
    clean.ingest()
    assert crashed.document() == clean.document()
    assert not crashed.ingest().written  # the replay is idempotent


# -- T8 to T12: deletions by id-set reconciliation only --------------------------------------


def test_t8_one_complete_snapshot_without_a_comment_counts_one_absence() -> None:
    r = rig()
    keep, gone = r.comment("keep", day=2), r.comment("gone", day=3)
    r.ingest()
    r.world.delete_comment(gone)

    assert not r.ingest().written  # a since-read never infers a deletion
    outcome = r.reconcile()

    assert outcome.written and r.record(gone).absent_confirmations == 1
    assert r.record(gone).state is RecordState.LIVE and r.record(gone).body == "gone"
    assert r.record(keep).absent_confirmations == 0


def test_t9_a_second_complete_snapshot_tombstones_purges_the_body_and_keeps_digests() -> None:
    r = rig()
    gone = r.comment("a mistake", day=2)
    r.ingest()
    r.service.mark_considered(ConsiderRequest(r.report, "triage/a"))
    digests = [rv.digest for rv in r.record(gone).revisions]
    r.world.delete_comment(gone)

    r.reconcile()
    r.reconcile()

    record = r.record(gone)
    assert record.state is RecordState.TOMBSTONED and record.tombstone_seq is not None
    assert record.body is None and [rv.digest for rv in record.revisions] == digests
    assert record.author is not None and record.considered_rev == 1
    assert r.service.thread_of(r.report).new_evidence == (gone.id,)  # deleted since triage


def test_t9_bodies_are_kept_when_purging_is_off() -> None:
    r = rig(policy=ThreadPolicy(purge_deleted_bodies=False, deletion_confirmations=1))
    gone = r.comment("keep my words", day=2)
    r.ingest()
    r.world.delete_comment(gone)

    r.reconcile()

    assert r.record(gone).state is RecordState.TOMBSTONED and r.record(gone).body == "keep my words"


class _FailingIds(_Delegate):
    """A comment-id listing that fails on its second page."""

    def __init__(self, inner: MemoryTracker) -> None:
        super().__init__(inner)
        self.calls = 0

    def list_ids(self, query):
        self.calls += 1
        if query.subject is IdSubject.COMMENTS and self.calls == 2:
            raise TransientFailure("server error mid-listing")
        return self.inner.list_ids(query)


def test_t10_a_snapshot_that_fails_mid_pagination_is_incomplete_and_changes_nothing() -> None:
    r = rig(wrap=_FailingIds, page_size=2)
    keys = [r.comment(f"c{n}", day=n) for n in (2, 3, 4, 5)]  # three left: two id pages
    r.ingest()
    before = r.thread()
    r.world.delete_comment(keys[0])

    outcome = r.reconcile()

    assert not outcome.complete and not outcome.written
    assert r.thread() == before and r.record(keys[0]).absent_confirmations == 0


def test_t11_a_record_first_seen_after_the_snapshot_began_is_never_a_candidate() -> None:
    hook: list[Callable[[], None]] = []

    class LateArrival(_Delegate):
        def list_ids(self, query):
            page = self.inner.list_ids(query)
            if page.next_cursor is None and hook:
                hook.pop()()  # a comment lands and is ingested after the listing was taken
            return page

    r = rig(wrap=LateArrival)
    early = r.comment("early", day=2)
    r.ingest()
    late: list[CommentKey] = []

    def arrive() -> None:
        late.append(r.comment("late", day=3))
        assert r.ingest().written

    hook.append(arrive)
    outcome = r.reconcile()

    assert late and late[0].id in r.thread().records
    assert r.record(late[0]).absent_confirmations == 0  # absent from the snapshot, not a candidate
    assert r.record(early).absent_confirmations == 0 and not outcome.written


class _PageShift(_Delegate):
    """Hides ids from complete id listings, as a concurrent delete shifting a page would."""

    def __init__(self, inner: MemoryTracker) -> None:
        super().__init__(inner)
        self.hidden: set[str] = set()

    def list_ids(self, query):
        page = self.inner.list_ids(query)
        return dataclasses.replace(page, ids=tuple(i for i in page.ids if i not in self.hidden))


def test_t12_a_tombstoned_id_that_reappears_in_a_snapshot_is_live_again() -> None:
    wrapped: list[_PageShift] = []
    r = rig(wrap=lambda inner: wrapped.append(_PageShift(inner)) or wrapped[-1])
    key = r.comment("still here", day=2)
    r.ingest()
    wrapped[0].hidden.add(key.id)
    r.reconcile()
    r.reconcile()
    assert r.record(key).state is RecordState.TOMBSTONED

    wrapped[0].hidden.clear()
    r.reconcile()

    record = r.record(key)
    assert record.state is RecordState.LIVE and record.resurrected == 1 and record.rev == 1
    assert record.tombstone_seq is None and record.absent_confirmations == 0


def test_t12_a_tombstoned_id_seen_again_by_a_read_is_live_with_its_content_restored() -> None:
    wrapped: list[_PageShift] = []
    r = rig(wrap=lambda inner: wrapped.append(_PageShift(inner)) or wrapped[-1])
    key = r.comment("still here", day=2)
    r.ingest()
    wrapped[0].hidden.add(key.id)
    r.reconcile()
    r.reconcile()
    assert r.record(key).body is None

    r.world.edit_comment(key, "still here, edited", at=at(4))
    r.ingest()

    record = r.record(key)
    assert record.state is RecordState.LIVE and record.resurrected == 1
    assert record.rev == 2 and record.body == "still here, edited"


# -- T13, T14: own-message recognition -------------------------------------------------------


def test_t13_own_recognition_matrix_on_the_memory_tracker() -> None:
    r = rig(
        enabled=READ_OPERATIONS | {Operation.COMMENT_CREATE}, integrations=frozenset({"app-42"})
    )
    r.world.set_write_time(at(3))
    receipt = r.tracker.post_comment(
        r.issue,
        "we merged a fix",
        marker=OwnMarker(IDENTITY.hive, IDENTITY.install, "ob1_" + "r" * 26),
        grant=WriteGrant("g-1", Operation.COMMENT_CREATE, r.issue),
    )
    assert receipt.comment is not None
    r.outbox.receipts.add(receipt.comment.key.id)
    by_integration = r.comment("ci passed", day=4, author="ci", integration="app-42")
    unledgered = r.comment("posted by hand on the bot token", day=4, author="maintainer")
    _mark(r.world, unledgered, OwnMarker(IDENTITY.hive, IDENTITY.install, "ob1_" + "u" * 26))
    peer = r.comment("from another hive", day=4, author="bob")
    _mark(r.world, peer, OwnMarker("acme/other", IDENTITY.install, "ob1_" + "p" * 26))
    shared_token = r.comment("a human on the shared token", day=5, author="maintainer")
    pasted = r.comment("I copied your footer", day=5, author="alice")
    _mark(r.world, pasted, OwnMarker(IDENTITY.hive, IDENTITY.install, "ob1_" + "r" * 26))

    r.ingest()

    def role(key):
        record = r.record(key)
        return record.role, record.evidence

    assert role(receipt.comment.key) == (Role.OWN, Evidence.RECEIPT)
    assert role(by_integration) == (Role.OWN, Evidence.INTEGRATION)
    assert role(unledgered) == (Role.OWN, Evidence.ACTOR_MARKER)
    assert Flag.UNLEDGERED.value in r.record(unledgered).flags
    assert role(peer) == (Role.PEER, Evidence.MARKER)
    assert role(shared_token) == (Role.EXTERNAL, Evidence.NONE)  # a known actor, no marker
    assert role(pasted) == (Role.EXTERNAL, Evidence.NONE)  # a marker alone is never enough
    view = r.service.thread_of(r.report)
    assert view.new_evidence == (shared_token.id, pasted.id)
    assert view.flagged == (unledgered.id,)
    assert r.world.write_requests() == 1  # the test's own post; ingestion wrote nothing


def test_t13_an_edit_never_changes_a_role_in_either_direction() -> None:
    r = rig()
    own = r.comment("bot words", day=2, author="maintainer")
    _mark(r.world, own, OwnMarker(IDENTITY.hive, IDENTITY.install, OUTBOX_ID))
    human = r.comment("human words", day=2, author="maintainer")
    r.ingest()

    r.world.edit_comment(own, "a human rewrote the bot's comment", at=at(3))
    r.world.comments[own.id].marker = None  # and dropped the marker line
    r.world.edit_comment(human, "now with a pasted footer", at=at(3))
    _mark(r.world, human, OwnMarker(IDENTITY.hive, IDENTITY.install, OUTBOX_ID))
    r.ingest()

    assert r.record(own).role is Role.OWN and r.record(own).rev == 2
    assert Flag.EDITED_EXTERNALLY.value in r.record(own).flags
    assert r.record(human).role is Role.EXTERNAL and r.record(human).rev == 2


def test_t14_an_ambiguous_supervised_post_is_reconciled_to_its_pending_outbox_record() -> None:
    r = rig(enabled=READ_OPERATIONS | {Operation.COMMENT_CREATE})
    r.world.set_write_time(at(3))
    r.world.fail_next_write_ambiguously()
    with pytest.raises(WriteOutcomeUnknown):
        r.tracker.post_comment(
            r.issue,
            "your fix is merged",
            marker=OwnMarker(IDENTITY.hive, IDENTITY.install, OUTBOX_ID),
            grant=WriteGrant("g-1", Operation.COMMENT_CREATE, r.issue),
        )
    r.outbox.pending_ids.add(OUTBOX_ID)

    outcome = r.ingest()

    (posted,) = r.thread().records.values()
    assert (posted.role, posted.evidence, posted.outbox_id) == (
        Role.OWN,
        Evidence.RECONCILED,
        OUTBOX_ID,
    )
    assert outcome.reconciled == ((OUTBOX_ID, posted.id),)
    assert r.world.write_requests() == 1  # exactly the one post; ingestion never writes
    assert r.ingest().reconciled == ()  # bound once


# -- T15 to T17 ------------------------------------------------------------------------------


def test_t15_a_closed_issue_is_ingested_like_an_open_one() -> None:
    closed, opened = rig(state=IssueState.CLOSED), rig()
    for target in (closed, opened):
        target.comment("still broken after close", day=2)

    closed.ingest()
    opened.ingest()

    assert closed.document() == opened.document()


def test_t16_an_issue_missing_from_two_complete_snapshots_marks_the_thread_missing_only() -> None:
    r = rig()
    r.comment("hello", day=2)
    r.ingest()
    before = r.store.get(r.report)
    r.world.delete_issue(r.issue)
    request = PresenceRequest((r.report,), ACTOR)

    r.service.reconcile_presence(request)
    assert r.thread().state is ThreadState.PRESENT and r.thread().missing_confirmations == 1
    r.service.reconcile_presence(request)

    assert r.thread().state is ThreadState.MISSING
    after = r.store.get(r.report)
    assert after == before and after.status == "open" and "intake:untriaged" in after.labels
    assert r.store.history(r.report) == (("intake", "untriaged", "imported"),)


def test_t16_a_presence_pass_with_a_failed_listing_changes_nothing() -> None:
    class FailingIssues(_Delegate):
        def list_ids(self, query):
            if query.subject is IdSubject.ISSUES:
                raise TransientFailure("server error")
            return self.inner.list_ids(query)

    r = rig(wrap=FailingIssues)
    r.comment("hello", day=2)
    r.ingest()
    before = r.thread()

    outcome = r.service.reconcile_presence(PresenceRequest((r.report,), ACTOR))

    assert not outcome.complete and r.thread() == before


@pytest.mark.parametrize(
    ("scope", "subject"),
    [(CheckpointScope.PROJECT, ""), (CheckpointScope.ISSUE, "memory://acme/widgets/issues/99")],
)
def test_t17_a_token_of_another_scope_is_refused_before_any_read(scope, subject) -> None:
    r = rig()
    r.comment("hello", day=2)
    r.ingest()
    thread = r.thread()
    foreign = ThreadCheckpoint(scope, subject, "memory:2026-01-02T00:00:00+00:00", thread.seq + 1)
    r.store.write_thread(
        r.report,
        dataclasses.replace(thread, seq=thread.seq + 1, checkpoint=foreign),
        expect_seq=thread.seq,
        actor="dev/test",
    )
    reads = r.world.read_requests()

    with pytest.raises(IntakeRefused) as refused:
        r.ingest()

    assert refused.value.reason is RefusalReason.CHECKPOINT_SCOPE
    assert r.world.read_requests() == reads


# -- T18, T19: isolation and marker privacy --------------------------------------------------


def test_t18_ingestion_never_writes_the_tracker_or_the_public_store_or_another_document() -> None:
    r = rig()
    public = MemoryReportStore(dataclasses.replace(r.store.scope(), operational_prefix="pub"))
    public_row = public.create(ReportDraft("projection row", "bug", "origin:github", "pull"))
    public_before = json.dumps(
        [dataclasses.asdict(public.get(public_row)), public.metadata(public_row)], default=str
    )
    commits = {"commits": ["abc123"], "schema": 1}
    r.store.put_metadata(r.report, "git.commits", commits)
    source_before = r.store.get(r.report)
    keys = [r.comment(f"c{n}", day=n) for n in (2, 3)]

    r.ingest()
    r.world.edit_comment(keys[0], "edited", at=at(4))
    r.ingest()
    r.world.delete_comment(keys[1])
    r.reconcile()
    r.reconcile()
    r.service.reconcile_presence(PresenceRequest((r.report,), ACTOR))
    r.service.mark_considered(ConsiderRequest(r.report, "triage/a"))

    assert r.world.write_requests() == 0
    assert public.writes == 1  # only its own create above
    public_after = json.dumps(
        [dataclasses.asdict(public.get(public_row)), public.metadata(public_row)], default=str
    )
    assert public_after == public_before
    assert r.store.metadata(r.report)["git.commits"] == commits
    assert r.store.get(r.report) == source_before  # labels, status, binding, intake.source


def test_t19_the_identity_and_an_opaque_outbox_id_name_no_fleet_bead() -> None:
    prefixes = ("bh", "hq", "acme", "pub", "widgets")
    marker = OwnMark(IDENTITY.hive, IDENTITY.install, OUTBOX_ID)

    assert marker_bead_ids(marker, prefixes) == ()
    assert marker_bead_ids(dataclasses.replace(marker, outbox_id="acme-0007"), prefixes) == (
        "acme-0007",
    )


# -- refusals before any read, the lock and the seq re-check ---------------------------------


def test_a_published_operational_store_is_refused_before_any_read_unless_acknowledged() -> None:
    r = rig(posture=StorePosture(published=True))
    r.comment("hello", day=2)
    operations = (
        r.ingest,
        r.reconcile,
        lambda: r.service.reconcile_presence(PresenceRequest((r.report,), ACTOR)),
        lambda: r.service.mark_considered(ConsiderRequest(r.report, "triage/a")),
    )
    for operation in operations:
        with pytest.raises(IntakeRefused) as refused:
            operation()
        assert refused.value.reason is RefusalReason.PUBLISHED_STORE
    assert r.world.read_requests() == 0 and r.store.thread_writes == []

    acknowledged = rig(posture=StorePosture(published=True, acknowledged=True))
    acknowledged.comment("hello", day=2)
    assert acknowledged.ingest().written


def test_a_report_without_a_thread_binding_or_of_another_project_is_refused_before_reads() -> None:
    r = rig()
    native = r.store.create(ReportDraft("born native", "bug", "origin:report", "dev/test"))
    foreign = r.store.create(
        ReportDraft(
            "elsewhere",
            "bug",
            "origin:github",
            "importer/a",
            binding=IssueBinding(
                "", "memory", "someone/else", "memory://someone/else/issues/1", "#1"
            ),
        )
    )
    cases = [
        ("acme-ghost", RefusalReason.NOT_FOUND),
        (native, RefusalReason.NOT_BOUND),
        (foreign, RefusalReason.OTHER_PROJECT),
    ]
    for report, reason in cases:
        with pytest.raises(IntakeRefused) as refused:
            r.service.ingest_thread(ThreadRequest(report, ACTOR))
        assert refused.value.reason is reason
    with pytest.raises(IntakeRefused) as refused:
        r.service.reconcile_presence(PresenceRequest((r.report, foreign), ACTOR))
    assert refused.value.reason is RefusalReason.OTHER_PROJECT
    assert r.world.read_requests() == 0


def test_a_held_binding_is_refused_as_busy_without_reading() -> None:
    r = rig()
    r.comment("hello", day=2)
    with r.lock.hold("memory:acme/widgets"):
        with pytest.raises(IntakeRefused) as refused:
            r.ingest()
    assert refused.value.reason is RefusalReason.INTAKE_BUSY
    assert r.world.read_requests() == 0
    assert r.ingest().written and r.lock.holds == ["memory:acme/widgets"] * 2


def test_a_document_moved_by_another_writer_is_refused_and_keeps_that_writers_version() -> None:
    """Another host (its own lock) commits between this pass's read and write: the store's seq
    re-check refuses this write, so neither writer's update is lost."""
    racing: list[Callable[[], None]] = []

    class Interleaved(_Delegate):
        def list_comments(self, query):
            page = self.inner.list_comments(query)
            if racing:
                racing.pop()()
            return page

    r = rig(wrap=Interleaved)
    r.comment("hello", day=2)
    other_host = ThreadService(r.store, r.tracker, InProcessIntakeLock(), posture=PRIVATE)
    racing.append(lambda: other_host.ingest_thread(ThreadRequest(r.report, "intake/other")))

    with pytest.raises(IntakeRefused) as refused:
        r.ingest()

    assert refused.value.reason is RefusalReason.THREAD_MOVED
    assert r.store.thread_writes == [(r.report, r.thread().seq)]  # only the other host's write
    assert not r.ingest().written  # and this host now sees it as already ingested


def test_a_malformed_stored_document_is_a_store_error_and_is_never_overwritten() -> None:
    r = rig()
    r.comment("hello", day=2)
    r.store.put_metadata(r.report, THREAD_KEY, {"schema": 1, "seq": "not a number"})

    with pytest.raises(IntakeStoreError) as failed:
        r.ingest()

    assert failed.value.operation == "read_thread"
    assert r.store.metadata(r.report)[THREAD_KEY] == {"schema": 1, "seq": "not a number"}


def test_a_body_cap_keeps_the_whole_bodys_digest_and_flags_the_record() -> None:
    capped, whole = rig(policy=ThreadPolicy(max_body_chars=5)), rig()
    for target in (capped, whole):
        target.comment("a rather long body", day=2)

    capped.ingest()
    whole.ingest()

    (short,) = capped.thread().records.values()
    (full,) = whole.thread().records.values()
    assert short.body == "a rat" and Flag.TRUNCATED.value in short.flags
    assert short.latest.digest == full.latest.digest and full.body == "a rather long body"


def test_considering_through_an_older_seq_keeps_later_arrivals_new() -> None:
    r = rig()
    first = r.comment("first", day=2)
    r.ingest()
    seen = r.service.thread_of(r.report)
    second = r.comment("second", day=3)
    r.ingest()

    outcome = r.service.mark_considered(ConsiderRequest(r.report, "triage/a", seen.seq))

    assert outcome.written
    assert r.record(first).considered_rev == 1 and r.record(second).considered_rev is None
    assert r.service.thread_of(r.report).new_evidence == (second.id,)
    again = r.service.mark_considered(ConsiderRequest(r.report, "triage/a", seen.seq))
    assert not again.written  # idempotent
    assert r.store.history(r.report) == (("intake", "untriaged", "imported"),)  # intake untouched


def test_reconcile_and_consider_on_a_report_never_ingested_do_nothing() -> None:
    r = rig()
    assert r.reconcile() == r.reconcile() and not r.reconcile().written
    assert not r.service.mark_considered(ConsiderRequest(r.report, "triage/a")).written
    outcome = r.service.reconcile_presence(PresenceRequest((r.report,), ACTOR))
    assert outcome.complete and not outcome.outcomes[0].written
    assert r.store.thread_writes == [] and r.service.thread_of(r.report) is None


# -- the consumer port -----------------------------------------------------------------------


class _Labels:
    def violations(self, labels):
        return ()


def test_the_port_delegates_to_the_thread_service_and_is_off_without_one() -> None:
    r = rig()
    r.comment("hello", day=2)
    off = IntakeService(r.store, _Labels())
    for call in (
        lambda port: port.ingest_thread(ThreadRequest(r.report, ACTOR)),
        lambda port: port.reconcile_thread(ThreadRequest(r.report, ACTOR)),
        lambda port: port.reconcile_presence(PresenceRequest((r.report,), ACTOR)),
        lambda port: port.mark_considered(ConsiderRequest(r.report, "triage/a")),
    ):
        for port in (off, NullIntake()):
            with pytest.raises(IntakeRefused) as refused:
                call(port)
            assert refused.value.reason is RefusalReason.INTAKE_DISABLED
    assert off.thread_of(r.report) is None and NullIntake().thread_of(r.report) is None
    assert r.world.read_requests() == 0

    on = IntakeService(r.store, _Labels(), threads=r.service)
    assert on.ingest_thread(ThreadRequest(r.report, ACTOR)).written
    view = on.thread_of(r.report)
    assert view is not None and len(view.thread.records) == 1 and view.seq == view.thread.seq
    assert on.mark_considered(ConsiderRequest(r.report, "triage/a")).written
    assert not on.reconcile_thread(ThreadRequest(r.report, ACTOR)).written
    assert on.reconcile_presence(PresenceRequest((r.report,), ACTOR)).complete
