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

**Append-only, idempotent.** The proposed value is the baseline's current list with the new shas
appended in oldest-first order: existing entries keep their place and are never removed. A bead
whose commits are all recorded yields no event, so extracting again after an apply proposes
nothing. A bead whose existing value is not a clean JSON array of strings **disagrees** with the
proposal: it is reported (:attr:`CommitsReport.drift`, and the archive annotation the ``beadlog``
CLI writes) and no event is emitted for it, so it is never overwritten.

**Events.** ``set`` of ``metadata.git.commits`` with ``provenance: derived`` (the bead id was
derived deterministically from observed commits), ``occurred_at`` = the committer date of the
newest appended commit (raised to the time of the baseline's latest ``create`` or
``set`` of that field when the commit predates it, so the new value wins the last-writer-wins
fold), ``recorded_at`` null, ``source`` = ``git`` / ``commit/<newest sha>`` and one ``commit``
evidence entry per appended sha.

**Shallow clones are refused**: their history is truncated, so beads whose commits predate the cut
would be silently missed. Option ``allow_shallow=true`` accepts the truncated history.

Options (``ExtractRequest.options``): ``rev`` (default ``HEAD``), ``since`` (a commit: only
``since..rev``; otherwise a date handed to ``git log --since``), ``allow_shallow`` and ``git``
(the binary, default ``git``).
"""

from __future__ import annotations

import json
import re
import subprocess
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from beadlog import (
    BeadEvent,
    BeadRef,
    BeadState,
    EventKind,
    Evidence,
    TimestampError,
    fold,
    format_timestamp,
    instant,
    mint_event,
)

from .contracts import ExtractRequest

STRATEGY_NAME = "git-commits"
STRATEGY_VERSION = "0.1.0"
SOURCE_SYSTEM = "git"
# The BeadLog field the linkage lives in: the flat bd metadata key ``git.commits``.
FIELD = "metadata.git.commits"
ANNOTATION_KEY = "bdlg.git-commits"
SHALLOW_REMEDIATION = (
    "run `git fetch --unshallow` to fetch the full history "
    "(or pass option allow_shallow=true to extract from the truncated history)"
)

# -- the canonical matcher -----------------------------------------------------------------

# A bead ID is ``<namespace>-<slug>`` with optional dotted child suffixes, where the slug is
# base36-ish and may itself carry hyphenated segments (`bh-4o07n`, `bhui-do1`, `bh-baml-8u9.17`).
# Anchored on the namespace so it does not match every hyphenated English word, but greedy to the
# right so a multi-segment ID arrives as ONE token; `resolve_candidate` trims back to the longest
# segment that actually resolves. A token glued to surrounding word characters is not matched.
CANDIDATE_TEMPLATE = r"(?<![0-9A-Za-z_-])(?:{ns})-[0-9a-z]+(?:[.-][0-9a-z]+)*(?![0-9A-Za-z_])"

# Separators a greedy candidate can be trimmed back on, one segment at a time.
_TRIM_RE = re.compile(r"[.-][0-9a-z]+$")


def candidate_pattern(namespaces: Iterable[str]) -> re.Pattern[str]:
    """The candidate regex for one hive's namespace set (the leading segments of its bead ids)."""
    names = sorted({str(name) for name in namespaces}, key=len, reverse=True)
    if not names:
        raise ValueError("no namespaces to anchor on: the live bead ID set was empty")
    alt = "|".join(re.escape(name) for name in names)
    return re.compile(CANDIDATE_TEMPLATE.format(ns=alt), re.IGNORECASE)


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


# -- git adapter ---------------------------------------------------------------------------

_FIELD_SEP = "\x1f"
_RECORD_SEP = "\x1e"


class CommitsSourceError(ValueError):
    """The git history cannot be read or the baseline cannot resolve bead ids (a ``ValueError``,
    so the ``beadlog`` CLI reports it as a failed operation)."""


class ShallowCloneError(CommitsSourceError):
    """The repository is a shallow clone, so its history is truncated."""


@dataclass(frozen=True)
class Commit:
    """One commit: ``message`` is subject AND body; ``date`` the committer date (ISO 8601)."""

    sha: str
    parents: int
    subject: str
    message: str
    date: str


def _git(
    root: Path | str, args: Sequence[str], git: str = "git"
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            [git, "-C", str(root), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        raise CommitsSourceError(f"cannot run {git}: {exc}") from exc


def _git_out(root: Path | str, args: Sequence[str], git: str = "git") -> str:
    result = _git(root, args, git)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        raise CommitsSourceError(
            f"git {' '.join(args[:2])} failed in {root} ({result.returncode}): "
            f"{detail[-1] if detail else ''}"
        )
    return result.stdout


def is_shallow(root: Path | str, git: str = "git") -> bool:
    """Whether the repository at ``root`` is a shallow clone."""
    return _git_out(root, ["rev-parse", "--is-shallow-repository"], git).strip() == "true"


def require_full_history(root: Path | str, git: str = "git") -> None:
    """Refuse a shallow clone with the remediation text."""
    if is_shallow(root, git):
        raise ShallowCloneError(
            f"{root} is a shallow clone: its history is truncated, so beads whose commits "
            f"predate the cut would be missed; {SHALLOW_REMEDIATION}"
        )


def read_commits(
    root: Path | str,
    rev: str = "HEAD",
    *,
    since: str | None = None,
    limit: int | None = None,
    git: str = "git",
) -> list[Commit]:
    """Walk ``rev`` backwards, newest first.

    ``since`` narrows the walk: a commit (anything ``git`` resolves to one) means only the
    commits after it, ``since..rev``; any other value is a date for ``git log --since``.
    """
    fmt = _FIELD_SEP.join(["%H", "%P", "%cI", "%s", "%B"]) + _RECORD_SEP
    args = ["log", f"--pretty=format:{fmt}", "--no-color"]
    if since:
        probe = _git(root, ["rev-parse", "--verify", "--quiet", f"{since}^{{commit}}"], git)
        if probe.returncode == 0:
            args.append(f"{since}..{rev}")
        else:
            args += [f"--since={since}", rev]
    else:
        args.append(rev)
    if limit:
        args.append(f"-n{limit}")
    raw = _git_out(root, args, git)
    commits: list[Commit] = []
    for chunk in raw.split(_RECORD_SEP):
        chunk = chunk.strip("\n")
        if not chunk.strip():
            continue
        parts = chunk.split(_FIELD_SEP)
        if len(parts) < 5:
            continue
        sha, parents, date, subject, body = parts[:5]
        commits.append(
            Commit(
                sha=sha,
                parents=len(parents.split()),
                subject=subject,
                # %B already holds subject + body; both stay explicit so "subject AND body"
                # is visible rather than implied.
                message=f"{subject}\n{body}",
                date=date,
            )
        )
    return commits


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


def commit_time(value: str) -> str | None:
    """A git ISO 8601 date as a canonical BeadLog timestamp, or ``None`` if unparsable."""
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if moment.tzinfo is None:
            return None
        return format_timestamp(moment)
    except (ValueError, TimestampError):
        return None


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


def _truthy(value: Any) -> bool:
    return str(value).lower() in ("1", "true", "yes", "on")


def _live_beads(states: Mapping[BeadRef, BeadState], group: str) -> dict[str, BeadState]:
    """Lowercase id -> folded state for the beads of ``group`` that exist and are not erased."""
    return {
        ref.id.lower(): state
        for ref, state in states.items()
        if ref.group == group and state.exists and not state.deleted and not state.erased
    }


def _floors(baseline: Iterable[BeadEvent], group: str) -> dict[str, str]:
    """Per bead, the time of the latest baseline event that decides ``metadata.git.commits``
    (a create, a checkpoint or a set of the field), as the event states it."""
    out: dict[str, str] = {}
    for event in baseline:
        if event.bead.group != group:
            continue
        kind = event.kind
        decides = kind in (EventKind.CREATE, EventKind.CHECKPOINT) or (
            kind is EventKind.SET and event.data.get("field") == FIELD
        )
        when = event.occurred_at or event.recorded_at
        if not decides or when is None:
            continue
        known = out.get(event.bead.id)
        if known is None or instant(when) > instant(known):
            out[event.bead.id] = when
    return out


def propose(
    commits_newest_first: Sequence[Commit],
    request: ExtractRequest,
    *,
    rev: str = "HEAD",
    since: str | None = None,
) -> CommitsProposal:
    """The events appending ``commits`` to the beads of ``request.baseline`` (module doc)."""
    states = fold(request.baseline)
    live = _live_beads(states, request.group)
    if not live:
        raise CommitsSourceError(
            f"the baseline holds no beads of group {request.group!r} to resolve commit "
            "messages against: export the hive first and pass it as the baseline"
        )
    live_ids = frozenset(live)
    matches = match_commits(list(reversed(commits_newest_first)), live_ids)
    floors = _floors(request.baseline, request.group)
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
        existing: list[str] = []
        if raw is not None:
            parsed = parse_linkage(raw)
            if parsed is None:
                report.drift.append(
                    Drift(
                        bead.id,
                        "existing git.commits is not a JSON array of strings",
                        str(raw)[:200],
                        tuple(commit.sha for commit in matches.by_bead[key]),
                    )
                )
                continue
            existing = parsed
        seen = set(existing)
        new: list[Commit] = []
        for commit in matches.by_bead[key]:
            if commit.sha not in seen:
                seen.add(commit.sha)
                new.append(commit)
        if not new:
            report.present += 1
            continue
        timed = [(instant(t), t, commit) for commit in new if (t := commit_time(commit.date))]
        if timed:
            _, when, newest = max(timed, key=lambda item: item[0])
        else:
            when, newest = None, new[-1]
        floor = floors.get(bead.id)
        if floor is not None and (when is None or instant(when) < instant(floor)):
            when = floor
        events.append(
            mint_event(
                str(EventKind.SET),
                bead,
                {"field": FIELD, "value": dump_linkage([*existing, *(c.sha for c in new)])},
                occurred_at=when,
                recorded_at=None,
                extracted_at=request.extracted_at,
                source=(SOURCE_SYSTEM, f"commit/{newest.sha}"),
                provenance="derived",
                evidence=[Evidence("commit", commit.sha) for commit in new],
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
        if not _truthy(options.get("allow_shallow", "false")):
            require_full_history(request.root, binary)
        commits = read_commits(request.root, rev, since=since, git=binary)
        proposal = propose(commits, request, rev=rev, since=since)
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
    "CommitMatches",
    "CommitsProposal",
    "CommitsReport",
    "CommitsSourceError",
    "Drift",
    "GitCommitsStrategy",
    "ShallowCloneError",
    "candidate_pattern",
    "commit_time",
    "derive_namespaces",
    "dump_linkage",
    "extract_candidates",
    "is_shallow",
    "match_commits",
    "parse_linkage",
    "propose",
    "read_commits",
    "require_full_history",
    "resolve_candidate",
    "resolve_candidates",
]
