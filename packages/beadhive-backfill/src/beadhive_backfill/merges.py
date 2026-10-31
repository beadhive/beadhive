"""First-parent land commits: ``git log --first-parent`` -> bead / molecule / batch lands.

AGF / BeadHive convention, hence this package and not ``beadlog-sources``. ``bh work merge`` lands
work as a ``--no-ff`` bubble on the integration branch with one of three subjects (written by
``work_merge`` / ``work_group``):

- ``chore(merge): bead <bead id>``
- ``chore(merge): molecule <epic id>``
- ``chore(merge): batch <group>`` — the target is the **group name** (``batch group-a``) or, for
  early batches, a bead id; the subject never lists the members.

The committer date of such a commit is the bead's *occurred* ``merged_at`` (the bd ``closed``
event is the recorded time). Everything else on the first-parent chain is not a land: refresh
merges (``chore(merge): refresh wt/bead/epic/<id> from main``), ``Merge branch 'main' into ...``,
version bumps and the other ``chore(merge)`` housekeeping subjects are *counted*, never returned.
The three pre-convention subjects of July 2026 (``merge <id>``, ``merge molecule <id>``,
``chore(merge): merge <id>``) are returned with ``legacy=True``.

One ``git log --first-parent`` call reads the whole history; the runner is injected.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from beadlog_sources import GitReadError, Runner, bd_timestamp, subprocess_runner

LOG_FORMAT = "%H%x09%cI%x09%s"
DEFAULT_REF = "main"

_TARGET = r"(?P<target>[A-Za-z0-9][A-Za-z0-9._-]*)(?=$|[\s:(])"
_CURRENT = re.compile(rf"^chore\(merge\): (?P<kind>bead|molecule|batch) {_TARGET}")
_LEGACY = re.compile(rf"^(?:chore\(merge\): )?merge (?:(?P<kind>molecule|bead) )?{_TARGET}")
_REFRESH = re.compile(r"^chore\(merge\): refresh\b")
_MERGE_MAIN = re.compile(r"^Merge (?:remote-tracking )?branch '[^']+' into ")
_SHA = re.compile(r"[0-9a-f]{7,64}")


class LandKind(StrEnum):
    """What a land commit integrated."""

    BEAD = "bead"
    MOLECULE = "molecule"
    BATCH = "batch"


@dataclass(frozen=True)
class LandMerge:
    """One land commit: ``committed_at`` is its committer date as a canonical UTC timestamp."""

    sha: str
    committed_at: str
    kind: LandKind
    target: str
    subject: str
    legacy: bool = False


@dataclass(frozen=True)
class MergeScan:
    """The first-parent read: ``merges`` newest first (log order) and what was passed over.

    ``skipped`` counts by reason: ``refresh``, ``merge_main``, ``other`` (a first-parent commit
    that is not a land) and ``malformed_line`` / ``bad_date`` (an unparseable log line)."""

    merges: tuple[LandMerge, ...] = ()
    skipped: Counter[str] = field(default_factory=Counter, hash=False)


def classify_subject(subject: str) -> tuple[LandKind, str, bool] | str:
    """``(kind, target, legacy)`` for a land subject, else the skip reason."""
    if match := _CURRENT.match(subject):
        return LandKind(match.group("kind")), match.group("target"), False
    if _REFRESH.match(subject):
        return "refresh"
    if _MERGE_MAIN.match(subject):
        return "merge_main"
    if match := _LEGACY.match(subject):
        return LandKind(match.group("kind") or "bead"), match.group("target"), True
    return "other"


def parse_first_parent(lines: Iterable[str]) -> MergeScan:
    """Parse :data:`LOG_FORMAT` lines (``sha<TAB>committer date<TAB>subject``)."""
    merges: list[LandMerge] = []
    skipped: Counter[str] = Counter()
    for line in lines:
        if not line.strip():
            continue
        parts = line.rstrip("\n").split("\t", 2)
        if len(parts) != 3 or not _SHA.fullmatch(parts[0]):
            skipped["malformed_line"] += 1
            continue
        sha, raw_date, subject = parts
        verdict = classify_subject(subject)
        if isinstance(verdict, str):
            skipped[verdict] += 1
            continue
        committed = bd_timestamp(raw_date)
        if committed is None:
            skipped["bad_date"] += 1
            continue
        kind, target, legacy = verdict
        merges.append(LandMerge(sha, committed, kind, target, subject, legacy))
    return MergeScan(tuple(merges), skipped)


def first_parent_argv(
    ref: str = DEFAULT_REF, *, root: Path | str | None = None, binary: str = "git"
) -> list[str]:
    """The one read: ``git [-C <root>] log --first-parent --format=<LOG_FORMAT> <ref> --``."""
    argv = [binary]
    if root is not None:
        argv += ["-C", str(root)]
    return [*argv, "log", "--first-parent", f"--format={LOG_FORMAT}", ref, "--"]


def read_land_merges(
    root: Path | str | None = None,
    ref: str = DEFAULT_REF,
    *,
    binary: str = "git",
    timeout: float | None = 300.0,
    runner: Runner | None = None,
) -> MergeScan:
    """Read ``ref``'s first-parent history with one ``git log`` and classify its lands."""
    run = runner or subprocess_runner(timeout, GitReadError, "git log")
    return parse_first_parent(run(first_parent_argv(ref, root=root, binary=binary)).splitlines())


__all__ = [
    "DEFAULT_REF",
    "LOG_FORMAT",
    "LandKind",
    "LandMerge",
    "MergeScan",
    "classify_subject",
    "first_parent_argv",
    "parse_first_parent",
    "read_land_merges",
]
