"""``MilestoneService``: the merge and release triggers that DRAFT outbox records (I10).

``on_work_merged`` and ``on_release`` are the two milestone operations of
:class:`~..contracts.IntakePort` (``bh-6wj9y`` R9). What they establish, per report:

* the ``delivery`` dimension moves (``pending`` or ``reverted`` to ``merged`` at a merge,
  ``merged`` to ``released`` only when the :class:`~..contracts.ReleaseVerifier` answers ``YES``;
  ``NO`` and ``UNKNOWN`` are reported as not verified and change nothing);
* the fact is recorded in the report's ``intake.outbox`` document (``facts.merged`` /
  ``facts.released``), in the same write as the drafted record, before the dimension write, so a
  crash between the two leaves a fact the next run turns into the dimension, never a dimension no
  fact explains;
* when the hive turned the milestone's drafting on (and the outbox is on) a ``merged`` or
  ``released`` record is drafted through the O1 machine. A ``released`` record supersedes an
  unsent ``merged`` one in the ``milestone`` group (O6); a re-run finds the :class:`DraftKey` and
  drafts nothing (O2);
* the completion policy is evaluated: a report whose delivery reached the policy's milestone and
  whose milestone record is settled (or, with no record expected, at once) is closed through
  ``ReportStore.close``, the only way a report closes. Merged but unreleased under
  ``close-on-release`` keeps the report open and held with ``delivery:merged``.

The progress milestones (bh-dev42) only draft and never touch a dimension or the completion
policy: ``on_work_started`` (the claim hook) drafts a ``started`` record for every report that
tracks the claimed work, keyed on the work so only its first claim drafts; ``delivery_notice``
(``bh work delivery blocked|deprioritised``) drafts the operator's public reason under fixed
wording. Both draft only while a report's delivery is ``pending`` and only with their own switch
on; the leak check runs on the drafted text, so a reason naming a bead id leaves a held record.

A rejected report (a ``duplicate-of`` ends in one) never gets a milestone from any trigger below:
its delivery is its canonical report's, which already tracks the work (bh-5kpwc).

**Nothing here sends.** This service holds no tracker, no grant and no writer: the most a milestone
can do is leave a ``drafted`` (or ``held``) record for an operator to render, approve and send
through the supervised sender. It is free to call from the work layer: a failure for one report is
returned in its :class:`MilestoneItem` and the next report is still handled.
"""

from __future__ import annotations

from collections.abc import Callable

from ..contracts import IdSource, IntakeLock, ReleaseVerifier, ReportStore, Sanitizer
from ..domain import (
    COMPLETION_CLOSE,
    DELIVERY_DIM,
    BeadView,
    CloseReason,
    CompletionPolicy,
    DraftRequest,
    IntakeRefused,
    IntakeStoreError,
    IssueBinding,
    LeakPolicy,
    MergedFact,
    MergeFact,
    MilestoneItem,
    MilestoneOutcome,
    MilestonePolicy,
    NoticeFact,
    OpKind,
    OpState,
    OutboxDocument,
    OutboxPolicy,
    RefusalReason,
    ReleasedFact,
    ReleaseFact,
    RenderContext,
    StartFact,
    StorePosture,
    ThreadState,
    Verdict,
    completion_event,
    delivery_after_merge,
    delivery_after_release,
    delivery_complete,
    delivery_of,
    delivery_unowned,
    fold_facts,
    is_awaiting_triage,
    merged_draft,
    merged_facts,
    notice_draft,
    notice_reason_problem,
    released_draft,
    released_facts,
    require_private_store,
    started_draft,
)
from ..domain import outbox as machine
from ..domain.milestones import NOTICE_EVENTS, PROGRESS_DELIVERY
from .threads import binding_key

MERGED = "merged"
RELEASED = "released"
STARTED = "started"


def _why(exc: IntakeRefused | IntakeStoreError) -> str:
    return exc.detail or str(exc)


class MilestoneService:
    """The milestone triggers over one :class:`ReportStore`.

    ``outbox`` is the hive's outbox policy (drafting needs it enabled) and ``milestones`` its
    milestone switches and completion policy; both default to off. ``leak_policy``, ``sanitizer``
    and ``posture`` have no default, as the sender's: ``sanitizer=None`` holds every drafted record
    (fail closed) and a published store refuses a draft. ``verifier`` is bound for the release
    trigger only (without one ``on_release`` refuses)."""

    def __init__(
        self,
        store: ReportStore,
        lock: IntakeLock,
        *,
        ids: IdSource,
        leak_policy: LeakPolicy,
        sanitizer: Sanitizer | None,
        posture: StorePosture,
        outbox: OutboxPolicy | None = None,
        milestones: MilestonePolicy | None = None,
        verifier: ReleaseVerifier | None = None,
    ) -> None:
        self._store = store
        self._lock = lock
        self._ids = ids
        self._outbox = outbox or OutboxPolicy()
        self._ms = milestones or MilestonePolicy()
        self._leaks = leak_policy
        self._sanitizer = sanitizer
        self._posture = posture
        self._verifier = verifier

    # -- the merge trigger ---------------------------------------------------------------------

    def on_work_merged(self, fact: MergeFact) -> MilestoneOutcome:
        """Every report that tracks ``fact.work`` (open or closed): ``delivery:merged``, the
        merged fact, a ``merged`` record when drafting is on, then the completion policy."""
        items = [
            self._guarded(report, MERGED, lambda r=report: self._merged(r, fact))
            for report in self._store.trackers_of(fact.work)
        ]
        return MilestoneOutcome(tuple(items))

    def _merged(self, report: str, fact: MergeFact) -> MilestoneItem:
        view = self._store.get(report)
        if view is None or view.binding is None:
            return MilestoneItem(report, MERGED, note="not a bound report")
        if delivery_unowned(view.labels):
            return MilestoneItem(report, MERGED, note=_UNOWNED)
        binding = view.binding
        with self._lock.hold(binding_key(binding)):
            doc = self._store.read_outbox(report) or OutboxDocument()
            current = delivery_of(view.labels)
            new = delivery_after_merge(view.labels)
            if new is None:  # a re-run, an already released report, or never linked
                after, drafted, refused = self._catch_up(
                    doc,
                    view,
                    binding,
                    current,
                    MERGED,
                    lambda c: merged_draft(binding, fact, close=c),
                )
                if after is not doc:
                    self._store.write_outbox(report, after, expect_seq=doc.seq, actor=fact.actor)
                closed, failed = self._try_close(view, current, after, fact.actor)
                return MilestoneItem(
                    report,
                    MERGED,
                    drafted=drafted,
                    closed=closed,
                    note=_unchanged(current),
                    error=_joined(refused, failed),
                )
            after, drafted, refused = self._draft(
                doc, view, binding, lambda c: merged_draft(binding, fact, close=c), MERGED
            )
            after = fold_facts(doc, after, merged_facts(doc, fact.work, fact.commit))
            if after is not doc:
                self._store.write_outbox(report, after, expect_seq=doc.seq, actor=fact.actor)
            self._store.set_state(
                report,
                DELIVERY_DIM,
                new,
                reason=f"work {fact.work} merged at {fact.commit[:12]}",
                actor=fact.actor,
            )
            closed, failed = self._try_close(view, new, after, fact.actor)
        return MilestoneItem(
            report,
            MERGED,
            delivery=new,
            drafted=drafted,
            closed=closed,
            error=_joined(refused, failed),
        )

    # -- the release trigger -------------------------------------------------------------------

    def on_release(self, fact: ReleaseFact) -> MilestoneOutcome:
        """Every report at ``delivery:merged`` (open or closed) whose merge the verifier shows in
        ``fact.commit``: ``delivery:released``, the released fact, a ``released`` record when
        drafting is on, then the completion policy. Open reports already at ``delivery:released``
        only get the completion policy again (a crash between the dimension and the close)."""
        verifier = self._verifier
        if verifier is None:
            raise IntakeRefused(
                RefusalReason.INTAKE_DISABLED,
                "no release verifier is bound, so a release cannot be verified",
            )
        verdicts: dict[str, Verdict] = {}

        def verdict_of(merge_commit: str) -> Verdict:
            if merge_commit not in verdicts:
                verdicts[merge_commit] = verifier.contains(fact.commit, merge_commit)
            return verdicts[merge_commit]

        items: list[MilestoneItem] = []
        for view in self._store.bound_reports():
            value = delivery_of(view.labels)
            if value == MERGED or (value == RELEASED and view.status != "closed"):
                items.append(
                    self._guarded(
                        view.id, RELEASED, lambda v=view: self._released(v, fact, verdict_of)
                    )
                )
        return MilestoneOutcome(tuple(items))

    def _released(
        self, view: BeadView, fact: ReleaseFact, verdict_of: Callable[[str], Verdict]
    ) -> MilestoneItem:
        report, binding = view.id, view.binding
        if binding is None:
            return MilestoneItem(report, RELEASED, note="not a bound report")
        if delivery_unowned(view.labels):
            return MilestoneItem(report, RELEASED, note=_UNOWNED)
        with self._lock.hold(binding_key(binding)):
            doc = self._store.read_outbox(report) or OutboxDocument()
            current = delivery_of(view.labels)
            if current == RELEASED:
                after, drafted, refused = self._catch_up(
                    doc,
                    view,
                    binding,
                    current,
                    RELEASED,
                    lambda c: released_draft(binding, fact, close=c),
                )
                if after is not doc:
                    self._store.write_outbox(report, after, expect_seq=doc.seq, actor=fact.actor)
                closed, failed = self._try_close(view, current, after, fact.actor)
                return MilestoneItem(
                    report,
                    RELEASED,
                    drafted=drafted,
                    closed=closed,
                    note=_unchanged(current),
                    error=_joined(refused, failed),
                )
            merged = doc.facts.merged
            if doc.facts.unlinked is not None:
                return MilestoneItem(
                    report,
                    RELEASED,
                    verdict=Verdict.UNKNOWN,
                    note="not verified: the report was unlinked from the work it described",
                )
            verdict = Verdict.UNKNOWN if merged is None else verdict_of(merged.commit)
            new = delivery_after_release(view.labels, verdict)
            if new is None:
                return MilestoneItem(
                    report, RELEASED, verdict=verdict, note=_not_verified(verdict, fact.tag)
                )
            after, drafted, refused = doc, "", ""
            if view.status != "closed" or self._ms.append_release_to_closed:
                after, drafted, refused = self._draft(
                    doc,
                    view,
                    binding,
                    lambda c: released_draft(binding, fact, close=c),
                    RELEASED,
                    closed=view.status == "closed",
                )
            after = fold_facts(doc, after, released_facts(doc, fact.tag, fact.commit))
            if after is not doc:
                self._store.write_outbox(report, after, expect_seq=doc.seq, actor=fact.actor)
            self._store.set_state(
                report,
                DELIVERY_DIM,
                new,
                reason=f"verified in release {fact.tag} at {fact.commit[:12]}",
                actor=fact.actor,
            )
            closed, failed = self._try_close(view, new, after, fact.actor)
        return MilestoneItem(
            report,
            RELEASED,
            delivery=new,
            drafted=drafted,
            verdict=verdict,
            closed=closed,
            error=_joined(refused, failed),
        )

    # -- catching up a report linked to work that already merged (bh-61xkc) --------------------

    def on_linked(self, report: str, work: str, actor: str) -> MilestoneOutcome:
        """``report`` now tracks ``work``; when that work has already merged (or released), give
        the report the milestones it missed, through the SAME steps the triggers take: the merged
        fact, ``delivery:merged`` and a ``merged`` record when drafting is on, then (when the
        release was verified) ``delivery:released`` and a ``released`` record, then the completion
        policy. Without this a report accepted late onto merged work stays ``delivery:pending``
        forever. A rejected report (a ``duplicate-of`` ends in one) is never caught up: its
        delivery is its canonical report's, so the trigger it reaches through ``_merged`` reports
        that and stops.

        The merge (and release) is the one already recorded on another report that tracks
        ``work`` (its ``facts.merged`` names ``work``); a release counts only where that report
        reached ``delivery:released`` through a ``YES`` verdict, so nothing unverified is assumed.
        Work that is not closed, or closed with no recorded merge (no report tracked it when it
        landed), changes nothing and is reported. Never sends: drafts only, like the triggers."""
        target = self._store.get(work)
        if target is None or target.status != "closed":
            return MilestoneOutcome()
        merged, released = self._recorded_delivery(work, exclude=report)
        if merged is None:
            note = (
                f"{work} is closed but no report that tracks it recorded its merge, so the merge "
                "commit is unknown; delivery stays pending"
            )
            return MilestoneOutcome((MilestoneItem(report, MERGED, note=note),))
        fact = MergeFact(work, merged.commit, actor)
        items = [self._guarded(report, MERGED, lambda: self._merged(report, fact))]
        if released is not None:
            items.append(
                self._guarded(
                    report,
                    RELEASED,
                    lambda: self._caught_up_release(report, merged, released, actor),
                )
            )
        return MilestoneOutcome(tuple(items))

    def _recorded_delivery(
        self, work: str, *, exclude: str
    ) -> tuple[MergedFact | None, ReleasedFact | None]:
        """The merge of ``work`` another tracking report recorded, and its verified release."""
        for other in self._store.trackers_of(work):
            if other == exclude:
                continue
            doc = self._store.read_outbox(other)
            merged = doc.facts.merged if doc is not None else None
            if doc is None or merged is None or merged.work != work:
                continue
            view = self._store.get(other)
            released = doc.facts.released
            verified = (
                released is not None
                and released.verified
                and view is not None
                and delivery_of(view.labels) == RELEASED
            )
            return merged, (released if verified else None)
        return None, None

    def _caught_up_release(
        self, report: str, merged: MergedFact, released: ReleasedFact, actor: str
    ) -> MilestoneItem:
        view = self._store.get(report)
        if view is None:
            return MilestoneItem(report, RELEASED, note="not a bound report")

        def verdict_of(merge_commit: str) -> Verdict:
            # the release was verified to contain exactly this merge on the other report
            return Verdict.YES if merge_commit == merged.commit else Verdict.UNKNOWN

        return self._released(view, ReleaseFact(released.tag, released.commit, actor), verdict_of)

    # -- the progress milestones (bh-dev42): started, blocked, deprioritised -----------------

    def on_work_started(self, fact: StartFact) -> MilestoneOutcome:
        """The claim hook: a ``started`` record for every report (open or closed) that tracks
        ``fact.work`` and whose delivery is still ``pending``. Keyed on the work, so only the
        FIRST claim drafts: a re-claim, a resume after a bounce or a claim after an abandon finds
        the key and drafts nothing (O2). Free (no store read at all) unless the outbox and
        ``draft_on_start`` are both on. Never changes a dimension, never closes a report."""
        if not (self._outbox.enabled and self._ms.drafts(STARTED)):
            return MilestoneOutcome()
        items = [
            self._guarded(
                report,
                STARTED,
                lambda r=report: self._progress(r, STARTED, lambda b: started_draft(b, fact)),
            )
            for report in self._store.trackers_of(fact.work)
        ]
        return MilestoneOutcome(tuple(items))

    def delivery_notice(self, fact: NoticeFact) -> MilestoneOutcome:
        """``bh work delivery blocked|deprioritised``: draft the operator's public ``reason``
        under the event's fixed wording for every report (open or closed) that tracks
        ``fact.work`` and whose delivery is still ``pending``. The leak check runs on the drafted
        text, so a reason naming a bead id (or a branch, a path, a seat) leaves the record
        ``held`` for the operator to fix or deliberately publish. Refused before any write for an
        event that is not a notice, a reason that cannot be posted, the outbox or the event's
        switch off, or work this store does not hold."""
        if fact.event not in NOTICE_EVENTS:
            raise IntakeRefused(
                RefusalReason.INVALID_REQUEST,
                f"{fact.event.value!r} is not an operator delivery notice (blocked, deprioritised)",
            )
        problem = notice_reason_problem(fact.reason)
        if problem:
            reason = (
                RefusalReason.REASON_REQUIRED
                if not fact.reason.strip()
                else RefusalReason.INVALID_DRAFT
            )
            raise IntakeRefused(reason, problem)
        if not self._outbox.enabled:
            raise IntakeRefused(
                RefusalReason.OUTBOX_DISABLED,
                "intake.external.outbox.enabled is off (the default), so nothing can be drafted",
            )
        event = fact.event.value
        if not self._ms.drafts(event):
            raise IntakeRefused(
                RefusalReason.INTAKE_DISABLED,
                f"intake.external.milestones.draft_on_{event} is off (the default)",
            )
        if self._store.get(fact.work) is None:
            raise IntakeRefused(RefusalReason.NOT_FOUND, f"no work {fact.work!r} in this store")
        items = [
            self._guarded(
                report,
                event,
                lambda r=report: self._progress(r, event, lambda b: notice_draft(b, fact)),
            )
            for report in self._store.trackers_of(fact.work)
        ]
        return MilestoneOutcome(tuple(items))

    def _progress(
        self, report: str, event: str, request: Callable[[IssueBinding], DraftRequest]
    ) -> MilestoneItem:
        """One report of a progress milestone: draft ``request(binding)`` in ONE document write
        when the report's delivery is ``pending``; anything else is reported and left alone."""
        view = self._store.get(report)
        if view is None or view.binding is None:
            return MilestoneItem(report, event, note="not a bound report")
        if delivery_unowned(view.labels):
            return MilestoneItem(report, event, note=_UNOWNED)
        binding = view.binding
        current = delivery_of(view.labels)
        if current != PROGRESS_DELIVERY:
            return MilestoneItem(report, event, note=_no_progress(current, event))
        draft = request(binding)
        with self._lock.hold(binding_key(binding)):
            doc = self._store.read_outbox(report) or OutboxDocument()
            after, drafted, refused = self._draft(doc, view, binding, lambda _c: draft, event)
            if after is not doc:
                self._store.write_outbox(report, after, expect_seq=doc.seq, actor=draft.actor)
        note = "" if drafted or refused else f"a {event} record for this work already exists"
        return MilestoneItem(
            report,
            event,
            drafted=drafted,
            held=bool(drafted) and _is_held(after, drafted),
            note=note,
            error=refused,
        )

    # -- the completion policy -----------------------------------------------------------------

    def complete(self, report: str, actor: str) -> bool:
        """Evaluate the completion policy for ``report`` now (the sender calls this after a
        milestone record settles). True when the report was closed by this call."""
        view = self._store.get(report)
        if view is None or view.binding is None:
            return False
        with self._lock.hold(binding_key(view.binding)):
            doc = self._store.read_outbox(report) or OutboxDocument()
            return self._close_if_complete(view, delivery_of(view.labels), doc, actor)

    def _try_close(
        self, view: BeadView, delivery: str | None, doc: OutboxDocument, actor: str
    ) -> tuple[bool, str]:
        """The completion policy as a trigger step: a refused or failed close (a live blocker
        besides the hold, a store error) is reported with the dimension already written."""
        try:
            return self._close_if_complete(view, delivery, doc, actor), ""
        except (IntakeRefused, IntakeStoreError) as exc:
            return False, f"not closed: {_why(exc)}"

    def _close_if_complete(
        self, view: BeadView, delivery: str | None, doc: OutboxDocument, actor: str
    ) -> bool:
        """The close rule (``bh-6wj9y`` R3). Never closes a report that is closed, still awaiting
        triage (untriaged, or waiting for its reporter on ``needs-info``), or no longer tracking
        the work its delivery describes."""
        if view.status == "closed" or is_awaiting_triage(view.labels):
            return False
        if doc.facts.unlinked is not None:
            return False
        done = machine.close_rule(
            doc,
            delivery=delivery,
            completion=self._ms.completion,
            outbox_enabled=self._records_expected(),
        )
        if not done:
            return False
        self._store.close(view.id, reason=f"delivery complete ({delivery})", actor=actor)
        return True

    def _records_expected(self) -> bool:
        """Whether the completion milestone's record is expected to exist (the close rule's
        ``outbox_enabled``). With the outbox on the completion milestone ALWAYS drafts its record
        (bh-cir2q), whatever ``draft_on_<event>`` says for progress notices, so the report waits
        for it; with the outbox off nothing is drafted and the report completes at once."""
        return self._outbox.enabled

    # -- drafting ------------------------------------------------------------------------------

    def _catch_up(
        self,
        doc: OutboxDocument,
        view: BeadView,
        binding: IssueBinding,
        delivery: str | None,
        event: str,
        request: Callable[[CloseReason | None], DraftRequest],
    ) -> tuple[OutboxDocument, str, str]:
        """A re-run trigger for an OPEN report whose delivery already completed it but whose
        completion record was never drafted (it completed before the record was drafted for
        every hive, or a draft was refused): draft it now, so the report is not left open for
        nothing. A record of the event that exists (any state) is never drafted again."""
        policy = self._ms.completion
        if (
            view.status == "closed"
            or event != completion_event(policy)
            or not delivery_complete(policy, delivery)
            or any(r.event.value == event for r in doc.records.values())
        ):
            return doc, "", ""
        return self._draft(doc, view, binding, request, event)

    def _completion_close(
        self, doc: OutboxDocument, event: str, *, closed: bool
    ) -> CloseReason | None:
        """The close a milestone record carries (bh-cir2q), or ``None``: the record of the
        completion milestone closes the issue (reason completed) while the report is still open
        and the tracker enables ``issue.close``. A ``released`` record under close-on-merge also
        carries it while the ``merged`` record's close is still unsent, since it supersedes that
        record and the close must not be lost with it."""
        if closed or not self._outbox.close_enabled:
            return None
        policy = self._ms.completion
        if event == completion_event(policy):
            return COMPLETION_CLOSE
        if event == RELEASED and policy is CompletionPolicy.CLOSE_ON_MERGE:
            for record in doc.records.values():
                op = record.op(OpKind.CLOSE)
                if (
                    record.event.value == MERGED
                    and op is not None
                    and op.state not in machine.TERMINAL
                ):
                    return COMPLETION_CLOSE
        return None

    def _draft(
        self,
        doc: OutboxDocument,
        view: BeadView,
        binding: IssueBinding,
        request: Callable[[CloseReason | None], DraftRequest],
        event: str,
        *,
        closed: bool = False,
    ) -> tuple[OutboxDocument, str, str]:
        """O1 for a milestone record: ``(document, outbox id of a NEW record or "", refusal text
        or "")``. Drafted when this hive drafts ``event`` as a progress notice, and ALWAYS for the
        completion milestone (with its close, :meth:`_completion_close`). A refusal (a published
        store, a public commit link) leaves the document as it was and is reported; it never stops
        the dimension."""
        completes = event == completion_event(self._ms.completion) and not closed
        if not (self._outbox.enabled and (self._ms.drafts(event) or completes)):
            return doc, "", ""
        try:
            require_private_store(self._posture)
            applied = machine.draft(
                doc,
                request(self._completion_close(doc, event, closed=closed)),
                outbox_id=self._ids.outbox_id(),
                policy=self._outbox,
                ctx=self._ctx(view, binding),
            )
        except IntakeRefused as exc:
            return doc, "", f"no {event} record drafted: {_why(exc)}"
        return applied.document, (applied.outbox_id if applied.changed else ""), ""

    def _ctx(self, view: BeadView, binding: IssueBinding) -> RenderContext:
        thread = self._store.read_thread(view.id)
        return RenderContext(
            binding.url,
            self._leaks,
            self._sanitizer,
            thread_missing=thread is not None and thread.state is ThreadState.MISSING,
        )

    # -- one report's failure never hides the rest ----------------------------------------------

    @staticmethod
    def _guarded(report: str, event: str, step: Callable[[], MilestoneItem]) -> MilestoneItem:
        try:
            return step()
        except (IntakeRefused, IntakeStoreError) as exc:
            return MilestoneItem(report, event, error=_why(exc))


_UNOWNED = (
    "rejected or closed as a duplicate: its delivery is not its own, so no milestone is drafted"
)


def _joined(*parts: str) -> str:
    return "; ".join(part for part in parts if part)


def _unchanged(delivery: str | None) -> str:
    if delivery is None:
        return "no delivery state (not linked with `link`); nothing to advance"
    return f"already delivery:{delivery}; nothing to advance"


def _no_progress(delivery: str | None, event: str) -> str:
    if delivery is None:
        return f"no delivery state (not linked with `link`); no {event} record drafted"
    return f"already delivery:{delivery}; a {event} record would be stale news, none drafted"


def _is_held(doc: OutboxDocument, outbox_id: str) -> bool:
    record = doc.records.get(outbox_id)
    op = record.op(OpKind.COMMENT) if record is not None else None
    return op is not None and op.state is OpState.HELD


def _not_verified(verdict: Verdict, tag: str) -> str:
    if verdict is Verdict.NO:
        return f"not verified: its merge is not in {tag}; delivery stays merged"
    return f"not verified: could not establish whether its merge is in {tag}; delivery stays merged"


__all__ = ["MilestoneService"]
