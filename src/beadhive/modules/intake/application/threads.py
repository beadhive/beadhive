"""``ThreadService``: comment ingestion for bound reports (the ``bh-fptob`` model).

The pure rules live in :mod:`..domain.thread`; this layer is the only tracker-typed code of the
conversation model (resolution X2): it maps ``TrackerComment`` to :class:`Observation`, drives the
module's own pager and snapshot loop, and commits each step as ONE ``intake.thread`` write under
the binding's :class:`IntakeLock` with the store's ``seq`` re-check.

Every operation refuses before any tracker read when the operational store would be published
(:func:`require_private_store`), when the report is not a bound report, when the report is bound
to another project, or when the stored checkpoint belongs to another query scope. Nothing here
writes to the tracker: ingestion reads comments and ids only, so ``write_requests()`` stays 0.
"""

from __future__ import annotations

import dataclasses

from beadhive.modules.tracker.contracts import (
    Actor,
    Checkpoint,
    CommentQuery,
    IdQuery,
    IdSubject,
    IssueKey,
    PageCursor,
    TrackerAdapter,
    TrackerComment,
    TrackerError,
)

from ..contracts import IntakeLock, OutboxView, ReportStore
from ..domain import (
    CheckpointScope,
    ConsiderRequest,
    IntakeRefused,
    IssueBinding,
    Observation,
    OwnIdentity,
    OwnMark,
    PresenceOutcome,
    PresenceRequest,
    Recognition,
    RefusalReason,
    StorePosture,
    Thread,
    ThreadActor,
    ThreadEvent,
    ThreadIssue,
    ThreadOutcome,
    ThreadPolicy,
    ThreadRequest,
    ThreadView,
    advance_checkpoint,
    apply_observations,
    checkpoint_for,
    flagged,
    mark_considered,
    new_evidence,
    new_thread,
    reconcile_comments,
    reconcile_presence,
    require_private_store,
)
from .paging import collect_snapshot, iterate_pages

# -- mapping: the only place a tracker type meets a thread record ----------------------------


def thread_actor(actor: Actor | None) -> ThreadActor | None:
    if actor is None:
        return None
    return ThreadActor(actor.key.id, actor.key.kind.value, actor.display)


def observation(comment: TrackerComment) -> Observation:
    marker = comment.marker
    return Observation(
        id=comment.key.id,
        issue_url=comment.issue.url,
        body=comment.body,
        created_at=comment.created_at,
        updated_at=comment.revision.updated_at,
        global_id=comment.key.global_id,
        author=thread_actor(comment.author),
        via=comment.via.id if comment.via is not None else None,
        marker=None if marker is None else OwnMark(marker.hive, marker.install, marker.outbox_id),
    )


def issue_key(binding: IssueBinding) -> IssueKey:
    return IssueKey(binding.identifier, binding.url)


def binding_key(binding: IssueBinding) -> str:
    """The :class:`IntakeLock` key: one binding (``tracker`` + ``project``), per ADR section 4."""
    return f"{binding.tracker}:{binding.project}"


class _NoOutbox:
    """The outbox view before an outbox exists: no receipt, nothing pending or issued."""

    def receipt_ids(self) -> frozenset[str]:
        return frozenset()

    def pending(self, outbox_id: str) -> bool:
        return False

    def issued(self, outbox_id: str) -> bool:
        return False

    def known_actors(self) -> frozenset[ThreadActor]:
        return frozenset()


class ThreadService:
    """The conversation operations of :class:`IntakePort` over one store and one tracker.

    ``posture`` is required: the composition root states whether the operational store would be
    published, so an unwired service fails closed. ``identity`` is this hive's marker identity
    (None: no self marker can match, so nothing is ``own`` except by receipt or integration).
    ``integrations`` are configured integration ids for R7 row 3 (empty until App support)."""

    def __init__(
        self,
        store: ReportStore,
        tracker: TrackerAdapter,
        lock: IntakeLock,
        *,
        posture: StorePosture,
        identity: OwnIdentity | None = None,
        outbox: OutboxView | None = None,
        policy: ThreadPolicy | None = None,
        integrations: frozenset[str] = frozenset(),
    ) -> None:
        self._store = store
        self._tracker = tracker
        self._lock = lock
        self._posture = posture
        self._identity = identity
        self._outbox: OutboxView = outbox if outbox is not None else _NoOutbox()
        self._policy = policy or ThreadPolicy()
        self._integrations = frozenset(integrations)

    # -- reads -------------------------------------------------------------------------------

    def thread_of(self, report: str) -> ThreadView | None:
        """The stored thread with its derived reads (no tracker request, no lock)."""
        thread = self._store.read_thread(report)
        if thread is None:
            return None
        return ThreadView(report, thread, new_evidence(thread), flagged(thread))

    # -- ingest (R5) -------------------------------------------------------------------------

    def ingest_thread(self, request: ThreadRequest) -> ThreadOutcome:
        """Read the report's comments since its checkpoint and commit them, with the advanced
        checkpoint, in ONE document write (none at all when nothing changed)."""
        binding = self._preflight(request.report)
        with self._lock.hold(binding_key(binding)):
            thread = self._load(request.report, binding)
            expect = thread.seq
            since = checkpoint_for(thread, CheckpointScope.ISSUE, binding.url)  # before any read
            ctx = self._recognition()
            query = CommentQuery(
                since=Checkpoint(since) if since is not None else None, issue=issue_key(binding)
            )
            events: list[ThreadEvent] = []
            token: str | None = None
            complete, detail = True, ""

            def fetch(cursor: PageCursor | None):
                return self._tracker.list_comments(dataclasses.replace(query, cursor=cursor))

            try:
                for page in iterate_pages(fetch):
                    applied = apply_observations(
                        thread, (observation(c) for c in page.items), ctx, self._policy
                    )
                    thread, events = applied.thread, [*events, *applied.events]
                    if page.checkpoint is not None:
                        token = page.checkpoint.token  # covers this page and every earlier one
            except TrackerError as exc:
                complete, detail = False, str(exc)
            advanced = advance_checkpoint(thread, CheckpointScope.ISSUE, binding.url, token)
            thread = advanced.thread
            written = self._commit(request.report, thread, expect, request.actor)
        return ThreadOutcome(
            request.report,
            thread.seq,
            written=written,
            complete=complete,
            checkpoint_advanced=advanced.changed,
            events=(*events, *advanced.events),
            detail=detail,
        )

    # -- deletions (R6) ----------------------------------------------------------------------

    def reconcile_thread(self, request: ThreadRequest) -> ThreadOutcome:
        """Infer deletions from ONE complete comment-id snapshot. The snapshot is collected
        outside the lock; a record ingested after it began is never a deletion candidate."""
        binding = self._preflight(request.report)
        started = self._store.read_thread(request.report)
        if started is None:  # nothing ingested yet: nothing can have been deleted
            return ThreadOutcome(request.report, 0)
        snapshot = collect_snapshot(
            self._tracker, IdQuery(IdSubject.COMMENTS, issue=issue_key(binding))
        )
        if not snapshot.complete:
            detail = "the comment id listing failed part-way; no deletion is inferred"
            return ThreadOutcome(request.report, started.seq, complete=False, detail=detail)
        with self._lock.hold(binding_key(binding)):
            thread = self._load(request.report, binding)
            expect = thread.seq
            applied = reconcile_comments(
                thread,
                snapshot.ids,
                complete=True,
                snapshot_start_seq=started.seq,
                policy=self._policy,
            )
            written = self._commit(request.report, applied.thread, expect, request.actor)
        return ThreadOutcome(
            request.report, applied.thread.seq, written=written, events=applied.events
        )

    def reconcile_presence(self, request: PresenceRequest) -> PresenceOutcome:
        """One complete issue-id snapshot applied to each named report's thread presence."""
        require_private_store(self._posture)
        bindings = [(report, self._bound_here(report)) for report in request.reports]
        snapshot = collect_snapshot(self._tracker, IdQuery(IdSubject.ISSUES))
        if not snapshot.complete:
            return PresenceOutcome(complete=False)
        outcomes: list[ThreadOutcome] = []
        for report, binding in bindings:
            with self._lock.hold(binding_key(binding)):
                thread = self._store.read_thread(report)
                if thread is None:
                    outcomes.append(ThreadOutcome(report, 0))
                    continue
                expect = thread.seq
                applied = reconcile_presence(
                    thread, snapshot.ids, complete=True, policy=self._policy
                )
                written = self._commit(report, applied.thread, expect, request.actor)
            outcomes.append(
                ThreadOutcome(report, applied.thread.seq, written=written, events=applied.events)
            )
        return PresenceOutcome(complete=True, outcomes=tuple(outcomes))

    # -- the triage watermark ----------------------------------------------------------------

    def mark_considered(self, request: ConsiderRequest) -> ThreadOutcome:
        """Record what a triage pass read (no tracker request). Never touches ``intake``."""
        require_private_store(self._posture)
        binding = self._binding(request.report)
        with self._lock.hold(binding_key(binding)):
            thread = self._store.read_thread(request.report)
            if thread is None:
                return ThreadOutcome(request.report, 0)
            expect = thread.seq
            applied = mark_considered(thread, request.through_seq)
            written = self._commit(request.report, applied.thread, expect, request.actor)
        return ThreadOutcome(
            request.report, applied.thread.seq, written=written, events=applied.events
        )

    # -- helpers -----------------------------------------------------------------------------

    def _binding(self, report: str) -> IssueBinding:
        view = self._store.get(report)
        if view is None:
            raise IntakeRefused(RefusalReason.NOT_FOUND, f"{report} not found")
        if view.binding is None:
            raise IntakeRefused(
                RefusalReason.NOT_BOUND,
                f"{report} is not bound to an external issue, so it has no thread",
            )
        return view.binding

    def _preflight(self, report: str) -> IssueBinding:
        """Every refusal that must precede a tracker read, in order."""
        require_private_store(self._posture)
        return self._bound_here(report)

    def _bound_here(self, report: str) -> IssueBinding:
        """The report's binding, refused unless it names the bound tracker project (a thread
        of another project would read as deleted against this project's listings)."""
        binding = self._binding(report)
        project = self._tracker.describe().project
        if (binding.tracker, binding.project) != (project.tracker, project.locator):
            raise IntakeRefused(
                RefusalReason.OTHER_PROJECT,
                f"{report} is bound to {binding.tracker}:{binding.project}, not to the "
                f"bound tracker project {project.tracker}:{project.locator}",
            )
        return binding

    def _load(self, report: str, binding: IssueBinding) -> Thread:
        stored = self._store.read_thread(report)
        if stored is not None:
            return stored
        issue = ThreadIssue(binding.identifier, binding.url)
        return new_thread(binding.tracker, binding.project, issue)

    def _recognition(self) -> Recognition:
        me = thread_actor(self._tracker.whoami())
        known = self._outbox.known_actors() | ({me} if me is not None else frozenset())
        return Recognition(
            identity=self._identity,
            known_actors=frozenset(known),
            receipt_ids=self._outbox.receipt_ids(),
            pending=self._outbox.pending,
            issued=self._outbox.issued,
            integrations=self._integrations,
        )

    def _commit(self, report: str, thread: Thread, expect: int, actor: str) -> bool:
        """One durable write, or none when nothing changed."""
        if thread.seq == expect:
            return False
        self._store.write_thread(report, thread, expect_seq=expect, actor=actor)
        return True


__all__ = ["ThreadService", "binding_key", "issue_key", "observation", "thread_actor"]
