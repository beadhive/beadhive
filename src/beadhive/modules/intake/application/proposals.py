"""``ProposalService``: propose, review and apply-reviewed (bh-9aqmv, epic bh-hrx2r).

**propose** stores an analyst's proposal on a report (``intake.proposal``): the input codec
(:func:`~..domain.proposal_document.content_from_document`) and the leak lint of its
``reporter_text`` run first, so a refusal writes nothing. Over an open proposal it replaces the
content and keeps the reviews (un-verified: it wants a fresh review); over a stale one it writes a
fresh one. An applied proposal is final, with one exception: a report applied as ``needs-info``
that its reporter woke (back on ``intake:untriaged``, or its thread advanced past the seq the
applied round read) starts a NEW round, the applied record kept unchanged in ``history``.

**review** records a verifier's verdict (:func:`~..domain.proposal_document.verdict_from_document`):
``confirmed`` verifies the proposal; ``corrected`` un-verifies it, unless the verdict carries the
corrected content, which is adopted in the same write and verifies it (after the same leak lint).

**apply_reviewed** runs EVERY pre-apply check first (:func:`~..domain.proposal_checks.check_run`),
then takes the open proposals canonicals first (:func:`~..domain.triage.apply_order`) and applies
only those whose last review confirmed them or whose correction they adopted, with no finding.
Each maps onto the existing triage verbs (:func:`~..domain.triage.plan_apply`) through the
:class:`TriageActions` it is given (the :class:`~.service.IntakeService` itself), so every
refusal those verbs make still holds. A reply or a question is DRAFTED in the outbox and never
sent; a reply that ends the thread (``fixed``, ``reject``, ``duplicate-of``) asks for the issue's
close on the same record, which the sender drafts only while the close gate is on and sends only
after the reply and the report's labels. An applied proposal is skipped, so a second apply
changes nothing. With the label projection bound (``intake.external.labels.enabled``) the touched
reports' labels are refreshed (drafted, like every label change). ``dry_run`` checks and plans
and writes nothing.

The repository facts, the landed commits, the integration branch, the leak policy and the
reroute re-filer come in as a :class:`TriageBinding` from the composition root: this module reads
no configuration, git or clock.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from ..contracts import LandedCommits, ProjectIssues, RepoFacts, ReportStore, Sanitizer
from ..domain import (
    DEFAULT_REPORTER_TEXT_LIMIT,
    INTAKE_UNTRIAGED,
    ApplyItem,
    ApplyOutcome,
    ApplyPlan,
    ApplyRequest,
    ApplyStatus,
    ApplyStep,
    BeadView,
    CheckContext,
    CloseReason,
    ConsiderRequest,
    Disposition,
    DispositionOutcome,
    DispositionRequest,
    IntakeRefused,
    IntakeStoreError,
    LabelSyncOutcome,
    LabelSyncRequest,
    LeakPolicy,
    LinkOutcome,
    LinkRequest,
    MentionFact,
    NeedsInfoOutcome,
    NeedsInfoRequest,
    OutboxResult,
    Proposal,
    ProposalCheck,
    ProposalDisposition,
    ProposalDocumentError,
    ProposalReviewOutcome,
    ProposalReviewRequest,
    ProposalStatus,
    ProposeKind,
    ProposeOutcome,
    ProposeRequest,
    RefusalReason,
    ReplyDraft,
    TargetFact,
    ThreadOutcome,
    add_review,
    amend,
    apply_order,
    check_proposal,
    check_run,
    content_from_document,
    content_seq,
    duplicate_text,
    evidence_after,
    fixed_text,
    half_rejected,
    keeps_open,
    leak_check,
    mark_applied,
    may_disposition,
    may_reopen,
    mentions,
    new_proposal,
    new_round,
    plan_apply,
    reopen_text,
    verdict_from_document,
    verification_gap,
)
from ..domain.leaks import unresolved
from ..domain.proposal_checks import CLOSED
from .labels import LabelProjection

#: The root's re-filing of a rerouted report into the hive it belongs in: it receives the report
#: (read once, before anything is written) and its proposal, files it elsewhere and returns the
#: reason to record; it raises to abort, leaving the report untouched.
Refile = Callable[[BeadView, Proposal], str]


@dataclass(frozen=True)
class TriageBinding:
    """What propose, review and apply read besides the store, bound by the composition root.

    ``repo`` answers the checks' repository questions; ``landed`` the commits that landed a closed
    work bead (``None``: none known, so a ``fixed`` claim needs its own ``fix_commit``);
    ``branch`` is the integration branch; ``leak_policy`` and ``sanitizer`` lint the
    reporter-visible text (``None``: no sanitizer, so every reporter text is refused, fail
    closed); ``refile`` re-files a rerouted report (``None``: a reroute is blocked);
    ``issues`` lists the bound tracker project's issues for the mention check (``bh-iamru``;
    ``None``: no tracker issue read is bound, so a ``reporter_text`` naming a ``#N`` cannot pass
    the checks)."""

    repo: RepoFacts
    leak_policy: LeakPolicy
    sanitizer: Sanitizer | None
    branch: str = "main"
    landed: LandedCommits | None = None
    refile: Refile | None = None
    reporter_text_limit: int = DEFAULT_REPORTER_TEXT_LIMIT
    issues: ProjectIssues | None = None


class TriageActions(Protocol):
    """The triage verbs apply runs (``IntakeService`` provides them all)."""

    def dispose(self, request: DispositionRequest) -> DispositionOutcome: ...

    def link(self, request: LinkRequest) -> LinkOutcome: ...

    def needs_info(self, request: NeedsInfoRequest) -> NeedsInfoOutcome: ...

    def mark_considered(self, request: ConsiderRequest) -> ThreadOutcome: ...

    def draft_reply(self, request: ReplyDraft) -> OutboxResult: ...

    def draft_reopen(self, report: str, text: str, actor: str) -> OutboxResult | None: ...

    def reroute_elsewhere(
        self, request: DispositionRequest, refile: Callable[[BeadView], str]
    ) -> DispositionOutcome: ...


def triage_off() -> IntakeRefused:
    return IntakeRefused(
        RefusalReason.INTAKE_DISABLED,
        "triage proposals are not bound for this hive (they need the repository facts and the "
        "leak policy; external intake is opt-in)",
    )


@dataclass(frozen=True)
class _Done:
    """A run's record of a report applied (or planned) before its family members."""

    work: str
    reference: str
    planned: bool = False  # a dry run's canonical: its new work does not exist yet


class ProposalService:
    """Propose, review and apply-reviewed over one :class:`ReportStore`.

    ``actions`` runs the triage verbs; ``binding`` is ``None`` when triage proposals are not bound
    (every operation refuses with ``INTAKE_DISABLED``); ``projection`` is the label projection,
    bound only when labels are enabled."""

    def __init__(
        self,
        store: ReportStore,
        actions: TriageActions,
        binding: TriageBinding | None,
        *,
        projection: LabelProjection | None = None,
        replies: bool = True,
    ) -> None:
        self._store = store
        self._actions = actions
        self._binding = binding
        self._projection = projection
        #: an outbox is bound, so apply can draft a duplicate's reply (``plan_apply``)
        self._replies = replies

    def _require_binding(self) -> TriageBinding:
        if self._binding is None:
            raise triage_off()
        return self._binding

    def _lint(self, text: str, binding: TriageBinding) -> None:
        """Refuse (``UNRESOLVED_FINDING``) reporter text that would leak, before any write."""
        if not text:
            return
        leaks = unresolved(leak_check(text, binding.leak_policy, sanitizer=binding.sanitizer))
        if leaks:
            named = ", ".join(f"{f.id} {f.match!r}" for f in leaks)
            raise IntakeRefused(
                RefusalReason.UNRESOLVED_FINDING,
                f"reporter_text would leak ({named}); nothing was written: rewrite it without "
                "internal ids, labels, seats, paths or secrets",
            )

    def _view(self, report: str) -> BeadView:
        view = self._store.get(report)
        if view is None:
            raise IntakeRefused(RefusalReason.NOT_FOUND, f"{report} not found")
        return view

    # -- propose -------------------------------------------------------------------------------

    def propose(self, request: ProposeRequest) -> ProposeOutcome:
        """Store the analyst's proposal on ``request.report`` (see the module docstring)."""
        binding = self._require_binding()
        try:
            content = content_from_document(request.document)
        except ProposalDocumentError as exc:
            raise IntakeRefused(RefusalReason.PROPOSAL_INVALID, str(exc)) from exc
        self._lint(content["reporter_text"], binding)
        view = self._view(request.report)
        stored = self._store.read_proposal(request.report)
        if stored is None:
            kind, proposal = ProposeKind.CREATED, new_proposal(**content)
        elif stored.status is ProposalStatus.APPLIED:
            if not may_reopen(stored):
                raise IntakeRefused(
                    RefusalReason.PROPOSAL_IMMUTABLE,
                    f"{request.report}'s proposal was applied as {stored.disposition.value}; "
                    "that outcome is final",
                )
            if not self._woken(view, stored):
                raise IntakeRefused(
                    RefusalReason.PROPOSAL_TRANSITION,
                    f"{request.report} was applied as needs-info and its reporter has not "
                    "answered yet; a new round starts once it is woken",
                )
            kind, proposal = ProposeKind.NEW_ROUND, new_round(stored, new_proposal(**content))
        elif stored.status is ProposalStatus.STALE:
            fresh = new_proposal(**content)
            proposal = dataclasses.replace(fresh, seq=stored.seq + 1, history=stored.history)
            kind = ProposeKind.REPLACED
        else:
            kind, proposal = ProposeKind.AMENDED, amend(stored, **content)
        expect = stored.seq if stored is not None else 0
        self._store.write_proposal(request.report, proposal, expect_seq=expect, actor=request.actor)
        return ProposeOutcome(request.report, kind, proposal)

    def _woken(self, view: BeadView, applied: Proposal) -> bool:
        """A report applied as needs-info that its reporter woke: back on ``intake:untriaged``,
        or reporter evidence changed after the seq the applied round read. Apply's own recorded
        read moves the thread's seq, and the question it asked is an own comment: neither wakes
        it (bh-yu6sy)."""
        if INTAKE_UNTRIAGED in view.labels:
            return True
        thread = self._store.read_thread(view.id)
        return thread is not None and bool(evidence_after(thread, applied.thread_seq))

    # -- review --------------------------------------------------------------------------------

    def review(self, request: ProposalReviewRequest) -> ProposalReviewOutcome:
        """Record the verifier's verdict on ``request.report``'s proposal (see the module
        docstring). A verdict naming a pass that is not the next one is refused, so the same
        verdict recorded twice writes once."""
        binding = self._require_binding()
        try:
            verdict = verdict_from_document(request.document)
        except ProposalDocumentError as exc:
            raise IntakeRefused(RefusalReason.PROPOSAL_INVALID, str(exc)) from exc
        if verdict.adopt is not None:
            self._lint(verdict.adopt["reporter_text"], binding)
        self._view(request.report)
        stored = self._store.read_proposal(request.report)
        if stored is None:
            raise IntakeRefused(
                RefusalReason.NOT_FOUND, f"{request.report} has no proposal to review"
            )
        following = len(stored.reviews) + 1
        if verdict.pass_no and verdict.pass_no != following:
            raise IntakeRefused(
                RefusalReason.PROPOSAL_MOVED,
                f"the verdict is for review pass {verdict.pass_no}, but {request.report}'s next "
                f"pass is {following}; re-read the proposal and review it again",
            )
        updated = add_review(
            stored,
            verdict.seat or request.actor,
            verdict.verdict,
            verdict.corrections,
            adopt=verdict.adopt,
        )
        self._store.write_proposal(
            request.report, updated, expect_seq=stored.seq, actor=request.actor
        )
        return ProposalReviewOutcome(request.report, updated, adopted=verdict.adopt is not None)

    # -- apply ---------------------------------------------------------------------------------

    def _stored(self) -> dict[str, Proposal]:
        found = {
            view.id: self._store.read_proposal(view.id)
            for view in self._store.reports_with_proposal()
        }
        return {r: p for r, p in sorted(found.items()) if p is not None}

    def _context(self, run: Mapping[str, Proposal], binding: TriageBinding) -> CheckContext:
        """The facts every check of ``run`` reads: each target's status (and, for a closed one,
        its landed commits) from the store, each report's current thread seq."""
        targets: dict[str, TargetFact] = {}
        for target in sorted({t.id for p in run.values() for t in p.targets}):
            view = self._store.get(target)
            if view is None:
                continue  # missing: the checks report it
            landed = (
                binding.landed.landed_commits(target)
                if binding.landed is not None and view.status == CLOSED
                else ()
            )
            targets[target] = TargetFact(
                target,
                view.status,
                tuple(landed),
                bound=view.binding is not None,
                untriaged=INTAKE_UNTRIAGED in view.labels,
            )
        seqs: dict[str, int] = {}
        for report in run:
            thread = self._store.read_thread(report)
            if thread is not None:  # a triage read moves seq, not the comments (bh-yu6sy)
                seqs[report] = content_seq(thread)
        issues, gap = self._issues(run, binding)
        return CheckContext(
            branch=binding.branch,
            repo=binding.repo,
            leak_policy=binding.leak_policy,
            sanitizer=binding.sanitizer,
            targets=targets,
            thread_seqs=seqs,
            reporter_text_limit=binding.reporter_text_limit,
            issues=issues,
            issues_gap=gap,
        )

    def _issues(
        self, run: Mapping[str, Proposal], binding: TriageBinding
    ) -> tuple[dict[str, MentionFact] | None, str]:
        """The bound project's issues, each with the bound report it is (``bh-iamru``), read
        ONLY when some ``reporter_text`` of ``run`` mentions a ``#N``: one tracker listing and one
        store listing for the whole run, never a request per mention. ``(None, why)`` when they
        cannot be read, which the checks report as unverified (fail closed)."""
        if not any(mentions(p.reporter_text) for p in run.values()):
            return None, ""
        if binding.issues is None:
            return None, "no tracker with issues.read is bound"
        try:
            listed = binding.issues.identifiers()
            bound = {
                view.binding.url: view.id
                for view in self._store.bound_reports()
                if view.binding is not None
            }
        except IntakeRefused as exc:
            return None, exc.detail
        except IntakeStoreError as exc:
            return None, f"the bound reports could not be listed ({exc.operation})"
        return {
            identifier: MentionFact(identifier, url, bound.get(url, ""))
            for identifier, url in listed.items()
        }, ""

    def apply_reviewed(self, request: ApplyRequest) -> ApplyOutcome:
        """Check every open proposal, then apply the reviewed ones canonicals first (see the
        module docstring)."""
        binding = self._require_binding()
        stored = self._stored()
        applied_before = tuple(r for r, p in stored.items() if p.status is ProposalStatus.APPLIED)
        open_run = {
            r: p
            for r, p in stored.items()
            if p.status in (ProposalStatus.PROPOSED, ProposalStatus.VERIFIED)
        }
        # a member's canonical applied by an earlier run still resolves its family
        run = dict(open_run)
        for proposal in open_run.values():
            canonical = stored.get(proposal.share_with)
            if canonical is not None and canonical.status is ProposalStatus.APPLIED:
                run.setdefault(proposal.share_with, canonical)
        checks = check_run(run, self._context(run, binding))
        by_report = {c.report: c for c in checks}

        done: dict[str, _Done] = {}
        for report in applied_before:
            done[report] = _Done(self._store.tracked_work(report) or "", self._reference(report))
        items: list[ApplyItem] = []
        touched: list[str] = []
        for report in apply_order(open_run):
            item = self._apply_one(
                report, open_run[report], by_report[report], done, binding, request
            )
            items.append(item)
            if item.status in (ApplyStatus.APPLIED, ApplyStatus.PLANNED):
                planned = item.status is ApplyStatus.PLANNED
                done[report] = _Done(item.work, self._reference(report), planned)
            if item.status is ApplyStatus.APPLIED or (
                item.status is ApplyStatus.FAILED and item.steps
            ):
                touched.append(report)
        labels, labels_error = self._refresh_labels(touched, request)
        return ApplyOutcome(
            items=tuple(items),
            already_applied=applied_before,
            checks=tuple(c for c in checks if c.report in open_run),
            dry_run=request.dry_run,
            labels=labels,
            labels_error=labels_error,
        )

    def backfill_reply(self, report: str, proposal: Proposal) -> str:
        """The reply an applied ``proposal`` would have drafted for ``report`` (``""``: none can
        be composed now), for the close backfill of a report that never got one: its reviewed
        ``reporter_text``, else the template apply uses: ``duplicate_text`` naming the canonical's
        issue, or ``fixed_text`` with the fix version COMPUTED now (needs the triage binding; a
        claim whose checks no longer pass composes nothing). Reads only."""
        if proposal.reporter_text:
            return proposal.reporter_text
        if proposal.disposition is ProposalDisposition.DUPLICATE_OF:
            return duplicate_text(self._reference(proposal.share_with))
        if proposal.disposition is ProposalDisposition.FIXED and self._binding is not None:
            context = self._context({report: proposal}, self._binding)
            check = check_proposal(report, proposal, context)
            return fixed_text(check.computed_fixed_in) if check.passed else ""
        return ""

    def _reference(self, report: str) -> str:
        view = self._store.get(report)
        return view.binding.identifier if view is not None and view.binding is not None else ""

    def _apply_one(
        self,
        report: str,
        proposal: Proposal,
        check: ProposalCheck,
        done: Mapping[str, _Done],
        binding: TriageBinding,
        request: ApplyRequest,
    ) -> ApplyItem:
        base = ApplyItem(
            report,
            proposal.disposition,
            ApplyStatus.BLOCKED,
            share_with=proposal.share_with,
            fixed_in=check.computed_fixed_in,
            findings=check.findings,
            relates=check.relates,
        )
        if proposal.disposition is ProposalDisposition.DECISION:
            return _with(
                base, status=ApplyStatus.LISTED, detail="an operator decision: never applied"
            )
        gap = verification_gap(proposal)
        if gap:
            return _with(base, detail=gap)
        if not check.passed:
            failed = "; ".join(f.kind.value for f in check.findings)
            return _with(base, detail=f"pre-apply checks failed: {failed}")
        canonical: _Done | None = None
        if proposal.share_with:
            canonical = done.get(proposal.share_with)
            if canonical is None:
                return _with(
                    base,
                    detail=f"its canonical {proposal.share_with} is not applied, so there is no "
                    "work to share yet",
                )
        view = self._store.get(report)
        resuming = view is not None and half_rejected(view.status, view.labels)
        if view is None or view.status == CLOSED or not (resuming or may_disposition(view.labels)):
            return _with(
                base,
                detail=f"{report} is not a report awaiting triage (missing, closed or already "
                "fielded)",
            )
        plan = plan_apply(
            proposal,
            bound=view.binding is not None,
            fixed_in=check.computed_fixed_in,
            canonical_work=(canonical.work or None) if canonical is not None else None,
            canonical_ref=canonical.reference if canonical is not None else "",
            replies=self._replies,
        )
        if canonical is not None and canonical.planned and not canonical.work:
            # the canonical is only planned: its new work does not exist yet on a dry run
            planned = plan_apply(
                proposal,
                bound=view.binding is not None,
                canonical_work="(planned)",
                canonical_ref=canonical.reference,
                replies=self._replies,
            )
            assert isinstance(planned, ApplyPlan)  # a duplicate with canonical work always plans
            plan = dataclasses.replace(planned, work="")
        if isinstance(plan, str):
            return _with(base, detail=plan)
        if resuming:
            # an earlier run drafted the reply (and linked), set intake:rejected, then failed
            # to close (bh-mqv12): only the close is left
            if ApplyStep.REJECT not in plan.steps:
                return _with(
                    base,
                    detail=f"{report} is on intake:rejected but its proposal does not reject it",
                )
            plan = dataclasses.replace(plan, steps=(ApplyStep.REJECT,))
        if ApplyStep.REROUTE in plan.steps and binding.refile is None:
            return _with(
                base,
                steps=plan.steps,
                detail="a reroute needs the root's re-filer (where the report belongs); none is "
                "bound",
            )
        if request.dry_run:
            return _with(base, status=ApplyStatus.PLANNED, steps=plan.steps, work=plan.work)
        return self._run(base, view, proposal, plan, binding, request.actor)

    def _run(
        self,
        base: ApplyItem,
        view: BeadView,
        proposal: Proposal,
        plan: ApplyPlan,
        binding: TriageBinding,
        actor: str,
    ) -> ApplyItem:
        report = view.id
        work, drafted, milestones, ran = plan.work, [], (), []

        def refile(checked: BeadView) -> str:
            assert binding.refile is not None  # _apply_one blocked a reroute without one
            return binding.refile(checked, proposal)

        try:
            # first, and idempotent: a failure here leaves the report untouched (bh-iamru)
            for other in base.relates:
                self._store.relate(report, other, actor=actor)
        except (IntakeRefused, IntakeStoreError) as exc:
            return _with(
                base,
                status=ApplyStatus.FAILED,
                detail=f"recording relates-to failed: {_why(exc)}",
            )
        if self._replies and view.binding is not None and keeps_open(plan):
            # a reversed outcome (bh-ih484): the report stays tracked but its issue is closed, so
            # the reopen is drafted first (never sent), ahead of any reply that explains it
            try:
                reopened = self._actions.draft_reopen(report, reopen_text(), actor)
            except (IntakeRefused, IntakeStoreError) as exc:
                return _with(
                    base,
                    status=ApplyStatus.FAILED,
                    detail=f"drafting the reopen failed: {_why(exc)}",
                )
            if reopened is not None:
                drafted.append(reopened.outbox_id)
        try:
            for step in plan.steps:
                if step in (ApplyStep.ACCEPT, ApplyStep.ACCEPT_NEW_WORK, ApplyStep.ACCEPT_WORK):
                    outcome = self._actions.dispose(
                        DispositionRequest(
                            report,
                            Disposition.ACCEPT,
                            actor,
                            issue_type=proposal.type,
                            priority=proposal.priority,
                            work=plan.work if step is ApplyStep.ACCEPT_WORK else "",
                            new_work=step is ApplyStep.ACCEPT_NEW_WORK,
                        )
                    )
                    work, milestones = outcome.work, outcome.milestones
                elif step is ApplyStep.LINK:
                    # a duplicate shares its canonical's work, never its delivery (bh-5kpwc)
                    duplicate = proposal.disposition is ProposalDisposition.DUPLICATE_OF
                    linked = self._actions.link(
                        LinkRequest(report, plan.work, actor, duplicate=duplicate)
                    )
                    milestones = linked.milestones
                elif step is ApplyStep.DRAFT_REPLY:
                    # a duplicate's close names its canonical report; the sender resolves its
                    # bound issue (bh-iamru)
                    canonical = proposal.share_with if plan.close is CloseReason.DUPLICATE else ""
                    reply = ReplyDraft(
                        report, plan.reason, actor, close=plan.close, duplicate_of=canonical
                    )
                    drafted.append(self._actions.draft_reply(reply).outbox_id)
                elif step is ApplyStep.REJECT:
                    self._actions.dispose(
                        DispositionRequest(report, Disposition.REJECT, actor, reason=plan.reason)
                    )
                elif step is ApplyStep.NEEDS_INFO:
                    # the verified analysis read the thread through its thread_seq: record that
                    # read, so needs-info sees those comments as read and a later reply as new
                    # (bh-yu6sy)
                    self._actions.mark_considered(
                        ConsiderRequest(report, actor, through_seq=proposal.thread_seq)
                    )
                    asked = self._actions.needs_info(NeedsInfoRequest(report, plan.question, actor))
                    drafted.append(asked.outbox_id)
                elif step is ApplyStep.REROUTE:
                    self._actions.reroute_elsewhere(
                        DispositionRequest(report, Disposition.REROUTE, actor), refile
                    )
                ran.append(step)
        except (IntakeRefused, IntakeStoreError) as exc:
            return _with(
                base,
                status=ApplyStatus.FAILED,
                steps=tuple(ran),
                work=work,
                drafted=tuple(drafted),
                detail=f"{_step_name(plan, len(ran))} failed: {_why(exc)}",
            )
        item = _with(
            base,
            status=ApplyStatus.APPLIED,
            steps=tuple(ran),
            work=work,
            drafted=tuple(drafted),
            milestones=tuple(milestones),
        )
        try:
            self._store.write_proposal(
                report, mark_applied(proposal), expect_seq=proposal.seq, actor=actor
            )
        except (IntakeRefused, IntakeStoreError) as exc:
            return _with(
                item,
                status=ApplyStatus.FAILED,
                detail=f"the verbs ran, but the proposal could not be marked applied: {_why(exc)}",
            )
        return item

    def _refresh_labels(
        self, touched: list[str], request: ApplyRequest
    ) -> tuple[LabelSyncOutcome | None, str]:
        if self._projection is None or request.dry_run or not touched:
            return None, ""
        try:
            return (
                self._projection.sync(LabelSyncRequest(request.actor, reports=tuple(touched))),
                "",
            )
        except (IntakeRefused, IntakeStoreError) as exc:
            return None, f"the label refresh failed: {_why(exc)}"


_with = dataclasses.replace


def _step_name(plan: ApplyPlan, index: int) -> str:
    return plan.steps[index].value if index < len(plan.steps) else "marking it applied"


def _why(exc: IntakeRefused | IntakeStoreError) -> str:
    if isinstance(exc, IntakeRefused):
        return exc.detail or exc.reason.value
    return exc.detail


__all__ = ["ProposalService", "Refile", "TriageActions", "TriageBinding", "triage_off"]
