"""archive.py — list and prune the soft-archive graveyard created by ``ws rig retire``.

``ws rig retire`` moves retired clones into ``archive.dir`` (default
``$GIT_WORKSPACE/.archived``) under a ``<provider>/<org>/<repo>`` subpath.  This module
provides the read and reclaim commands:

- ``list_archived(archive_dir)``  → ``list[ArchivedRepo]`` sorted by descending age.
- ``prune_archived(archive_dir, *, older_than_days, all, dry_run)`` → ``PruneResult``.

Guard: ``prune_archived`` resolves every candidate path and asserts it is strictly inside
``archive_dir`` before calling ``shutil.rmtree`` — so a misconfigured or symlinked
``archive.dir`` can never cause collateral damage outside the graveyard.
"""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dir_size(path: Path) -> int:
    """Recursively sum the sizes of all files under ``path``."""
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for f in files:
                try:
                    total += (Path(root) / f).stat().st_size
                except OSError:
                    pass
    except (OSError, PermissionError):
        pass
    return total


def _age_days(path: Path) -> float:
    """Age of ``path`` in fractional days (mtime-based)."""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return 0.0
    return (time.time() - mtime) / 86400.0


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class ArchivedRepo:
    """One archived clone entry under ``archive_dir``."""

    path: Path
    """Absolute path of the archived clone (``<archive_dir>/<provider>/<org>/<repo>``)."""

    triplet: str
    """``<provider>/<org>/<repo>`` — the human-readable identity."""

    age_days: float
    """Fractional days since the directory was last modified (mtime)."""

    size_bytes: int
    """Total size of all files under the clone directory."""


@dataclass
class PruneResult:
    """Outcome of ``prune_archived``."""

    removed: list[str] = field(default_factory=list)
    """Triplets removed (or would-remove under dry-run)."""

    reclaimed_bytes: int = 0
    """Total bytes freed (0 on dry-run — nothing was actually removed)."""

    dry_run: bool = False


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def list_archived(archive_dir: Path) -> list[ArchivedRepo]:
    """Return all ``<provider>/<org>/<repo>`` entries under ``archive_dir``, sorted by age
    (oldest first — matches the prune ordering so the output and pruning are consistent).

    Returns an empty list when ``archive_dir`` does not exist.
    """
    if not archive_dir.exists():
        return []

    repos: list[ArchivedRepo] = []
    for provider_dir in sorted(archive_dir.iterdir()):
        if not provider_dir.is_dir():
            continue
        for org_dir in sorted(provider_dir.iterdir()):
            if not org_dir.is_dir():
                continue
            for repo_dir in sorted(org_dir.iterdir()):
                if not repo_dir.is_dir():
                    continue
                triplet = f"{provider_dir.name}/{org_dir.name}/{repo_dir.name}"
                repos.append(
                    ArchivedRepo(
                        path=repo_dir,
                        triplet=triplet,
                        age_days=_age_days(repo_dir),
                        size_bytes=_dir_size(repo_dir),
                    )
                )
    # Oldest first
    repos.sort(key=lambda r: r.age_days, reverse=True)
    return repos


def prune_archived(
    archive_dir: Path,
    *,
    older_than_days: float,
    remove_all: bool = False,
    dry_run: bool = False,
) -> PruneResult:
    """Remove archived repos that are older than ``older_than_days`` days.

    When ``remove_all`` is True, every archived repo is removed regardless of age.
    When ``dry_run`` is True, nothing is mutated — the result reports what *would* be removed
    and the total bytes that would be reclaimed.

    Path-escape guard: each candidate path is resolved and checked to be strictly under the
    resolved ``archive_dir`` before any ``shutil.rmtree`` call.  A repo path that escapes the
    archive dir is silently skipped (never deleted).
    """
    result = PruneResult(dry_run=dry_run)
    repos = list_archived(archive_dir)
    resolved_root = archive_dir.resolve()

    for repo in repos:
        if not remove_all and repo.age_days < older_than_days:
            continue

        # Path-escape guard: resolve first, then check containment.
        resolved_path = repo.path.resolve()
        try:
            resolved_path.relative_to(resolved_root)
        except ValueError:
            # Path escapes archive_dir — skip unconditionally.
            continue

        size = repo.size_bytes
        result.removed.append(repo.triplet)

        if not dry_run:
            shutil.rmtree(resolved_path, ignore_errors=True)
            result.reclaimed_bytes += size

    return result
