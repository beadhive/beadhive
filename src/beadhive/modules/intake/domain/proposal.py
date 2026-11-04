"""The triage proposal (bh-hrx2r): what an analyst pass proposes for one report, the review
verdicts recorded against it, and the status machine between them.

A proposal is the deterministic half of review-gated triage. The skill's analyst pass writes one
onto a report bead; a verifier pass reviews it; only a ``verified`` proposal may be applied, and an
applied proposal is immutable. Proposals may live on ANY report, native or bound to a tracker issue,
and are private operational data: they are never published and never reach a tracker.

Status machine (each move advances ``seq`` by one)::

    proposed --confirmed review--> verified --apply--> applied   (terminal, immutable)
    verified --corrected/unsure review--> proposed
    verified --amend--> proposed
    proposed|verified --stale--> stale                           (terminal)

Every rule returns a new :class:`Proposal` and never mutates; a refused move raises
:class:`~.errors.IntakeRefused` before anything is changed. Pure; no I/O.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from enum import StrEnum

from .errors import IntakeRefused, RefusalReason

PROPOSAL_SCHEMA = 1


class ProposalDisposition(StrEnum):
    """What the analyst proposes to do with the report."""

    NEW = "new"  # genuinely new work: create it
    TRACKED = "tracked"  # already tracked by existing work
    FIXED = "fixed"  # fixed by released or merged work
    PARTIALLY_FIXED = "partially-fixed"
    DUPLICATE_OF = "duplicate-of"  # a duplicate of the canonical report in ``share_with``
    REJECT = "reject"
    NEEDS_INFO = "needs-info"
    REROUTE = "reroute"
    DECISION = "decision"  # an operator decision is needed first


class TargetRole(StrEnum):
    OWNER = "owner"  # the work that owns the outcome
    RELATED = "related"  # work that touches the report without owning it


class ReviewVerdict(StrEnum):
    CONFIRMED = "confirmed"
    CORRECTED = "corrected"
    UNSURE = "unsure"


class ProposalStatus(StrEnum):
    PROPOSED = "proposed"
    VERIFIED = "verified"
    APPLIED = "applied"
    STALE = "stale"


@dataclass(frozen=True)
class ProposalTarget:
    id: str
    role: TargetRole = TargetRole.OWNER


@dataclass(frozen=True)
class ProposalReview:
    """One verifier pass. ``pass_no`` counts from 1 in recording order; ``corrections`` is what the
    reviewer says is wrong and is required for (only) a ``corrected`` verdict."""

    pass_no: int
    seat: str
    verdict: ReviewVerdict
    corrections: tuple[str, ...] = ()


@dataclass(frozen=True)
class Proposal:
    """The ``intake.proposal`` document of one report bead. Never mutated.

    ``share_with`` is the canonical report a duplicate is shared with, ``analysed_commit`` the
    repository commit the analysis was made at and ``thread_seq`` the ``intake.thread`` seq it
    read, so a later check can tell the proposal went stale. ``fixed_in`` is the release a fix
    claim names and ``fix_commit`` the commit it cites; both are claims, which the pre-apply
    checks verify (the version is computed from the repository and overwrites a claimed one)."""

    disposition: ProposalDisposition
    targets: tuple[ProposalTarget, ...] = ()
    share_with: str = ""
    type: str = ""
    priority: str = ""
    reporter_text: str = ""
    evidence: tuple[str, ...] = ()
    analysed_commit: str = ""
    thread_seq: int = 0
    fixed_in: str = ""
    fix_commit: str = ""
    reviews: tuple[ProposalReview, ...] = ()
    status: ProposalStatus = ProposalStatus.PROPOSED
    seq: int = 0
    schema: int = PROPOSAL_SCHEMA


def validate_proposal(proposal: Proposal) -> None:
    """Refuse (``PROPOSAL_INVALID``) a proposal whose shape is inconsistent."""
    ids = [t.id for t in proposal.targets]
    if any(not i for i in ids):
        raise IntakeRefused(RefusalReason.PROPOSAL_INVALID, "a target has no id")
    if len(set(ids)) != len(ids):
        raise IntakeRefused(RefusalReason.PROPOSAL_INVALID, "a target is listed twice")
    if proposal.thread_seq < 0:
        raise IntakeRefused(RefusalReason.PROPOSAL_INVALID, "thread_seq is negative")
    for expected, review in enumerate(proposal.reviews, start=1):
        if review.pass_no != expected:
            raise IntakeRefused(
                RefusalReason.PROPOSAL_INVALID, f"review passes are not 1..n at {review.pass_no}"
            )
        if not review.seat:
            raise IntakeRefused(RefusalReason.PROPOSAL_INVALID, "a review has no seat")
        if (review.verdict is ReviewVerdict.CORRECTED) != bool(review.corrections):
            raise IntakeRefused(
                RefusalReason.PROPOSAL_INVALID,
                f"review pass {review.pass_no}: corrections go with, and only with, a "
                "corrected verdict",
            )


def new_proposal(
    disposition: ProposalDisposition,
    *,
    targets: tuple[ProposalTarget, ...] = (),
    share_with: str = "",
    type: str = "",  # noqa: A002 - the stored field's name
    priority: str = "",
    reporter_text: str = "",
    evidence: tuple[str, ...] = (),
    analysed_commit: str = "",
    thread_seq: int = 0,
    fixed_in: str = "",
    fix_commit: str = "",
) -> Proposal:
    """A fresh ``proposed`` proposal at ``seq`` 1."""
    proposal = Proposal(
        disposition=disposition,
        targets=tuple(targets),
        share_with=share_with,
        type=type,
        priority=priority,
        reporter_text=reporter_text,
        evidence=tuple(evidence),
        analysed_commit=analysed_commit,
        thread_seq=thread_seq,
        fixed_in=fixed_in,
        fix_commit=fix_commit,
        seq=1,
    )
    validate_proposal(proposal)
    return proposal


def _require_open(proposal: Proposal, doing: str) -> None:
    if proposal.status is ProposalStatus.APPLIED:
        raise IntakeRefused(
            RefusalReason.PROPOSAL_IMMUTABLE, f"an applied proposal cannot be {doing}"
        )
    if proposal.status is ProposalStatus.STALE:
        raise IntakeRefused(
            RefusalReason.PROPOSAL_TRANSITION,
            f"a stale proposal cannot be {doing}; write a fresh proposal",
        )


def add_review(
    proposal: Proposal,
    seat: str,
    verdict: ReviewVerdict,
    corrections: tuple[str, ...] = (),
) -> Proposal:
    """Record the next review pass. ``confirmed`` verifies the proposal; ``corrected`` (and
    ``unsure``, which verifies nothing) moves a verified proposal back to ``proposed``."""
    _require_open(proposal, "reviewed")
    review = ProposalReview(len(proposal.reviews) + 1, seat, verdict, tuple(corrections))
    status = (
        ProposalStatus.VERIFIED if verdict is ReviewVerdict.CONFIRMED else ProposalStatus.PROPOSED
    )
    updated = dataclasses.replace(
        proposal, reviews=(*proposal.reviews, review), status=status, seq=proposal.seq + 1
    )
    validate_proposal(updated)
    return updated


def amend(proposal: Proposal, **changes) -> Proposal:
    """Change the proposal's content (the fields the analyst owns). Any amendment un-verifies it:
    the result is ``proposed`` and wants a fresh confirming review."""
    _require_open(proposal, "amended")
    owned = {
        "disposition",
        "targets",
        "share_with",
        "type",
        "priority",
        "reporter_text",
        "evidence",
        "analysed_commit",
        "thread_seq",
        "fixed_in",
        "fix_commit",
    }
    unknown = set(changes) - owned
    if unknown:
        raise IntakeRefused(
            RefusalReason.PROPOSAL_INVALID, f"not an amendable field: {sorted(unknown)}"
        )
    updated = dataclasses.replace(
        proposal, **changes, status=ProposalStatus.PROPOSED, seq=proposal.seq + 1
    )
    validate_proposal(updated)
    return updated


def mark_applied(proposal: Proposal) -> Proposal:
    """``verified`` -> ``applied``; anything else is refused. Applied is terminal."""
    if proposal.status is ProposalStatus.APPLIED:
        raise IntakeRefused(RefusalReason.PROPOSAL_IMMUTABLE, "the proposal is already applied")
    if proposal.status is not ProposalStatus.VERIFIED:
        raise IntakeRefused(
            RefusalReason.PROPOSAL_TRANSITION,
            f"only a verified proposal can be applied, not a {proposal.status.value} one",
        )
    return dataclasses.replace(proposal, status=ProposalStatus.APPLIED, seq=proposal.seq + 1)


def mark_stale(proposal: Proposal) -> Proposal:
    """``proposed`` or ``verified`` -> ``stale``; already stale is returned as it is, applied is
    refused."""
    if proposal.status is ProposalStatus.STALE:
        return proposal
    _require_open(proposal, "marked stale")
    return dataclasses.replace(proposal, status=ProposalStatus.STALE, seq=proposal.seq + 1)


__all__ = [
    "PROPOSAL_SCHEMA",
    "Proposal",
    "ProposalDisposition",
    "ProposalReview",
    "ProposalStatus",
    "ProposalTarget",
    "ReviewVerdict",
    "TargetRole",
    "add_review",
    "amend",
    "mark_applied",
    "mark_stale",
    "new_proposal",
    "validate_proposal",
]
