#!/usr/bin/env python3
"""Canonical bead-ID -> commit correlator (spike bh-rwryq).

Measures how many commits in a repo's git history mention **at least one bead ID that
actually resolves** in that repo's own live bead store. Two consumers are expected to
copy the two functions marked CANONICAL below verbatim rather than reinvent them:
the durable-linkage backfill, and beadhive-ui's commit correlator.

The whole point is the **two-step filter**, not a bigger regex:

  1. ``extract_candidates`` — a deliberately loose, namespace-anchored pattern over the
     full commit message (subject AND body). It over-matches on purpose: ``bh-infra``,
     ``bh-version``, ``bh-harness`` and every branch/hive name of that shape come back
     as candidates.
  2. ``resolve_candidates`` — an existence check against the repo's own live bead ID
     set. Anything that does not resolve is DROPPED, not counted. This is what makes
     the signal safe to build a staleness overlay on; a loose pattern alone permanently
     poisons it.

Hive scoping is a hard constraint: the live ID set is loaded per repo, from that repo's
own store (``bh bd export`` run with cwd inside it). Resolution is NEVER aggregated
across hives -- a token found in repo A is only ever checked against repo A's beads.

Not product code. Spike artifact for bh-rwryq.1 / .2 / .3; see
``docs/spikes/bh-rwryq.3-correlation-yield.md``.

Usage::

    uv run python scripts/bead_commit_correlation.py --repo . --limit 500
    uv run python scripts/bead_commit_correlation.py --repo . --limit 500 --json
    uv run python scripts/bead_commit_correlation.py --repo . --show-false-positives
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

# --------------------------------------------------------------------------------------
# CANONICAL part 1 of 2 — candidate extraction
# --------------------------------------------------------------------------------------

# A bead ID is ``<namespace>-<slug>`` with optional dotted child suffixes, where the slug
# is base36-ish and may itself carry hyphenated segments (beadhive-family hives in the
# wild use `bh-4o07n`, `bhui-do1`, `bh-baml-8u9.17`). The pattern below is anchored on the
# namespace so it does not match every hyphenated English word, but is greedy to the right
# so a multi-segment ID arrives as ONE token. Over-matching to the right is safe and
# intended: `resolve_candidates` trims back to the longest segment that actually resolves.
#
# Deliberately NOT matched: a token glued to surrounding word characters (so `foobh-1` and
# `bh-1x` inside `bh-1xZ` do not produce phantom candidates).
CANDIDATE_TEMPLATE = r"(?<![0-9A-Za-z_-])(?:{ns})-[0-9a-z]+(?:[.-][0-9a-z]+)*(?![0-9A-Za-z_])"

# Separators a greedy candidate can be trimmed back on, longest-prefix first.
_TRIM_RE = re.compile(r"[.-][0-9a-z]+$")


def candidate_pattern(namespaces: Iterable[str]) -> re.Pattern[str]:
    """Build the CANONICAL candidate regex for one hive's namespace set.

    ``namespaces`` is the set of leading segments observed in that repo's live bead IDs
    (e.g. ``{"bh"}`` for beadhive, ``{"bhui"}`` for beadhive-ui). Anchoring on the hive's
    own namespace is what keeps the loose stage from matching arbitrary hyphenated prose.
    """
    names = sorted({str(n) for n in namespaces}, key=len, reverse=True)
    if not names:
        raise ValueError("no namespaces to anchor on: the live bead ID set was empty")
    alt = "|".join(re.escape(n) for n in names)
    return re.compile(CANDIDATE_TEMPLATE.format(ns=alt), re.IGNORECASE)


def extract_candidates(text: str, pattern: re.Pattern[str]) -> list[str]:
    """CANONICAL: pull every bead-ID-shaped token out of a full commit message.

    ``text`` must be subject AND body -- section 06 measured hygiene on both, and the
    tool-generated ``chore(merge): bead <id>`` shape puts the ID in the subject while
    hand-written trailers put it in the body.

    Returns candidates in order of appearance, lowercased, duplicates preserved (mention
    counts are what the false-positive rate is computed over).
    """
    return [m.group(0).lower() for m in pattern.finditer(text)]


# --------------------------------------------------------------------------------------
# CANONICAL part 2 of 2 — resolve-backed filtering
# --------------------------------------------------------------------------------------


def resolve_candidate(candidate: str, live_ids: frozenset[str]) -> str | None:
    """CANONICAL: map one candidate token to a real bead ID, or ``None`` if it is noise.

    Longest-prefix resolution: the greedy pattern may have swept trailing prose into the
    token (``bh-q160-style``), so trim one ``[.-]segment`` at a time and return the first
    form that exists in the live store. ``bh-infra`` / ``bh-version`` / ``bh-harness``
    trim down to nothing that resolves and correctly return ``None``.
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
    candidates: list[str], live_ids: frozenset[str]
) -> tuple[list[str], list[str]]:
    """CANONICAL: split candidates into (resolved bead IDs, dropped false positives).

    O(1) per candidate against a set loaded ONCE per repo -- a 500-commit walk issues one
    ``bh bd export``, never hundreds of ``bh bd show`` lookups.
    """
    resolved: list[str] = []
    dropped: list[str] = []
    for cand in candidates:
        hit = resolve_candidate(cand, live_ids)
        if hit is None:
            dropped.append(cand)
        else:
            resolved.append(hit)
    return resolved, dropped


@cache
def load_live_ids(repo: str) -> frozenset[str]:
    """Load one repo's live bead ID set from its OWN store. Cached per repo path.

    Uses ``bh bd export`` (hive-aware; bare ``bd`` can hit the wrong database) with cwd
    inside the repo, so the hive scoping is structural rather than a convention. No new
    data access path is introduced.
    """
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as handle:
        out = Path(handle.name)
    try:
        proc = subprocess.run(
            ["bh", "bd", "export", "--all", "-o", str(out)],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"`bh bd export` failed in {repo} (exit {proc.returncode}): "
                f"{proc.stderr.strip()[-500:]}"
            )
        ids: set[str] = set()
        for line in out.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            bead_id = record.get("id")
            if isinstance(bead_id, str) and bead_id:
                ids.add(bead_id.lower())
        if not ids:
            raise RuntimeError(f"`bh bd export` returned no bead IDs for {repo}")
        return frozenset(ids)
    finally:
        out.unlink(missing_ok=True)


def derive_namespaces(live_ids: frozenset[str], coverage: float = 0.95) -> set[str]:
    """Infer the hive's ID namespace(s) from its own live IDs.

    ``.beads/config.yaml`` leaves ``issue-prefix`` commented out in all three
    beadhive-family repos, so the namespace is read off the data instead: take the leading
    hyphen segment of every live ID and keep the most frequent ones until ``coverage`` of
    the corpus is accounted for. That yields ``bh`` / ``bhui`` / ``bh`` respectively.
    """
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


# --------------------------------------------------------------------------------------
# Commit walk + bucketing (spike reporting; not part of the canonical two functions)
# --------------------------------------------------------------------------------------

_FIELD_SEP = "\x1f"
_RECORD_SEP = "\x1e"

# "Legitimately unmappable": no single bead to point at BY CONSTRUCTION, per bh-rwryq's
# framing. Kept deliberately narrow -- widening this bucket flatters the yield number.
_BUMP_RE = re.compile(
    r"^(?:bump:\s*version|bump version|chore\(release\)|release:|bump version to)\b", re.IGNORECASE
)
_BATCH_MERGE_RE = re.compile(
    r"^(?:chore\(merge\):\s*batch\b|merge\s+(?:pull request|branch|remote-tracking)\b)",
    re.IGNORECASE,
)


@dataclass
class Commit:
    sha: str
    parents: int
    subject: str
    message: str
    date: str


@dataclass
class RepoReport:
    repo: str
    rev: str = "HEAD"
    namespaces: list[str] = field(default_factory=list)
    live_ids: int = 0
    total_commits_in_repo: int = 0
    window_requested: int = 0
    examined: int = 0
    window_is_whole_history: bool = False
    linked_loose: int = 0
    linked_tight: int = 0
    linked_loose_only: int = 0
    unmappable_bump: int = 0
    unmappable_batch_merge: int = 0
    true_misses: int = 0
    pre_adoption_misses: int = 0
    mentions_total: int = 0
    mentions_resolved: int = 0
    mentions_false: int = 0
    distinct_candidates: int = 0
    distinct_resolved: int = 0
    distinct_false: int = 0
    false_positive_tokens: dict[str, int] = field(default_factory=dict)

    @property
    def yield_rate(self) -> float:
        return (self.linked_tight / self.examined * 100) if self.examined else 0.0

    @property
    def fp_rate_before(self) -> float:
        return (self.mentions_false / self.mentions_total * 100) if self.mentions_total else 0.0

    @property
    def fp_rate_after(self) -> float:
        # Zero by construction: every non-resolving mention is dropped by step 2.
        return 0.0

    @property
    def mappable(self) -> int:
        return self.examined - self.unmappable_bump - self.unmappable_batch_merge

    @property
    def yield_rate_mappable(self) -> float:
        return (self.linked_tight / self.mappable * 100) if self.mappable else 0.0


def read_commits(repo: str, limit: int | None, rev: str = "HEAD") -> list[Commit]:
    """Walk ``rev`` backwards, newest first.

    ``rev`` is pinned rather than defaulted to HEAD by callers that must not measure
    their own spike commits: this script's own commit bodies quote ``bh-infra`` /
    ``bh-version`` as examples, which would otherwise inflate the false-positive count
    it is reporting.
    """
    fmt = _FIELD_SEP.join(["%H", "%P", "%ad", "%s", "%B"]) + _RECORD_SEP
    cmd = ["git", "-C", repo, "log", rev, f"--pretty=format:{fmt}", "--date=short"]
    if limit:
        cmd.append(f"-n{limit}")
    raw = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    commits: list[Commit] = []
    for chunk in raw.split(_RECORD_SEP):
        chunk = chunk.strip("\n")
        if not chunk.strip():
            continue
        parts = chunk.split(_FIELD_SEP)
        if len(parts) < 5:
            continue
        sha, parents, date, subject, body = parts[0], parts[1], parts[2], parts[3], parts[4]
        commits.append(
            Commit(
                sha=sha,
                parents=len(parents.split()),
                subject=subject,
                # %B already contains subject + body; keep both explicitly so the
                # "subject AND body" requirement is visible rather than implied.
                message=f"{subject}\n{body}",
                date=date,
            )
        )
    return commits


def analyse(repo: str, limit: int | None = 500, rev: str = "HEAD") -> RepoReport:
    repo = str(Path(repo).resolve())
    live_ids = load_live_ids(repo)
    namespaces = derive_namespaces(live_ids)
    pattern = candidate_pattern(namespaces)

    total = int(
        subprocess.run(
            ["git", "-C", repo, "rev-list", "--count", rev],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    )
    commits = read_commits(repo, limit, rev)

    rep = RepoReport(
        repo=repo,
        rev=rev,
        namespaces=sorted(namespaces),
        live_ids=len(live_ids),
        total_commits_in_repo=total,
        window_requested=limit or total,
        examined=len(commits),
        window_is_whole_history=(limit is None or total <= limit),
    )

    all_candidates: Counter[str] = Counter()
    false_tokens: Counter[str] = Counter()
    resolved_tokens: set[str] = set()

    # Bead adoption point: the oldest commit in the window that resolves to a real bead.
    # Reported as a diagnostic only -- NOT folded into the unmappable bucket.
    adoption_index: int | None = None

    per_commit: list[tuple[Commit, list[str], list[str]]] = []
    for idx, commit in enumerate(commits):
        cands = extract_candidates(commit.message, pattern)
        resolved, dropped = resolve_candidates(cands, live_ids)
        all_candidates.update(cands)
        false_tokens.update(dropped)
        resolved_tokens.update(resolved)
        if resolved:
            adoption_index = idx
        per_commit.append((commit, resolved, dropped))

    for idx, (commit, resolved, dropped) in enumerate(per_commit):
        # "Loose" = what a pattern-only matcher (no resolve check) would have linked.
        if resolved or dropped:
            rep.linked_loose += 1
        if resolved:
            rep.linked_tight += 1
            continue
        if dropped:
            # Linked under the loose pattern, unlinked once tightened: the tightening's
            # real, non-tautological effect at commit level.
            rep.linked_loose_only += 1
        if _BUMP_RE.match(commit.subject):
            rep.unmappable_bump += 1
        elif commit.parents >= 2 and _BATCH_MERGE_RE.match(commit.subject):
            rep.unmappable_batch_merge += 1
        else:
            rep.true_misses += 1
            # git log is newest-first, so a LARGER index is OLDER. Anything older than
            # the oldest bead-mentioning commit predates this hive's bead adoption.
            if adoption_index is not None and idx > adoption_index:
                rep.pre_adoption_misses += 1

    rep.mentions_total = sum(all_candidates.values())
    rep.mentions_false = sum(false_tokens.values())
    rep.mentions_resolved = rep.mentions_total - rep.mentions_false
    rep.distinct_candidates = len(all_candidates)
    rep.distinct_false = len(false_tokens)
    rep.distinct_resolved = len(resolved_tokens)
    rep.false_positive_tokens = dict(false_tokens.most_common())
    return rep


def render(rep: RepoReport, show_fp: bool = False) -> str:
    window = (
        f"whole history ({rep.examined} commits; repo has {rep.total_commits_in_repo})"
        if rep.window_is_whole_history
        else f"last {rep.examined} of {rep.total_commits_in_repo}"
    )
    loose_pct = (rep.linked_loose / rep.examined * 100) if rep.examined else 0.0
    lines = [
        f"repo:                 {rep.repo}",
        f"rev:                  {rep.rev}",
        f"namespace(s):         {', '.join(rep.namespaces)}  ({rep.live_ids} live bead IDs)",
        f"window:               {window}",
        "",
        f"linked (loose):       {rep.linked_loose}/{rep.examined}  = {loose_pct:.1f}%",
        f"linked (tightened):   {rep.linked_tight}/{rep.examined}  = {rep.yield_rate:.1f}%",
        f"  lost to tightening: {rep.linked_loose_only} commit(s) whose ONLY mention was noise",
        "",
        f"unmappable: bump      {rep.unmappable_bump}",
        f"unmappable: batch/PR  {rep.unmappable_batch_merge}",
        f"true misses           {rep.true_misses}"
        f"  (pre-bead-adoption: {rep.pre_adoption_misses},"
        f" post-adoption: {rep.true_misses - rep.pre_adoption_misses})",
        f"yield over mappable:  {rep.linked_tight}/{rep.mappable}"
        f"  = {rep.yield_rate_mappable:.1f}%",
        "",
        f"mentions:             {rep.mentions_total} total, {rep.mentions_resolved} resolved,"
        f" {rep.mentions_false} false",
        f"distinct tokens:      {rep.distinct_candidates} candidates,"
        f" {rep.distinct_resolved} resolved, {rep.distinct_false} false",
        f"FP rate BEFORE:       {rep.fp_rate_before:.1f}%  (mention-level)",
        f"FP rate AFTER:        {rep.fp_rate_after:.1f}%  (dropped by the resolve check)",
    ]
    if show_fp and rep.false_positive_tokens:
        lines.append("")
        lines.append("false positives dropped:")
        for token, count in rep.false_positive_tokens.items():
            lines.append(f"  {count:>4}  {token}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--repo", default=".", help="repo to analyse (its OWN hive resolves)")
    ap.add_argument("--limit", type=int, default=500, help="commit window; 0 = whole history")
    ap.add_argument(
        "--rev", default="HEAD", help="rev to walk back from (pin to exclude spike commits)"
    )
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--show-false-positives", action="store_true", help="list dropped tokens")
    args = ap.parse_args(argv)

    rep = analyse(args.repo, args.limit or None, args.rev)
    if args.json:
        payload = {k: v for k, v in vars(rep).items() if k != "false_positive_tokens"} | {
            "false_positive_tokens": rep.false_positive_tokens,
            "yield_rate": round(rep.yield_rate, 2),
            "yield_rate_mappable": round(rep.yield_rate_mappable, 2),
            "fp_rate_before": round(rep.fp_rate_before, 2),
            "fp_rate_after": round(rep.fp_rate_after, 2),
            "mappable": rep.mappable,
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(render(rep, show_fp=args.show_false_positives))
    return 0


if __name__ == "__main__":
    sys.exit(main())
