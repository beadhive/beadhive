"""Detect local-only content that needs protection before a worktree is removed.

Git owns enumeration: ``status --ignored`` already applies the repository's ignore rules and
collapses ignored directories.  This module only classifies those bounded records.  In
particular, junk is rejected before any filesystem metadata call so dependency/build trees are
never traversed merely to decide that they are disposable.
"""

from __future__ import annotations

import fnmatch
import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

DEFAULT_PRECIOUS_GLOBS: tuple[str, ...] = (
    ".env",
    ".env.*",
    "*.db",
    "*.sqlite*",
    "*.duckdb",
    "dumps/**",
    "data/**",
    "*.dump",
    "*.sql",
    "downloads/**",
    "outputs/**",
    "*.pem",
    "*.key",
)
DEFAULT_JUNK_GLOBS: tuple[str, ...] = (
    "node_modules/**",
    ".venv/**",
    "__pycache__/**",
    "*.pyc",
    ".pytest_cache/**",
    ".ruff_cache/**",
    "dist/**",
    "build/**",
    ".mypy_cache/**",
)
DEFAULT_PRECIOUS_MIN_BYTES = 1024 * 1024

# A directory that remains below the byte threshold must still have a hard work bound.  Reaching
# either cap is treated conservatively as threshold-sized, because a destructive safety probe
# must prefer a false-positive review over silently declaring an incompletely measured tree safe.
_DIRECTORY_ENTRY_LIMIT = 4096
_DIRECTORY_DEPTH_LIMIT = 32


@dataclass(frozen=True)
class PreciousFile:
    """One ignored or untracked path that needs protection or operator review."""

    path: str
    bytes: int
    category: Literal["precious", "review"]
    matched_glob: str | None


def _run_git(*args, **kwargs):
    """Resolve the established worktree git seam lazily, avoiding an import cycle."""
    from . import worktree

    return worktree._run_git(*args, **kwargs)


def _glob_match(path: str, globs: tuple[str, ...]) -> str | None:
    """Return the first matching glob, with a Python 3.11 fallback for ``full_match``.

    Git marks collapsed directories with a trailing slash.  A ``tree/**`` taxonomy entry names
    both that collapsed directory and its contents, so its directory root is an explicit match.
    Basename matching preserves the long-standing config-glob convention for nested files such
    as ``service/.env`` and ``cache/item.pyc``.
    """
    normalized = path.replace("\\", "/")
    stripped = normalized.rstrip("/")
    pure = PurePosixPath(stripped)
    candidates = (normalized, stripped, pure.name)
    full_match = getattr(pure, "full_match", None)

    for pattern in globs:
        normalized_pattern = str(pattern).replace("\\", "/")
        if full_match is not None and (
            full_match(normalized_pattern)
            or PurePosixPath(pure.name).full_match(normalized_pattern)
        ):
            return str(pattern)
        if any(fnmatch.fnmatchcase(candidate, normalized_pattern) for candidate in candidates):
            return str(pattern)
        if normalized_pattern.endswith("/**"):
            directory_pattern = normalized_pattern[:-3].rstrip("/")
            if any(
                fnmatch.fnmatchcase(candidate, directory_pattern)
                for candidate in (stripped, pure.name)
            ):
                return str(pattern)
    return None


def _bounded_directory_bytes(path: Path, min_bytes: int) -> int:
    """Measure a directory without following symlinks or exceeding fixed traversal caps."""
    total = 0
    visited = 0
    stack: list[tuple[Path, int]] = [(path, 0)]

    while stack:
        directory, depth = stack.pop()
        if depth > _DIRECTORY_DEPTH_LIMIT:
            return max(total, min_bytes)
        try:
            entries = os.scandir(directory)
        except OSError:
            return max(total, min_bytes)
        with entries:
            for entry in entries:
                visited += 1
                if visited > _DIRECTORY_ENTRY_LIMIT:
                    return max(total, min_bytes)
                try:
                    metadata = entry.stat(follow_symlinks=False)
                except OSError:
                    return max(total, min_bytes)
                if stat.S_ISDIR(metadata.st_mode):
                    stack.append((Path(entry.path), depth + 1))
                else:
                    total += metadata.st_size
                    if total >= min_bytes:
                        return total
    return total


def _bounded_bytes(path: Path, min_bytes: int) -> int:
    try:
        metadata = path.lstat()
    except OSError:
        # A status record can race a deletion.  The path no longer exists, so there is no local
        # content left for this scan to protect.
        return 0
    if stat.S_ISDIR(metadata.st_mode):
        return _bounded_directory_bytes(path, min_bytes)
    return metadata.st_size


def _status_paths(output: str) -> list[str]:
    """Parse only ignored/untracked records from porcelain-v1's NUL format."""
    paths: list[str] = []
    for record in output.split("\0"):
        if len(record) >= 4 and record[:2] in {"!!", "??"} and record[2] == " ":
            paths.append(record[3:])
    return paths


def scan_precious(
    path: str | Path,
    *,
    precious_globs: list[str] | tuple[str, ...],
    junk_globs: list[str] | tuple[str, ...],
    min_bytes: int,
) -> list[PreciousFile]:
    """Return precious/review ignored and untracked content below *path*.

    Precious globs win regardless of size.  Unknown paths are returned for review only when the
    bounded measurement reaches ``min_bytes``.  Junk wins before either rule and, load-bearingly,
    before any ``lstat`` or directory scan.
    """
    if min_bytes < 0:
        raise ValueError("min_bytes must be non-negative")

    root = Path(path)
    result = _run_git(
        [
            "git",
            "-C",
            str(root),
            "status",
            "--porcelain",
            "--ignored",
            "-z",
            "--untracked-files=normal",
        ],
        capture=True,
    )
    precious_patterns = tuple(str(item) for item in precious_globs)
    junk_patterns = tuple(str(item) for item in junk_globs)
    found: list[PreciousFile] = []

    for relative in _status_paths(result.stdout or ""):
        if _glob_match(relative, junk_patterns) is not None:
            continue
        matched = _glob_match(relative, precious_patterns)
        size = _bounded_bytes(root / relative.rstrip("/"), min_bytes)
        if matched is not None:
            found.append(PreciousFile(relative, size, "precious", matched))
        elif size >= min_bytes:
            found.append(PreciousFile(relative, size, "review", None))

    return sorted(found, key=lambda item: item.path)


__all__ = [
    "DEFAULT_JUNK_GLOBS",
    "DEFAULT_PRECIOUS_GLOBS",
    "DEFAULT_PRECIOUS_MIN_BYTES",
    "PreciousFile",
    "scan_precious",
]
