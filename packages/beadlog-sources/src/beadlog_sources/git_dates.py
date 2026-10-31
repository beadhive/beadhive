"""Commit-date lookup: many shas -> author and committer dates in one ``git`` call.

A neutral git utility (BeadHive-free): ``git log --ignore-missing --no-walk=unsorted`` over every
sha at once, so the cost is one process for the whole hive rather than one per commit. Unknown
shas are *reported* (``missing``), never fatal, and a string that is not a hex sha is reported as
``invalid`` and never reaches the command line. Dates come back as canonical BeadLog timestamps
(UTC, ``Z``), since git prints them with the committer's own offset.

Argv length is the one bound on "one call": shas are sent in chunks of ``chunk`` (default 4000,
about 160 KB of arguments), so a hive with fewer commits than that is exactly one call.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .bd import bd_timestamp
from .runner import Runner, subprocess_runner

DEFAULT_CHUNK = 4000
LOG_FORMAT = "%H%x09%aI%x09%cI"
_SHA = re.compile(r"[0-9a-fA-F]{4,64}")
_FULL_SHA = 40


class GitReadError(RuntimeError):
    """``git`` failed, or printed something that is not a date table."""


@dataclass(frozen=True)
class CommitDate:
    """One commit's author and committer date, as canonical UTC BeadLog timestamps."""

    sha: str
    author_date: str
    committer_date: str


@dataclass(frozen=True)
class CommitDates:
    """The lookup result: ``dates`` by requested sha, plus what could not be resolved."""

    dates: dict[str, CommitDate] = field(default_factory=dict)
    missing: tuple[str, ...] = ()
    invalid: tuple[str, ...] = ()
    malformed_lines: int = 0
    calls: int = 0


def dates_argv(
    shas: Iterable[str], *, binary: str = "git", root: Path | str | None = None
) -> list[str]:
    """The one bulk read: ``git [-C <root>] log --ignore-missing --no-walk=unsorted <shas> --``."""
    argv = [binary]
    if root is not None:
        argv += ["-C", str(root)]
    argv += ["log", "--ignore-missing", "--no-walk=unsorted", f"--format={LOG_FORMAT}"]
    return [*argv, *shas, "--"]


def parse_dates(text: str) -> tuple[dict[str, CommitDate], int]:
    """The ``(full sha -> CommitDate, malformed line count)`` of :data:`LOG_FORMAT` output."""
    found: dict[str, CommitDate] = {}
    malformed = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) != 3 or not _SHA.fullmatch(parts[0]):
            malformed += 1
            continue
        author, committer = bd_timestamp(parts[1]), bd_timestamp(parts[2])
        if author is None or committer is None:
            malformed += 1
            continue
        found[parts[0].lower()] = CommitDate(parts[0].lower(), author, committer)
    return found, malformed


def _by_prefix(found: dict[str, CommitDate], prefix: str) -> list[CommitDate]:
    if len(prefix) >= _FULL_SHA:  # a full sha is an exact lookup; never scan
        return []
    return [date for full, date in found.items() if full.startswith(prefix)]


def commit_dates(
    shas: Iterable[str],
    *,
    root: Path | str | None = None,
    binary: str = "git",
    chunk: int = DEFAULT_CHUNK,
    timeout: float | None = 300.0,
    runner: Runner | None = None,
) -> CommitDates:
    """Author and committer dates of ``shas`` (full or abbreviated), keyed by the sha as given.

    Duplicates collapse. An empty (or all-invalid) request makes no call at all, since a bare
    ``git log`` would walk ``HEAD``.
    """
    if chunk < 1:
        raise ValueError("chunk must be at least 1")
    run = runner or subprocess_runner(timeout, GitReadError, "git log")
    wanted: dict[str, str] = {}  # as given -> normalized
    invalid: list[str] = []
    for sha in shas:
        if sha in wanted or sha in invalid:
            continue
        if isinstance(sha, str) and _SHA.fullmatch(sha):
            wanted[sha] = sha.lower()
        else:
            invalid.append(str(sha))
    found: dict[str, CommitDate] = {}
    malformed = calls = 0
    unique = sorted(set(wanted.values()))
    for start in range(0, len(unique), chunk):
        calls += 1
        part, bad = parse_dates(
            run(dates_argv(unique[start : start + chunk], binary=binary, root=root))
        )
        found.update(part)
        malformed += bad
    dates: dict[str, CommitDate] = {}
    missing: list[str] = []
    for given, norm in wanted.items():
        hits = [found[norm]] if norm in found else _by_prefix(found, norm)
        if len(hits) == 1:
            dates[given] = hits[0]
        else:  # unknown, or an abbreviation that names several commits
            missing.append(given)
    return CommitDates(dates, tuple(missing), tuple(invalid), malformed, calls)


__all__ = [
    "DEFAULT_CHUNK",
    "LOG_FORMAT",
    "CommitDate",
    "CommitDates",
    "GitReadError",
    "commit_dates",
    "dates_argv",
    "parse_dates",
]
