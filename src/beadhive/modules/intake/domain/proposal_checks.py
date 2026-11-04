"""Deterministic pre-apply checks of triage proposals (bh-hrx2r).

The 2026-11-04 single-pass triage was wrong on a quarter of its reporter-visible outcomes: targets
were closed or only loosely related, families were split, fix versions were claimed rather than
computed, and later reporter comments were never read. Every one of those is mechanically
checkable, so nothing reaches apply until these checks pass. Each check fails CLOSED (an answer it
cannot establish is a finding, never a pass) and each finding names the fix.

The checks, per proposal of a run:

* targets: every ``owner`` target exists, and is OPEN (for a ``fixed`` claim it is the CLOSED
  fixing bead instead); every ``related`` target exists;
* fix claims (``fixed`` / ``partially-fixed``): the fixing commits (``fix_commit`` and the closed
  fixing bead's landed commits) are each an ancestor of the integration branch, and the fix
  version is COMPUTED as the version-sorted first tag containing all of them. A claimed
  ``fixed_in`` that disagrees is a finding that reports the computed version;
  :func:`adopt_computed_fix` overwrites the claim (and so un-verifies the proposal);
* families: ``share_with`` names a canonical report in the same run (not dangling, no cycle, no
  chain through a second canonical), and the canonical holds the owner disposition;
* field consistency: ``new`` has no ``fixed_in``; ``fixed`` has no type or priority; and so on;
* ``reporter_text``: no unresolved :mod:`.leaks` finding (bead ids, internal labels, seats,
  paths, secrets; no sanitizer is itself a finding) and within the length limit;
* staleness: the report's thread advanced past the ``thread_seq`` the analysis read, or the
  integration branch moved past ``analysed_commit`` in a path the evidence cites.

Pure: no I/O, no clock. Repository facts come from an injected :class:`RepoQueries` (the
contracts' ``RepoFacts`` port, implemented over git at the root), every other fact (target status,
landed commits, the stored thread seq) is passed in by the caller as plain data.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from .delivery import Verdict
from .leaks import LeakPolicy, Scanner, unresolved
from .leaks import check as leak_check
from .proposal import (
    Proposal,
    ProposalDisposition,
    TargetRole,
    amend,
)

#: The default ceiling on ``reporter_text``: a short reply, a question or a reason, not an essay.
DEFAULT_REPORTER_TEXT_LIMIT = 2000

#: The dispositions that own a family's outcome: what a ``duplicate-of`` member links to.
OWNER_DISPOSITIONS = frozenset(
    {
        ProposalDisposition.NEW,
        ProposalDisposition.TRACKED,
        ProposalDisposition.FIXED,
        ProposalDisposition.PARTIALLY_FIXED,
    }
)
#: The dispositions that claim (some of) the report is fixed.
FIX_DISPOSITIONS = frozenset({ProposalDisposition.FIXED, ProposalDisposition.PARTIALLY_FIXED})
#: Dispositions whose reporter-visible message is the point: they need ``reporter_text``.
TEXT_DISPOSITIONS = frozenset({ProposalDisposition.REJECT, ProposalDisposition.NEEDS_INFO})

#: A bead status that is no longer live work.
CLOSED = "closed"


class RepoQueries(Protocol):
    """What the checks ask of the repository (the contracts' ``RepoFacts`` satisfies it)."""

    def is_ancestor(self, commit: str, branch: str) -> Verdict: ...

    def first_tag_containing(self, commit: str) -> str | None: ...

    def changed_paths(self, since: str, until: str) -> tuple[str, ...] | None: ...


class CheckKind(StrEnum):
    OWNER_MISSING = "owner_missing"  # an owner target that does not exist
    OWNER_CLOSED = "owner_closed"  # an owner target of live work that is closed
    FIX_OPEN = "fix_open"  # a fixed claim whose fixing bead is still open
    TARGET_MISSING = "target_missing"  # a related target that does not exist
    FIX_UNPROVEN = "fix_unproven"  # a fix claim that names no fixing commit
    FIX_NOT_ON_BRANCH = "fix_not_on_branch"  # a fixing commit not (provably) on the branch
    FIX_VERSION = "fix_version"  # the claimed fixed_in is not the computed version
    SHARE_DANGLING = "share_dangling"  # share_with names no report of this run
    SHARE_CYCLE = "share_cycle"  # share_with leads back to the report
    SHARE_CHAIN = "share_chain"  # share_with names a member, so the family has two canonicals
    CANONICAL_DISPOSITION = "canonical_disposition"  # the canonical does not own the outcome
    INCONSISTENT_FIELDS = "inconsistent_fields"  # fields the disposition forbids or needs
    REPORTER_TEXT_LEAK = "reporter_text_leak"  # an unresolved leak finding in reporter_text
    REPORTER_TEXT_LENGTH = "reporter_text_length"  # reporter_text over the limit
    STALE_THREAD = "stale_thread"  # the thread advanced since the analysis read it
    STALE_PATHS = "stale_paths"  # the branch changed a cited path since analysed_commit
    STALE_UNKNOWN = "stale_unknown"  # staleness cannot be established


#: The kinds that mean "re-review": the proposal was right once, and the world moved.
STALE_KINDS = frozenset({CheckKind.STALE_THREAD, CheckKind.STALE_PATHS, CheckKind.STALE_UNKNOWN})


@dataclass(frozen=True)
class TargetFact:
    """What the store says of one target bead: its status and, for a closed fixing bead, the
    commits that landed it (oldest first). An id absent from the facts does not exist."""

    id: str
    status: str
    commits: tuple[str, ...] = ()


@dataclass(frozen=True)
class CheckContext:
    """Everything a run of checks reads besides the proposals.

    ``branch`` is the integration branch; ``targets`` the store's facts of every target named;
    ``thread_seqs`` each report's current ``intake.thread`` seq (absent: no thread, so 0);
    ``leak_policy`` and ``sanitizer`` what the outbox's leak check uses (``None``: no sanitizer is
    bound, itself a finding)."""

    branch: str
    repo: RepoQueries
    leak_policy: LeakPolicy
    sanitizer: Scanner | None
    targets: Mapping[str, TargetFact] = field(default_factory=dict)
    thread_seqs: Mapping[str, int] = field(default_factory=dict)
    reporter_text_limit: int = DEFAULT_REPORTER_TEXT_LIMIT


@dataclass(frozen=True)
class CheckFinding:
    """One reason a proposal may not be applied: what is wrong and what fixes it."""

    kind: CheckKind
    detail: str
    fix: str


@dataclass(frozen=True)
class ProposalCheck:
    """The checks' verdict on one report's proposal.

    ``computed_fixed_in`` is the fix version computed from the repository for a fix claim whose
    commits are all on the branch (``""``: merged but in no release yet); ``None`` when there is
    no fix claim or it could not be computed. Apply reads it, never the claimed ``fixed_in``."""

    report: str
    findings: tuple[CheckFinding, ...] = ()
    computed_fixed_in: str | None = None

    @property
    def passed(self) -> bool:
        return not self.findings

    @property
    def stale(self) -> bool:
        return any(f.kind in STALE_KINDS for f in self.findings)

    def kinds(self) -> frozenset[CheckKind]:
        return frozenset(f.kind for f in self.findings)


# -- repository answers, asked once per question in a run --------------------------------------


class _Memo:
    """A :class:`RepoQueries` that asks the wrapped one each distinct question once."""

    def __init__(self, repo: RepoQueries) -> None:
        self._repo = repo
        self._answers: dict[tuple[str, ...], object] = {}

    def _ask(self, key: tuple[str, ...], ask):
        if key not in self._answers:
            self._answers[key] = ask()
        return self._answers[key]

    def is_ancestor(self, commit: str, branch: str) -> Verdict:
        return self._ask(
            ("ancestor", commit, branch), lambda: self._repo.is_ancestor(commit, branch)
        )

    def first_tag_containing(self, commit: str) -> str | None:
        return self._ask(("tag", commit), lambda: self._repo.first_tag_containing(commit))

    def changed_paths(self, since: str, until: str) -> tuple[str, ...] | None:
        return self._ask(("changed", since, until), lambda: self._repo.changed_paths(since, until))


# -- versions and cited paths ----------------------------------------------------------------


def version_key(tag: str) -> tuple[tuple[int, int | str], ...]:
    """A sort key that orders tags as ``git tag --sort=version:refname`` does for plain release
    tags: digit runs compare as numbers (``v0.10.0`` after ``v0.9.0``), everything else as text."""
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part) for part in re.split(r"(\d+)", tag) if part
    )


_SHA = re.compile(r"^[0-9a-fA-F]{7,64}$")
_EXTENSION = re.compile(r"\.[A-Za-z][A-Za-z0-9]{0,9}$")
_LOCATION = re.compile(r"(?:#L\d+(?:-L?\d+)?|:\d+(?:[-:]\d+)*)$")
_TRIM = "()[]{}<>,;'\"`"


def cited_paths(evidence: Iterable[str]) -> tuple[str, ...]:
    """The repository paths ``evidence`` cites (``src/x.py``, ``src/x.py:12``, ``x.md#L3-L9``),
    sorted and unique. A token is a path when it has a ``/`` or ends in a file extension; a URL,
    a sha, a version or a bead id (``bh-x.1``) is not one."""
    found: set[str] = set()
    for item in evidence:
        for token in item.split():
            token = token.lstrip(_TRIM).rstrip(_TRIM + ".")
            if not token or "://" in token or _SHA.fullmatch(token):
                continue
            token = _LOCATION.sub("", token)
            token = token.removeprefix("./").rstrip("/") if token != "/" else ""
            if token and ("/" in token or _EXTENSION.search(token)):
                found.add(token)
    return tuple(sorted(found))


def _touches(changed: str, cited: str) -> bool:
    return changed == cited or changed.startswith(cited + "/")


# -- the checks ---------------------------------------------------------------------------------


def _targets(proposal: Proposal, context: CheckContext) -> list[CheckFinding]:
    found: list[CheckFinding] = []
    fixed = proposal.disposition is ProposalDisposition.FIXED
    for target in proposal.targets:
        fact = context.targets.get(target.id)
        if target.role is TargetRole.RELATED:
            if fact is None:
                found.append(
                    CheckFinding(
                        CheckKind.TARGET_MISSING,
                        f"related target {target.id} does not exist",
                        "drop it or name an existing bead",
                    )
                )
            continue
        if fact is None:
            found.append(
                CheckFinding(
                    CheckKind.OWNER_MISSING,
                    f"owner target {target.id} does not exist",
                    "name the existing work that owns the outcome",
                )
            )
        elif fixed and fact.status != CLOSED:
            found.append(
                CheckFinding(
                    CheckKind.FIX_OPEN,
                    f"fixing bead {target.id} is still {fact.status}, so nothing is fixed yet",
                    "propose tracked with it as the owner, or name the closed fixing bead",
                )
            )
        elif not fixed and fact.status == CLOSED:
            found.append(
                CheckFinding(
                    CheckKind.OWNER_CLOSED,
                    f"owner target {target.id} is closed",
                    "name open work that owns the outcome, propose new work, or propose fixed "
                    "if the closed bead fixed it",
                )
            )
    return found


def _fix_commits(proposal: Proposal, context: CheckContext) -> tuple[str, ...]:
    commits: list[str] = [proposal.fix_commit] if proposal.fix_commit else []
    if proposal.disposition is ProposalDisposition.FIXED:
        for target in proposal.targets:
            fact = context.targets.get(target.id)
            if target.role is TargetRole.OWNER and fact is not None and fact.status == CLOSED:
                commits.extend(fact.commits)
    return tuple(dict.fromkeys(c for c in commits if c))


def _fix(
    proposal: Proposal, context: CheckContext, repo: RepoQueries
) -> tuple[list[CheckFinding], str | None]:
    """The fix-claim checks and the computed version (``None``: not computed)."""
    if proposal.disposition not in FIX_DISPOSITIONS:
        return [], None
    if proposal.disposition is ProposalDisposition.PARTIALLY_FIXED and not (
        proposal.fix_commit or proposal.fixed_in
    ):
        return [], None  # nothing claimed about the fixed part; the residual is the work
    commits = _fix_commits(proposal, context)
    if not commits:
        return [
            CheckFinding(
                CheckKind.FIX_UNPROVEN,
                "the fix claim names no fixing commit (no fix_commit, and no closed fixing bead "
                "with a landed commit)",
                "name the closed fixing bead or the fixing commit in fix_commit",
            )
        ], None
    found: list[CheckFinding] = []
    for commit in commits:
        verdict = repo.is_ancestor(commit, context.branch)
        if verdict is Verdict.YES:
            continue
        cannot = verdict is not Verdict.NO
        found.append(
            CheckFinding(
                CheckKind.FIX_NOT_ON_BRANCH,
                f"fixing commit {commit} "
                + (
                    f"cannot be verified on {context.branch}"
                    if cannot
                    else f"is not on {context.branch}"
                ),
                "name a commit that landed on the integration branch, or propose tracked",
            )
        )
    if found:
        return found, None
    tags = [repo.first_tag_containing(commit) for commit in commits]
    released = [tag for tag in tags if tag]
    # the fix is released once its LAST commit is: the latest of the per-commit first tags
    computed = max(released, key=version_key) if len(released) == len(tags) else ""
    if proposal.fixed_in and proposal.fixed_in != computed:
        shown = computed or "no release yet (merged, unreleased)"
        found.append(
            CheckFinding(
                CheckKind.FIX_VERSION,
                f"fixed_in claims {proposal.fixed_in}, but the computed fix version is {shown}",
                f"adopt the computed version ({shown}) and re-review the reporter text",
            )
        )
    return found, computed


def _family(report: str, proposal: Proposal, run: Mapping[str, Proposal]) -> list[CheckFinding]:
    if not proposal.share_with:
        return []
    canonical = proposal.share_with
    if canonical == report:
        return [
            CheckFinding(
                CheckKind.SHARE_CYCLE,
                f"share_with names the report itself ({report})",
                "name the family's canonical report, or clear share_with on the canonical",
            )
        ]
    if canonical not in run:
        return [
            CheckFinding(
                CheckKind.SHARE_DANGLING,
                f"share_with names {canonical}, which has no proposal in this run",
                "propose the canonical in the same run, or name a canonical that is in it",
            )
        ]
    seen, current = [report], canonical
    while True:
        if current in seen:
            return [
                CheckFinding(
                    CheckKind.SHARE_CYCLE,
                    "share_with is a cycle: " + " -> ".join([*seen, current]),
                    "pick one canonical and clear its share_with",
                )
            ]
        following = run[current].share_with if current in run else ""
        if not following:
            break
        seen.append(current)
        current = following
    if current != canonical:
        end = (
            f"the canonical ({current})"
            if current in run
            else "the family's one canonical, proposed in this run"
        )
        return [
            CheckFinding(
                CheckKind.SHARE_CHAIN,
                f"share_with names {canonical}, which itself shares with "
                f"{run[canonical].share_with}: a family has one canonical",
                f"point share_with at {end}",
            )
        ]
    owner = run[canonical].disposition
    if owner not in OWNER_DISPOSITIONS:
        return [
            CheckFinding(
                CheckKind.CANONICAL_DISPOSITION,
                f"canonical {canonical} is {owner.value}, which owns no outcome to share",
                "give the canonical the family's owner disposition (new, tracked, fixed or "
                "partially-fixed), or dispose of each report on its own",
            )
        ]
    return []


def _fields(proposal: Proposal) -> list[CheckFinding]:
    d = proposal.disposition
    wrong: list[str] = []
    if d not in FIX_DISPOSITIONS and (proposal.fixed_in or proposal.fix_commit):
        wrong.append(f"{d.value} has no fixed_in or fix_commit (only a fix claim does)")
    if d is ProposalDisposition.FIXED and (proposal.type or proposal.priority):
        wrong.append("fixed has no type or priority (no work is created)")
    if (d is ProposalDisposition.DUPLICATE_OF) != bool(proposal.share_with):
        wrong.append("share_with goes with, and only with, duplicate-of")
    owners = [t for t in proposal.targets if t.role is TargetRole.OWNER]
    if d is ProposalDisposition.TRACKED and not owners:
        wrong.append("tracked names an owner target")
    if d is ProposalDisposition.NEW and owners:
        wrong.append("new work has no owner target (the work is created)")
    if d is ProposalDisposition.DUPLICATE_OF and owners:
        wrong.append("a duplicate has no owner target (the canonical holds it)")
    if d in TEXT_DISPOSITIONS and not proposal.reporter_text.strip():
        wrong.append(f"{d.value} needs reporter_text (the reason or the question)")
    if not wrong:
        return []
    return [
        CheckFinding(
            CheckKind.INCONSISTENT_FIELDS,
            f"{d.value}: " + "; ".join(wrong),
            "amend the proposal so its fields match its disposition",
        )
    ]


def _reporter_text(proposal: Proposal, context: CheckContext) -> list[CheckFinding]:
    text = proposal.reporter_text
    if not text:
        return []
    found: list[CheckFinding] = []
    leaks = unresolved(leak_check(text, context.leak_policy, sanitizer=context.sanitizer))
    if leaks:
        named = ", ".join(f"{f.id} {f.match!r}" for f in leaks)
        found.append(
            CheckFinding(
                CheckKind.REPORTER_TEXT_LEAK,
                f"reporter_text would leak: {named}",
                "rewrite reporter_text without internal ids, labels, seats, paths or secrets",
            )
        )
    if len(text) > context.reporter_text_limit:
        found.append(
            CheckFinding(
                CheckKind.REPORTER_TEXT_LENGTH,
                f"reporter_text is {len(text)} characters, over the "
                f"{context.reporter_text_limit} limit",
                "shorten reporter_text",
            )
        )
    return found


def _staleness(
    report: str, proposal: Proposal, context: CheckContext, repo: RepoQueries
) -> list[CheckFinding]:
    found: list[CheckFinding] = []
    current = context.thread_seqs.get(report, 0)
    if current > proposal.thread_seq:
        found.append(
            CheckFinding(
                CheckKind.STALE_THREAD,
                f"the thread is at seq {current}, past the {proposal.thread_seq} the analysis "
                "read (a newer reporter comment)",
                "re-review with the full thread and record its seq",
            )
        )
    paths = cited_paths(proposal.evidence)
    if not paths:
        return found
    if not proposal.analysed_commit:
        found.append(
            CheckFinding(
                CheckKind.STALE_UNKNOWN,
                "the evidence cites paths but the proposal records no analysed_commit",
                "record the commit the analysis read in analysed_commit",
            )
        )
        return found
    changed = repo.changed_paths(proposal.analysed_commit, context.branch)
    if changed is None:
        found.append(
            CheckFinding(
                CheckKind.STALE_UNKNOWN,
                f"cannot tell what {context.branch} changed since {proposal.analysed_commit}",
                "re-analyse at a commit of the integration branch",
            )
        )
        return found
    moved = sorted({c for c in paths for p in changed if _touches(p, c)})
    if moved:
        found.append(
            CheckFinding(
                CheckKind.STALE_PATHS,
                f"{context.branch} changed {', '.join(moved)} since {proposal.analysed_commit}",
                "re-review against the current integration branch and record the new commit",
            )
        )
    return found


def _check(
    report: str,
    proposal: Proposal,
    run: Mapping[str, Proposal],
    context: CheckContext,
    repo: RepoQueries,
) -> ProposalCheck:
    fix_findings, computed = _fix(proposal, context, repo)
    findings = [
        *_fields(proposal),
        *_targets(proposal, context),
        *fix_findings,
        *_family(report, proposal, run),
        *_reporter_text(proposal, context),
        *_staleness(report, proposal, context, repo),
    ]
    return ProposalCheck(report, tuple(findings), computed)


def check_proposal(
    report: str,
    proposal: Proposal,
    context: CheckContext,
    *,
    run: Mapping[str, Proposal] | None = None,
) -> ProposalCheck:
    """Every check on ``report``'s ``proposal``. ``run`` is the batch it is applied with (report
    id to proposal), where ``share_with`` must resolve; it defaults to this proposal alone."""
    family = dict(run) if run is not None else {}
    family.setdefault(report, proposal)
    return _check(report, proposal, family, context, _Memo(context.repo))


def check_run(run: Mapping[str, Proposal], context: CheckContext) -> tuple[ProposalCheck, ...]:
    """Every check on every proposal of ``run`` (report id to proposal), in report order. Each
    distinct repository question is asked once for the whole run."""
    repo = _Memo(context.repo)
    return tuple(_check(r, run[r], run, context, repo) for r in sorted(run))


def adopt_computed_fix(proposal: Proposal, check: ProposalCheck) -> Proposal:
    """``proposal`` with its claimed ``fixed_in`` overwritten by the computed version. A changed
    claim is an amendment, so the result is ``proposed`` and wants a fresh confirming review (the
    reporter text may quote the wrong version). Unchanged when nothing was computed or it agrees."""
    computed = check.computed_fixed_in
    if computed is None or computed == proposal.fixed_in:
        return proposal
    return amend(proposal, fixed_in=computed)


__all__ = [
    "CLOSED",
    "DEFAULT_REPORTER_TEXT_LIMIT",
    "FIX_DISPOSITIONS",
    "OWNER_DISPOSITIONS",
    "STALE_KINDS",
    "TEXT_DISPOSITIONS",
    "CheckContext",
    "CheckFinding",
    "CheckKind",
    "ProposalCheck",
    "RepoQueries",
    "TargetFact",
    "adopt_computed_fix",
    "check_proposal",
    "check_run",
    "cited_paths",
    "version_key",
]
