"""The delivery milestones (``bh-6wj9y`` R9; I10, bh-dev42): the facts, the policy, the drafts.

A milestone only DRAFTS: nothing here holds a grant, a writer or a tracker, so a milestone can
never send. The pure parts live here (standard library only): the two trigger inputs
(:class:`MergeFact`, :class:`ReleaseFact`), the per-hive switches (:class:`MilestonePolicy`), what
a trigger reports per report (:class:`MilestoneItem`, :class:`MilestoneOutcome`), the fixed public
wording of each milestone message, and the document edits that record what a trigger established.

The wording is a constant per event with ``{tag}`` as its only placeholder, so a milestone message
carries no bead id, branch, path or seat by construction; the leak check still runs on the
rendered text (a tag shaped like a bead id is held, not sent).

The progress milestones (bh-dev42) follow the same rule. ``started`` is fixed wording, drafted by
the claim hook on the FIRST claim of work a report tracks (its record is keyed on the work, so a
re-claim, a resume after a bounce or a claim after an abandon finds the key and drafts nothing).
``blocked`` and ``deprioritised`` are drafted only by an explicit operator verb: fixed wording
followed by the operator's public reason, which is the one free text a milestone carries and so
the one the leak check matters for (a reason naming a bead id is held, never sent). All three are
in the ``milestone`` group, so a newer milestone supersedes an unsent older one, and all three
draft only while the report's delivery is ``pending``: a progress note after a merge would
supersede the unsent ``merged`` record with stale news.
"""

from __future__ import annotations

import dataclasses
import hashlib
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
STARTED_MESSAGE = Message(
    "milestone.started",
    "Work to address this issue has started.",
)
#: The fixed lead of the two operator milestones; the operator's public reason follows it.
BLOCKED_LEAD = "Work to address this issue is blocked for now."
DEPRIORITISED_LEAD = "Work to address this issue has been deprioritised for now."
#: The progress events and the ``delivery`` value they may be drafted at (bh-dev42).
PROGRESS_EVENTS = frozenset({OutboxEvent.STARTED, OutboxEvent.BLOCKED, OutboxEvent.DEPRIORITISED})
NOTICE_EVENTS = frozenset({OutboxEvent.BLOCKED, OutboxEvent.DEPRIORITISED})
PROGRESS_DELIVERY = "pending"
#: The longest public reason a notice accepts (a comment, not an essay).
MAX_REASON_CHARS = 1000


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
class StartFact:
    """The input of the claim hook (bh-dev42): ``work`` was claimed, by ``actor``, and the claim
    was a fresh one (not a re-attach of a claim the actor already held)."""

    work: str
    actor: str


@dataclass(frozen=True)
class NoticeFact:
    """The input of ``bh work delivery blocked|deprioritised`` (bh-dev42): an operator recorded
    ``work`` as ``event`` (``BLOCKED`` or ``DEPRIORITISED``) with a public ``reason``."""

    work: str
    event: OutboxEvent
    reason: str
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
    # the progress milestones (bh-dev42), each off by default: the claim hook drafts ``started``
    # only with ``draft_on_start``; the operator verbs draft only with their own switch
    draft_on_start: bool = False
    draft_on_blocked: bool = False
    draft_on_deprioritised: bool = False

    def drafts(self, event: str) -> bool:
        """Whether this policy drafts a record for ``event`` (an unknown event: never)."""
        return {
            "merged": self.draft_on_merge,
            "released": self.draft_on_release,
            "started": self.draft_on_start,
            "blocked": self.draft_on_blocked,
            "deprioritised": self.draft_on_deprioritised,
        }.get(str(event), False)


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
    # the drafted record is held by the leak check (a finding to fix or publish; bh-dev42)
    held: bool = False


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


def started_draft(binding: IssueBinding, fact: StartFact) -> DraftRequest:
    """The ``started`` record of ``fact``: keyed on the work, so only the FIRST claim of that work
    drafts one per report (a re-claim, a resume or a claim after an abandon finds the key, O2)."""
    return DraftRequest(
        binding,
        OutboxEvent.STARTED,
        fact.work,
        fact.actor,
        facts=PublicFacts(),
        message=STARTED_MESSAGE,
    )


def notice_message(event: OutboxEvent, reason: str) -> Message:
    """The fixed lead of a ``blocked`` / ``deprioritised`` milestone followed by the operator's
    public ``reason`` (validated by :func:`notice_reason_problem` first)."""
    lead = BLOCKED_LEAD if event is OutboxEvent.BLOCKED else DEPRIORITISED_LEAD
    return Message(f"milestone.{event.value}", f"{lead}\n\n{reason.strip()}")


def notice_reason_problem(reason: str) -> str:
    """Why ``reason`` cannot be a notice's public text, or ``""``. It must say something, stay
    short, and use no ``{placeholder}`` (the renderer fills ``{tag}`` and its siblings, which would
    silently rewrite the operator's words). Leaks are NOT judged here: the leak check holds the
    drafted record instead, so the operator sees the finding on the record."""
    text = reason.strip()
    if not text:
        return "a public reason is required (it is posted to the reporter after approval)"
    if len(text) > MAX_REASON_CHARS:
        return f"the public reason is longer than {MAX_REASON_CHARS} characters"
    for name in ("tag", "version", "commit_url", "progress"):
        if "{" + name + "}" in text:
            return f"the public reason may not contain the template placeholder {{{name}}}"
    return ""


def notice_draft(binding: IssueBinding, fact: NoticeFact) -> DraftRequest:
    """The ``blocked`` / ``deprioritised`` record of ``fact``: keyed on the work and a digest of
    the reason, so re-running the verb with the same words drafts nothing (O2) while new words
    draft a new record that supersedes the unsent older one (O6)."""
    text = fact.reason.strip()
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return DraftRequest(
        binding,
        fact.event,
        f"{fact.work}#{digest}",
        fact.actor,
        facts=PublicFacts(),
        message=notice_message(fact.event, text),
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
    "BLOCKED_LEAD",
    "DEPRIORITISED_LEAD",
    "MAX_REASON_CHARS",
    "MERGED_MESSAGE",
    "NOTICE_EVENTS",
    "PROGRESS_DELIVERY",
    "PROGRESS_EVENTS",
    "RELEASED_MESSAGE",
    "STARTED_MESSAGE",
    "CompletionPolicy",
    "MergeFact",
    "MilestoneItem",
    "MilestoneOutcome",
    "MilestonePolicy",
    "NoticeFact",
    "ReleaseFact",
    "StartFact",
    "Verdict",
    "fold_facts",
    "merged_draft",
    "merged_facts",
    "notice_draft",
    "notice_message",
    "notice_reason_problem",
    "released_draft",
    "released_facts",
    "started_draft",
]
