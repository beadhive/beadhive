"""safety.py — all-branch repo safety scan engine.

Ports the battle-tested git plumbing from scan.sh / verify-safe.sh into Python,
extended to inspect ALL local branches (scan.sh only checked the current branch).

Public API
----------
- ``Category``       — enum of risk categories (mirrors the verify-safe.sh risk set)
- ``BranchInfo``     — per-branch snapshot: ahead, behind, has_upstream, dirty
- ``ScanResult``     — full repo record returned by ``scan``
- ``scan(repo_path)`` — entry point; returns a ``ScanResult``
- ``DifficultyResult`` — verdict + reasons returned by ``difficulty``
- ``difficulty(record)`` — collapse scan signals into easy|medium|hard|not-a-candidate
- ``RetireVerdict``  — SAFE | NEEDS_BACKUP | BLOCKED
- ``RetireResult``   — verdict + reasons returned by ``assess_retire``
- ``assess_retire(repo_path)`` — pure safety verdict that gates every destructive retire action
- ``on_default_branch(path)`` — read-only ``(ok, detail)`` default-branch preflight check
"""

from __future__ import annotations

import datetime
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
    """Measure a repository's disk usage in bytes: one os.walk over the whole repo root — working
    tree AND .git — summing each file's on-disk size. Unreadable files/dirs are skipped; returns 0
    when nothing is readable. (Walking .git directly is the accurate total; the old
    git-count-objects fast path only sized packed objects and needed a walk fallback anyway.)
    """
    repo_root = Path(repo_path).resolve()
    total_bytes = 0
    try:
        for root, _dirs, files in os.walk(repo_root):
            for f in files:
                try:
                    total_bytes += (Path(root) / f).stat().st_size
                except OSError:
                    pass
    except (OSError, PermissionError):
        pass
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

# Rig-state artifacts: paths whose dirtiness is AGF bookkeeping, not repo risk.
# A fresh onboard (`ws rig init` / stealth `bd` setup) leaves exactly this residue
# (untracked .claude/settings.json + managed CLAUDE.md; churning .beads/*.jsonl
# ledgers), so counting it as a dirty-tree hard signal flipped repos EASY→HARD the
# moment they registered. Difficulty discounts dirt made up solely of these paths.
_HIVE_STATE_PREFIXES: tuple[str, ...] = (".beads/", ".claude/")
_HIVE_STATE_FILES: frozenset[str] = frozenset({"CLAUDE.md", "AGENTS.md"})

# Dirty categories → what the category would be without the working-tree dirt.
_HIVE_DIRT_DOWNGRADE: dict[Category, Category] = {
    Category.WIP_DIRTY: Category.READY,
    Category.WIP_AND_AHEAD: Category.PUSH_NEEDED,
    Category.NO_ORIGIN_DIRTY: Category.NO_ORIGIN_CLEAN,
}


def _is_hive_state_path(path: str) -> bool:
    """True when *path* (relative to the repo root) is a rig-state artifact."""
    return path in _HIVE_STATE_FILES or path.startswith(_HIVE_STATE_PREFIXES)


def _non_hive_dirty_paths(repo_path: str) -> list[str] | None:
    """Dirty working-tree paths excluding rig-state artifacts (None on git failure).

    Parses NUL-separated ``git status --porcelain=v1 -z`` (unquoted paths; can't
    use ``_run``, which strips the status-code whitespace off the first entry).
    For renames/copies both sides must be rig-state for the entry to be discounted.
    """
    result = subprocess.run(
        ["git", "-C", repo_path, "status", "--porcelain=v1", "-z"],
        capture_output=True,
        text=True,
        env=_CLEAN_ENV,
    )
    if result.returncode != 0:
        return None
    real_dirt: list[str] = []
    tokens = result.stdout.split("\0")
    i = 0
    while i < len(tokens):
        entry = tokens[i]
        i += 1
        if len(entry) < 4:  # "XY " + at least a 1-char path
            continue
        code, path = entry[:2], entry[3:]
        paths = [path]
        if code[0] in "RC" and i < len(tokens) and tokens[i]:
            paths.append(tokens[i])  # rename/copy source is the next NUL token
            i += 1
        if not all(_is_hive_state_path(p) for p in paths):
            real_dirt.append(path)
    return real_dirt


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


# ---------------------------------------------------------------------------
# Retire safety verdict
# ---------------------------------------------------------------------------


class RetireVerdict(StrEnum):
    """Retirement safety verdict.

    ``SAFE``         — every branch is pushed, the tree is clean, no stashes, origin present.
    ``NEEDS_BACKUP`` — work exists that would be lost on deletion; back up before retiring.
    ``BLOCKED``      — structural error (not a repo, no origin + no commits); cannot proceed.
    """

    SAFE = "SAFE"
    NEEDS_BACKUP = "NEEDS_BACKUP"
    BLOCKED = "BLOCKED"


@dataclass
class RetireResult:
    """Retirement safety verdict with per-item reasons.

    ``verdict``  — ``SAFE`` | ``NEEDS_BACKUP`` | ``BLOCKED``
    ``reasons``  — ordered list of human-readable explanations (empty when ``SAFE``)
    """

    verdict: RetireVerdict
    reasons: list[str] = field(default_factory=list)


# Ranking for verdict escalation (higher value = more severe).
_RETIRE_RANK: dict[RetireVerdict, int] = {
    RetireVerdict.SAFE: 0,
    RetireVerdict.NEEDS_BACKUP: 1,
    RetireVerdict.BLOCKED: 2,
}


def assess_retire(repo_path: str | Path) -> RetireResult:
    """Pure read-only verdict for whether a repository is safe to retire.

    Calls ``scan()`` internally and maps the verify-safe.sh default-fail +
    strict-fail risk sets onto three verdict tiers.  Performs no mutations
    (no commits, pushes, branch creation, or deletion).

    Verdict mapping
    ---------------
    * ``BLOCKED``      — ``NOT_A_REPO``, ``NO_ORIGIN_EMPTY``
    * ``NEEDS_BACKUP`` — ``NO_ORIGIN_CLEAN``, ``NO_ORIGIN_DIRTY``, ``PUSH_NEEDED``,
                         ``WIP_AND_AHEAD``, ``WIP_DIRTY``, ``NO_UPSTREAM``,
                         plus any stash entries or detached-HEAD WIP.
    * ``SAFE``         — ``READY`` with no stashes and no detached HEAD.

    Parameters
    ----------
    repo_path:
        Path to the repository root (or any path inside a git worktree).

    Returns
    -------
    RetireResult
        ``verdict`` — ``SAFE`` | ``NEEDS_BACKUP`` | ``BLOCKED``
        ``reasons`` — per-item explanations (empty list when verdict is ``SAFE``)
    """
    path = str(Path(repo_path).resolve())
    record = scan(path)
    reasons: list[str] = []
    verdict = RetireVerdict.SAFE

    def _escalate(to: RetireVerdict) -> None:
        nonlocal verdict
        if _RETIRE_RANK[to] > _RETIRE_RANK[verdict]:
            verdict = to

    # --- Gate: not a git repository at all ---
    if record.category == Category.NOT_A_REPO:
        return RetireResult(
            verdict=RetireVerdict.BLOCKED,
            reasons=["not a git repository — cannot assess retirement safety"],
        )

    # --- No-origin cases ---
    if not record.has_origin:
        if record.category == Category.NO_ORIGIN_EMPTY:
            _escalate(RetireVerdict.BLOCKED)
            reasons.append(
                "no origin remote and no commits — repository cannot be safely assessed"
            )
        else:
            # NO_ORIGIN_CLEAN or NO_ORIGIN_DIRTY
            _escalate(RetireVerdict.NEEDS_BACKUP)
            reasons.append("no origin remote — local commits have no remote backup")

    # --- Per-branch checks ---
    for branch in record.branches:
        if branch.ahead > 0:
            _escalate(RetireVerdict.NEEDS_BACKUP)
            commit_s = "commit" if branch.ahead == 1 else "commits"
            reasons.append(
                f"branch '{branch.name}' has {branch.ahead} unpushed {commit_s}"
            )
        if not branch.has_upstream and record.has_origin:
            _escalate(RetireVerdict.NEEDS_BACKUP)
            reasons.append(
                f"branch '{branch.name}' has no upstream tracking ref — push status unknown"
            )
        if branch.dirty:
            _escalate(RetireVerdict.NEEDS_BACKUP)
            reasons.append(f"branch '{branch.name}' has uncommitted changes")

    # --- Stashes force at least NEEDS_BACKUP ---
    if record.stash_count > 0:
        _escalate(RetireVerdict.NEEDS_BACKUP)
        s = "entry" if record.stash_count == 1 else "entries"
        reasons.append(
            f"{record.stash_count} stash {s} would be lost on retirement"
        )

    # --- Detached HEAD: commits may not be reachable from any named branch ---
    rc, head_ref = _run(["rev-parse", "--abbrev-ref", "HEAD"], path)
    if rc == 0 and head_ref == "HEAD":
        rc2, status_out = _run(["status", "--porcelain"], path)
        is_dirty = rc2 == 0 and bool(status_out.strip())
        _escalate(RetireVerdict.NEEDS_BACKUP)
        if is_dirty:
            reasons.append("HEAD is detached with uncommitted changes")
        else:
            reasons.append(
                "HEAD is detached — commits may be lost on garbage collection"
            )

    return RetireResult(verdict=verdict, reasons=reasons)


# ---------------------------------------------------------------------------
# Backup unpushed work
# ---------------------------------------------------------------------------


@dataclass
class BackupResult:
    """Result of ``backup_unpushed``.

    ``nothing_to_do``       — True when the repo is already safe (clean, all pushed).
    ``wip_branches_pushed`` — list of wip branch names pushed to origin.
    ``repo_published``      — True if ``gh repo create`` was run to publish the repo.
    ``dry_run``             — mirrors the input flag.
    ``actions``             — human-readable list of actions taken or planned.
    """

    nothing_to_do: bool
    wip_branches_pushed: list[str]
    repo_published: bool
    dry_run: bool
    actions: list[str] = field(default_factory=list)


def _current_branch(path: str) -> str:
    """Return the checked-out branch name, or empty string for detached HEAD."""
    rc, head = _run(["rev-parse", "--abbrev-ref", "HEAD"], path)
    if rc == 0 and head and head != "HEAD":
        return head
    return ""


def _default_branch(path: str) -> str:
    """Resolve the repo's default branch name.

    Preference order (read-only git plumbing, matching the onboarding preflight design):
    1. ``git symbolic-ref refs/remotes/origin/HEAD`` — the remote's advertised default.
    2. ``git config init.defaultBranch`` — the local/user configured default.
    3. ``main`` — the ultimate fallback.

    Never raises: a repo without an ``origin`` (or without ``origin/HEAD`` set) simply
    falls through to the next source.
    """
    rc, ref = _run(["symbolic-ref", "refs/remotes/origin/HEAD"], path)
    if rc == 0 and ref:
        # ref is like 'refs/remotes/origin/main' — take the trailing branch name.
        return ref.rsplit("/", 1)[-1]
    rc, configured = _run(["config", "init.defaultBranch"], path)
    if rc == 0 and configured:
        return configured
    return "main"


def on_default_branch(path: str | Path) -> tuple[bool, str]:
    """Report whether ``path``'s HEAD is on the repo's default branch.

    Returns ``(True, <branch>)`` when the checked-out branch is the resolved default
    (see ``_default_branch`` for resolution order), and ``(False, <detail>)`` otherwise.
    ``<detail>`` explains the mismatch: a detached HEAD, or the current-vs-default names.

    Read-only and typer-free — safe to call from a preflight check. Handles detached HEAD
    and repos without an ``origin`` gracefully (never raises for those cases).
    """
    repo = str(Path(path).resolve())
    default = _default_branch(repo)
    # symbolic-ref (not rev-parse) so a freshly-init'd repo on an *unborn* branch (no commits
    # yet) still reports its branch name; it fails only for a truly detached HEAD.
    rc, current = _run(["symbolic-ref", "--short", "HEAD"], repo)
    if rc != 0 or not current:
        return False, f"detached HEAD (default is '{default}')"
    if current == default:
        return True, current
    return False, f"on '{current}', not default '{default}'"


def _branch_exists(path: str, branch: str) -> bool:
    """Return True iff branch exists in the local repo."""
    rc, _ = _run(["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], path)
    return rc == 0


def _unique_wip_name(path: str, base: str) -> str:
    """Return *base* if it is available as a local branch name, else *base*-N."""
    if not _branch_exists(path, base):
        return base
    for i in range(2, 1000):
        candidate = f"{base}-{i}"
        if not _branch_exists(path, candidate):
            return candidate
    raise RuntimeError(f"Cannot find a unique WIP branch name starting with {base!r}")


def _gh_authenticated() -> bool:
    """Return True iff ``gh`` is installed and authenticated."""
    try:
        result = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        # gh is not installed at all — treat as unauthenticated rather than crashing.
        return False
    return result.returncode == 0


def _set_upstream(path: str, branch: str, upstream_ref: str) -> None:
    """Point *branch*'s tracking ref at *upstream_ref* (e.g. ``origin/wip/...``).

    Used after a durable WIP backup so ``assess_retire`` no longer flags the original
    branch as ahead / no-upstream: its commits are now reachable on the remote and the
    branch tracks that remote ref (ahead==0).  Raises ``RuntimeError`` on failure.
    """
    rc, out = _run(["branch", f"--set-upstream-to={upstream_ref}", branch], path)
    if rc != 0:
        raise RuntimeError(
            f"git branch --set-upstream-to={upstream_ref} {branch} failed: {out}"
        )


def _snapshot_dirty_branch(
    path: str,
    current_branch: str,
    wip_branch: str,
    has_origin: bool,
    dry_run: bool,
) -> tuple[bool, list[str]]:
    """Port of snapshot-wip.sh: commit all dirty files to *wip_branch* and push.

    Creates *wip_branch* from the current HEAD, stages everything (``git add -A``),
    commits with ``--no-verify``, pushes to origin (if *has_origin*), then switches
    back to *current_branch* and restores its tip via ``reset --soft``.

    Returns ``(pushed_to_origin, actions)``.
    Raises ``RuntimeError`` on any git failure.
    """
    timestamp = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    actions: list[str] = []
    pushed = False

    if dry_run:
        actions.append(f"git switch -c {wip_branch}")
        actions.append("git add -A")
        actions.append(f'git commit --no-verify -m "wip: retire snapshot {timestamp}"')
        if has_origin:
            actions.append(f"git push -u origin {wip_branch}")
            pushed = True
        actions.append(f"git switch {current_branch}")
        actions.append(f"git reset --soft {wip_branch}~1")
        actions.append("git reset HEAD --")
        return pushed, actions

    # 1. Create + switch to WIP branch (at the same commit as current_branch).
    rc, out = _run(["switch", "-c", wip_branch], path)
    if rc != 0:
        raise RuntimeError(f"git switch -c {wip_branch} failed: {out}")
    actions.append(f"git switch -c {wip_branch}")

    try:
        # 2. Stage everything including untracked files.
        #    A failed ``add`` would silently produce a PARTIAL backup, so gate on its rc.
        rc, out = _run(["add", "-A"], path)
        if rc != 0:
            raise RuntimeError(f"git add -A failed: {out}")
        actions.append("git add -A")

        # 3. Commit — skip hooks so pre-commit checks don't block the backup.
        rc, out = _run(
            ["commit", "--no-verify", "-m", f"wip: retire snapshot {timestamp}"],
            path,
        )
        if rc != 0:
            raise RuntimeError(f"git commit failed: {out}")
        actions.append("git commit --no-verify")

        # 4. Push the WIP branch to origin so the backup is durable.
        if has_origin:
            rc, out = _run(["push", "-u", "origin", wip_branch], path)
            if rc != 0:
                raise RuntimeError(f"git push failed: {out}")
            actions.append(f"git push -u origin {wip_branch}")
            pushed = True

    finally:
        # 5. Always switch back to the original branch.
        rc, out = _run(["switch", current_branch], path)
        if rc != 0:
            raise RuntimeError(
                f"CRITICAL: failed to switch back to {current_branch!r}: {out}"
            )
        actions.append(f"git switch {current_branch}")

    # 6. Restore original branch HEAD to its pre-snapshot tip.
    #    ``reset --soft`` rewinds HEAD without touching the working tree.  This is a
    #    post-condition restore (the work is already safely committed on *wip_branch*),
    #    so a failure is not data loss — but the post-condition must hold, so surface it.
    rc, out = _run(["reset", "--soft", f"{wip_branch}~1"], path)
    if rc != 0:
        actions.append(f"WARNING: git reset --soft {wip_branch}~1 failed: {out}")
    else:
        actions.append(f"git reset --soft {wip_branch}~1")

    # 7. Drop staged paths so the index reflects the pre-snapshot state.
    rc, out = _run(["reset", "HEAD", "--"], path)
    if rc != 0:
        actions.append(f"WARNING: git reset HEAD -- failed: {out}")
    else:
        actions.append("git reset HEAD --")

    return pushed, actions


def _publish_no_origin(path: str, dry_run: bool) -> list[str]:
    """Port of publish.sh: create a GitHub repo for a no-origin local repo and push.

    Guards: requires ``gh`` installed + authenticated, no pre-existing origin,
    at least one commit.  Runs ``gh repo create --source=. --push --remote=origin``.

    Returns a list of human-readable action strings.
    Raises ``RuntimeError`` on any pre-condition failure.
    """
    # Guard: gh must be authenticated.
    if not _gh_authenticated():
        raise RuntimeError(
            "gh CLI not available or not authenticated — run 'gh auth login'"
        )

    cmd_str = "gh repo create --source=. --push --remote=origin"
    actions = [cmd_str]

    if not dry_run:
        result = subprocess.run(
            ["gh", "repo", "create", "--source=.", "--push", "--remote=origin"],
            cwd=path,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"gh repo create failed: {result.stderr.strip()}"
            )

    return actions


def _backup_branch_at_tip(
    path: str, branch: str, wip_branch: str, dry_run: bool
) -> list[str]:
    """Capture *branch*'s tip under *wip_branch*, push it durably, and re-point *branch*.

    Covers both the *ahead* (has upstream, unpushed commits) and *no-upstream* (commits
    with no tracking ref) cases — for the latter ``ahead`` is 0, so a plain ``ahead>0``
    filter misses it.  The branch tip is captured under a ``wip/retire-…`` ref, pushed to
    origin, and the original branch is re-pointed to track that durable ref so
    ``assess_retire`` rates it SAFE afterward.  Raises ``RuntimeError`` on any git failure.
    """
    if dry_run:
        return [
            f"git branch {wip_branch} {branch}",
            f"git push -u origin {wip_branch}",
            f"git branch --set-upstream-to=origin/{wip_branch} {branch}",
        ]
    actions: list[str] = []
    rc, out = _run(["branch", wip_branch, branch], path)
    if rc != 0:
        raise RuntimeError(f"git branch {wip_branch} {branch} failed: {out}")
    actions.append(f"git branch {wip_branch} {branch}")
    rc, out = _run(["push", "-u", "origin", wip_branch], path)
    if rc != 0:
        raise RuntimeError(f"git push -u origin {wip_branch} failed: {out}")
    actions.append(f"git push -u origin {wip_branch}")
    _set_upstream(path, branch, f"origin/{wip_branch}")
    actions.append(f"git branch --set-upstream-to=origin/{wip_branch} {branch}")
    return actions


def _snapshot_detached(
    path: str, wip_branch: str, has_origin: bool, dry_run: bool
) -> tuple[bool, list[str]]:
    """Snapshot a detached HEAD onto *wip_branch* (attaching HEAD) and push it.

    Detached-HEAD commits are reachable from no branch and are garbage-collection
    eligible; this captures them on a real branch.  Any uncommitted changes are committed
    first.  HEAD is left attached to *wip_branch* so ``assess_retire`` no longer flags a
    detached HEAD.  Returns ``(pushed, actions)``; raises ``RuntimeError`` on any failure.
    """
    timestamp = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    if dry_run:
        acts = [
            f"git switch -c {wip_branch}",
            f'git commit --no-verify -m "wip: retire detached snapshot {timestamp}" (if dirty)',
        ]
        if has_origin:
            acts.append(f"git push -u origin {wip_branch}")
        return has_origin, acts

    actions: list[str] = []
    rc, out = _run(["switch", "-c", wip_branch], path)
    if rc != 0:
        raise RuntimeError(f"git switch -c {wip_branch} (detached) failed: {out}")
    actions.append(f"git switch -c {wip_branch}")

    rc, status_out = _run(["status", "--porcelain"], path)
    if rc == 0 and status_out.strip():
        rc, out = _run(["add", "-A"], path)
        if rc != 0:
            raise RuntimeError(f"git add -A (detached) failed: {out}")
        actions.append("git add -A")
        rc, out = _run(
            ["commit", "--no-verify", "-m", f"wip: retire detached snapshot {timestamp}"],
            path,
        )
        if rc != 0:
            raise RuntimeError(f"git commit (detached) failed: {out}")
        actions.append("git commit --no-verify")

    pushed = False
    if has_origin:
        rc, out = _run(["push", "-u", "origin", wip_branch], path)
        if rc != 0:
            raise RuntimeError(f"git push -u origin {wip_branch} (detached) failed: {out}")
        actions.append(f"git push -u origin {wip_branch}")
        pushed = True
    return pushed, actions


def _backup_stashes(
    path: str, date_str: str, has_origin: bool, dry_run: bool
) -> list[str]:
    """Back up every stash entry to a durable remote ref, then clear the local stashes.

    Each ``stash@{i}`` commit is parked on a local ``refs/stash-backup/retire-<date>/<i>``
    ref and pushed to origin (carrying its tree + parents), after which the local stash
    list is cleared so the repository assesses SAFE.  Refuses (raises ``RuntimeError``)
    when there is no origin to push to rather than silently dropping the stashes.
    """
    rc, out = _run(["stash", "list"], path)
    entries = [ln for ln in out.splitlines() if ln.strip()] if rc == 0 else []
    if not entries:
        return []
    if not has_origin:
        plural = "y" if len(entries) == 1 else "ies"
        raise RuntimeError(
            f"{len(entries)} stash entr{plural} present but no origin to back them up "
            "to — refusing to drop them"
        )

    actions: list[str] = []
    if dry_run:
        for i in range(len(entries)):
            actions.append(
                f"git push origin stash@{{{i}}}:refs/stash-backup/retire-{date_str}/{i}"
            )
        actions.append("git stash clear")
        return actions

    for i in range(len(entries)):
        rc, sha = _run(["rev-parse", f"stash@{{{i}}}"], path)
        if rc != 0 or not sha.strip():
            raise RuntimeError(f"failed to resolve stash@{{{i}}}: {sha}")
        ref = f"refs/stash-backup/retire-{date_str}/{i}"
        # Park the stash commit on a real ref locally so push can name it reliably.
        rc, out = _run(["update-ref", ref, sha.strip()], path)
        if rc != 0:
            raise RuntimeError(f"git update-ref {ref} failed: {out}")
        rc, out = _run(["push", "origin", f"{ref}:{ref}"], path)
        if rc != 0:
            raise RuntimeError(f"git push stash backup ({ref}) failed: {out}")
        actions.append(f"git push origin {ref}")

    rc, out = _run(["stash", "clear"], path)
    if rc != 0:
        raise RuntimeError(f"git stash clear failed: {out}")
    actions.append("git stash clear")
    return actions


def backup_unpushed(
    repo_path: str | Path,
    *,
    dry_run: bool = False,
) -> BackupResult:
    """Back up unpushed work by pushing durable WIP branches and/or publishing the repo.

    Ports two bash scripts into a single Python function:

    *snapshot-wip.sh* (dirty / ahead branches with origin):
        For the checked-out branch when the working tree is dirty, creates a
        ``wip/retire-<date>`` branch, stages everything (``git add -A``), commits
        with ``--no-verify``, pushes to origin, then switches back and restores
        the original branch tip via ``reset --soft``.

        For branches that are ahead of their upstream (but not dirty), creates a
        ``wip/retire-<date>/<safe-branch-name>`` branch at the branch tip and pushes
        it — no checkout required.

    *publish.sh* (no-origin repos with commits):
        Runs ``gh repo create --source=. --push --remote=origin`` to publish an
        otherwise-unreachable local repo.  Requires ``gh`` auth.

    Parameters
    ----------
    repo_path:
        Path to the repository root (or any path inside a git worktree).
    dry_run:
        If True, preview the exact actions without mutating anything
        (no branches created, no pushes, no ``gh`` calls).

    Returns
    -------
    BackupResult
        ``nothing_to_do`` — True when the repo is already safe (READY, no work to back up).
        ``wip_branches_pushed`` — branch names pushed (empty on dry_run or nothing_to_do).
        ``repo_published`` — True if ``gh repo create`` was executed.
        ``dry_run`` — mirrors the input flag.
        ``actions`` — human-readable list of what was done (or what *would* be done).
    """
    path = str(Path(repo_path).resolve())
    record = scan(path)

    # --- Blocked: not a git repo at all ---
    if record.category == Category.NOT_A_REPO:
        raise ValueError(f"Not a git repository: {path}")

    # --- Drive off the retire verdict, NOT the coarse Category. ---
    # A repo can be Category.READY yet still NEEDS_BACKUP (stash entries, detached HEAD),
    # so an ``ahead>0`` / category==READY short-circuit would silently skip real at-risk
    # work.  Only a genuinely SAFE verdict means there is nothing to back up.
    assessment = assess_retire(path)
    if assessment.verdict == RetireVerdict.SAFE:
        return BackupResult(
            nothing_to_do=True,
            wip_branches_pushed=[],
            repo_published=False,
            dry_run=dry_run,
            actions=["all branches clean and pushed — nothing to back up"],
        )

    # NO_ORIGIN_EMPTY is BLOCKED (no commits) — there is genuinely nothing to back up.
    if record.category == Category.NO_ORIGIN_EMPTY:
        return BackupResult(
            nothing_to_do=True,
            wip_branches_pushed=[],
            repo_published=False,
            dry_run=dry_run,
            actions=["no commits — nothing to back up"],
        )

    date_str = datetime.date.today().isoformat()
    wip_branches_pushed: list[str] = []
    repo_published = False
    all_actions: list[str] = []

    # -----------------------------------------------------------------------
    # Step 0: Detached HEAD — capture GC-eligible commits onto a real branch
    # (and re-attach HEAD) before anything else.
    # -----------------------------------------------------------------------
    rc, head_ref = _run(["rev-parse", "--abbrev-ref", "HEAD"], path)
    is_detached = rc == 0 and head_ref == "HEAD"
    if is_detached:
        wip_name = _unique_wip_name(path, f"wip/retire-{date_str}/detached")
        pushed, acts = _snapshot_detached(path, wip_name, record.has_origin, dry_run)
        all_actions.extend(acts)
        if pushed and not dry_run:
            wip_branches_pushed.append(wip_name)

    # -----------------------------------------------------------------------
    # Step 1: Snapshot the dirty working tree (must run BEFORE publish so
    # that the working tree is clean when gh repo create runs).
    # -----------------------------------------------------------------------
    dirty_branch = next((b for b in record.branches if b.dirty), None)
    dirty_wip_branch: str | None = None

    if dirty_branch is not None and not is_detached:
        cur = _current_branch(path)
        if cur:
            base_wip = f"wip/retire-{date_str}"
            wip_name = _unique_wip_name(path, base_wip)
            dirty_wip_branch = wip_name
            pushed, acts = _snapshot_dirty_branch(
                path, cur, wip_name, record.has_origin, dry_run
            )
            all_actions.extend(acts)
            if pushed and not dry_run:
                wip_branches_pushed.append(wip_name)
            # When the snapshotted branch would still read as at-risk (ahead of its
            # upstream, or no upstream at all), re-point it at the durable WIP ref so
            # its now-backed-up commits no longer assess as unbacked work.
            if (
                not dry_run
                and record.has_origin
                and (dirty_branch.ahead > 0 or not dirty_branch.has_upstream)
            ):
                _set_upstream(path, cur, f"origin/{wip_name}")
                all_actions.append(
                    f"git branch --set-upstream-to=origin/{wip_name} {cur}"
                )

    # -----------------------------------------------------------------------
    # Step 2: Publish no-origin repos, then push EVERY branch + tags (not just
    # the checked-out one that ``gh repo create --push`` handles).
    # -----------------------------------------------------------------------
    if not record.has_origin:
        acts = _publish_no_origin(path, dry_run)
        all_actions.extend(acts)
        if not dry_run:
            repo_published = True
            rc, ourl = _run(["remote", "get-url", "origin"], path)
            if rc == 0 and ourl.strip():
                rc, bout = _run(
                    ["for-each-ref", "--format=%(refname:short)", "refs/heads/"], path
                )
                for bname in [ln.strip() for ln in bout.splitlines() if ln.strip()]:
                    rc, pout = _run(["push", "-u", "origin", bname], path)
                    if rc != 0:
                        raise RuntimeError(f"git push -u origin {bname} failed: {pout}")
                    all_actions.append(f"git push -u origin {bname}")
                    if bname not in wip_branches_pushed:
                        wip_branches_pushed.append(bname)
                rc, tout = _run(["push", "origin", "--tags"], path)
                if rc != 0:
                    raise RuntimeError(f"git push origin --tags failed: {tout}")
                all_actions.append("git push origin --tags")
        elif dirty_wip_branch is not None:
            all_actions.append(f"git push -u origin {dirty_wip_branch}")

    # -----------------------------------------------------------------------
    # Step 3: Back up ahead AND no-upstream branches when origin exists.
    # (No-upstream branches have ahead==0, so the old ``ahead>0`` filter missed them.)
    # -----------------------------------------------------------------------
    if record.has_origin:
        for branch in record.branches:
            if branch.dirty:
                continue  # already handled by the dirty snapshot above
            if branch.ahead > 0 or not branch.has_upstream:
                safe_name = branch.name.replace("/", "-").replace(".", "-")
                base_wip = f"wip/retire-{date_str}/{safe_name}"
                wip_name = _unique_wip_name(path, base_wip)
                acts = _backup_branch_at_tip(path, branch.name, wip_name, dry_run)
                all_actions.extend(acts)
                if not dry_run:
                    wip_branches_pushed.append(wip_name)

    # -----------------------------------------------------------------------
    # Step 4: Back up stash entries (push durable refs, then clear them).
    # -----------------------------------------------------------------------
    rc, ourl = _run(["remote", "get-url", "origin"], path)
    origin_now = rc == 0 and bool(ourl.strip())
    all_actions.extend(_backup_stashes(path, date_str, origin_now, dry_run))

    # -----------------------------------------------------------------------
    # Invariant: a NEEDS_BACKUP repo must be SAFE after backup — never silently
    # report success while at-risk work remains.
    # -----------------------------------------------------------------------
    if not dry_run:
        post = assess_retire(path)
        if post.verdict != RetireVerdict.SAFE:
            raise RuntimeError(
                "backup did not make the repository safe to retire; remaining risks: "
                + "; ".join(post.reasons)
            )

    return BackupResult(
        nothing_to_do=False,
        wip_branches_pushed=wip_branches_pushed,
        repo_published=repo_published,
        dry_run=dry_run,
        actions=all_actions,
    )


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

    # Rig-state discount: dirt made up solely of AGF bookkeeping paths
    # (.beads/, .claude/, CLAUDE.md) says nothing about onboarding difficulty —
    # score the category the tree would have without it, so the verdict stays
    # stable across the candidate→rig transition.
    if repo_path is not None and cat in _HIVE_DIRT_DOWNGRADE:
        real_dirt = _non_hive_dirty_paths(str(Path(repo_path).resolve()))
        if real_dirt is not None and not real_dirt:
            reasons.append(f"cleanliness: {cat} is hive-state artifacts only (discounted)")
            cat = _HIVE_DIRT_DOWNGRADE[cat]
            any_dirty = False

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
