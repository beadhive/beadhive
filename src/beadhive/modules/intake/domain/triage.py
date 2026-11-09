"""Propose, review and apply-reviewed (bh-9aqmv, epic bh-hrx2r): the records and the pure rules.

The deterministic half of review-gated triage ends here. An analyst pass PROPOSES a disposition
for a report, a verifier pass REVIEWS it, and ``apply_reviewed`` turns only the proposals a
review stands behind into the existing triage verbs:

* a proposal is applicable when its LAST review confirmed it, or corrected it and the proposal
  adopted that correction (:func:`verification_gap`);
* every pre-apply check runs first, and a proposal with any finding is blocked;
* canonicals go first, then the family members that share their work (:func:`apply_order`);
* each disposition maps onto the verbs triage already has (:func:`plan_apply`); a ``decision``
  is listed and never applied.

Apply drafts and never sends: a reply or a question is an outbox draft an operator approves and
sends later. A reply that ends the report's thread (``fixed``, ``reject``, ``duplicate-of``) also
asks for the issue's close on the same record (:func:`close_reason_for`), sent after the reply and
the labels; a ``needs-info`` question never closes anything. Pure: records and rules only, no I/O.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .label_sync import LabelSyncOutcome
from .outbox import CloseReason
from .proposal import (
    Proposal,
    ProposalDisposition,
    ProposalStatus,
    ReviewVerdict,
    TargetRole,
)
from .proposal_checks import CheckFinding, ProposalCheck

# -- requests and outcomes -------------------------------------------------------------------


@dataclass(frozen=True)
class ProposeRequest:
    """Store an analyst's proposal on ``report``. ``document`` is the proposal as the skill wrote
    it (:func:`~.proposal_document.content_from_document`)."""

    report: str
    document: Mapping[str, Any]
    actor: str


class ProposeKind(StrEnum):
    CREATED = "created"  # the report had no proposal
    AMENDED = "amended"  # an open proposal's content replaced; its reviews kept, un-verified
    REPLACED = "replaced"  # a stale proposal replaced by a fresh one
    NEW_ROUND = "new_round"  # a woken needs-info report: the applied round kept in history


@dataclass(frozen=True)
class ProposeOutcome:
    report: str
    kind: ProposeKind
    proposal: Proposal


@dataclass(frozen=True)
class ProposalReviewRequest:
    """Record a verifier's verdict on ``report``'s proposal. ``document`` is the verdict
    (:func:`~.proposal_document.verdict_from_document`); its ``seat`` defaults to ``actor``."""

    report: str
    document: Mapping[str, Any]
    actor: str


@dataclass(frozen=True)
class ProposalReviewOutcome:
    report: str
    proposal: Proposal
    adopted: bool = False  # the verdict's corrected content was adopted (so it is verified)

    @property
    def verified(self) -> bool:
        return self.proposal.status is ProposalStatus.VERIFIED


@dataclass(frozen=True)
class ApplyRequest:
    """``apply --reviewed``. ``dry_run`` checks and plans everything and writes nothing."""

    actor: str
    dry_run: bool = False


class ApplyStatus(StrEnum):
    APPLIED = "applied"  # the verbs ran and the proposal is marked applied
    PLANNED = "planned"  # dry run: it would be applied
    BLOCKED = "blocked"  # unverified, a check finding, or its canonical is not applied
    LISTED = "listed"  # a decision: listed for the operator, never applied
    FAILED = "failed"  # a verb was refused or the store failed (see ``detail``)


class ApplyStep(StrEnum):
    """One existing triage verb, as apply runs it."""

    ACCEPT = "accept"  # a plain accept (a born-native report's new work is itself)
    ACCEPT_NEW_WORK = "accept --new-work"
    ACCEPT_WORK = "accept --work"
    LINK = "link"
    DRAFT_REPLY = "draft reply"
    REJECT = "reject"
    NEEDS_INFO = "needs-info"
    REROUTE = "reroute"


@dataclass(frozen=True)
class ApplyItem:
    """One report's proposal in an apply run, in the order apply took it.

    ``steps`` are the verbs (run, or planned on a dry run); ``work`` the work the report tracks
    after them (for a planned ``accept --new-work``, empty: it does not exist yet);
    ``share_with`` the canonical a family member shares with; ``fixed_in`` the COMPUTED fix
    version of a fix claim (``""``: merged, unreleased; ``None``: no fix claim); ``drafted`` the
    outbox ids drafted (never sent); ``findings`` the check findings and ``detail`` why a blocked
    or failed proposal was not applied. ``relates`` are the other bound reports whose issues the
    reply mentions (``bh-iamru``): apply records a ``relates-to`` to each before its verbs (planned
    on a dry run)."""

    report: str
    disposition: ProposalDisposition
    status: ApplyStatus
    steps: tuple[ApplyStep, ...] = ()
    work: str = ""
    share_with: str = ""
    fixed_in: str | None = None
    drafted: tuple[str, ...] = ()
    findings: tuple[CheckFinding, ...] = ()
    detail: str = ""
    milestones: tuple[Any, ...] = ()
    relates: tuple[str, ...] = ()


@dataclass(frozen=True)
class ApplyOutcome:
    """An apply run. ``items`` in apply order (canonicals first); ``already_applied`` the reports
    whose proposal an earlier run applied (skipped: apply is idempotent); ``checks`` every check
    run; ``labels`` the label projection's refresh of the touched reports (``None``: the
    projection is off, nothing was touched, or a dry run) and ``labels_error`` why it failed."""

    items: tuple[ApplyItem, ...] = ()
    already_applied: tuple[str, ...] = ()
    checks: tuple[ProposalCheck, ...] = ()
    dry_run: bool = False
    labels: LabelSyncOutcome | None = None
    labels_error: str = ""

    def by_status(self, status: ApplyStatus) -> tuple[str, ...]:
        return tuple(i.report for i in self.items if i.status is status)

    @property
    def applied(self) -> tuple[str, ...]:
        return self.by_status(ApplyStatus.APPLIED)

    @property
    def blocked(self) -> tuple[str, ...]:
        return self.by_status(ApplyStatus.BLOCKED)

    @property
    def decisions(self) -> tuple[str, ...]:
        return self.by_status(ApplyStatus.LISTED)

    @property
    def drafted(self) -> tuple[str, ...]:
        return tuple(d for i in self.items for d in i.drafted)


# -- the rules -------------------------------------------------------------------------------


def verification_gap(proposal: Proposal) -> str:
    """Why ``proposal`` is not verified for apply (``""``: it is). Applicable means its LAST
    review confirmed it, or corrected it and the proposal adopted exactly that correction, and
    nothing amended it since (an amendment un-verifies it)."""
    if not proposal.reviews:
        return "not reviewed: a verifier pass must confirm it, or correct it and adopt the fix"
    last = proposal.reviews[-1]
    if proposal.status is ProposalStatus.VERIFIED:
        if last.verdict is ReviewVerdict.CONFIRMED:
            return ""
        if last.verdict is ReviewVerdict.CORRECTED and proposal.adopted == last.pass_no:
            return ""
    if last.verdict is ReviewVerdict.CORRECTED:
        return (
            f"review pass {last.pass_no} corrected it and the correction was not adopted: "
            "re-propose and review again, or record the verdict with the corrected content"
        )
    if last.verdict is ReviewVerdict.UNSURE:
        return f"review pass {last.pass_no} was unsure: it needs a confirming review"
    return f"amended since review pass {last.pass_no} confirmed it: it needs a fresh review"


def apply_order(run: Mapping[str, Proposal]) -> tuple[str, ...]:
    """The order apply takes ``run`` (report id to proposal): every canonical a member names,
    then every other proposal that shares with no one, then the family members, each group by
    report id. A member's canonical is therefore applied before it in the same run."""
    canonicals = {p.share_with for p in run.values() if p.share_with}
    first = sorted(r for r in run if r in canonicals and not run[r].share_with)
    middle = sorted(r for r in run if r not in canonicals and not run[r].share_with)
    members = sorted(r for r in run if run[r].share_with)
    return (*first, *middle, *members)


def owner_of(proposal: Proposal) -> str:
    """The proposal's first ``owner`` target (``""``: none)."""
    return next((t.id for t in proposal.targets if t.role is TargetRole.OWNER), "")


@dataclass(frozen=True)
class ApplyPlan:
    """The verbs one proposal maps to. ``work`` is the work ``accept --work`` or ``link`` names;
    ``reason`` the reporter-visible reason of a reject (also the drafted reply's text);
    ``question`` the needs-info question; ``close`` why the drafted reply also closes the issue
    (``None``: it does not)."""

    steps: tuple[ApplyStep, ...]
    work: str = ""
    reason: str = ""
    question: str = ""
    close: CloseReason | None = None


#: The close each thread-ending disposition asks for. A ``needs-info`` question waits for the
#: reporter, and every other outcome keeps the report tracked, so neither closes the issue.
_CLOSES: Mapping[ProposalDisposition, CloseReason] = {
    ProposalDisposition.FIXED: CloseReason.COMPLETED,
    ProposalDisposition.REJECT: CloseReason.NOT_PLANNED,
    ProposalDisposition.DUPLICATE_OF: CloseReason.DUPLICATE,
}


def close_reason_for(disposition: ProposalDisposition) -> CloseReason | None:
    """Why a report closed with ``disposition`` closes its issue (``None``: it does not)."""
    return _CLOSES.get(disposition)


def fixed_text(fixed_in: str | None) -> str:
    """The reporter-visible reason of a ``fixed`` claim with no fixing bead, from the COMPUTED
    version (never the claimed one)."""
    if fixed_in:
        return f"This is fixed in {fixed_in}. Please upgrade, and reopen if it still happens."
    return (
        "This is fixed on the main line and will ship in the next release. Please reopen if it "
        "still happens after that."
    )


def reopen_text() -> str:
    """The reporter-visible reply a reopen rides on when triage reverses an outcome that left the
    issue closed (``bh-ih484``): a template, never the report's own words."""
    return (
        "On a second look this needs more attention than closing it suggested, so we are "
        "reopening this issue."
    )


def keeps_open(plan: ApplyPlan) -> bool:
    """Whether ``plan`` leaves the report open and tracked (it accepts it, or asks its reporter),
    so an issue still CLOSED on the tracker contradicts the outcome and gets a reopen. A reject or
    a reroute ends the thread: the close is right."""
    return ApplyStep.REJECT not in plan.steps and ApplyStep.REROUTE not in plan.steps


def duplicate_text(canonical_ref: str) -> str:
    """The reporter-visible reason of a ``duplicate-of`` with no reporter text of its own."""
    where = canonical_ref or "an earlier report"
    return f"This is a duplicate of {where}; the work is tracked there."


def plan_apply(
    proposal: Proposal,
    *,
    bound: bool,
    fixed_in: str | None = None,
    canonical_work: str | None = None,
    canonical_ref: str = "",
    replies: bool = True,
) -> ApplyPlan | str:
    """The verbs ``proposal`` maps to, or why it cannot be applied (a string).

    ``bound``: the report is bound to an external issue (a reply or question needs one, and a
    born-native report's ``new`` is a plain accept); ``fixed_in``: the COMPUTED fix version;
    ``canonical_work``: the work a member's canonical tracks (``None``: none yet);
    ``canonical_ref``: the canonical's issue reference, for a duplicate's reason; ``replies``:
    an outbox is bound to draft replies. Without one a ``duplicate-of`` is linked and rejected
    with no reply and no close, exactly as before closes existed; a ``fixed`` or ``reject`` reply
    still needs it (its draft refuses first, so nothing is written).

    A reply is drafted BEFORE any other verb, so a draft that refuses leaves the report untouched;
    drafting the same text again is O2 (no write), so a rerun after a later failure is safe."""
    d = proposal.disposition
    owner = owner_of(proposal)
    text = proposal.reporter_text
    reply = (ApplyStep.DRAFT_REPLY,) if bound else ()
    close = close_reason_for(d) if bound else None  # a close rides on the drafted reply
    if d is ProposalDisposition.DECISION:
        return "a decision is listed for the operator and never applied"
    if d is ProposalDisposition.NEW or (d is ProposalDisposition.PARTIALLY_FIXED and not owner):
        return ApplyPlan((ApplyStep.ACCEPT_NEW_WORK,) if bound else (ApplyStep.ACCEPT,))
    if d in (ProposalDisposition.TRACKED, ProposalDisposition.PARTIALLY_FIXED):
        return ApplyPlan((ApplyStep.ACCEPT_WORK,), work=owner)
    if d is ProposalDisposition.FIXED:
        if owner:
            return ApplyPlan((ApplyStep.ACCEPT_WORK,), work=owner)
        return ApplyPlan(
            (*reply, ApplyStep.REJECT), reason=text or fixed_text(fixed_in), close=close
        )
    if d is ProposalDisposition.DUPLICATE_OF:
        if not canonical_work:
            return f"its canonical {proposal.share_with} tracks no work yet"
        told = reply if replies else ()  # the outbox off: linked and rejected, as on 0.35.2
        return ApplyPlan(
            (*told, ApplyStep.LINK, ApplyStep.REJECT),
            work=canonical_work,
            reason=text or duplicate_text(canonical_ref),
            close=close if told else None,
        )
    if d is ProposalDisposition.NEEDS_INFO:
        if not bound:
            return "needs-info asks the reporter on the bound issue; a born-native report has none"
        return ApplyPlan((ApplyStep.NEEDS_INFO,), question=text)
    if d is ProposalDisposition.REJECT:
        return ApplyPlan((*reply, ApplyStep.REJECT), reason=text, close=close)
    if d is ProposalDisposition.REROUTE:
        return ApplyPlan((ApplyStep.REROUTE,), reason=text)
    return f"no verb maps {d.value}"  # pragma: no cover - the enum is closed


__all__ = [
    "ApplyItem",
    "ApplyOutcome",
    "ApplyPlan",
    "ApplyRequest",
    "ApplyStatus",
    "ApplyStep",
    "ProposalReviewOutcome",
    "ProposalReviewRequest",
    "ProposeKind",
    "ProposeOutcome",
    "ProposeRequest",
    "apply_order",
    "close_reason_for",
    "duplicate_text",
    "fixed_text",
    "keeps_open",
    "owner_of",
    "reopen_text",
    "plan_apply",
    "verification_gap",
]
