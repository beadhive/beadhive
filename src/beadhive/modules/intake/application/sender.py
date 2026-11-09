"""``OutboxService``: the supervised sender for ``comment.create``, ``labels.change``,
``issue.close``, ``issue.reopen``, ``issue.type`` and ``issue.fields`` (``bh-6wj9y`` R6, R7; D2;
``bh-pr5ll``; ``bh-poqy0``; ``bh-ih484``).

The pure O1 to O19 machine lives in :mod:`..domain.outbox` and the translation to and from the
tracker port in :mod:`.outbox`; this layer runs the machine against a :class:`ReportStore` and a
tracker in R6's order, and is the only code that ever calls a writer:

* **Off by default, twice per operation.** Every operation refuses with ``OUTBOX_DISABLED``
  unless the injected :class:`OutboxPolicy` is enabled, and a ``labels.change`` additionally
  refuses with ``LABELS_DISABLED`` unless ``OutboxPolicy.labels_enabled`` (its own flag,
  separate from the comment's), and an ``issue.close`` with ``CLOSE_DISABLED`` unless
  ``OutboxPolicy.close_enabled`` (set from the bound adapter's own ``issue.close`` enable), and
  an ``issue.type`` / ``issue.fields`` with ``TYPE_DISABLED`` / ``FIELDS_DISABLED`` unless
  ``OutboxPolicy.type_enabled`` / ``fields_enabled`` and an ``issue.reopen`` with
  ``REOPEN_DISABLED`` unless ``OutboxPolicy.reopen_enabled`` (likewise the adapter's own
  enables). Even
  then nothing is written without the adapter's own
  per-operation enable (``probe_capabilities`` reports the operation usable, else O5 refuses; a
  disabled writer refuses before any request, which is O11) AND an operator approval of THAT
  operation that minted its own single-use grant (O5); approving the comment never approves the
  labels. No timer, flag, config key or agent seat can approve.
* **One durable write per step**, each under the binding's :class:`IntakeLock` and with the
  store's ``seq`` re-check. On every lock acquisition a ``sending`` operation is recovered to
  ``unknown`` first (O13): this holder has the lock its writer held, so that writer is gone.
* **Write-ahead, then exactly one request.** O9 (the attempt, the consumed grant) is persisted
  BEFORE ``post_comment`` / ``change_labels`` / ``close_issue``; the grant is handed to the
  adapter once. A proof of no write is O11, any other exception O12; nothing here retries.
* **A close goes last.** A reply drafted with ``ReplyDraft.close`` carries an ``issue.close``
  operation, and O9 refuses it (``close_not_ready``, before any request) until that record's
  comment is ``sent``, no other comment of the report is unsent and the report's latest labels
  record is ``sent``: per report, comment, labels, close. :meth:`OutboxService.backfill_closes`
  drafts the close a report closed before this existed never got, on its latest reply (or on the
  reply it drafts when the report never got one).
* **A reopen goes first.** An ``issue.reopen`` is the operator's explicit correction of a close
  (:meth:`OutboxService.reopen`, ``bh work outbox reopen``: no trigger drafts one). It rides on the
  reply that explains it (``ReplyDraft.reopen``), and O9 refuses that reply's comment
  (``reopen_not_ready``, before any request) until the reopen is sent or retired; the reopen itself
  is refused while another record of the issue still holds an unsent close, and never drafted
  while another reopen of the issue is pending. Discarding the reopen retires its reply with it,
  and so does the report closing afterwards (:meth:`OutboxService.retire_reopens`). Per report:
  reopen, comment, labels, close, an order only ``send-approved`` guarantees across separate
  records. It reconciles by reading the issue's state back.
* **Only owned labels.** A label operation is refused unless every label it adds or removes lies
  under the root's owned prefixes (``LeakPolicy.owned_label_prefixes``, the adapter's
  ``write.labels.owned_prefixes``): at draft, at the leak check (``label_not_owned``, never
  overridable), at approve and at send before the write-ahead, and the same prefixes travel as
  the ``LabelOwnership`` the writer checks again before any request.
* **Reconcile** reads, per call and outside the lock, one complete comment listing (for a
  comment) and one read of the issue (its labels for a label operation, its state for a close;
  neither carries a marker),
  each begun after the attempt ended (its ``start_seq`` is the document ``seq`` read before the
  read), and applies it under the lock: the own marker from a known actor, or labels that
  already satisfy the diff, is O14 (or O19 late); a complete read without it is one absence
  (O15), and k absences (``OutboxPolicy.absent_confirmations``, default 2) ``failed(absent)``
  (O16). An ambiguous timeout therefore never causes a second write until two complete reads have
  shown nothing was written and an operator has retried (O17) and approved again.

**Label operations read the issue's labels fresh** (``bh-uim9i``): a render, an approval and a
send of a ``labels.change`` read the issue's labels now (one ``IssueResolver.get``, else a paged
listing) instead of trusting the stored snapshot, which only an import refreshes; when that read
cannot be made the snapshot is the fallback. The batch (:meth:`OutboxService.review_label_batch`,
:meth:`OutboxService.approve_label_batch`) renders every drafted labels operation from ONE issue
listing, ONE pass over the outbox documents and (to approve) ONE capability probe, and approving
its digest mints one single-use grant per item; an item whose owned labels moved since its draft is
skipped and reported for re-draft.

**Finding a record reads one report** (``bh-pc9rf``): an outbox id is opaque, so the report that
holds it comes from the injected :class:`OutboxIndex`, confirmed against that report's own
document; only a miss scans (one :meth:`ReportStore.read_outboxes` pass) and rebuilds the index.
:meth:`OutboxService.send_approved` sends every approved operation in ONE process: one pass over
the outbox documents, one issue listing for the labels, and the lock's batch scope
(:class:`BatchLock`), ordered per report reopen, comment, labels, close; each operation is the same
write-ahead, single request and receipt as ``send``, with its own grant.

The render is the dry run: no store write, and no tracker request beyond that one label read. The
marker line is encoded by
the injected ``marker_line`` (the adapter's own codec) and joined to the body by the port's
``join_marker_line``, the same rule the adapter's ``post_comment`` applies, so the bytes shown,
leak-checked and digested are the bytes posted (``bh-crf27``).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager, nullcontext

from beadhive.modules.tracker.contracts import CloseReason as TrackerCloseReason
from beadhive.modules.tracker.contracts import (
    CommentQuery,
    CommentWriter,
    IssueCloser,
    IssueFieldWriter,
    IssueKey,
    IssueQuery,
    IssueReopener,
    IssueResolver,
    IssueState,
    IssueTypeWriter,
    LabelOwnership,
    LabelWriter,
    OwnMarker,
    PageCursor,
    TrackerAdapter,
    TrackerComment,
    TrackerError,
    TrackerIssue,
    join_marker_line,
)

from ..contracts import BatchLock, IdSource, IntakeLock, OutboxIndex, ReportStore, Sanitizer
from ..domain import (
    NOTIFIED_DIM,
    SEND_ORDER,
    ApprovalRequest,
    BatchSend,
    BatchSendFailure,
    BatchSendOutcome,
    CanonicalIssue,
    CloseBackfill,
    CloseBackfillItem,
    CloseBackfillOutcome,
    CloseReason,
    DraftRequest,
    IntakeRefused,
    IntakeStoreError,
    IssueBinding,
    LabelBase,
    LabelBatch,
    LabelBatchApprove,
    LabelBatchItem,
    LabelBatchOutcome,
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
    OutboxOperation,
    OutboxPolicy,
    OutboxQuery,
    OutboxRecord,
    OutboxResult,
    OwnIdentity,
    Proposal,
    ProposalStatus,
    RefusalReason,
    RenderContext,
    RenderedRecord,
    ReopenOutcome,
    ReopenRequest,
    ReplyDraft,
    SendPlan,
    StorePosture,
    ThreadActor,
    ThreadState,
    batch_digest,
    close_reason_for,
    is_outbox_id,
    outbox_view,
    reopen_reply_text,
    reply_revision,
    require_private_store,
)
from ..domain import outbox as machine
from ..domain.outbox import TYPE_VALUE, VALUE_KINDS
from . import outbox as translate
from .labels import issue_index, label_base
from .paging import iterate_pages
from .threads import binding_key, thread_actor
from .threads import issue_key as binding_issue

#: The states a reconcile pass can still move: the ambiguous ones, and the retired ones a late
#: marker would bring back (O19).
_RECONCILABLE = frozenset({OpState.SENDING, OpState.UNKNOWN})


def _op_needs_reconcile(op: OutboxOperation) -> bool:
    if op.state in _RECONCILABLE:
        return True
    if op.state is OpState.ABANDONED and op.outcome_unknown:
        return True
    return op.state is OpState.FAILED and op.fail_reason == machine.ABSENT


def _needs_reconcile(record: OutboxRecord) -> bool:
    return any(_op_needs_reconcile(op) for op in record.operations)


def _disabled() -> IntakeRefused:
    return IntakeRefused(
        RefusalReason.OUTBOX_DISABLED,
        "the outbox is off for this hive (intake.external.outbox.enabled; opt-in)",
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
        completion: Callable[[str, str], bool] | None = None,
        index: OutboxIndex | None = None,
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
        self._completion = completion
        self._index = index

    # -- reads -------------------------------------------------------------------------------

    def outbox(self, query: OutboxQuery) -> OutboxListing:
        """The records, oldest first per report (no tracker request, no lock, no write)."""
        self._require_enabled()
        if query.report:
            one = self._store.read_outbox(query.report)
            docs = {query.report: one} if one is not None else {}
        else:
            docs = self._all_outboxes()  # one pass, and the index rebuilt from it
        entries: list[OutboxEntry] = []
        for report, doc in docs.items():
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
        reads = record.op(OpKind.LABELS) is not None and self._policy.labels_enabled
        fresh = self._fresh(binding) if reads else None  # nothing to approve: no read
        ctx = self._ctx(report, binding, outbox_id, labels=fresh)
        renders = tuple(machine.render(record, op.kind, ctx) for op in record.operations)
        return RenderedRecord(report, record, renders)

    # -- O1: a reply -------------------------------------------------------------------------

    def draft_reply(self, request: ReplyDraft) -> OutboxResult:
        """O1 for an operator reply (O2 when the same text already answers the same thread seq),
        checked at once (O3 holds a leak). With ``request.close`` and the close gate on, the record
        also carries an ``issue.close`` operation, naming ``request.duplicate_of``'s bound issue as
        a duplicate's canonical (:meth:`_canonical`); with ``request.reopen`` and the reopen gate
        on, an ``issue.reopen`` that is sent before the comment. Drafting never sends and needs no
        grant."""
        self._require_enabled()
        require_private_store(self._posture)
        binding = self._binding(request.report)
        close = request.close if self._policy.close_enabled else None
        reopen = request.reopen and self._policy.reopen_enabled
        canonical = None
        if close is CloseReason.DUPLICATE and request.duplicate_of:
            canonical = self._canonical(request.duplicate_of, binding)
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
                # the close rides on the reply only while the hive's close gate is on
                close=close,
                duplicate_of=canonical,
                reopen=reopen,
            )
            applied = machine.draft(
                doc,
                draft,
                outbox_id=outbox_id,
                policy=self._policy,
                ctx=self._ctx(request.report, binding, outbox_id),
            )
            self._commit(request.report, applied, expect, request.actor)
        if applied.changed:
            self._indexed({applied.outbox_id: request.report})
        return self._result(request.report, applied, rules, OpKind.COMMENT)

    def reopen(self, request: ReopenRequest) -> ReopenOutcome:
        """The operator's explicit correction of a close (``bh-ih484``, ``bh work outbox
        reopen``): for ONE report whose bound issue reads CLOSED, a reply carrying the text and
        an ``issue.reopen`` (:meth:`draft_reply` with ``reopen``), drafted and never approved or
        sent. It reopens an issue closed by anyone, because the operator chose to. Refused when the
        outbox or the reopen gate is off, the report is not bound to the bound tracker project,
        the issue cannot be read or is not closed, or a reopen for the issue is already pending
        (``reopen_pending``: never two). ``dry_run`` makes every check, reading the one issue and
        the stored outbox, and writes nothing: the outcome carries the planned reply and write."""
        self._require_enabled()
        machine.require_kind_enabled(OpKind.REOPEN, self._policy)
        binding = self._bound_here(request.report)
        found = self._read_issue(binding)
        if found is None:
            raise IntakeRefused(
                RefusalReason.REOPEN_NOT_NEEDED,
                f"{request.report}'s issue could not be read, so it is not known to be closed",
            )
        if found.state is not IssueState.CLOSED:
            raise IntakeRefused(
                RefusalReason.REOPEN_NOT_NEEDED,
                f"{request.report}'s issue is {found.state.value}; only a closed issue is reopened",
            )
        text = request.text.strip() or reopen_reply_text()
        scan = tuple(machine.check(text, self._leaks, sanitizer=self._sanitizer))
        outcome = ReopenOutcome(
            request.report,
            text,
            binding.url,
            machine.REOPEN_PAYLOAD,
            dry_run=request.dry_run,
            findings=scan,
        )
        if request.dry_run:
            pending = machine.pending_reopen(
                self._store.read_outbox(request.report) or OutboxDocument(), binding.url
            )
            if pending is not None:  # the refusal the draft would make
                raise IntakeRefused(
                    RefusalReason.REOPEN_PENDING,
                    f"{pending.outbox_id} already holds an unsent issue.reopen for this issue; "
                    "send it, or discard it (`outbox discard`), before drafting another",
                )
            return outcome
        result = self.draft_reply(ReplyDraft(request.report, text, request.actor, reopen=True))
        return dataclasses.replace(outcome, result=result)

    def retire_reopens(self, report: str, reason: str, actor: str) -> tuple[str, ...]:
        """The outbox ids whose pending reopen (and the reply that explains it) was retired
        because ``report`` was closed afterwards (``bh-ih484``); empty when nothing was pending, or
        with the outbox or the reopen gate off. It needs no approval and never sends or reads the
        tracker. A refusal or store failure leaves the records as they were and is raised for the
        caller to report: the close it follows already stands."""
        if not (self._policy.enabled and self._policy.reopen_enabled):
            return ()
        binding = self._binding(report)
        with self._lock.hold(binding_key(binding)) as epoch:
            doc, expect, _ = self._open(report, epoch, actor)
            applied = machine.retire_reopens(doc, reason=reason, actor=actor)
            self._commit(report, applied, expect, actor)
        return tuple(self._retired_ids(doc, applied))

    @staticmethod
    def _retired_ids(before: OutboxDocument, applied: OutboxApplied) -> list[str]:
        """The records whose reopen moved in ``applied``."""
        return [
            record.outbox_id
            for record in applied.document.records.values()
            if (old := before.record(record.outbox_id)) is not None
            and old.op(OpKind.REOPEN) is not None
            and old.op(OpKind.REOPEN) != record.op(OpKind.REOPEN)
        ]

    # -- O5: the gate ------------------------------------------------------------------------

    def approve(self, request: OutboxApprove) -> OutboxResult:
        """O5: a human operator approves the digest of a fresh dry run, which mints exactly one
        single-use grant (O4 first for each named ``publish``, in the same write). The result
        carries the dry run and the grant id. Refuses unless the tracker reports the write and
        ``comments.read`` usable, so an unknown outcome can always be reconciled."""
        self._require_enabled()
        machine.require_kind_enabled(request.kind, self._policy)  # before any tracker request
        require_private_store(self._posture)
        report, _ = self._locate(request.outbox_id)
        binding = self._bound_here(report)
        capabilities = translate.capabilities(self._tracker.probe_capabilities())
        fresh = self._fresh(binding) if request.kind is OpKind.LABELS else None
        with self._lock.hold(binding_key(binding)) as epoch:
            doc, expect, rules = self._open(report, epoch, request.actor)
            ctx = self._ctx(report, binding, request.outbox_id, labels=fresh)
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
        result = self._result(report, applied, (*rules, *steps), request.kind)
        return dataclasses.replace(
            result,
            rendered=rendered,
            grant_id=approval.grant_id if approval is not None else "",
            detail=f"approved {rendered.short_digest}; `send` posts it once",
        )

    # -- O9 to O13: send ---------------------------------------------------------------------

    def send(self, outbox_id: str, *, actor: str, kind: OpKind = OpKind.COMMENT) -> OutboxResult:
        """O9 persisted, then exactly one ``post_comment`` / ``change_labels`` / ``close_issue`` /
        ``reopen_issue``, then O10, O11 or O12, all under one hold of the binding's lock. An
        approval whose render moved is invalidated instead (O7) and nothing is sent; a close whose
        comment or the report's latest labels are not sent yet is refused (``close_not_ready``),
        and so is a comment whose own reopen is not sent (``reopen_not_ready``), before any
        request."""
        self._require_enabled()
        machine.require_kind_enabled(kind, self._policy)  # before any tracker request
        require_private_store(self._posture)
        report, _ = self._locate(outbox_id)
        binding = self._bound_here(report)
        writer = self._writer(kind)
        fresh = self._fresh(binding) if kind is OpKind.LABELS else None
        return self._send_op(report, binding, outbox_id, kind, writer, fresh, actor)

    def _send_op(
        self,
        report: str,
        binding: IssueBinding,
        outbox_id: str,
        kind: OpKind,
        writer: object,
        fresh: LabelBase | None,
        actor: str,
    ) -> OutboxResult:
        """One operation's O9 to O13 under one hold of the binding's lock (``send`` and every
        item of ``send_approved``): the O7 re-check, the write-ahead, the ONE request with the
        grant, then the receipt or the outcome, each a durable write."""
        with self._lock.hold(binding_key(binding)) as epoch:
            doc, expect, rules = self._open(report, epoch, actor)
            record = self._record(doc, outbox_id)
            op = record.op(kind)
            if op is None:
                raise IntakeRefused(
                    RefusalReason.UNKNOWN_OUTBOX, f"{outbox_id} has no {kind.value} operation"
                )
            ctx = self._ctx(report, binding, outbox_id, labels=fresh)
            if op.state is OpState.APPROVED:
                moved = machine.invalidate(doc, outbox_id, kind, ctx=ctx, actor=actor)
                if moved.changed:  # O7: never send bytes the operator did not approve
                    self._commit(report, moved, expect, actor)
                    result = self._result(report, moved, rules, kind)
                    return dataclasses.replace(
                        result, detail="the render moved since it was approved; approve it again"
                    )
            ahead = machine.begin_send(
                doc,
                outbox_id,
                kind,
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
                answer = self._write(writer, kind, record, plan)
            except Exception as exc:  # noqa: BLE001 - the classification is the contract
                if translate.send_outcome(exc) is translate.SendOutcome.REFUSED:
                    name = translate.refusal_name(exc)
                    outcome = machine.record_refusal(doc, outbox_id, kind, name, actor=actor)
                else:
                    outcome = machine.record_unknown(
                        doc, outbox_id, kind, actor=actor, detail=type(exc).__name__
                    )
            else:
                outcome = machine.record_receipt(
                    doc, outbox_id, kind, translate.receipt(answer), actor=actor
                )
            self._commit(report, outcome, ahead.document.seq, actor)
            notified, detail = self._notify(report, outbox_id, outcome, actor)
        detail = _joined(detail, self._complete(report, outcome, actor))  # outside the lock
        result = self._result(report, outcome, (*rules, *(s.rule for s in ahead.steps)), kind)
        return dataclasses.replace(result, notified=notified, detail=detail or result.detail)

    def send_approved(self, request: BatchSend) -> BatchSendOutcome:
        """Send every APPROVED operation (of ``request.kinds``, on ``request.reports``) in one
        process (``bh-pc9rf``): ONE pass over the outbox documents, ONE issue listing when a
        labels operation is due, and the lock's batch scope, so a batch of N pays the per-process
        costs once instead of N times. Per report the order is :data:`SEND_ORDER` (the reopen, every
        comment oldest first, then the labels, then the close), and every operation is exactly
        ``send``'s O9 to O13 with its own single-use grant, write-ahead and receipt. A refusal or a
        store failure on one operation is reported in ``failed`` and the rest continue (a close
        whose comment did not go out is refused by ``close_not_ready``); only a host-level refusal
        (``not_primary``) stops the batch, since nothing else could be sent either: the outcome
        so far is returned with ``stopped`` set, so what WAS sent is still reported."""
        self._require_enabled()
        kinds = request.kinds or SEND_ORDER
        for kind in request.kinds:  # a named kind is checked before any tracker request
            machine.require_kind_enabled(kind, self._policy)
        require_private_store(self._posture)
        if request.reports:
            docs = {r: d for r in request.reports if (d := self._store.read_outbox(r)) is not None}
        else:
            docs = self._all_outboxes()
        due = [
            (report, record.outbox_id, kind)
            for report in sorted(docs)
            for kind in SEND_ORDER
            if kind in kinds
            for record in sorted(docs[report].records.values(), key=_created)
            if (op := record.op(kind)) is not None and op.state is OpState.APPROVED
        ]
        if not due:
            return BatchSendOutcome()
        live = self._listing_or_none() if any(k is OpKind.LABELS for _, _, k in due) else None
        results: list[OutboxResult] = []
        failed: list[BatchSendFailure] = []
        stopped = ""
        bindings: dict[str, IssueBinding] = {}
        with self._batch():
            for report, outbox_id, kind in due:
                try:
                    machine.require_kind_enabled(kind, self._policy)
                    if report not in bindings:
                        bindings[report] = self._bound_here(report)
                    binding = bindings[report]
                    writer = self._writer(kind)
                    fresh = None
                    if kind is OpKind.LABELS:
                        issue = live.get(binding.url) if live is not None else None
                        fresh = label_base(issue) if issue is not None else self._fresh(binding)
                    sent = self._send_op(
                        report, binding, outbox_id, kind, writer, fresh, request.actor
                    )
                    results.append(sent)
                except IntakeRefused as exc:
                    failed.append(
                        BatchSendFailure(report, outbox_id, kind, exc.reason.value, exc.detail)
                    )
                    if exc.reason is RefusalReason.NOT_PRIMARY:
                        stopped = exc.detail
                        break
                except IntakeStoreError as exc:
                    failed.append(
                        BatchSendFailure(report, outbox_id, kind, "store_error", exc.detail)
                    )
        return BatchSendOutcome(tuple(results), tuple(failed), stopped)

    def _listing_or_none(self) -> dict[str, TrackerIssue] | None:
        """The issues by URL from ONE listing, or ``None`` when it cannot be made (each labels
        operation then reads its own issue, as ``send`` does)."""
        try:
            return issue_index(self._tracker, include_closed=True)
        except TrackerError:
            return None

    def _batch(self) -> AbstractContextManager[None]:
        """The lock's batch scope when it has one (:class:`BatchLock`), else nothing."""
        return self._lock.batch() if isinstance(self._lock, BatchLock) else nullcontext()

    def _writer(self, kind: OpKind) -> object:
        """The bound adapter as the writer ``kind`` needs (refused when it has none)."""
        if kind is OpKind.COMMENT:
            if not isinstance(self._tracker, CommentWriter):
                raise IntakeRefused(
                    RefusalReason.CAPABILITY_UNUSABLE,
                    "the bound tracker adapter has no comment writer",
                )
        elif kind is OpKind.CLOSE:
            if not isinstance(self._tracker, IssueCloser):
                raise IntakeRefused(
                    RefusalReason.CAPABILITY_UNUSABLE,
                    "the bound tracker adapter cannot close an issue (issue.close is not enabled)",
                )
        elif kind is OpKind.REOPEN:
            if not isinstance(self._tracker, IssueReopener):
                raise IntakeRefused(
                    RefusalReason.CAPABILITY_UNUSABLE,
                    "the bound tracker adapter cannot reopen an issue (issue.reopen is not "
                    "enabled)",
                )
        elif kind is OpKind.TYPE:
            if not isinstance(self._tracker, IssueTypeWriter):
                raise IntakeRefused(
                    RefusalReason.CAPABILITY_UNUSABLE,
                    "the bound tracker adapter cannot set a type (issue.type is not enabled)",
                )
        elif kind is OpKind.FIELDS:
            if not isinstance(self._tracker, IssueFieldWriter):
                raise IntakeRefused(
                    RefusalReason.CAPABILITY_UNUSABLE,
                    "the bound tracker adapter cannot set fields (issue.fields is not enabled)",
                )
        elif not isinstance(self._tracker, LabelWriter):
            raise IntakeRefused(
                RefusalReason.CAPABILITY_UNUSABLE, "the bound tracker adapter has no label writer"
            )
        return self._tracker

    def _ownership(self) -> LabelOwnership:
        """The labels this sender may ever change: the root's owned prefixes, none when unset."""
        return LabelOwnership(tuple(p for p in self._leaks.owned_label_prefixes if p))

    def _write(self, writer: object, kind: OpKind, record: OutboxRecord, plan: SendPlan):
        """The ONE request of an attempt, with the grant handed over once."""
        grant = translate.plan_grant(record, plan)
        issue = translate.issue_key(record)
        if kind is OpKind.COMMENT:
            assert isinstance(writer, CommentWriter)
            return writer.post_comment(
                issue,
                plan.body,
                marker=translate.own_marker(self._identity, plan.outbox_id),
                grant=grant,
            )
        if kind is OpKind.CLOSE:
            assert isinstance(writer, IssueCloser) and plan.close is not None
            canonical = plan.duplicate_of
            return writer.close_issue(
                issue,
                reason=TrackerCloseReason(plan.close.value),
                grant=grant,
                duplicate_of=(
                    None if canonical is None else IssueKey(canonical.identifier, canonical.url)
                ),
            )
        if kind is OpKind.REOPEN:
            assert isinstance(writer, IssueReopener)
            return writer.reopen_issue(issue, grant=grant)
        if kind is OpKind.TYPE:
            assert isinstance(writer, IssueTypeWriter) and plan.values
            return writer.set_issue_type(issue, dict(plan.values)[TYPE_VALUE], grant=grant)
        if kind is OpKind.FIELDS:
            assert isinstance(writer, IssueFieldWriter) and plan.values
            return writer.set_issue_fields(issue, dict(plan.values), grant=grant)
        assert isinstance(writer, LabelWriter)
        return writer.change_labels(
            issue, add=plan.add, remove=plan.remove, ownership=self._ownership(), grant=grant
        )

    # -- O13 to O16, O19: reconcile ----------------------------------------------------------

    def reconcile(self, outbox_id: str | None = None, *, actor: str) -> tuple[OutboxResult, ...]:
        """One reconcile pass: for ``outbox_id``, or for every record that is ambiguous or could
        still turn up late. Tracker-read-only; per report one comment listing (when a comment
        needs it) and one read of the issue (when a label or close operation does: its labels or
        its state). One result per operation that was reconciled."""
        self._require_enabled()
        require_private_store(self._posture)
        targets: dict[str, list[str]] = {}
        if outbox_id is not None:
            report, _ = self._locate(outbox_id)
            targets[report] = [outbox_id]
        else:
            for report, doc in self._all_outboxes().items():
                wanted = [
                    r.outbox_id
                    for r in sorted(doc.records.values(), key=_created)
                    if _needs_reconcile(r)
                ]
                if wanted:
                    targets[report] = wanted
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
        start_seq = doc.seq  # read before the reads begin (ordered by seq, never by time)
        pending = {
            oid: [op.kind for op in doc.records[oid].operations if _op_needs_reconcile(op)]
            for oid in outbox_ids
            if oid in doc.records
        }
        wanted = {kind for kinds in pending.values() for kind in kinds}
        # a read nobody needs is not made, and is vacuously complete
        comments, complete = self._listing(binding) if OpKind.COMMENT in wanted else ((), True)
        reads_issue = wanted & {OpKind.LABELS, OpKind.CLOSE, OpKind.REOPEN, *VALUE_KINDS}
        issue = self._read_issue(binding) if reads_issue else None
        labels = frozenset(issue.labels) if issue is not None and OpKind.LABELS in wanted else None
        state = (
            issue.state.value
            if issue is not None and wanted & {OpKind.CLOSE, OpKind.REOPEN}
            else None
        )
        issue_type = issue.issue_type if issue is not None and OpKind.TYPE in wanted else None
        fields = issue.fields if issue is not None and OpKind.FIELDS in wanted else None
        results: list[OutboxResult] = []
        with self._lock.hold(key) as epoch:
            doc, expect, again = self._open(report, epoch, actor)
            ledger = outbox_view(doc)
            known = set(ledger.known_actors())
            me = thread_actor(self._tracker.whoami())
            if me is not None:
                known.add(me)
            marked = translate.marked_comments(comments, self._identity, frozenset(known))
            observation = OutboxObservation(
                start_seq, complete, marked, labels, state, issue_type, fields
            )
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
                for kind in pending.get(step.outbox_id) or [None]:
                    result = self._result(report, step, (*recovered, *again), kind)
                    read_ok = {
                        OpKind.LABELS: labels is not None,
                        OpKind.CLOSE: state is not None,
                        OpKind.REOPEN: state is not None,
                        OpKind.TYPE: issue_type is not None,
                        OpKind.FIELDS: fields is not None,
                    }.get(kind, complete)  # type: ignore[arg-type]
                    commented = kind in (None, OpKind.COMMENT)
                    results.append(
                        dataclasses.replace(
                            result,
                            complete=read_ok,
                            notified=notified if commented else "",
                            detail=(detail if commented else "")
                            or self._reconcile_detail(step, kind, read_ok),
                        )
                    )
        completed = {step.outbox_id: self._complete(report, step, actor) for step in applied}
        return [
            dataclasses.replace(r, detail=_joined(r.detail, completed[r.outbox_id]))
            if completed.get(r.outbox_id)
            else r
            for r in results
        ]

    # -- O8, O18, O17 ------------------------------------------------------------------------

    def discard(
        self,
        outbox_id: str,
        *,
        reason: str,
        actor: str,
        accept_unknown: bool = False,
        kind: OpKind | None = None,
    ) -> OutboxResult:
        """O8 (any grant voided), or O18 for an ``unknown`` with ``accept_unknown``. ``kind``
        names one operation; without it every non-terminal operation of the record is retired."""
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
                kind=kind,
                accept_unknown=accept_unknown,
            )
            self._commit(report, applied, expect, actor)
        result = self._result(report, applied, rules, kind)
        completed = self._complete(report, applied, actor)  # outside the lock
        return dataclasses.replace(result, detail=completed) if completed else result

    def retry(self, outbox_id: str, *, actor: str, kind: OpKind = OpKind.COMMENT) -> OutboxResult:
        """O17: a ``failed`` operation back to ``drafted``; sending again needs a new approval
        (and, for labels, a fresh render against the labels as they now are)."""
        self._require_enabled()
        require_private_store(self._posture)
        report, _ = self._locate(outbox_id)
        binding = self._binding(report)
        with self._lock.hold(binding_key(binding)) as epoch:
            doc, expect, rules = self._open(report, epoch, actor)
            applied = machine.retry(doc, outbox_id, kind, actor=actor, policy=self._policy)
            self._commit(report, applied, expect, actor)
        result = self._result(report, applied, rules, kind)
        return dataclasses.replace(result, detail="drafted again; render, approve and send anew")

    # -- the close backfill (bh-pr5ll) -------------------------------------------------------

    def backfill_closes(
        self,
        request: CloseBackfill,
        *,
        reply_text: Callable[[str, Proposal], str] | None = None,
    ) -> CloseBackfillOutcome:
        """Draft the ``issue.close`` a report closed before closes existed never got: for every
        report CLOSED in the store, bound to this tracker project, whose applied proposal ends
        the thread (:func:`~..domain.triage.close_reason_for`) and whose issue ONE listing shows
        still open, add the close to its latest reply record (O1, checked at once). A report that
        never got its reply gets one drafted, with the close attached, from ``reply_text(report,
        proposal)`` (the reviewed text, or the template apply would have used); without that
        text it is reported and skipped. A reply that already carries a close is skipped, so a
        re-run drafts nothing twice. ``dry_run`` makes the same reads and writes nothing; neither
        approves nor sends anything."""
        self._require_enabled()
        machine.require_kind_enabled(OpKind.CLOSE, self._policy)  # before any tracker request
        require_private_store(self._posture)
        candidates: list[tuple[str, IssueBinding, Proposal, CloseReason]] = []
        project = self._tracker.describe().project
        for view in self._store.reports_with_proposal():
            proposal = self._store.read_proposal(view.id)
            if view.status != "closed" or view.binding is None or proposal is None:
                continue
            reason = close_reason_for(proposal.disposition)
            if proposal.status is not ProposalStatus.APPLIED or reason is None:
                continue
            if (view.binding.tracker, view.binding.project) != (project.tracker, project.locator):
                continue
            candidates.append((view.id, view.binding, proposal, reason))
        if not candidates:
            return CloseBackfillOutcome((), request.dry_run)
        try:
            live = issue_index(self._tracker, include_closed=True)
        except TrackerError as exc:
            raise IntakeRefused(
                RefusalReason.TRACKER_ERROR, f"the issue listing failed: {type(exc).__name__}"
            ) from exc
        items = []
        for report, binding, proposal, reason in candidates:
            issue = live.get(binding.url)
            if issue is not None and issue.state.value == "closed":
                continue  # nothing to do: the issue is closed already
            if issue is None:
                missing = "the issue is not in the listing"
                items.append(CloseBackfillItem(report, reason=reason, detail=missing))
                continue
            items.append(self._backfill_one(report, binding, proposal, reason, request, reply_text))
        return CloseBackfillOutcome(tuple(items), request.dry_run)

    def _backfill_one(
        self,
        report: str,
        binding: IssueBinding,
        proposal: Proposal,
        reason: CloseReason,
        request: CloseBackfill,
        reply_text: Callable[[str, Proposal], str] | None,
    ) -> CloseBackfillItem:
        doc = self._store.read_outbox(report) or OutboxDocument()
        replies = sorted(
            (r for r in doc.records.values() if r.event is OutboxEvent.REPLY),
            key=_created,
        )
        if not replies or replies[-1].op(OpKind.COMMENT) is None:
            return self._backfill_reply(report, proposal, reason, request, reply_text)
        carrier = replies[-1]
        if carrier.op(OpKind.CLOSE) is not None:
            detail = "its reply already carries a close"
            return CloseBackfillItem(report, carrier.outbox_id, reason, detail=detail)
        canonical = None
        if reason is CloseReason.DUPLICATE and proposal.share_with:
            canonical = self._canonical(proposal.share_with, binding)
        named = canonical.url if canonical is not None else ""
        if request.dry_run:
            return CloseBackfillItem(report, carrier.outbox_id, reason, duplicate_of=named)
        with self._lock.hold(binding_key(binding)) as epoch:
            doc, expect, _ = self._open(report, epoch, request.actor)
            if carrier.outbox_id not in doc.records:  # pragma: no cover - a record never leaves
                raise IntakeRefused(RefusalReason.UNKNOWN_OUTBOX, carrier.outbox_id)
            applied = machine.add_close(
                doc,
                carrier.outbox_id,
                reason,
                actor=request.actor,
                policy=self._policy,
                ctx=self._ctx(report, binding, carrier.outbox_id),
                duplicate_of=canonical,
            )
            self._commit(report, applied, expect, request.actor)
        return CloseBackfillItem(
            report, carrier.outbox_id, reason, drafted=applied.changed, duplicate_of=named
        )

    def _backfill_reply(
        self,
        report: str,
        proposal: Proposal,
        reason: CloseReason,
        request: CloseBackfill,
        reply_text: Callable[[str, Proposal], str] | None,
    ) -> CloseBackfillItem:
        """The report never got its reply: draft it from the applied proposal, close attached.
        The text is leak-checked like every draft (O3 holds it), and both operations still need
        their own operator approval."""
        text = reply_text(report, proposal) if reply_text is not None else ""
        if not text.strip():
            detail = "no reply to carry the close, and no reviewed text to draft one from"
            return CloseBackfillItem(report, reason=reason, detail=detail)
        canonical = proposal.share_with if reason is CloseReason.DUPLICATE else ""
        named = ""
        if canonical:
            found = self._canonical(canonical, self._binding(report))
            named = found.url if found is not None else ""
        if request.dry_run:
            return CloseBackfillItem(report, reason=reason, reply_text=text, duplicate_of=named)
        reply = ReplyDraft(report, text, request.actor, close=reason, duplicate_of=canonical)
        drafted = self.draft_reply(reply)
        return CloseBackfillItem(
            report, drafted.outbox_id, reason, drafted=True, reply_text=text, duplicate_of=named
        )

    # -- the batch (bh-uim9i): one review of every drafted labels operation -------------------

    def review_label_batch(self) -> LabelBatch:
        """The rendered batch (``bh work labels review``): every drafted ``labels.change``,
        ``issue.type`` and ``issue.fields`` operation of a labels record (a value operation only
        while its gate is on), ordered by report then draft, each with its digest as drafted, the
        change it makes and whether its issue moved since (``stale``); and the digest of the
        ``(outbox_id, digest)`` list. One pass over the outbox documents and one issue listing (none
        when nothing is drafted); no store write."""
        self._require_enabled()
        docs = self._all_outboxes()
        return self._label_batch(docs, self._live_for(docs))

    def approve_label_batch(self, request: LabelBatchApprove) -> LabelBatchOutcome:
        """Approve the batch whose digest an operator reviewed: re-render it (ONE capability
        probe, ONE pass over the outbox documents, ONE issue listing), refuse unless its digest is
        still the one reviewed, then approve every item through O5, each minting its own
        single-use grant under its binding's lock. A STALE item (its owned labels moved since its
        draft) is skipped and reported for re-draft by the next sync; an item an approval refuses
        is reported and the rest still approved. Each item is approved as its own kind, so a
        type or field operation is its own approval with its own grant. Nothing is sent."""
        self._require_enabled()
        machine.require_kind_enabled(OpKind.LABELS, self._policy)
        require_private_store(self._posture)
        if not machine.is_operator_seat(request.actor, self._policy):
            raise IntakeRefused(
                RefusalReason.OPERATOR_SEAT_REQUIRED,
                f"approving a batch needs a human operator seat, not {request.actor!r}",
            )
        capabilities = translate.capabilities(self._tracker.probe_capabilities())
        docs = self._all_outboxes()
        live = self._live_for(docs)
        batch = self._label_batch(docs, live)
        if not batch.items:
            raise IntakeRefused(RefusalReason.UNKNOWN_OUTBOX, "no drafted labels operation")
        if not machine.digest_matches(request.digest, batch.digest):
            raise IntakeRefused(
                RefusalReason.STALE_DIGEST,
                f"the batch is now {batch.short_digest}; review it again before approving",
            )
        approved: list[OutboxResult] = []
        stale: list[LabelBatchItem] = []
        failed: list[tuple[str, str]] = []
        bindings: dict[str, IssueBinding] = {}
        with self._batch():  # one process, one host check (bh-pc9rf)
            for item in batch.items:
                if item.stale:
                    stale.append(item)
                    continue
                try:
                    if item.report not in bindings:
                        bindings[item.report] = self._bound_here(item.report)
                    approved.append(
                        self._approve_item(
                            item, bindings[item.report], live[item.url], capabilities, request
                        )
                    )
                except IntakeRefused as exc:
                    if exc.reason is RefusalReason.STALE_DIGEST:
                        stale.append(dataclasses.replace(item, stale=True, note=exc.detail))
                    else:
                        failed.append((item.outbox_id, str(exc)))
        return LabelBatchOutcome(batch, tuple(approved), tuple(stale), tuple(failed))

    def _approve_item(
        self,
        item: LabelBatchItem,
        binding: IssueBinding,
        issue: TrackerIssue,
        capabilities: machine.Capabilities,
        request: LabelBatchApprove,
    ) -> OutboxResult:
        """O5 for one batch item, against the listing's labels, minting its own grant."""
        fresh = label_base(issue)
        with self._lock.hold(binding_key(binding)) as epoch:
            doc, expect, rules = self._open(item.report, epoch, request.actor)
            ctx = self._ctx(item.report, binding, item.outbox_id, labels=fresh)
            applied = machine.approve(
                doc,
                ApprovalRequest(item.outbox_id, item.kind, item.digest, request.actor),
                grant_id=self._ids.grant_id(),
                policy=self._policy,
                ctx=ctx,
                capabilities=capabilities,
            )
            self._commit(item.report, applied, expect, request.actor)
            record = applied.document.records[item.outbox_id]
            rendered = machine.render(record, item.kind, ctx)
        approval = record.op(item.kind).approval  # type: ignore[union-attr]
        result = self._result(item.report, applied, rules, item.kind)
        return dataclasses.replace(
            result,
            rendered=rendered,
            grant_id=approval.grant_id if approval is not None else "",
            detail=f"approved {rendered.short_digest} in batch; `send` changes it once",
        )

    def _all_outboxes(self) -> dict[str, OutboxDocument]:
        """Every outbox document, read once, by report (a batch's or a scan's one pass), and the
        outbox index rebuilt from it: the pass is complete, so it is the index's truth."""
        docs = dict(self._store.read_outboxes())
        if self._index is not None:
            self._index.rebuild(
                {oid: report for report, doc in docs.items() for oid in doc.records}
            )
        return docs

    def _indexed(self, entries: dict[str, str]) -> None:
        if self._index is not None:
            self._index.add(entries)

    def _drafted(
        self, docs: dict[str, OutboxDocument]
    ) -> list[tuple[str, OutboxRecord, OutboxOperation]]:
        """The drafted operations of the labels records (labels, then type, then fields, each a
        value kind only while its gate is on), ordered by report then draft."""
        kinds = [OpKind.LABELS]
        kinds += [OpKind.TYPE] if self._policy.type_enabled else []
        kinds += [OpKind.FIELDS] if self._policy.fields_enabled else []
        out: list[tuple[str, OutboxRecord, OutboxOperation]] = []
        for report in sorted(docs):
            for record in sorted(docs[report].records.values(), key=_created):
                for kind in kinds:
                    op = record.op(kind)
                    if op is None or op.state is not OpState.DRAFTED:
                        continue
                    if (op.labels if kind is OpKind.LABELS else op.values) is not None:
                        out.append((report, record, op))
        return out

    def _live_for(self, docs: dict[str, OutboxDocument]) -> dict[str, TrackerIssue]:
        """The issues by URL from ONE listing (open and closed); none when nothing is drafted."""
        if not self._drafted(docs):
            return {}
        try:
            return issue_index(self._tracker, include_closed=True)
        except TrackerError as exc:
            raise IntakeRefused(
                RefusalReason.TRACKER_ERROR, f"the issue listing failed: {type(exc).__name__}"
            ) from exc

    def _label_batch(
        self, docs: dict[str, OutboxDocument], live: dict[str, TrackerIssue]
    ) -> LabelBatch:
        items: list[LabelBatchItem] = []
        for report, record, op in self._drafted(docs):
            if op.kind in VALUE_KINDS:
                items.append(self._value_item(report, record, op, live.get(record.binding.url)))
                continue
            assert op.labels is not None
            url = record.binding.url
            base = LabelBase(op.labels.base_revision, op.labels.base_owned)
            drafted = machine.render(record, OpKind.LABELS, self._bare_ctx(url, base))
            issue = live.get(url)
            stale, note = False, ""
            if issue is None:
                stale, note = True, "the issue is not in the listing (deleted or moved)"
            else:  # any owned label moved (even one the diff does not name), or the render did
                now = machine.render(record, OpKind.LABELS, self._bare_ctx(url, label_base(issue)))
                owned = frozenset(label for label in issue.labels if self._leaks.owns(label))
                if owned != op.labels.base_owned or now.digest != drafted.digest:
                    stale, note = True, "its owned labels moved since the draft; sync re-drafts it"
            items.append(
                LabelBatchItem(
                    report,
                    record.outbox_id,
                    drafted.digest,
                    url,
                    drafted.add,
                    drafted.remove,
                    stale,
                    note,
                )
            )
        return LabelBatch(tuple(items), batch_digest(items))

    def _value_item(
        self, report: str, record: OutboxRecord, op: OutboxOperation, issue: TrackerIssue | None
    ) -> LabelBatchItem:
        """A drafted ``issue.type`` / ``issue.fields`` operation as a batch item: stale when the
        issue is gone, its values were not read, or a value it was drafted on has moved."""
        intent = op.values
        assert intent is not None
        url = record.binding.url
        rendered = machine.render(record, op.kind, self._bare_ctx(url, LabelBase("", frozenset())))
        stale, note = False, ""
        live = _live_values(issue, op.kind)
        if issue is None:
            stale, note = True, "the issue is not in the listing (deleted or moved)"
        elif live is None:
            stale, note = True, f"the issue's {_noun(op.kind)} was not read; sync re-drafts it"
        elif any(
            not machine.same_value(name, live.get(name, ""), was) for name, was in intent.base
        ):
            stale, note = True, f"its {_noun(op.kind)} moved since the draft; sync re-drafts it"
        return LabelBatchItem(
            report,
            record.outbox_id,
            rendered.digest,
            url,
            stale=stale,
            note=note,
            kind=op.kind,
            values=intent.values,
        )

    def _bare_ctx(self, url: str, labels: LabelBase) -> RenderContext:
        """A label render's context: a label operation reads no marker and no thread."""
        return RenderContext(url, self._leaks, self._sanitizer, labels=labels)

    # -- helpers -----------------------------------------------------------------------------

    def _require_enabled(self) -> None:
        if not self._policy.enabled:
            raise _disabled()

    def _locate(self, outbox_id: str) -> tuple[str, OutboxDocument]:
        """The report holding ``outbox_id`` and its document. An id is opaque (``ob1_`` plus 26
        base32 characters), never a bead id. The index answers with ONE report read, confirmed
        against that report's document (a record never leaves its report); a miss, or an answer
        the document does not confirm, scans every outbox once and rebuilds the index."""
        if not is_outbox_id(outbox_id):
            raise IntakeRefused(
                RefusalReason.INVALID_OUTBOX_ID, f"{outbox_id!r} is not an outbox id (ob1_...)"
            )
        hint = self._index.report_of(outbox_id) if self._index is not None else None
        if hint:
            try:
                doc = self._store.read_outbox(hint)
            except IntakeStoreError:  # e.g. the hinted bead is gone: a miss, so scan
                doc = None
            if doc is not None and outbox_id in doc.records:
                return hint, doc
        for report, doc in self._all_outboxes().items():
            if outbox_id in doc.records:
                return report, doc
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

    def _canonical(self, report: str, member: IssueBinding) -> CanonicalIssue | None:
        """The issue a duplicate close names as its canonical (``bh-iamru``): ``report``'s bound
        issue when it is another issue of ``member``'s tracker project. ``None`` (a plain
        duplicate close, naming no original) when the canonical report is missing, born native or
        bound elsewhere: a tracker links duplicates within one project. Reads the store only."""
        view = self._store.get(report)
        canonical = view.binding if view is not None else None
        if canonical is None or canonical.url == member.url:
            return None
        if (canonical.tracker, canonical.project) != (member.tracker, member.project):
            return None
        return CanonicalIssue(canonical.identifier, canonical.url)

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

    def _ctx(
        self,
        report: str,
        binding: IssueBinding,
        outbox_id: str,
        *,
        labels: LabelBase | None = None,
    ) -> RenderContext:
        """The render context of ``outbox_id``. ``labels`` is the issue's labels read fresh
        (a label operation's); without it, the stored snapshot's (an import refreshes it)."""
        thread = self._store.read_thread(report)
        if labels is None:
            view = self._store.get(report)
            snapshot = view.snapshot if view is not None else None
            if snapshot is not None:
                labels = LabelBase(snapshot.revision, frozenset(snapshot.labels))
        marker = translate.own_marker(self._identity, outbox_id)
        return RenderContext(
            binding.url,
            self._leaks,
            self._sanitizer,
            marker_line=self._marker_line(marker),
            join_marker_line=join_marker_line,
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

    def _complete(self, report: str, applied: OutboxApplied, actor: str) -> str:
        """The completion policy after a MILESTONE comment settled (``sent``, ``abandoned`` or
        ``superseded``): the injected hook closes the report when it is complete. It runs OUTSIDE
        the binding's lock (the hook takes it) and never un-sends or fails the operation: a
        failure is reported in the result's detail. Without a hook, or for a reply, nothing."""
        record = applied.record
        if self._completion is None or record is None or not applied.changed:
            return ""
        if record.event not in {OutboxEvent.MERGED, OutboxEvent.RELEASED}:
            return ""
        op = record.op(OpKind.COMMENT)
        if op is None or op.state not in machine.SETTLED:
            return ""
        try:
            closed = self._completion(report, actor)
        except (IntakeRefused, IntakeStoreError) as exc:
            return f"the report could not be completed: {exc.detail}"
        return "the report is complete and was closed" if closed else ""

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

    def _labels(self, binding: IssueBinding) -> frozenset[str] | None:
        """The issue's labels now (one read through ``IssueResolver.get``, else a paged listing
        matched on the issue); ``None`` when the read failed or never found the issue, so nothing
        is inferred from it."""
        found = self._read_issue(binding)
        return frozenset(found.labels) if found is not None else None

    def _fresh(self, binding: IssueBinding) -> LabelBase | None:
        """The issue's labels read now, with the tracker's revision, for a label render; ``None``
        (the snapshot is used) when the read cannot be made."""
        found = self._read_issue(binding)
        return label_base(found) if found is not None else None

    def _read_issue(self, binding: IssueBinding) -> TrackerIssue | None:
        issue = binding_issue(binding)
        tracker = self._tracker
        try:
            if isinstance(tracker, IssueResolver):
                return tracker.get(issue)

            def fetch(cursor: PageCursor | None):
                return tracker.list_issues(IssueQuery(cursor=cursor))

            for page in iterate_pages(fetch):
                for row in page.items:
                    if row.key == issue:
                        return row
        except TrackerError:
            return None
        return None

    def _reconcile_detail(self, applied: OutboxApplied, kind: OpKind | None, read_ok: bool) -> str:
        noun = {
            OpKind.LABELS: "label read",
            OpKind.CLOSE: "issue read",
            OpKind.REOPEN: "issue read",
            OpKind.TYPE: "issue read",
            OpKind.FIELDS: "issue read",
        }.get(
            kind,  # type: ignore[arg-type]
            "comment listing",
        )
        if not read_ok:
            return f"the {noun} failed part-way; nothing was inferred"
        record = applied.record
        op = record.op(kind or OpKind.COMMENT) if record is not None else None
        if op is None:
            return ""
        if op.state is OpState.UNKNOWN:
            return (
                f"not found in {op.absent_confirmations} of the "
                f"{self._policy.absent_confirmations} complete reads needed; reconcile again"
            )
        if op.state is OpState.FAILED and op.fail_reason == machine.ABSENT:
            return "confirmed absent: nothing was written; `retry`, then approve and send anew"
        if op.duplicate_observed:
            return f"duplicate observed: {', '.join(op.duplicates)} (never removed automatically)"
        return ""

    @staticmethod
    def _result(
        report: str, applied: OutboxApplied, rules: Iterable[str], kind: OpKind | None = None
    ) -> OutboxResult:
        record = applied.record
        if kind is None and record is not None and record.operations:
            kind = OpKind.COMMENT if record.op(OpKind.COMMENT) else record.operations[0].kind
        kind = kind or OpKind.COMMENT
        op = record.op(kind) if record is not None else None
        receipt = op.receipt if op is not None else None
        return OutboxResult(
            report,
            applied.outbox_id,
            kind,
            op.state if op is not None else None,
            (*rules, *(s.rule for s in applied.steps)),
            comment_id=(receipt.comment_id or "") if receipt is not None else "",
        )


def _live_values(issue: TrackerIssue | None, kind: OpKind) -> dict[str, str] | None:
    """The issue's values a ``kind`` operation sets, as one read saw them (``None``: not read)."""
    if issue is None:
        return None
    if kind is OpKind.TYPE:
        return None if issue.issue_type is None else {TYPE_VALUE: issue.issue_type}
    return None if issue.fields is None else dict(issue.fields)


def _noun(kind: OpKind) -> str:
    return "type" if kind is OpKind.TYPE else "field values"


def _joined(first: str, second: str) -> str:
    return "; ".join(part for part in (first, second) if part)


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
        if self._ledgers is None:  # one pass over every outbox document (bh-pc9rf)
            docs = self._store.read_outboxes().values()
            self._ledgers = tuple(outbox_view(doc) for doc in docs)
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
