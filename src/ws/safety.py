"""safety.py — all-branch repo safety scan engine.

Ports the battle-tested git plumbing from scan.sh / verify-safe.sh into Python,
extended to inspect ALL local branches (scan.sh only checked the current branch).

Public API
----------
- ``Category``  — enum of risk categories (mirrors the verify-safe.sh risk set)
- ``BranchInfo`` — per-branch snapshot: ahead, behind, has_upstream, dirty
- ``ScanResult`` — full repo record returned by ``scan``
- ``scan(repo_path)`` — entry point; returns a ``ScanResult``
- ``DifficultyResult`` — verdict + reasons returned by ``difficulty``
- ``difficulty(record)`` — collapse scan signals into easy|medium|hard|not-a-candidate
"""

from __future__ import annotations

import math
import os
import subprocess
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

# Scrub dir-pointing GIT_* vars so every `-C <repo>` always wins.
# (git hooks export GIT_DIR / GIT_INDEX_FILE / GIT_WORK_TREE, which override -C.)
_CLEAN_ENV: dict[str, str] = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def format_bytes(nbytes: int) -> str:
    """Human-readable byte count (shared by survey and doctor)."""
    if nbytes < 1024:
        return f"{nbytes} B"
    if nbytes < 1024 * 1024:
        return f"{nbytes / 1024:.1f} KB"
    if nbytes < 1024 * 1024 * 1024:
        return f"{nbytes / (1024 * 1024):.1f} MB"
    return f"{nbytes / (1024 * 1024 * 1024):.1f} GB"


# Risk ranking for per-origin categories: higher = riskier (used by _worst_category).
_RISK: dict[str, int] = {
    "WIP_AND_AHEAD": 6,
    "PUSH_NEEDED": 5,
    "WIP_DIRTY": 4,
    "NO_UPSTREAM": 3,
    "READY": 2,
}


class Category(StrEnum):
    """Overall repo risk category, derived from the verify-safe.sh risk set.

    Default-fail (risk of losing work):
        NOT_A_REPO, NO_ORIGIN_*, PUSH_NEEDED, WIP_AND_AHEAD
    Strict-fail (work may still be recoverable, but unsafe to bulk-update):
        WIP_DIRTY, NO_UPSTREAM
    Safe:
        READY
    """

    READY = "READY"
    PUSH_NEEDED = "PUSH_NEEDED"
    WIP_DIRTY = "WIP_DIRTY"
    WIP_AND_AHEAD = "WIP_AND_AHEAD"
    NO_UPSTREAM = "NO_UPSTREAM"
    NO_ORIGIN_CLEAN = "NO_ORIGIN_CLEAN"
    NO_ORIGIN_DIRTY = "NO_ORIGIN_DIRTY"
    NO_ORIGIN_EMPTY = "NO_ORIGIN_EMPTY"
    NOT_A_REPO = "NOT_A_REPO"


@dataclass
class BranchInfo:
    """Per-branch status snapshot.

    ``dirty`` is True only for the currently checked-out branch; all other
    branches have ``dirty=False`` because they share no worktree state.
    """

    name: str
    ahead: int
    behind: int
    has_upstream: bool
    dirty: bool


@dataclass
class ScanResult:
    """Full repo safety scan record returned by ``scan``."""

    category: Category
    has_origin: bool
    stash_count: int
    disk_bytes: int = 0
    branches: list[BranchInfo] = field(default_factory=list)
    worktrees: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Internal git helpers
# ---------------------------------------------------------------------------


def _run(args: list[str], repo: str) -> tuple[int, str]:
    """Run ``git -C repo <args>``; return ``(returncode, stripped stdout)``."""
    result = subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True,
        text=True,
        env=_CLEAN_ENV,
    )
    return result.returncode, (result.stdout or "").strip()


def _parse_worktrees(porcelain: str) -> list[str]:
    """Extract linked-worktree paths from ``git worktree list --porcelain`` output.

    The first block is always the main worktree; return only the linked ones.
    """
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in porcelain.splitlines():
        if line.startswith("worktree ") and current:
            blocks.append(current)
            current = []
        current.append(line)
    if current:
        blocks.append(current)

    linked: list[str] = []
    for block in blocks[1:]:  # skip the main worktree (always first)
        for line in block:
            if line.startswith("worktree "):
                linked.append(line[len("worktree "):].strip())
                break
    return linked


def _branch_category(branch: BranchInfo) -> str:
    """Derive a category name for a single branch (origin-present path only)."""
    if not branch.has_upstream:
        return "NO_UPSTREAM"
    if branch.dirty and branch.ahead > 0:
        return "WIP_AND_AHEAD"
    if branch.dirty:
        return "WIP_DIRTY"
    if branch.ahead > 0:
        return "PUSH_NEEDED"
    return "READY"


def _worst_category(branches: list[BranchInfo]) -> Category:
    """Return the highest-risk category across all branches."""
    if not branches:
        return Category.READY
    worst = "READY"
    for b in branches:
        cat = _branch_category(b)
        if _RISK.get(cat, 0) > _RISK.get(worst, 0):
            worst = cat
    return Category(worst)


def _measure_disk_usage(repo_path: str) -> int:
    """Measure disk usage for a repository in bytes.

    Sums all files in the working tree + .git directory. Tries git count-objects
    first for .git efficiency, then falls back to os.walk for total accuracy.
    Returns 0 if no data is available.
    """
    repo_root = Path(repo_path).resolve()
    total_bytes = 0

    # Walk working tree (excluding .git directory)
    try:
        for root, dirs, files in os.walk(repo_root):
            # Skip .git during the working tree walk; we'll measure it separately
            dirs[:] = [d for d in dirs if d != ".git"]
            for f in files:
                try:
                    total_bytes += (Path(root) / f).stat().st_size
                except OSError:
                    pass
    except (OSError, PermissionError):
        pass

    # Measure .git directory
    git_dir = repo_root / ".git"
    if git_dir.exists():
        # Try git count-objects -v first (efficient, packed object size)
        rc, count_out = _run(["count-objects", "-v"], str(repo_root))
        git_bytes = 0
        if rc == 0 and count_out:
            # Parse for size-pack (in KiB, packed objects) or size (loose objects)
            for line in count_out.splitlines():
                if line.startswith("size-pack:"):
                    try:
                        size_kib = int(line.split(":")[1].strip())
                        git_bytes = size_kib * 1024
                        break
                    except (ValueError, IndexError):
                        pass
                elif line.startswith("size:"):
                    try:
                        size_kib = int(line.split(":")[1].strip())
                        git_bytes = size_kib * 1024
                    except (ValueError, IndexError):
                        pass

        # If count-objects didn't work, walk .git to sum all files
        if git_bytes == 0:
            try:
                for root, _dirs, files in os.walk(git_dir):
                    for f in files:
                        try:
                            git_bytes += (Path(root) / f).stat().st_size
                        except OSError:
                            pass
            except (OSError, PermissionError):
                pass

        total_bytes += git_bytes

    return total_bytes


def _scan_branches(path: str) -> list[BranchInfo]:
    """Enumerate all local branches and collect per-branch info.

    Key extension over scan.sh: iterates ``refs/heads/`` instead of checking
    only HEAD. ``dirty`` is computed once for the current worktree and attached
    only to the checked-out branch; all others get ``dirty=False``.
    """
    # Current branch (empty string when HEAD is detached)
    rc, head = _run(["rev-parse", "--abbrev-ref", "HEAD"], path)
    current = head if rc == 0 and head != "HEAD" else ""

    # Worktree dirty state — only the checked-out branch can be dirty
    rc, status_out = _run(["status", "--porcelain"], path)
    is_dirty = rc == 0 and bool(status_out.strip())

    # All local branches + their upstream tracking refs in one call
    rc, ref_out = _run(
        ["for-each-ref", "--format=%(refname:short)\t%(upstream:short)", "refs/heads/"],
        path,
    )
    if rc != 0 or not ref_out.strip():
        return []

    infos: list[BranchInfo] = []
    for line in ref_out.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t", 1)
        name = parts[0].strip()
        upstream = parts[1].strip() if len(parts) > 1 else ""
        has_upstream = bool(upstream)

        ahead = behind = 0
        if has_upstream:
            # --left-right --count: output is "<behind>\t<ahead>"
            # (upstream is the "left" side of the symmetric-difference range)
            rc2, ab = _run(
                ["rev-list", "--left-right", "--count", f"{upstream}...{name}"],
                path,
            )
            if rc2 == 0:
                ab_parts = ab.split()
                if len(ab_parts) == 2:
                    try:
                        behind = int(ab_parts[0])
                        ahead = int(ab_parts[1])
                    except ValueError:
                        pass

        infos.append(
            BranchInfo(
                name=name,
                ahead=ahead,
                behind=behind,
                has_upstream=has_upstream,
                dirty=is_dirty and name == current,
            )
        )
    return infos


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def scan(repo_path: str | Path) -> ScanResult:
    """Scan a git repository and return a structured safety record.

    Extends scan.sh by inspecting ALL local branches (not just HEAD).
    Derives an overall ``Category`` from the verify-safe.sh risk set.

    Parameters
    ----------
    repo_path:
        Path to the repository root (or any path inside a git worktree).

    Returns
    -------
    ScanResult
        ``category``     — overall risk category (see ``Category`` for the full set)
        ``has_origin``   — True iff a remote named ``origin`` is configured
        ``stash_count``  — number of stash entries
        ``disk_bytes``   — total disk usage in bytes (working tree + .git)
        ``branches``     — per-branch info for every local branch
        ``worktrees``    — paths of every linked (non-main) worktree
    """
    path = str(Path(repo_path).resolve())

    # Gate 1: is it a git repo at all?
    rc, _ = _run(["rev-parse", "--git-dir"], path)
    if rc != 0:
        disk_bytes = _measure_disk_usage(path)
        return ScanResult(
            category=Category.NOT_A_REPO,
            has_origin=False,
            stash_count=0,
            disk_bytes=disk_bytes,
        )

    # Origin presence
    rc, origin_url = _run(["remote", "get-url", "origin"], path)
    has_origin = rc == 0 and bool(origin_url)

    # Commit count (HEAD is absent in a brand-new empty repo)
    rc, count_str = _run(["rev-list", "--count", "HEAD"], path)
    commit_count = int(count_str) if rc == 0 and count_str.isdigit() else 0

    # Stash count
    rc, stash_out = _run(["stash", "list"], path)
    stash_count = len([ln for ln in stash_out.splitlines() if ln.strip()]) if rc == 0 else 0

    # Linked worktrees
    rc, wt_out = _run(["worktree", "list", "--porcelain"], path)
    worktrees = _parse_worktrees(wt_out) if rc == 0 else []

    # Measure disk usage
    disk_bytes = _measure_disk_usage(path)

    # No-origin path — mirrors scan.sh classify() for the no-remote case
    if not has_origin:
        rc, status_out = _run(["status", "--porcelain"], path)
        is_dirty = rc == 0 and bool(status_out.strip())

        if commit_count == 0:
            cat = Category.NO_ORIGIN_EMPTY
        elif is_dirty:
            cat = Category.NO_ORIGIN_DIRTY
        else:
            cat = Category.NO_ORIGIN_CLEAN

        branches = _scan_branches(path)
        return ScanResult(
            category=cat,
            has_origin=False,
            stash_count=stash_count,
            disk_bytes=disk_bytes,
            branches=branches,
            worktrees=worktrees,
        )

    # Has origin: derive category from all local branches
    branches = _scan_branches(path)
    cat = _worst_category(branches)

    return ScanResult(
        category=cat,
        has_origin=True,
        stash_count=stash_count,
        disk_bytes=disk_bytes,
        branches=branches,
        worktrees=worktrees,
    )


# ---------------------------------------------------------------------------
# Difficulty scoring
# ---------------------------------------------------------------------------

# Maturity thresholds (commit count)
MATURITY_EASY_COMMITS: int = 50   # >= this many commits → mature (easy signal)
MATURITY_HARD_COMMITS: int = 5    # < this many commits → immature (hard signal)

# Maturity thresholds (last-commit recency, in days)
MATURITY_RECENT_DAYS: float = 90.0    # <= this many days → recently active (easy signal)
MATURITY_STALE_DAYS: float = 365.0   # >= this many days → stale/abandoned (hard signal)


@dataclass
class DifficultyResult:
    """Onboarding difficulty verdict for a repository.

    ``verdict``  — ``"easy"`` | ``"medium"`` | ``"hard"`` | ``"not-a-candidate"``
    ``reasons``  — ordered list of human-readable signal descriptions
    """

    verdict: str
    reasons: list[str] = field(default_factory=list)


def _maturity_commit_count(repo_path: str) -> int:
    """Return total commit count reachable from HEAD (0 for empty/non-repo)."""
    rc, out = _run(["rev-list", "--count", "HEAD"], repo_path)
    return int(out) if rc == 0 and out.strip().isdigit() else 0


def _last_commit_age_days(repo_path: str) -> float:
    """Return age of the most-recent commit in fractional days (inf when absent).

    Uses git's ``%ct`` (committer date, Unix timestamp) so the result is
    timezone-independent and matches what ``git log`` shows.
    """
    rc, out = _run(["log", "-1", "--format=%ct"], repo_path)
    if rc != 0 or not out.strip():
        return float("inf")
    try:
        commit_ts = int(out.strip())
        return (time.time() - commit_ts) / 86400.0
    except ValueError:
        return float("inf")


def last_commit_age_days(repo_path: str | Path) -> float:
    """Return age of the most-recent commit in fractional days (inf when absent).

    Public entry point around the internal ``_last_commit_age_days`` helper,
    accepting both ``str`` and ``Path`` inputs.  Returns ``inf`` for empty
    repos and non-git directories.
    """
    return _last_commit_age_days(str(Path(repo_path).resolve()))


def difficulty(
    record: ScanResult,
    *,
    repo_path: str | Path | None = None,
    classify: str | None = None,
) -> DifficultyResult:
    """Collapse scan signals into a single onboarding-difficulty verdict.

    Parameters
    ----------
    record:
        A ``ScanResult`` from ``scan()``.
    repo_path:
        Optional path to the repository root.  When supplied, maturity signals
        (commit count + last-commit recency) are included in the verdict.
        Without it, only cleanliness signals (from *record*) are evaluated.
    classify:
        Optional pre-computed result from ``registry.classify(provider, org,
        repo)``.  Pass ``"excluded"`` to short-circuit to ``"not-a-candidate"``
        without evaluating any other signals.

    Returns
    -------
    DifficultyResult
        ``verdict`` — ``"easy"`` | ``"medium"`` | ``"hard"`` | ``"not-a-candidate"``
        ``reasons`` — ordered signal descriptions explaining the verdict
    """
    # Short-circuit: excluded repos are never candidates for onboarding.
    if classify == "excluded":
        return DifficultyResult(
            verdict="not-a-candidate",
            reasons=["registry: excluded"],
        )

    reasons: list[str] = []
    hard_count = 0
    easy_count = 0

    # ------------------------------------------------------------------
    # Maturity signals (only when repo_path is provided)
    # ------------------------------------------------------------------
    if repo_path is not None:
        rp = str(Path(repo_path).resolve())

        commit_count = _maturity_commit_count(rp)
        if commit_count >= MATURITY_EASY_COMMITS:
            easy_count += 1
            reasons.append(f"maturity: {commit_count} commits (mature)")
        elif commit_count < MATURITY_HARD_COMMITS:
            hard_count += 1
            reasons.append(f"maturity: only {commit_count} commits (immature)")
        else:
            reasons.append(f"maturity: {commit_count} commits")

        age_days = _last_commit_age_days(rp)
        if math.isinf(age_days):
            hard_count += 1
            reasons.append("recency: no commits")
        elif age_days <= MATURITY_RECENT_DAYS:
            easy_count += 1
            reasons.append(f"recency: active ({age_days:.0f}d since last commit)")
        elif age_days >= MATURITY_STALE_DAYS:
            hard_count += 1
            reasons.append(f"recency: stale ({age_days:.0f}d since last commit)")
        else:
            reasons.append(f"recency: {age_days:.0f}d since last commit")

    # ------------------------------------------------------------------
    # Cleanliness signals (derived from the ScanResult record)
    # ------------------------------------------------------------------
    cat = record.category
    any_dirty = any(b.dirty for b in record.branches)

    if cat == Category.READY:
        easy_count += 1
        reasons.append("cleanliness: READY")
    elif cat in (Category.WIP_AND_AHEAD, Category.WIP_DIRTY):
        hard_count += 1
        reasons.append(f"cleanliness: {cat} (dirty working tree)")
    elif cat == Category.NO_ORIGIN_DIRTY:
        hard_count += 1
        reasons.append("cleanliness: no origin + dirty working tree")
    elif cat == Category.NO_ORIGIN_EMPTY:
        hard_count += 1
        reasons.append("cleanliness: no origin + no commits (empty repo)")
    elif cat == Category.NOT_A_REPO:
        hard_count += 1
        reasons.append("cleanliness: not a git repository")
    elif cat == Category.PUSH_NEEDED:
        reasons.append("cleanliness: PUSH_NEEDED (unpushed commits exist)")
    elif cat == Category.NO_UPSTREAM:
        reasons.append("cleanliness: NO_UPSTREAM (branches without tracking ref)")
    elif cat == Category.NO_ORIGIN_CLEAN:
        reasons.append("cleanliness: no origin remote configured")
    else:
        if any_dirty:
            hard_count += 1
            reasons.append(f"cleanliness: {cat} (dirty)")
        else:
            reasons.append(f"cleanliness: {cat}")

    # ------------------------------------------------------------------
    # Verdict: hard > easy (≥2 signals) > medium
    # ------------------------------------------------------------------
    if hard_count > 0:
        verdict = "hard"
    elif easy_count >= 2:
        verdict = "easy"
    else:
        verdict = "medium"

    return DifficultyResult(verdict=verdict, reasons=reasons)
