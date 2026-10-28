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

**Nothing here sends.** This service holds no tracker, no grant and no writer: the most a milestone
can do is leave a ``drafted`` (or ``held``) record for an operator to render, approve and send
through the supervised sender. It is free to call from the work layer: a failure for one report is
returned in its :class:`MilestoneItem` and the next report is still handled.
"""

from __future__ import annotations

from collections.abc import Callable

from ..contracts import IdSource, IntakeLock, ReleaseVerifier, ReportStore, Sanitizer
from ..domain import (
    DELIVERY_DIM,
    BeadView,
    DraftRequest,
    IntakeRefused,
    IntakeStoreError,
    IssueBinding,
    LeakPolicy,
    MergeFact,
    MilestoneItem,
    MilestoneOutcome,
    MilestonePolicy,
    OutboxDocument,
    OutboxPolicy,
    RefusalReason,
    ReleaseFact,
    RenderContext,
    StorePosture,
    ThreadState,
    Verdict,
    completion_event,
    delivery_after_merge,
    delivery_after_release,
    delivery_of,
    fold_facts,
    is_untriaged_intake,
    merged_draft,
    merged_facts,
    released_draft,
    released_facts,
    require_private_store,
)
from ..domain import outbox as machine
from .threads import binding_key

MERGED = "merged"
RELEASED = "released"


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
        binding = view.binding
        with self._lock.hold(binding_key(binding)):
            doc = self._store.read_outbox(report) or OutboxDocument()
            current = delivery_of(view.labels)
            new = delivery_after_merge(view.labels)
            if new is None:  # a re-run, an already released report, or never linked
                closed, failed = self._try_close(view, current, doc, fact.actor)
                return MilestoneItem(
                    report, MERGED, closed=closed, note=_unchanged(current), error=failed
                )
            after, drafted, refused = self._draft(
                doc, view, binding, merged_draft(binding, fact), MERGED
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
        with self._lock.hold(binding_key(binding)):
            doc = self._store.read_outbox(report) or OutboxDocument()
            current = delivery_of(view.labels)
            if current == RELEASED:
                closed, failed = self._try_close(view, current, doc, fact.actor)
                return MilestoneItem(
                    report, RELEASED, closed=closed, note=_unchanged(current), error=failed
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
                    doc, view, binding, released_draft(binding, fact), RELEASED
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
        """The close rule (``bh-6wj9y`` R3). Never closes a report that is closed, still
        untriaged, or no longer tracking the work its delivery describes."""
        if view.status == "closed" or is_untriaged_intake(view.labels):
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
        ``outbox_enabled``). With the outbox off, or drafting off for that milestone, nothing is
        ever drafted for it, so the report would wait forever: it completes as with no outbox."""
        return self._outbox.enabled and self._ms.drafts(completion_event(self._ms.completion))

    # -- drafting ------------------------------------------------------------------------------

    def _draft(
        self,
        doc: OutboxDocument,
        view: BeadView,
        binding: IssueBinding,
        request: DraftRequest,
        event: str,
    ) -> tuple[OutboxDocument, str, str]:
        """O1 for a milestone record when this hive drafts ``event``: ``(document, outbox id of a
        NEW record or "", refusal text or "")``. A refusal (a published store, a public commit
        link) leaves the document as it was and is reported; it never stops the dimension."""
        if not (self._outbox.enabled and self._ms.drafts(event)):
            return doc, "", ""
        try:
            require_private_store(self._posture)
            applied = machine.draft(
                doc,
                request,
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


def _joined(*parts: str) -> str:
    return "; ".join(part for part in parts if part)


def _unchanged(delivery: str | None) -> str:
    if delivery is None:
        return "no delivery state (not linked with `link`); nothing to advance"
    return f"already delivery:{delivery}; nothing to advance"


def _not_verified(verdict: Verdict, tag: str) -> str:
    if verdict is Verdict.NO:
        return f"not verified: its merge is not in {tag}; delivery stays merged"
    return f"not verified: could not establish whether its merge is in {tag}; delivery stays merged"


__all__ = ["MilestoneService"]
