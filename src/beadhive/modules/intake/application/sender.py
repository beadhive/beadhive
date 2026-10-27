"""``OutboxService``: the supervised sender for ``comment.create`` (``bh-6wj9y`` R6, R7; D2).

The pure O1 to O19 machine lives in :mod:`..domain.outbox` and the translation to and from the
tracker port in :mod:`.outbox`; this layer runs the machine against a :class:`ReportStore` and a
tracker in R6's order, and is the only code that ever calls a writer:

* **Off by default.** Every operation refuses with ``OUTBOX_DISABLED`` unless the injected
  :class:`OutboxPolicy` is enabled. Even then nothing is posted without the adapter's own
  per-operation enable (``probe_capabilities`` reports ``comment.create`` usable, else O5
  refuses; a disabled writer refuses before any request, which is O11) AND an operator approval
  that minted a single-use grant (O5). No timer, flag, config key or agent seat can approve.
* **One durable write per step**, each under the binding's :class:`IntakeLock` and with the
  store's ``seq`` re-check. On every lock acquisition a ``sending`` operation is recovered to
  ``unknown`` first (O13): this holder has the lock its writer held, so that writer is gone.
* **Write-ahead, then exactly one request.** O9 (the attempt, the consumed grant) is persisted
  BEFORE ``post_comment``; the grant is handed to the adapter once. A proof of no write is O11, any
  other exception O12; nothing here retries.
* **Reconcile** reads one complete comment listing per call, begun after the attempt ended (its
  ``start_seq`` is the document ``seq`` read before the listing), outside the lock, and applies
  it under the lock: the own marker from a known actor is O14 (or O19 late), a complete listing
  without it one absence (O15), and k absences (``OutboxPolicy.absent_confirmations``, default
  2) ``failed(absent)`` (O16). An ambiguous timeout therefore never causes a second post until
  two complete listings have shown nothing was written and an operator has retried (O17) and
  approved again.

The render is the dry run: no tracker request and no store write. The marker line is encoded by
the injected ``marker_line`` (the adapter's own codec), so the bytes shown are the bytes posted.
``labels.change`` is the next bead's (I9b): approving or sending it refuses ``NOT_AVAILABLE``.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Iterable

from beadhive.modules.tracker.contracts import (
    CommentQuery,
    CommentWriter,
    OwnMarker,
    PageCursor,
    TrackerAdapter,
    TrackerComment,
    TrackerError,
)

from ..contracts import IdSource, IntakeLock, ReportStore, Sanitizer
from ..domain import (
    NOTIFIED_DIM,
    ApprovalRequest,
    DraftRequest,
    IntakeRefused,
    IntakeStoreError,
    IssueBinding,
    LabelBase,
    LeakPolicy,
    Message,
    OpKind,
    OpState,
    OutboxApplied,
    OutboxApprove,
    OutboxDocument,
    OutboxEntry,
    OutboxEvent,
    OutboxListing,
    OutboxObservation,
    OutboxPolicy,
    OutboxQuery,
    OutboxRecord,
    OutboxResult,
    OwnIdentity,
    RefusalReason,
    RenderContext,
    RenderedRecord,
    ReplyDraft,
    StorePosture,
    ThreadActor,
    ThreadState,
    is_outbox_id,
    outbox_view,
    reply_revision,
    require_private_store,
)
from ..domain import outbox as machine
from . import outbox as translate
from .paging import iterate_pages
from .threads import binding_key, thread_actor
from .threads import issue_key as binding_issue

#: The states a reconcile pass can still move: the ambiguous ones, and the retired ones a late
#: marker would bring back (O19).
_RECONCILABLE = frozenset({OpState.SENDING, OpState.UNKNOWN})


def _needs_reconcile(record: OutboxRecord) -> bool:
    for op in record.operations:
        if op.state in _RECONCILABLE:
            return True
        if op.state is OpState.ABANDONED and op.outcome_unknown:
            return True
        if op.state is OpState.FAILED and op.fail_reason == machine.ABSENT:
            return True
    return False


def _disabled() -> IntakeRefused:
    return IntakeRefused(
        RefusalReason.OUTBOX_DISABLED,
        "the outbox is off for this hive (intake.external.outbox.enabled; opt-in)",
    )


def _labels_later(kind: OpKind) -> IntakeRefused:
    return IntakeRefused(
        RefusalReason.NOT_AVAILABLE,
        f"{kind.value} is not sendable in this build; only comment.create is (labels: I9b)",
    )


class OutboxService:
    """The outbox operations of :class:`IntakePort` over one store and one tracker.

    ``marker_line`` encodes an :class:`OwnMarker` exactly as the bound adapter appends it, so a
    render shows the posted bytes; ``holder`` names this sender in each attempt (host, process);
    ``posture`` is required, as for thread ingestion, so an unwired service fails closed."""

    def __init__(
        self,
        store: ReportStore,
        tracker: TrackerAdapter,
        lock: IntakeLock,
        *,
        ids: IdSource,
        identity: OwnIdentity,
        marker_line: Callable[[OwnMarker], str],
        policy: OutboxPolicy,
        leak_policy: LeakPolicy,
        sanitizer: Sanitizer | None,
        posture: StorePosture,
        holder: str = "intake",
    ) -> None:
        self._store = store
        self._tracker = tracker
        self._lock = lock
        self._ids = ids
        self._identity = identity
        self._marker_line = marker_line
        self._policy = policy
        self._leaks = leak_policy
        self._sanitizer = sanitizer
        self._posture = posture
        self._holder = holder

    # -- reads -------------------------------------------------------------------------------

    def outbox(self, query: OutboxQuery) -> OutboxListing:
        """The records, oldest first per report (no tracker request, no lock, no write)."""
        self._require_enabled()
        reports = (
            [query.report] if query.report else [v.id for v in self._store.reports_with_outbox()]
        )
        entries: list[OutboxEntry] = []
        for report in reports:
            doc = self._store.read_outbox(report)
            if doc is None:
                continue
            for record in sorted(doc.records.values(), key=lambda r: r.created_seq):
                if query.open_only and all(op.terminal for op in record.operations):
                    continue
                entries.append(OutboxEntry(report, record))
        return OutboxListing(tuple(entries))

    def render(self, outbox_id: str) -> RenderedRecord:
        """The dry run (``bh work outbox show``): every operation exactly as it would be sent,
        with its findings and digest. No tracker request and no store write."""
        self._require_enabled()
        report, doc = self._locate(outbox_id)
        binding = self._binding(report)
        record = doc.records[outbox_id]
        ctx = self._ctx(report, binding, outbox_id)
        renders = tuple(machine.render(record, op.kind, ctx) for op in record.operations)
        return RenderedRecord(report, record, renders)

    # -- O1: a reply -------------------------------------------------------------------------

    def draft_reply(self, request: ReplyDraft) -> OutboxResult:
        """O1 for an operator reply (O2 when the same text already answers the same thread seq),
        checked at once (O3 holds a leak). Drafting never sends and needs no grant."""
        self._require_enabled()
        require_private_store(self._posture)
        binding = self._binding(request.report)
        with self._lock.hold(binding_key(binding)) as epoch:
            doc, expect, rules = self._open(request.report, epoch, request.actor)
            thread = self._store.read_thread(request.report)
            outbox_id = self._ids.outbox_id()
            draft = DraftRequest(
                binding,
                OutboxEvent.REPLY,
                reply_revision(request.text, thread.seq if thread is not None else 0),
                request.actor,
                message=Message("reply", request.text),
            )
            applied = machine.draft(
                doc,
                draft,
                outbox_id=outbox_id,
                policy=self._policy,
                ctx=self._ctx(request.report, binding, outbox_id),
            )
            self._commit(request.report, applied, expect, request.actor)
        return self._result(request.report, applied, rules)

    # -- O5: the gate ------------------------------------------------------------------------

    def approve(self, request: OutboxApprove) -> OutboxResult:
        """O5: a human operator approves the digest of a fresh dry run, which mints exactly one
        single-use grant (O4 first for each named ``publish``, in the same write). The result
        carries the dry run and the grant id. Refuses unless the tracker reports the write and
        ``comments.read`` usable, so an unknown outcome can always be reconciled."""
        self._require_enabled()
        if request.kind is not OpKind.COMMENT:
            raise _labels_later(request.kind)
        require_private_store(self._posture)
        report, _ = self._locate(request.outbox_id)
        binding = self._bound_here(report)
        capabilities = translate.capabilities(self._tracker.probe_capabilities())
        with self._lock.hold(binding_key(binding)) as epoch:
            doc, expect, rules = self._open(report, epoch, request.actor)
            ctx = self._ctx(report, binding, request.outbox_id)
            steps: list[str] = []
            if request.publish:
                published = machine.override(
                    doc,
                    request.outbox_id,
                    request.kind,
                    request.publish,
                    actor=request.actor,
                    policy=self._policy,
                    ctx=ctx,
                )
                doc, steps = published.document, [s.rule for s in published.steps]
            applied = machine.approve(
                doc,
                ApprovalRequest(request.outbox_id, request.kind, request.digest, request.actor),
                grant_id=self._ids.grant_id(),
                policy=self._policy,
                ctx=ctx,
                capabilities=capabilities,
            )
            self._commit(report, applied, expect, request.actor)
            record = applied.document.records[request.outbox_id]
            rendered = machine.render(record, request.kind, ctx)
        approval = record.op(request.kind).approval  # type: ignore[union-attr]
        result = self._result(report, applied, (*rules, *steps))
        return dataclasses.replace(
            result,
            rendered=rendered,
            grant_id=approval.grant_id if approval is not None else "",
            detail=f"approved {rendered.short_digest}; `send` posts it once",
        )

    # -- O9 to O13: send ---------------------------------------------------------------------

    def send(self, outbox_id: str, *, actor: str) -> OutboxResult:
        """O9 persisted, then exactly one ``post_comment``, then O10, O11 or O12, all under one
        hold of the binding's lock. An approval whose render moved is invalidated instead (O7)
        and nothing is sent."""
        self._require_enabled()
        require_private_store(self._posture)
        report, _ = self._locate(outbox_id)
        binding = self._bound_here(report)
        writer = self._tracker
        if not isinstance(writer, CommentWriter):
            raise IntakeRefused(
                RefusalReason.CAPABILITY_UNUSABLE, "the bound tracker adapter has no comment writer"
            )
        with self._lock.hold(binding_key(binding)) as epoch:
            doc, expect, rules = self._open(report, epoch, actor)
            record = self._record(doc, outbox_id)
            op = record.op(OpKind.COMMENT)
            if op is None:
                raise _labels_later(OpKind.LABELS)
            ctx = self._ctx(report, binding, outbox_id)
            if op.state is OpState.APPROVED:
                moved = machine.invalidate(doc, outbox_id, OpKind.COMMENT, ctx=ctx, actor=actor)
                if moved.changed:  # O7: never send bytes the operator did not approve
                    self._commit(report, moved, expect, actor)
                    result = self._result(report, moved, rules)
                    return dataclasses.replace(
                        result, detail="the render moved since it was approved; approve it again"
                    )
            ahead = machine.begin_send(
                doc,
                outbox_id,
                OpKind.COMMENT,
                actor=actor,
                policy=self._policy,
                ctx=ctx,
                epoch=epoch,
                holder=self._holder,
            )
            doc = self._commit(report, ahead, expect, actor)  # the write-ahead, durable first
            plan = ahead.plan
            assert plan is not None  # begin_send always plans its one write
            record = doc.records[outbox_id]
            try:
                answer = writer.post_comment(
                    translate.issue_key(record),
                    plan.body,
                    marker=translate.own_marker(self._identity, outbox_id),
                    grant=translate.plan_grant(record, plan),
                )
            except Exception as exc:  # noqa: BLE001 - the classification is the contract
                if translate.send_outcome(exc) is translate.SendOutcome.REFUSED:
                    name = translate.refusal_name(exc)
                    outcome = machine.record_refusal(
                        doc, outbox_id, OpKind.COMMENT, name, actor=actor
                    )
                else:
                    outcome = machine.record_unknown(
                        doc, outbox_id, OpKind.COMMENT, actor=actor, detail=type(exc).__name__
                    )
            else:
                outcome = machine.record_receipt(
                    doc, outbox_id, OpKind.COMMENT, translate.receipt(answer), actor=actor
                )
            self._commit(report, outcome, ahead.document.seq, actor)
            notified, detail = self._notify(report, outbox_id, outcome, actor)
        result = self._result(report, outcome, (*rules, *(s.rule for s in ahead.steps)))
        return dataclasses.replace(result, notified=notified, detail=detail or result.detail)

    # -- O13 to O16, O19: reconcile ----------------------------------------------------------

    def reconcile(self, outbox_id: str | None = None, *, actor: str) -> tuple[OutboxResult, ...]:
        """One reconcile pass: for ``outbox_id``, or for every record that is ambiguous or could
        still turn up late. Tracker-read-only; one comment listing per report."""
        self._require_enabled()
        require_private_store(self._posture)
        targets: dict[str, list[str]] = {}
        if outbox_id is not None:
            report, _ = self._locate(outbox_id)
            targets[report] = [outbox_id]
        else:
            for view in self._store.reports_with_outbox():
                doc = self._store.read_outbox(view.id)
                wanted = [
                    r.outbox_id
                    for r in sorted((doc.records if doc else {}).values(), key=_created)
                    if _needs_reconcile(r)
                ]
                if wanted:
                    targets[view.id] = wanted
        results: list[OutboxResult] = []
        for report, outbox_ids in targets.items():
            results.extend(self._reconcile_report(report, outbox_ids, actor))
        return tuple(results)

    def _reconcile_report(
        self, report: str, outbox_ids: list[str], actor: str
    ) -> list[OutboxResult]:
        binding = self._bound_here(report)
        key = binding_key(binding)
        with self._lock.hold(key) as epoch:  # O13 first, so every attempt has ended
            doc, _, recovered = self._open(report, epoch, actor)
        start_seq = doc.seq  # read before the listing begins (ordered by seq, never by time)
        comments, complete = self._listing(binding)
        results: list[OutboxResult] = []
        with self._lock.hold(key) as epoch:
            doc, expect, again = self._open(report, epoch, actor)
            ledger = outbox_view(doc)
            known = set(ledger.known_actors())
            me = thread_actor(self._tracker.whoami())
            if me is not None:
                known.add(me)
            marked = translate.marked_comments(comments, self._identity, frozenset(known))
            observation = OutboxObservation(start_seq, complete, marked)
            applied: list[OutboxApplied] = []
            for outbox_id in outbox_ids:
                step = machine.observe(
                    doc, outbox_id, observation, policy=self._policy, actor=actor
                )
                doc = step.document
                applied.append(step)
            if doc.seq != expect:
                self._store.write_outbox(report, doc, expect_seq=expect, actor=actor)
            for step in applied:
                notified, detail = self._notify(report, step.outbox_id, step, actor)
                result = self._result(report, step, (*recovered, *again))
                results.append(
                    dataclasses.replace(
                        result,
                        complete=complete,
                        notified=notified,
                        detail=detail or self._reconcile_detail(step, complete),
                    )
                )
        return results

    # -- O8, O18, O17 ------------------------------------------------------------------------

    def discard(
        self, outbox_id: str, *, reason: str, actor: str, accept_unknown: bool = False
    ) -> OutboxResult:
        """O8 (any grant voided), or O18 for an ``unknown`` with ``accept_unknown``."""
        self._require_enabled()
        require_private_store(self._posture)
        report, _ = self._locate(outbox_id)
        binding = self._binding(report)
        with self._lock.hold(binding_key(binding)) as epoch:
            doc, expect, rules = self._open(report, epoch, actor)
            applied = machine.discard(
                doc,
                outbox_id,
                reason=reason,
                actor=actor,
                policy=self._policy,
                accept_unknown=accept_unknown,
            )
            self._commit(report, applied, expect, actor)
        return self._result(report, applied, rules)

    def retry(self, outbox_id: str, *, actor: str) -> OutboxResult:
        """O17: a ``failed`` comment back to ``drafted``; sending again needs a new approval."""
        self._require_enabled()
        require_private_store(self._posture)
        report, _ = self._locate(outbox_id)
        binding = self._binding(report)
        with self._lock.hold(binding_key(binding)) as epoch:
            doc, expect, rules = self._open(report, epoch, actor)
            applied = machine.retry(
                doc, outbox_id, OpKind.COMMENT, actor=actor, policy=self._policy
            )
            self._commit(report, applied, expect, actor)
        result = self._result(report, applied, rules)
        return dataclasses.replace(result, detail="drafted again; render, approve and send anew")

    # -- helpers -----------------------------------------------------------------------------

    def _require_enabled(self) -> None:
        if not self._policy.enabled:
            raise _disabled()

    def _locate(self, outbox_id: str) -> tuple[str, OutboxDocument]:
        """The report holding ``outbox_id`` and its document. An id is opaque (``ob1_`` plus 26
        base32 characters), never a bead id."""
        if not is_outbox_id(outbox_id):
            raise IntakeRefused(
                RefusalReason.INVALID_OUTBOX_ID, f"{outbox_id!r} is not an outbox id (ob1_...)"
            )
        for view in self._store.reports_with_outbox():
            doc = self._store.read_outbox(view.id)
            if doc is not None and outbox_id in doc.records:
                return view.id, doc
        raise IntakeRefused(RefusalReason.UNKNOWN_OUTBOX, f"no outbox record {outbox_id}")

    @staticmethod
    def _record(doc: OutboxDocument, outbox_id: str) -> OutboxRecord:
        record = doc.records.get(outbox_id)
        if record is None:  # moved off this report since it was located: nothing to act on
            raise IntakeRefused(RefusalReason.UNKNOWN_OUTBOX, f"no outbox record {outbox_id}")
        return record

    def _binding(self, report: str) -> IssueBinding:
        view = self._store.get(report)
        if view is None:
            raise IntakeRefused(RefusalReason.NOT_FOUND, f"{report} not found")
        if view.binding is None:
            raise IntakeRefused(
                RefusalReason.NOT_BOUND,
                f"{report} is not bound to an external issue, so it has nowhere to post",
            )
        return view.binding

    def _bound_here(self, report: str) -> IssueBinding:
        """The binding, refused unless it names the bound tracker project (no request)."""
        binding = self._binding(report)
        project = self._tracker.describe().project
        if (binding.tracker, binding.project) != (project.tracker, project.locator):
            raise IntakeRefused(
                RefusalReason.OTHER_PROJECT,
                f"{report} is bound to {binding.tracker}:{binding.project}, not to the "
                f"bound tracker project {project.tracker}:{project.locator}",
            )
        return binding

    def _ctx(self, report: str, binding: IssueBinding, outbox_id: str) -> RenderContext:
        thread = self._store.read_thread(report)
        view = self._store.get(report)
        snapshot = view.snapshot if view is not None else None
        labels = (
            LabelBase(snapshot.revision, frozenset(snapshot.labels))
            if snapshot is not None
            else None
        )
        marker = translate.own_marker(self._identity, outbox_id)
        return RenderContext(
            binding.url,
            self._leaks,
            self._sanitizer,
            marker_line=self._marker_line(marker),
            labels=labels,
            thread_missing=thread is not None and thread.state is ThreadState.MISSING,
        )

    def _open(
        self, report: str, epoch: int, actor: str
    ) -> tuple[OutboxDocument, int, tuple[str, ...]]:
        """Under the lock: the stored document (empty when there is none), with every ``sending``
        operation recovered to ``unknown`` first (O13) and committed. Returns the document, the
        ``seq`` the next write must expect, and the rules applied."""
        doc = self._store.read_outbox(report) or OutboxDocument()
        recovered = machine.recover(doc, live_epoch=epoch, holder_gone=True, actor=actor)
        if not recovered.changed:
            return doc, doc.seq, ()
        self._store.write_outbox(report, recovered.document, expect_seq=doc.seq, actor=actor)
        return recovered.document, recovered.document.seq, tuple(s.rule for s in recovered.steps)

    def _commit(
        self, report: str, applied: OutboxApplied, expect: int, actor: str
    ) -> OutboxDocument:
        if applied.changed:
            self._store.write_outbox(report, applied.document, expect_seq=expect, actor=actor)
        return applied.document

    def _notify(
        self, report: str, outbox_id: str, applied: OutboxApplied, actor: str
    ) -> tuple[str, str]:
        """``notified=<event>`` once a comment reached ``sent`` (the sender is its only writer).
        A failure here never un-sends anything: it is reported, and the record says ``sent``."""
        value = applied.notified
        if not value:
            return "", ""
        try:
            self._store.set_state(
                report, NOTIFIED_DIM, value, reason=f"outbox {outbox_id} sent", actor=actor
            )
        except IntakeStoreError as exc:
            return "", f"sent, but notified={value} could not be recorded: {exc.detail}"
        return value, ""

    def _listing(self, binding: IssueBinding) -> tuple[tuple[TrackerComment, ...], bool]:
        """Every comment of the issue; ``complete=False`` on any port error part-way."""
        query = CommentQuery(issue=binding_issue(binding))
        comments: list[TrackerComment] = []

        def fetch(cursor: PageCursor | None):
            return self._tracker.list_comments(dataclasses.replace(query, cursor=cursor))

        try:
            for page in iterate_pages(fetch):
                comments.extend(page.items)
        except TrackerError:
            return tuple(comments), False
        return tuple(comments), True

    def _reconcile_detail(self, applied: OutboxApplied, complete: bool) -> str:
        if not complete:
            return "the comment listing failed part-way; nothing was inferred"
        record = applied.record
        op = record.op(OpKind.COMMENT) if record is not None else None
        if op is None:
            return ""
        if op.state is OpState.UNKNOWN:
            return (
                f"not found in {op.absent_confirmations} of the "
                f"{self._policy.absent_confirmations} complete listings needed; reconcile again"
            )
        if op.state is OpState.FAILED and op.fail_reason == machine.ABSENT:
            return "confirmed absent: nothing was posted; `retry`, then approve and send anew"
        if op.duplicate_observed:
            return f"duplicate observed: {', '.join(op.duplicates)} (never removed automatically)"
        return ""

    @staticmethod
    def _result(report: str, applied: OutboxApplied, rules: Iterable[str]) -> OutboxResult:
        record = applied.record
        op = record.op(OpKind.COMMENT) if record is not None else None
        receipt = op.receipt if op is not None else None
        return OutboxResult(
            report,
            applied.outbox_id,
            OpKind.COMMENT,
            op.state if op is not None else None,
            (*rules, *(s.rule for s in applied.steps)),
            comment_id=(receipt.comment_id or "") if receipt is not None else "",
        )


def _created(record: OutboxRecord) -> int:
    return record.created_seq


class StoreOutboxView:
    """The outbox read own-message recognition needs (``bh-fptob`` R7, with ``bh-6wj9y``'s
    ``issued``), over every ``intake.outbox`` document in the store.

    Read once, on first use, and kept for the life of the view: the composition root builds one
    per command, so an ingest classifies against the outbox as it stood when the command began.
    ``pending`` covers ``sending`` and ``unknown``; ``issued`` every operation that ever had an
    attempt, ``abandoned`` and ``failed`` ones included (D-d), so a late arrival is ``own``."""

    def __init__(self, store: ReportStore) -> None:
        self._store = store
        self._ledgers: tuple | None = None

    def _all(self) -> tuple:
        if self._ledgers is None:
            docs = (self._store.read_outbox(v.id) for v in self._store.reports_with_outbox())
            self._ledgers = tuple(outbox_view(doc) for doc in docs if doc is not None)
        return self._ledgers

    def receipt_ids(self) -> frozenset[str]:
        return frozenset().union(*(ledger.receipt_ids() for ledger in self._all()))

    def pending(self, outbox_id: str) -> bool:
        return any(ledger.pending(outbox_id) for ledger in self._all())

    def issued(self, outbox_id: str) -> bool:
        return any(ledger.issued(outbox_id) for ledger in self._all())

    def known_actors(self) -> frozenset[ThreadActor]:
        return frozenset().union(*(ledger.known_actors() for ledger in self._all()))


__all__ = ["OutboxService", "StoreOutboxView"]
