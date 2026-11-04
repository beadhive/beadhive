"""``FakeRepoFacts``: a scripted :class:`~beadhive.modules.intake.contracts.RepoFacts`.

Branch ancestry, releases and changed paths are plain data, so a check scenario states exactly
which commits landed on which branch, which release first contains each, and what changed since a
commit. No git, no clock, no I/O; every question asked is recorded so a test can count them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from beadhive.modules.intake.contracts import Verdict


@dataclass
class FakeRepoFacts:
    """``branches`` maps a branch to the commits in its history; ``releases`` lists ``(tag,
    commits it contains)`` in VERSION order (the first containing a commit is its fix version);
    ``changes`` maps ``(since, until)`` to the paths changed; ``unknown`` lists commits git cannot
    judge (unfetched or rewritten). A ``(since, until)`` pair not in ``changes`` has no answer
    (``None``), unless ``unchanged_by_default`` says nothing changed."""

    branches: Mapping[str, frozenset[str]] = field(default_factory=dict)
    releases: Sequence[tuple[str, frozenset[str]]] = ()
    changes: Mapping[tuple[str, str], tuple[str, ...]] = field(default_factory=dict)
    unknown: frozenset[str] = frozenset()
    unchanged_by_default: bool = False
    asked: list[tuple[str, ...]] = field(default_factory=list)

    def is_ancestor(self, commit: str, branch: str) -> Verdict:
        self.asked.append(("is_ancestor", commit, branch))
        if commit in self.unknown or branch not in self.branches:
            return Verdict.UNKNOWN
        return Verdict.YES if commit in self.branches[branch] else Verdict.NO

    def first_tag_containing(self, commit: str) -> str | None:
        self.asked.append(("first_tag_containing", commit))
        return next((tag for tag, commits in self.releases if commit in commits), None)

    def changed_paths(self, since: str, until: str) -> tuple[str, ...] | None:
        self.asked.append(("changed_paths", since, until))
        if (since, until) in self.changes:
            return tuple(self.changes[(since, until)])
        return () if self.unchanged_by_default and since not in self.unknown else None


__all__ = ["FakeRepoFacts"]
