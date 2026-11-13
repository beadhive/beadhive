"""Helpers shared by the file-reading strategies (``docs``, ``planning``). BeadHive-free.

Everything here reads a repository once and in bulk: a single ``git log`` yields the first-add
commit of every tracked path, so no strategy runs a git command (or a store call) per document.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import warnings
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from beadlog import (
    BeadEvent,
    BeadRef,
    BeadState,
    EventKind,
    TimestampError,
    format_timestamp,
    instant,
)

_FIELD_SEP = "\x1f"
_RECORD_SEP = "\x1e"
_BASE36 = "0123456789abcdefghijklmnopqrstuvwxyz"
_FRONT = re.compile(r"^---[ \t]*$")
_TITLE_KEY = re.compile(r"^title:[ \t]*(.*?)[ \t]*$")
_HEADING = re.compile(r"^#{1,6}[ \t]+(.+?)[ \t#]*$")


class SourceError(ValueError):
    """A repository cannot be read for a strategy (a ``ValueError``, so the ``beadlog`` CLI
    reports it as a failed operation)."""


def truthy(value: Any) -> bool:
    return str(value).lower() in ("1", "true", "yes", "on")


def csv(value: Any) -> list[str]:
    """A comma-separated option as a list (blank members dropped)."""
    return [item.strip() for item in str(value).split(",") if item.strip()]


# -- git -----------------------------------------------------------------------------------


def _run(root: Path | str, args: Sequence[str], git: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            [git, "-C", str(root), "-c", "core.quotepath=off", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        raise SourceError(f"cannot run {git}: {exc}") from exc


def is_git_repo(root: Path | str, git: str = "git") -> bool:
    return _run(root, ["rev-parse", "--git-dir"], git).returncode == 0


@dataclass(frozen=True)
class AddRecord:
    """The commit that first added a path: sha, committer date (ISO 8601) and subject."""

    sha: str
    date: str
    subject: str

    @property
    def when(self) -> str | None:
        return commit_time(self.date)


def commit_time(value: str) -> str | None:
    """A git ISO 8601 date as a canonical BeadLog timestamp, or ``None`` if unparsable."""
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if moment.tzinfo is None:
            return None
        return format_timestamp(moment)
    except (ValueError, TimestampError):
        return None


def first_adds(
    root: Path | str, pathspecs: Sequence[str] = (), git: str = "git"
) -> dict[str, AddRecord]:
    """The first-add commit of every path under ``pathspecs``, from ONE ``git log``.

    A path that was added more than once (deleted and re-added) maps to its oldest add. A path
    git only knows as the target of a rename is absent here (``--diff-filter=A`` excludes it);
    resolve those with :func:`follow_add`. A repository with no commits (or no git) yields ``{}``.
    """
    fmt = _RECORD_SEP + _FIELD_SEP.join(["%H", "%cI", "%s"])
    args = ["log", "--diff-filter=A", "--name-only", "--no-color", f"--pretty=format:{fmt}"]
    if pathspecs:
        args += ["--", *pathspecs]
    result = _run(root, args, git)
    if result.returncode != 0:
        return {}
    found: dict[str, AddRecord] = {}
    for chunk in result.stdout.split(_RECORD_SEP):
        if not chunk.strip():
            continue
        head, _, names = chunk.partition("\n")
        parts = head.split(_FIELD_SEP)
        if len(parts) < 3:
            continue
        record = AddRecord(parts[0], parts[1], parts[2])
        for name in names.splitlines():
            if name.strip():
                found[name.strip()] = record  # newest first: the last write is the oldest add
    return found


def follow_add(root: Path | str, path: str, git: str = "git") -> AddRecord | None:
    """The oldest add of ``path`` following renames (``git log --follow``), or ``None``."""
    fmt = _FIELD_SEP.join(["%H", "%cI", "%s"])
    result = _run(
        root, ["log", "--follow", "--diff-filter=A", f"--pretty=format:{fmt}", "--", path], git
    )
    if result.returncode != 0:
        return None
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        return None
    parts = lines[-1].split(_FIELD_SEP)
    return AddRecord(parts[0], parts[1], parts[2]) if len(parts) >= 3 else None


# -- the baseline --------------------------------------------------------------------------


# The retire reason a rolled-back backfill create leaves on its bead (bh-12hka).
ROLLBACK_REASON = "backfill-rollback"


def live_beads(states: Mapping[BeadRef, BeadState], group: str) -> dict[str, BeadState]:
    """Id -> folded state for the beads of ``group`` that exist and are not erased."""
    return {
        ref.id: state
        for ref, state in states.items()
        if ref.group == group and state.exists and not state.deleted and not state.erased
    }


def external_refs(live: Mapping[str, BeadState]) -> dict[str, list[str]]:
    """``external_ref`` -> the ids of the beads carrying it (sorted)."""
    out: dict[str, list[str]] = {}
    for ident in sorted(live):
        ref = live[ident].fields.get("external_ref")
        if isinstance(ref, str) and ref:
            out.setdefault(ref, []).append(ident)
    return out


def floors(baseline: Iterable[BeadEvent], group: str, field: str) -> dict[str, str]:
    """Per bead, the time of the latest baseline event that decides ``field`` (a create, a
    checkpoint or a set of the field), as the event states it. A new ``set`` is raised to this
    time so it wins the last-writer-wins fold."""
    out: dict[str, str] = {}
    for event in baseline:
        if event.bead.group != group:
            continue
        kind = event.kind
        decides = kind in (EventKind.CREATE, EventKind.CHECKPOINT) or (
            kind is EventKind.SET and event.data.get("field") == field
        )
        when = event.occurred_at or event.recorded_at
        if not decides or when is None:
            continue
        known = out.get(event.bead.id)
        if known is None or instant(when) > instant(known):
            out[event.bead.id] = when
    return out


def mint_bead_id(prefix: str, group: str, key: str, taken: Iterable[str] = ()) -> str:
    """A deterministic new bead id ``<prefix>-<8 base36>`` from ``(group, key)``; a collision with
    ``taken`` re-hashes with a counter, so the same inputs always give the same id."""
    used = set(taken)
    attempt = 0
    while True:
        material = f"{group}\0{key}\0{attempt}" if attempt else f"{group}\0{key}"
        number = int.from_bytes(hashlib.sha256(material.encode("utf-8")).digest()[:8], "big")
        slug = ""
        for _ in range(8):
            number, digit = divmod(number, 36)
            slug += _BASE36[digit]
        ident = f"{prefix}-{slug}"
        if ident not in used:
            return ident
        attempt += 1


# -- markdown ------------------------------------------------------------------------------


def split_frontmatter(text: str) -> tuple[list[str], list[str]]:
    """``(frontmatter lines, body lines)``; no frontmatter gives ``([], all lines)``. The block
    must open on line one with ``---`` and close at the next ``---``."""
    lines = text.splitlines()
    if lines and _FRONT.match(lines[0]):
        for index in range(1, len(lines)):
            if _FRONT.match(lines[index]):
                return lines[1:index], lines[index + 1 :]
    return [], lines


def doc_title(text: str, fallback: str) -> str:
    """The frontmatter ``title:``, else the first markdown heading, else ``fallback``."""
    front, body = split_frontmatter(text)
    for line in front:
        match = _TITLE_KEY.match(line)
        if match and match.group(1).strip("'\" "):
            return match.group(1).strip("'\" ")
    for line in body:
        match = _HEADING.match(line)
        if match:
            return match.group(1).strip()
    return fallback


__all__ = [
    "AddRecord",
    "SourceError",
    "commit_time",
    "csv",
    "doc_title",
    "external_refs",
    "first_adds",
    "floors",
    "follow_add",
    "is_git_repo",
    "live_beads",
    "mint_bead_id",
    "split_frontmatter",
    "truthy",
]


# -- exclude patterns ------------------------------------------------------------------------
#
# One matcher for the ``exclude`` option of the file-reading readers (``docs``, ``planning``).
# Patterns are globs over repo-relative posix paths: ``*`` and ``?`` stay within one path
# segment, ``**`` crosses segments, ``[..]`` is a character class. A trailing ``/`` matches the
# subtree below that directory. A pattern with no ``/`` other than a trailing one matches a file
# name (or, with the trailing ``/``, a directory name) at any depth, like gitignore; a pattern
# with an inner ``/`` (``docs/archive/``) is anchored at the root. Implemented with ``re`` (no
# ``PurePath.full_match``: it needs Python 3.13).


def _glob_regex(pattern: str) -> str:
    """The regex source (no anchors) of one glob ``pattern``."""
    out: list[str] = []
    i, n = 0, len(pattern)
    while i < n:
        char = pattern[i]
        if char == "*":
            if pattern.startswith("**", i):
                i += 2
                if pattern.startswith("/", i):  # ``**/`` is zero or more directories
                    i += 1
                    out.append("(?:.*/)?")
                else:
                    out.append(".*")
                continue
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        elif char == "[":
            end = pattern.find("]", i + 2 if pattern.startswith(("[!", "[^"), i) else i + 2)
            if end == -1:
                out.append(re.escape(char))
            else:
                body = pattern[i + 1 : end]
                negate = body[:1] in ("!", "^")
                body = body[1:] if negate else body
                body = body.replace("\\", "\\\\")
                out.append(f"[{'^/' if negate else ''}{body}]")
                i = end
        else:
            out.append(re.escape(char))
        i += 1
    return "".join(out)


@dataclass(frozen=True)
class ExcludeMatcher:
    """The compiled ``exclude`` option: :meth:`match` names the first pattern a path hits."""

    rules: tuple[tuple[str, re.Pattern[str], bool, bool], ...] = ()

    def __bool__(self) -> bool:
        return bool(self.rules)

    def match(self, path: str, *, directory: bool = False) -> str | None:
        """The first pattern that excludes repo-relative posix ``path`` (``None``: none does).

        ``directory`` marks ``path`` as a directory, which a subtree pattern matches too.
        """
        name = path.rsplit("/", 1)[-1]
        for pattern, regex, subtree, by_name in self.rules:
            if by_name:
                if regex.fullmatch(name):
                    return pattern
            elif regex.fullmatch(path) or (subtree and directory and regex.fullmatch(path + "/x")):
                return pattern
        return None


def exclude_matcher(value: Any) -> ExcludeMatcher:
    """Compile the ``exclude`` option (csv of globs). A leading ``!`` is a :class:`SourceError`
    (reserved for later negation)."""
    rules: list[tuple[str, re.Pattern[str], bool, bool]] = []
    for pattern in csv(value):
        if pattern.startswith("!"):
            raise SourceError(f"exclude pattern {pattern!r}: a leading '!' (negation) is reserved")
        subtree = pattern.endswith("/")
        body = pattern.rstrip("/")
        if not body:
            raise SourceError(f"exclude pattern {pattern!r} is empty")
        by_name = "/" not in body and not subtree
        anywhere = "/" not in body and subtree  # ``archive/``: that directory at any depth
        source = ("(?:.*/)?" if anywhere else "") + _glob_regex(body) + ("/.+" if subtree else "")
        rules.append((pattern, re.compile(source, re.DOTALL), subtree, by_name))
    return ExcludeMatcher(tuple(rules))


def warn_explicit_excluded(kind: str, path: str, pattern: str) -> None:
    """Report that an explicitly named file was dropped by an ``exclude`` pattern."""
    warnings.warn(
        f"{kind} file {path!r} is explicitly named but excluded by pattern {pattern!r}; dropped",
        UserWarning,
        stacklevel=3,
    )
