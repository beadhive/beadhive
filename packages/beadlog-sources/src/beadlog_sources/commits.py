"""The neutral ``git-commits`` strategy: bead ids in commit messages -> ``git.commits`` events.

BeadHive-free (``docs/design/backfill-platform-adr.md`` section 2). It reads a repository's git
history once (``git log``) and proposes, per bead, one ``set`` of ``metadata.git.commits`` that
*appends* the shas of the commits whose messages name that bead
(``docs/design/bead-commit-linkage-contract.md``: the flat key ``git.commits`` holds a JSON array
of full shas, itself serialized as a string).

**The two-step matcher** (promoted from the bh-rwryq spike and used verbatim in behavior):

1. :func:`extract_candidates` is deliberately loose: a namespace-anchored pattern over the whole
   message (subject *and* body) that over-matches ``bh-infra``, ``bh-version`` and every
   branch/hive name of that shape;
2. :func:`resolve_candidates` keeps only tokens that exist in the hive's own bead id set and
   drops the rest, so a loose pattern alone can never poison the linkage.

**Bead ids come from the baseline** (:attr:`ExtractRequest.baseline`), never a guessed prefix or a
store call: the live ids are the baseline beads of ``request.group`` that exist, and the
namespaces the pattern anchors on are read off those ids. A baseline with no beads is refused.

**The history is read through the git reader** (:func:`beadlog_sources.git_reader.read_git`): its
``change`` artifacts (commits and merges) carry the bead-looking tokens of each message's
subject, body and trailer values as ``bead-token`` refs, recognised with the namespaces read off
the baseline. The strategy adds the tokens of trailer *keys* (a closing ``bh-4o07n: done``
paragraph parses as a trailer) so every token of the whole message is a candidate, as before.

**Append-only, idempotent.** The proposed value is the baseline's current list with the new shas
appended in oldest-first order: existing entries keep their place and are never removed. A bead
whose commits are all recorded yields no event, so extracting again after an apply proposes
nothing. A bead whose existing value is not a clean JSON array of strings **disagrees** with the
proposal: it is reported (:attr:`CommitsReport.drift`, and the archive annotation the ``beadlog``
CLI writes) and no event is emitted for it, so it is never overwritten.

**Events.** ``set`` of ``metadata.git.commits`` with ``provenance: derived`` (the bead id was
derived deterministically from observed commits), ``occurred_at`` = the committer date of the
newest appended commit (raised to one microsecond past the time of the baseline's latest
``create`` or ``set`` of that field when the commit does not postdate it, so the new value
strictly wins the last-writer-wins fold: a tie would be broken by event id, a hash, so a commit
made in the same second as the one already recorded could lose; :func:`just_after`),
``recorded_at`` null, ``source`` = ``git`` / ``commit/<newest sha>`` and one ``commit``
evidence entry per appended sha.

**Shallow clones are refused**: their history is truncated, so beads whose commits predate the cut
would be silently missed. Option ``allow_shallow=true`` accepts the truncated history.

Options (``ExtractRequest.options``): ``rev`` (default ``HEAD``), ``since`` (a commit: only
``since..rev``; otherwise a date handed to ``git log --since``), ``allow_shallow`` and ``git``
(the binary, default ``git``).

The token recognition (:func:`candidate_pattern`) lives in :mod:`beadlog_sources.tokens` and the
git plumbing (:func:`read_commits`, :func:`require_full_history`, the errors) in
:mod:`beadlog_sources.git_reader`; both are re-exported here.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from beadlog import (
    BeadEvent,
    BeadRef,
    BeadState,
    EventKind,
    Evidence,
    fold,
    format_timestamp,
    instant,
    mint_event,
)

from ._sources import commit_time, floors, truthy
from .contracts import ExtractRequest
from .evidence import Artifact, Category
from .git_reader import (
    SHALLOW_REMEDIATION,
    Commit,
    CommitsSourceError,
    ShallowCloneError,
    is_shallow,
    read_commits,
    read_git,
    require_full_history,
    resolve_commit,
)
from .readers import ReadRequest
from .tokens import CANDIDATE_TEMPLATE, candidate_pattern, find_bead_tokens

STRATEGY_NAME = "git-commits"
STRATEGY_VERSION = "0.1.0"
SOURCE_SYSTEM = "git"
# The BeadLog field the linkage lives in: the flat bd metadata key ``git.commits``.
FIELD = "metadata.git.commits"
ANNOTATION_KEY = "bdlg.git-commits"

# -- the canonical matcher -----------------------------------------------------------------

# Separators a greedy candidate can be trimmed back on, one segment at a time.
_TRIM_RE = re.compile(r"[.-][0-9a-z]+$")


def extract_candidates(text: str, pattern: re.Pattern[str]) -> list[str]:
    """Every bead-ID-shaped token of a full commit message (subject AND body), lowercased, in
    order of appearance, duplicates preserved."""
    return [match.group(0).lower() for match in pattern.finditer(text)]


def resolve_candidate(candidate: str, live_ids: frozenset[str]) -> str | None:
    """Map one candidate token to a real (lowercase) bead id, or ``None`` if it is noise.

    Longest-prefix resolution: trim one ``[.-]segment`` at a time (``bh-q160-style`` ->
    ``bh-q160``) and return the first form in ``live_ids``; ``bh-infra`` and the like trim down
    to nothing that resolves.
    """
    probe = candidate
    while probe:
        if probe in live_ids:
            return probe
        trimmed = _TRIM_RE.sub("", probe, count=1)
        if trimmed == probe:
            return None
        probe = trimmed
    return None


def resolve_candidates(
    candidates: Sequence[str], live_ids: frozenset[str]
) -> tuple[list[str], list[str]]:
    """Split candidates into ``(resolved bead ids, dropped false positives)``."""
    resolved: list[str] = []
    dropped: list[str] = []
    for candidate in candidates:
        hit = resolve_candidate(candidate, live_ids)
        if hit is None:
            dropped.append(candidate)
        else:
            resolved.append(hit)
    return resolved, dropped


def derive_namespaces(live_ids: Iterable[str], coverage: float = 0.95) -> set[str]:
    """The hive's id namespace(s), read off its own ids: the most frequent leading hyphen
    segments, until ``coverage`` of the ids is accounted for."""
    counts = Counter(bead_id.split("-", 1)[0] for bead_id in live_ids if "-" in bead_id)
    total = sum(counts.values())
    if not total:
        return set()
    kept: set[str] = set()
    seen = 0
    for name, count in counts.most_common():
        kept.add(name)
        seen += count
        if seen / total >= coverage:
            break
    return kept


# -- reading the history through the git reader ----------------------------------------------

# A ``since`` date the git reader would take for a sha (all hex digits, such as ``20260901``) is
# handed over with a leading space: git's date parser skips it, the reader's sha test fails.
_HEX_SINCE = re.compile(r"[0-9a-fA-F]{7,64}")


def _runner(root: Path | str) -> Any:
    """The reader's command seam with this strategy's failure contract: output decoded as UTF-8
    with replacement, any failure a :class:`CommitsSourceError` naming the git command."""

    def run(argv: Sequence[str]) -> str:
        try:
            result = subprocess.run(
                list(argv),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        except OSError as exc:
            raise CommitsSourceError(f"cannot run {argv[0]}: {exc}") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip().splitlines()
            command = " ".join(argv[5:7])  # after ``git -C <root> -c core.quotepath=off``
            raise CommitsSourceError(
                f"git {command} failed in {root} ({result.returncode}): "
                f"{detail[-1] if detail else ''}"
            )
        return result.stdout

    return run


def read_changes(
    root: Path | str,
    rev: str = "HEAD",
    *,
    since: str | None = None,
    namespaces: Iterable[str] = (),
    git: str = "git",
) -> list[Artifact]:
    """The ``change`` artifacts (commits and merges) of ``rev``, newest first, read by the git
    reader with bead tokens anchored on ``namespaces``. ``since`` is a commit (only
    ``since..rev``) or else a date for ``git log --since``. The caller checks for a shallow
    clone (:func:`require_full_history`)."""
    options: dict[str, Any] = {
        "rev": rev,
        "git": git,
        "allow_shallow": "true",
        "namespaces": ",".join(sorted(set(namespaces))),
    }
    if since:
        sha = resolve_commit(root, since, git)
        options["since"] = (
            sha if sha is not None else (f" {since}" if _HEX_SINCE.fullmatch(since) else since)
        )
    read = read_git(ReadRequest(root=Path(root), options=options), _runner(root))
    return [a for a in read.artifacts if a.category is Category.CHANGE]


def change_tokens(change: Artifact, pattern: re.Pattern[str]) -> list[str]:
    """Every bead-looking token of one change: the reader's ``bead-token`` refs (subject, body
    and trailer values) plus those of its trailer keys."""
    found = [ref.raw for ref in change.refs if ref.kind == "bead-token"]
    for key in change.attrs.get("trailers") or {}:
        found.extend(find_bead_tokens(key, pattern))
    return found


# -- the pure domain -----------------------------------------------------------------------


@dataclass
class CommitMatches:
    """Commits grouped by the beads they resolve to (oldest first within each bead)."""

    by_bead: dict[str, list[Commit]] = field(default_factory=dict)
    examined: int = 0
    unmatched: int = 0


def match_commits(
    commits_oldest_first: Iterable[Commit],
    live_ids: frozenset[str],
    pattern: re.Pattern[str] | None = None,
) -> CommitMatches:
    """Group resolvable commits by bead. A commit naming several beads (a batch merge, a body
    that mentions a related bead) lands on each of them; repeat mentions count once. Bead ids
    are the lowercase members of ``live_ids``."""
    pattern = pattern or candidate_pattern(derive_namespaces(live_ids))
    out = CommitMatches()
    for commit in commits_oldest_first:
        out.examined += 1
        resolved, _dropped = resolve_candidates(
            extract_candidates(commit.message, pattern), live_ids
        )
        hit = set(resolved)
        if not hit:
            out.unmatched += 1
            continue
        for bead_id in hit:
            out.by_bead.setdefault(bead_id, []).append(commit)
    return out


@dataclass
class ChangeMatches:
    """Change artifacts grouped by the beads they resolve to (oldest first within each bead)."""

    by_bead: dict[str, list[Artifact]] = field(default_factory=dict)
    examined: int = 0
    unmatched: int = 0


def match_changes(
    changes_oldest_first: Iterable[Artifact],
    live_ids: frozenset[str],
    pattern: re.Pattern[str],
) -> ChangeMatches:
    """:func:`match_commits` over the git reader's change artifacts (:func:`change_tokens`)."""
    out = ChangeMatches()
    for change in changes_oldest_first:
        out.examined += 1
        resolved, _dropped = resolve_candidates(change_tokens(change, pattern), live_ids)
        hit = set(resolved)
        if not hit:
            out.unmatched += 1
            continue
        for bead_id in hit:
            out.by_bead.setdefault(bead_id, []).append(change)
    return out


def parse_linkage(raw: Any) -> list[str] | None:
    """The shas of a stored ``git.commits`` value, or ``None`` when it is not a clean JSON array
    of strings (the contract's wire format)."""
    if not isinstance(raw, str):
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(parsed, list) or not all(isinstance(sha, str) for sha in parsed):
        return None
    return list(parsed)


def dump_linkage(shas: Sequence[str]) -> str:
    """The wire format of a ``git.commits`` value: a JSON array serialized as a string."""
    return json.dumps(list(shas))


def just_after(value: str) -> str:
    """The first whole microsecond strictly after the canonical timestamp ``value``.

    The time a proposal is raised to when its newest commit does not postdate the baseline's
    decider of the field: equal is not enough, because two events at one instant (both with a
    null ``recorded_at``) are ordered by id, which is a hash, so the new value would win or lose
    the last-writer-wins fold by chance (bh-hxodo).
    """
    _, seconds, nanos = instant(value)
    moment = datetime.fromisoformat(seconds).replace(tzinfo=UTC)
    return format_timestamp(moment + timedelta(microseconds=nanos // 1000 + 1))


# -- the strategy --------------------------------------------------------------------------


@dataclass(frozen=True)
class Drift:
    """A bead whose existing ``git.commits`` disagrees with the proposal; nothing is emitted."""

    bead: str
    reason: str
    existing: str
    proposed: tuple[str, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "bead": self.bead,
            "reason": self.reason,
            "existing": self.existing,
            "proposed": list(self.proposed),
        }


@dataclass
class CommitsReport:
    """What one extraction saw, for the operator and the archive annotation."""

    rev: str
    since: str | None
    examined: int = 0
    unmatched: int = 0
    beads: int = 0
    present: int = 0
    proposed: int = 0
    new_shas: int = 0
    drift: list[Drift] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "strategy": STRATEGY_NAME,
            "version": STRATEGY_VERSION,
            "rev": self.rev,
            "since": self.since,
            "examined": self.examined,
            "unmatched": self.unmatched,
            "beads": self.beads,
            "present": self.present,
            "proposed": self.proposed,
            "new_shas": self.new_shas,
            "drift": [item.to_json() for item in self.drift],
        }


@dataclass(frozen=True)
class CommitsProposal:
    events: tuple[BeadEvent, ...]
    report: CommitsReport


def _live_beads(states: Mapping[BeadRef, BeadState], group: str) -> dict[str, BeadState]:
    """Lowercase id -> folded state for the beads of ``group`` that exist and are not erased."""
    return {
        ref.id.lower(): state
        for ref, state in states.items()
        if ref.group == group and state.exists and not state.deleted and not state.erased
    }


def propose(
    changes_newest_first: Sequence[Artifact],
    request: ExtractRequest,
    *,
    rev: str = "HEAD",
    since: str | None = None,
    states: Mapping[BeadRef, BeadState] | None = None,
) -> CommitsProposal:
    """The events appending the git reader's ``changes`` to the beads of ``request.baseline``
    (module doc). ``states`` is the folded baseline when the caller already has it."""
    live = _live_beads(fold(request.baseline) if states is None else states, request.group)
    if not live:
        raise CommitsSourceError(
            f"the baseline holds no beads of group {request.group!r} to resolve commit "
            "messages against: export the hive first and pass it as the baseline"
        )
    live_ids = frozenset(live)
    pattern = candidate_pattern(derive_namespaces(live_ids))
    matches = match_changes(list(reversed(changes_newest_first)), live_ids, pattern)
    decided = floors(request.baseline, request.group, FIELD)
    report = CommitsReport(
        rev=rev,
        since=since,
        examined=matches.examined,
        unmatched=matches.unmatched,
        beads=len(matches.by_bead),
    )
    events: list[BeadEvent] = []
    for key in sorted(matches.by_bead):
        state = live[key]
        bead = state.bead
        raw = state.fields.get(FIELD)
        shas = [str(change.attrs["sha"]) for change in matches.by_bead[key]]
        existing: list[str] = []
        if raw is not None:
            parsed = parse_linkage(raw)
            if parsed is None:
                report.drift.append(
                    Drift(
                        bead.id,
                        "existing git.commits is not a JSON array of strings",
                        str(raw)[:200],
                        tuple(shas),
                    )
                )
                continue
            existing = parsed
        seen = set(existing)
        new: list[Artifact] = []
        for sha, change in zip(shas, matches.by_bead[key], strict=True):
            if sha not in seen:
                seen.add(sha)
                new.append(change)
        if not new:
            report.present += 1
            continue
        timed = [
            (instant(t), t, change)
            for change in new
            if (t := commit_time(str(change.raw_attrs["committed_at"])))
        ]
        if timed:
            _, when, newest = max(timed, key=lambda item: item[0])
        else:
            when, newest = None, new[-1]
        floor = decided.get(bead.id)
        if floor is not None and (when is None or instant(when) <= instant(floor)):
            when = just_after(floor)
        new_shas = [str(change.attrs["sha"]) for change in new]
        events.append(
            mint_event(
                str(EventKind.SET),
                bead,
                {"field": FIELD, "value": dump_linkage([*existing, *new_shas])},
                occurred_at=when,
                recorded_at=None,
                extracted_at=request.extracted_at,
                source=(SOURCE_SYSTEM, f"commit/{newest.attrs['sha']}"),
                provenance="derived",
                evidence=[Evidence("commit", sha) for sha in new_shas],
            )
        )
        report.new_shas += len(new)
    report.proposed = len(events)
    return CommitsProposal(tuple(events), report)


class GitCommitsStrategy:
    """Source strategy ``git-commits``: the commits of ``request.root`` as ``git.commits``
    appends for the beads of ``request.baseline`` (module doc). ``report()`` describes the last
    extraction (the ``beadlog extract`` CLI stores it as the archive annotation)."""

    name = STRATEGY_NAME
    version = STRATEGY_VERSION

    def __init__(self) -> None:
        self._report: CommitsReport | None = None

    def propose(self, request: ExtractRequest) -> CommitsProposal:
        options = request.options
        binary = str(options.get("git", "git"))
        rev = str(options.get("rev", "HEAD"))
        since = str(options["since"]) if options.get("since") else None
        if not truthy(options.get("allow_shallow", "false")):
            require_full_history(request.root, binary)
        states = fold(request.baseline)
        namespaces = derive_namespaces(_live_beads(states, request.group))
        changes = read_changes(request.root, rev, since=since, namespaces=namespaces, git=binary)
        proposal = propose(changes, request, rev=rev, since=since, states=states)
        self._report = proposal.report
        return proposal

    def extract(self, request: ExtractRequest) -> tuple[BeadEvent, ...]:
        return self.propose(request).events

    def report(self) -> Mapping[str, Any] | None:
        """The last extraction's report as JSON (``None`` before the first extraction)."""
        return None if self._report is None else self._report.to_json()


__all__ = [
    "ANNOTATION_KEY",
    "CANDIDATE_TEMPLATE",
    "FIELD",
    "SHALLOW_REMEDIATION",
    "SOURCE_SYSTEM",
    "STRATEGY_NAME",
    "STRATEGY_VERSION",
    "Commit",
    "ChangeMatches",
    "CommitMatches",
    "CommitsProposal",
    "CommitsReport",
    "CommitsSourceError",
    "Drift",
    "GitCommitsStrategy",
    "ShallowCloneError",
    "candidate_pattern",
    "change_tokens",
    "commit_time",
    "derive_namespaces",
    "dump_linkage",
    "extract_candidates",
    "is_shallow",
    "just_after",
    "match_changes",
    "match_commits",
    "parse_linkage",
    "propose",
    "read_changes",
    "read_commits",
    "require_full_history",
    "resolve_candidate",
    "resolve_candidates",
]
