"""The merge and release milestones (``bh-6wj9y`` R9; I10): the facts, the policy and the drafts.

A milestone only DRAFTS: nothing here holds a grant, a writer or a tracker, so a milestone can
never send. The pure parts live here (standard library only): the two trigger inputs
(:class:`MergeFact`, :class:`ReleaseFact`), the per-hive switches (:class:`MilestonePolicy`), what
a trigger reports per report (:class:`MilestoneItem`, :class:`MilestoneOutcome`), the fixed public
wording of each milestone message, and the document edits that record what a trigger established.

The wording is a constant per event with ``{tag}`` as its only placeholder, so a milestone message
carries no bead id, branch, path or seat by construction; the leak check still runs on the
rendered text (a tag shaped like a bead id is held, not sent).
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass

from .delivery import CompletionPolicy, Verdict
from .models import IssueBinding
from .outbox import (
    DeliveryFacts,
    DraftRequest,
    MergedFact,
    Message,
    OutboxDocument,
    OutboxEvent,
    PublicFacts,
    ReleasedFact,
)

#: The fixed public wording of each milestone (``{tag}`` is the only placeholder).
MERGED_MESSAGE = Message(
    "milestone.merged",
    "A change that addresses this issue has been merged. It will be included in an upcoming "
    "release.",
)
RELEASED_MESSAGE = Message(
    "milestone.released",
    "A change that addresses this issue is now available in {tag}.",
)


@dataclass(frozen=True)
class MergeFact:
    """The input of the merge trigger: ``work`` landed on the integration branch as ``commit`` (the
    merge commit, not only a branch head), recorded by ``actor``."""

    work: str
    commit: str
    actor: str


@dataclass(frozen=True)
class ReleaseFact:
    """The input of the release trigger: ``tag`` names ``commit``, recorded by ``actor``."""

    tag: str
    commit: str
    actor: str


@dataclass(frozen=True)
class MilestonePolicy:
    """The ``intake.external`` milestone switches. Every draft is off by default.

    ``draft_on_merge`` and ``draft_on_release`` choose which milestones draft a record at all (and
    only when the outbox is on); ``append_release_to_closed`` lets a ``released`` record be drafted
    for a report that is already closed (close-on-merge closes it first). ``completion`` is the
    hive's completion policy."""

    draft_on_merge: bool = False
    draft_on_release: bool = False
    append_release_to_closed: bool = False
    completion: CompletionPolicy = CompletionPolicy.CLOSE_ON_MERGE

    def drafts(self, event: str) -> bool:
        """Whether this policy drafts a record for the ``merged`` or ``released`` ``event``."""
        return self.draft_on_merge if event == "merged" else self.draft_on_release


@dataclass(frozen=True)
class MilestoneItem:
    """What a trigger did for one report.

    ``delivery`` is the value it wrote (``""``: nothing changed); ``drafted`` the outbox id of the
    record it drafted (``""``: none, or the key already existed); ``verdict`` the release
    verifier's answer; ``closed`` whether the report completed and was closed; ``note`` why
    nothing changed or what is pending; ``error`` the step that failed (the report is left as it
    was before that step, and the trigger moved on)."""

    report: str
    event: str
    delivery: str = ""
    drafted: str = ""
    verdict: Verdict | None = None
    closed: bool = False
    note: str = ""
    error: str = ""


@dataclass(frozen=True)
class MilestoneOutcome:
    """Every report a trigger looked at. Empty when intake is off or no report is concerned."""

    items: tuple[MilestoneItem, ...] = ()

    @property
    def errors(self) -> tuple[MilestoneItem, ...]:
        return tuple(i for i in self.items if i.error)

    @property
    def reports(self) -> tuple[str, ...]:
        return tuple(i.report for i in self.items)

    def merged_with(self, other: MilestoneOutcome) -> MilestoneOutcome:
        return MilestoneOutcome((*self.items, *other.items))


def merged_draft(binding: IssueBinding, fact: MergeFact) -> DraftRequest:
    """The ``merged`` record of ``fact``: keyed on the merge commit, so a re-run drafts nothing."""
    return DraftRequest(
        binding,
        OutboxEvent.MERGED,
        fact.commit,
        fact.actor,
        facts=PublicFacts(),
        message=MERGED_MESSAGE,
    )


def released_draft(binding: IssueBinding, fact: ReleaseFact) -> DraftRequest:
    """The ``released`` record of ``fact``, keyed on the tag (its one public fact)."""
    return DraftRequest(
        binding,
        OutboxEvent.RELEASED,
        fact.tag,
        fact.actor,
        facts=PublicFacts(tag=fact.tag),
        message=RELEASED_MESSAGE,
    )


def fold_facts(
    before: OutboxDocument, after: OutboxDocument, facts: DeliveryFacts
) -> OutboxDocument:
    """``after`` (what a rule did to ``before``) carrying ``facts``, in ONE document write.

    When the rule already bumped ``seq`` the facts ride along; when it changed nothing (a draft
    key that already existed, or no draft at all) a changed fact bumps ``seq`` once; unchanged
    facts leave ``after`` as it is, so a re-run trigger writes nothing (X2)."""
    if facts == after.facts:
        return after
    seq = after.seq if after.seq > before.seq else before.seq + 1
    return dataclasses.replace(after, seq=seq, facts=facts)


def merged_facts(doc: OutboxDocument, work: str, commit: str) -> DeliveryFacts:
    return dataclasses.replace(doc.facts, merged=MergedFact(work, commit))


def released_facts(doc: OutboxDocument, tag: str, commit: str) -> DeliveryFacts:
    """Only a ``YES`` verdict records a release, so the fact is always ``verified``."""
    return dataclasses.replace(doc.facts, released=ReleasedFact(tag, commit, True))


__all__ = [
    "MERGED_MESSAGE",
    "RELEASED_MESSAGE",
    "CompletionPolicy",
    "MergeFact",
    "MilestoneItem",
    "MilestoneOutcome",
    "MilestonePolicy",
    "ReleaseFact",
    "Verdict",
    "fold_facts",
    "merged_draft",
    "merged_facts",
    "released_draft",
    "released_facts",
]
