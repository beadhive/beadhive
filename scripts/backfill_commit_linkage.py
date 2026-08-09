#!/usr/bin/env python3
"""Idempotent full-history backfill of `metadata.git.commits` (bh-1b0rc.3).

Walks ONE hive's (repo's) full git history, oldest-first, and writes durable bead<->commit
linkage (`docs/design/bead-commit-linkage-contract.md`) for beads that closed before
`bh work submit` / `bh work merge` started recording it live (bh-1b0rc.2). This is the
one-time recovery mechanism for that pre-adoption gap -- not a replacement for the live writer.

Reuses, rather than reimplements, the two pieces the epic already validated:

  * The CANONICAL matcher from the correlation-yield spike (bh-rwryq) --
    `scripts/bead_commit_correlation.py`'s `extract_candidates` / `resolve_candidates` (plus
    the supporting `candidate_pattern` / `derive_namespaces` / `load_live_ids` machinery), used
    HERE VERBATIM. Do not weaken or reimplement candidate extraction or resolve-backed
    filtering in this module -- a candidate that does not resolve against the hive's own live
    bead ID set is never written, full stop.
  * The write-side accumulate/idempotent-write algorithm -- `beadhive.git_linkage.record_commits`
    (bh-1b0rc.2). This module never touches `bd update` directly; it only ever decides WHICH
    shas belong to WHICH bead(s), then hands that off.

Hive scoping is a hard constraint carried over unchanged from the spike: the live ID set is
loaded ONCE per repo, from that repo's own store, and resolution is NEVER aggregated across
hives -- run this once per hive via `--repo`.

Usage::

    uv run python scripts/backfill_commit_linkage.py --repo . --dry-run
    uv run python scripts/backfill_commit_linkage.py --repo .
    uv run python scripts/backfill_commit_linkage.py --repo . --json
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

# `scripts/` has no `__init__.py` (a namespace package); when this file is invoked directly
# (`python scripts/backfill_commit_linkage.py`), sys.path[0] is `scripts/` itself, not the repo
# root, so `from scripts.bead_commit_correlation import ...` would otherwise fail. Make the
# import work the same way whether this file is run directly or imported as
# `scripts.backfill_commit_linkage` (e.g. from a test).
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# CANONICAL matcher (bh-rwryq spike) -- imported verbatim, per
# docs/design/bead-commit-linkage-contract.md and the bh-1b0rc.3 brief. `read_commits` isn't
# marked CANONICAL but is reused as-is too: it's just a `git log` field-wrapper, and a second,
# slightly-different one here would be exactly the kind of drift this epic exists to avoid.
from scripts.bead_commit_correlation import (  # noqa: E402
    candidate_pattern,
    derive_namespaces,
    extract_candidates,
    load_live_ids,
    read_commits,
    resolve_candidates,
)

from beadhive import git_linkage  # noqa: E402

__all__ = [
    "BackfillReport",
    "collect_bead_commits",
    "run_backfill",
    "render",
    "main",
]


@dataclass
class BackfillReport:
    """Summary of one backfill run (real or `--dry-run`) over one hive."""

    repo: str
    rev: str
    dry_run: bool
    live_ids: int = 0
    namespaces: list[str] = field(default_factory=list)
    examined: int = 0
    unmatched: int = 0
    beads_with_commits: int = 0
    beads_updated: int = 0
    total_new_shas: int = 0
    per_bead_new: dict[str, int] = field(default_factory=dict)


def collect_bead_commits(
    repo: str, rev: str = "HEAD"
) -> tuple[dict[str, list[str]], int, int, frozenset[str], list[str]]:
    """Walk `repo`'s FULL history oldest-first and group resolvable commits by bead.

    Returns ``(bead_to_shas, examined, unmatched, live_ids, namespaces)``. Each bead's SHA list
    is oldest-first, matching the contract's append-only-oldest-first order (bh-1b0rc.1) -- the
    walk itself is oldest-first, so appending as we go preserves it with no extra sort needed.

    A commit can resolve to MORE than one bead (a multi-bead batch-merge commit, or a commit
    whose body mentions a related bead) -- its sha is accumulated onto every bead it resolves
    to, not just the first.
    """
    live_ids = load_live_ids(repo)
    namespaces = derive_namespaces(live_ids)
    pattern = candidate_pattern(namespaces)

    commits = read_commits(repo, None, rev)  # newest-first, no window -- the whole history
    commits.reverse()  # oldest-first, per the linkage contract's append order

    bead_to_shas: dict[str, list[str]] = {}
    examined = 0
    unmatched = 0
    for commit in commits:
        examined += 1
        candidates = extract_candidates(commit.message, pattern)
        resolved, _dropped = resolve_candidates(candidates, live_ids)
        beads_hit = set(resolved)  # dedupe repeat mentions of the same bead within one commit
        if not beads_hit:
            unmatched += 1
            continue
        for bead_id in beads_hit:
            bead_to_shas.setdefault(bead_id, []).append(commit.sha)

    return bead_to_shas, examined, unmatched, live_ids, sorted(namespaces)


def _new_sha_count(bead_id: str, main: Path, shas: list[str]) -> int:
    """Preview-only: how many of `shas` are not already recorded for `bead_id`.

    Read-only -- uses `git_linkage.read_commits`, the read half of the same contract
    `record_commits` writes through -- and never calls `record_commits`. This is what lets
    `--dry-run` report accurately without writing anything, and is also what makes a rerun's
    "beads updated" count correct (a bead whose commits are ALL already recorded contributes 0
    and is skipped, matching `record_commits`'s own skip-the-write behavior).
    """
    existing = set(git_linkage.read_commits(bead_id, main))
    return sum(1 for sha in shas if sha not in existing)


def run_backfill(repo: str, rev: str = "HEAD", dry_run: bool = False) -> BackfillReport:
    """Backfill (or, in `dry_run`, preview) `git.commits` linkage across `repo`'s full history.

    Never aggregated across hives: `repo` is resolved and walked exactly once, and every bead
    resolution is checked only against `repo`'s own live bead ID set.
    """
    resolved_repo = str(Path(repo).resolve())
    main = Path(resolved_repo)

    bead_to_shas, examined, unmatched, live_ids, namespaces = collect_bead_commits(
        resolved_repo, rev
    )

    report = BackfillReport(
        repo=resolved_repo,
        rev=rev,
        dry_run=dry_run,
        live_ids=len(live_ids),
        namespaces=namespaces,
        examined=examined,
        unmatched=unmatched,
        beads_with_commits=len(bead_to_shas),
    )

    for bead_id in sorted(bead_to_shas):
        shas = bead_to_shas[bead_id]
        new_count = _new_sha_count(bead_id, main, shas)
        if new_count == 0:
            continue  # every sha already recorded -- this bead contributes nothing this run
        if dry_run:
            report.beads_updated += 1
            report.total_new_shas += new_count
            report.per_bead_new[bead_id] = new_count
            continue
        wrote = git_linkage.record_commits(bead_id, main, shas)
        if wrote:
            report.beads_updated += 1
            report.total_new_shas += new_count
            report.per_bead_new[bead_id] = new_count

    return report


def render(rep: BackfillReport) -> str:
    label = "would update" if rep.dry_run else "updated"
    lines = [
        f"repo:                 {rep.repo}",
        f"rev:                  {rep.rev}",
        f"mode:                 {'DRY RUN -- nothing written' if rep.dry_run else 'WRITE'}",
        f"namespace(s):         {', '.join(rep.namespaces)}  ({rep.live_ids} live bead IDs)",
        "",
        f"commits examined:     {rep.examined}",
        f"commits unmatched:    {rep.unmatched}  (zero resolved beads)",
        f"beads with commits:   {rep.beads_with_commits}",
        f"beads {label}:  {rep.beads_updated}",
        f"new SHAs {'to write' if rep.dry_run else 'written'}: {rep.total_new_shas}",
    ]
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--repo", default=".", help="hive repo to backfill (its OWN live bead store resolves)"
    )
    ap.add_argument("--rev", default="HEAD", help="rev to walk the full history back from")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be written, without calling record_commits at all",
    )
    ap.add_argument("--json", action="store_true", help="machine-readable summary")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    rep = run_backfill(args.repo, rev=args.rev, dry_run=args.dry_run)
    if args.json:
        print(json.dumps(asdict(rep), indent=2, sort_keys=True))
    else:
        print(render(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
