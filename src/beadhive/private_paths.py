"""Canonical locations for Beadhive-owned private hive state.

There are two deliberately separate private roots:

* :func:`git_private_root` is ``<git-common-dir>/bh``.  It holds control
  metadata shared by a main clone and all of its linked worktrees.
* :func:`repo_private_root` is ``<hive>/.bh`` in the primary checkout.  It
  holds artifacts and caches shared by every worktree of that hive.
* :func:`worktree_private_root` is ``<worktree>/.bh`` only for data that is
  explicitly local to one checkout, such as its observability overlay.

Resolvers never create a directory.  Writers must opt into creation with the
matching ``ensure_*`` function.  A non-repository path, a bare repository, or
malformed Git output resolves to ``None``; callers can consequently take their
existing no-cache / fresh-work path without risking a filesystem mutation.

Git-native administration is intentionally outside this API: refs, hooks,
``info/exclude``, ``info/attributes``, and Git extensions remain Git's paths.
``LEGACY_PRIVATE_PATHS`` records the old Beadhive-specific locations so later
migration beads have one mapping rather than rediscovering path strings.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .run import run

GIT_PRIVATE_DIRNAME = "bh"
REPO_PRIVATE_DIRNAME = ".bh"

PrivateRoot = Literal["git", "repo", "worktree"]


@dataclass(frozen=True)
class PrivatePathTarget:
    """One path relative to a canonical private root."""

    root: PrivateRoot
    relative: Path


@dataclass(frozen=True)
class LegacyPathMigration:
    """Canonical target(s) and semantics for one legacy Beadhive path."""

    targets: tuple[PrivatePathTarget, ...]
    semantics: str


# This is an inventory contract for later migration beads, not a live migration.
# Multiple targets are intentional: the flat validation ledger is decomposed into
# authoritative run history and a reconstructable green-verdict lookup.
LEGACY_PRIVATE_PATHS: dict[str, LegacyPathMigration] = {
    ".git/bh-validation-ledger.json": LegacyPathMigration(
        targets=(
            PrivatePathTarget("git", Path("validation/runs/<run-id>/manifest.json")),
            PrivatePathTarget("git", Path("validation/verdicts/<tree>/<command-hash>.json")),
        ),
        semantics=(
            "import rows as authoritative run manifests; only qualifying completed-green "
            "runs seed the reconstructable verdict index"
        ),
    ),
    ".git/bh-release-bump-gate.json": LegacyPathMigration(
        targets=(PrivatePathTarget("git", Path("release/bump-gate.json")),),
        semantics="release lifecycle and ownership control record",
    ),
    ".git/bh-release-bump-gate.log": LegacyPathMigration(
        targets=(PrivatePathTarget("repo", Path("release/bump-gates/<tree>/gate.log")),),
        semantics="shared diagnostic artifact referenced by the control record",
    ),
    ".git/bh/validation/active/<verify-worktree-id>.json": LegacyPathMigration(
        targets=(PrivatePathTarget("git", Path("validation/active/<run-id>.json")),),
        semantics=(
            "0.15.1 compatibility input for a reconstructable liveness index, never run truth"
        ),
    ),
    ".git/bh-build": LegacyPathMigration(
        targets=(PrivatePathTarget("git", Path("build")),),
        semantics="clone-local build scratch state",
    ),
    ".git/worktrees/<worktree>/bh-claim.json": LegacyPathMigration(
        targets=(PrivatePathTarget("git", Path("worktrees/<worktree-id>/claim.json")),),
        semantics="centralized per-worktree claim record",
    ),
    ".bh/testreport/<tree>": LegacyPathMigration(
        targets=(PrivatePathTarget("repo", Path("validation/runs/<run-id>/reports")),),
        semantics="legacy per-tree reports become artifacts of a fresh validation run",
    ),
    ".bh/otel.env": LegacyPathMigration(
        targets=(PrivatePathTarget("worktree", Path("observability/otel.env")),),
        semantics="explicitly worktree-local observability overlay",
    ),
    ".bh-verify.json": LegacyPathMigration(
        targets=(PrivatePathTarget("git", Path("validation/active/<run-id>.json")),),
        semantics=(
            "read-only orphan-compatibility input for the derived active index; never write "
            "a marker into a verification checkout"
        ),
    ),
}


def _metadata(hive: Path) -> tuple[Path, Path, Path] | None:
    """Return absolute ``(common_dir, toplevel, primary_toplevel)`` for a worktree.

    A ``rev-parse`` probe resolves the common directory and current top level;
    a second ``worktree list`` probe identifies the primary checkout.  Malformed
    or partial output is a failure rather than an invitation to guess a path.
    ``Path.is_dir`` also makes a deleted metadata target a safe miss.  No
    directory is created here.
    """
    hive = Path(hive).expanduser().absolute()
    if not hive.is_dir():
        return None
    try:
        result = run(
            [
                "git",
                "-C",
                str(hive),
                "rev-parse",
                "--git-common-dir",
                "--is-bare-repository",
                "--show-toplevel",
            ],
            check=False,
            capture=True,
        )
    except (OSError, ValueError):
        return None
    lines = (getattr(result, "stdout", "") or "").splitlines()
    if getattr(result, "returncode", 1) != 0 or len(lines) != 3:
        return None
    common_raw, bare, top_raw = (line.strip() for line in lines)
    if bare != "false" or not common_raw or not top_raw:
        return None
    common = Path(common_raw)
    if not common.is_absolute():
        common = hive / common
    top = Path(top_raw)
    if not top.is_absolute():  # defensive: Git normally returns an absolute top level
        return None
    try:
        common, top = common.resolve(), top.resolve()
    except OSError:
        return None
    if not common.is_dir() or not top.is_dir():
        return None
    try:
        listing = run(
            ["git", "-C", str(hive), "worktree", "list", "--porcelain"],
            check=False,
            capture=True,
        )
    except (OSError, ValueError):
        return None
    worktree_lines = (getattr(listing, "stdout", "") or "").splitlines()
    primary_raw = next(
        (
            line.removeprefix("worktree ").strip()
            for line in worktree_lines
            if line.startswith("worktree ")
        ),
        "",
    )
    if getattr(listing, "returncode", 1) != 0 or not primary_raw:
        return None
    try:
        primary = Path(primary_raw).resolve()
    except OSError:
        return None
    return (common, top, primary) if primary.is_dir() else None


def git_private_root(hive: str | Path) -> Path | None:
    """Read-only ``<git-common-dir>/bh`` resolver, or ``None`` on a safe miss."""
    metadata = _metadata(Path(hive))
    return metadata[0] / GIT_PRIVATE_DIRNAME if metadata else None


def repo_private_root(hive: str | Path) -> Path | None:
    """Read-only shared ``<hive>/.bh`` resolver, or ``None`` on a safe miss.

    ``hive`` may name the main clone or any linked worktree; both resolve to the
    primary checkout's one shared artifact/cache root.
    """
    metadata = _metadata(Path(hive))
    return metadata[2] / REPO_PRIVATE_DIRNAME if metadata else None


def worktree_private_root(hive: str | Path) -> Path | None:
    """Read-only ``<worktree>/.bh`` overlay for explicitly local state only."""
    metadata = _metadata(Path(hive))
    return metadata[1] / REPO_PRIVATE_DIRNAME if metadata else None


def _child(root: Path | None, parts: tuple[str | Path, ...]) -> Path | None:
    if root is None:
        return None
    child = Path(*parts)
    if child.is_absolute() or ".." in child.parts:
        raise ValueError("private-path children must stay below their canonical root")
    return root / child


def git_private_path(hive: str | Path, *parts: str | Path) -> Path | None:
    """A validated child of :func:`git_private_root`; this does not create it."""
    return _child(git_private_root(hive), parts)


def repo_private_path(hive: str | Path, *parts: str | Path) -> Path | None:
    """A validated child of :func:`repo_private_root`; this does not create it."""
    return _child(repo_private_root(hive), parts)


def worktree_private_path(hive: str | Path, *parts: str | Path) -> Path | None:
    """A validated child of :func:`worktree_private_root`; this does not create it."""
    return _child(worktree_private_root(hive), parts)


def ensure_git_private_root(hive: str | Path) -> Path | None:
    """Create and return the git-private root, or safely return ``None`` on failure."""
    root = git_private_root(hive)
    try:
        if root is not None:
            root.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return root


def ensure_repo_private_root(hive: str | Path) -> Path | None:
    """Create and return the repo-private root, or safely return ``None`` on failure."""
    root = repo_private_root(hive)
    try:
        if root is not None:
            root.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return root


def ensure_worktree_private_root(hive: str | Path) -> Path | None:
    """Create a local overlay root only where a caller explicitly requires one."""
    root = worktree_private_root(hive)
    try:
        if root is not None:
            root.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return root
