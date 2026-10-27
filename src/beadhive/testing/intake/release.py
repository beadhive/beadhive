"""``FakeReleaseVerifier``: a scripted :class:`~beadhive.modules.intake.contracts.ReleaseVerifier`.

Tags and ancestry are plain data, so a milestone scenario states exactly which tag names which
commit and which merge commits each release contains. No git, no clock, no I/O; every question
asked is recorded so a test can prove the verifier was read-only and asked once per merge commit.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from beadhive.modules.intake.contracts import Verdict


@dataclass
class FakeReleaseVerifier:
    """``tags`` maps a tag to the commit it names; ``contained`` maps a release commit to the
    merge commits its history contains; ``unknown`` lists merge commits the verifier cannot judge
    (a rewritten or unfetched history). Anything else is ``NO``."""

    tags: Mapping[str, str] = field(default_factory=dict)
    contained: Mapping[str, frozenset[str]] = field(default_factory=dict)
    unknown: frozenset[str] = frozenset()
    resolved: list[str] = field(default_factory=list)
    asked: list[tuple[str, str]] = field(default_factory=list)

    def resolve(self, tag: str) -> str | None:
        self.resolved.append(tag)
        return self.tags.get(tag)

    def contains(self, release_commit: str, merge_commit: str) -> Verdict:
        self.asked.append((release_commit, merge_commit))
        if merge_commit in self.unknown:
            return Verdict.UNKNOWN
        if merge_commit in self.contained.get(release_commit, frozenset()):
            return Verdict.YES
        return Verdict.NO


__all__ = ["FakeReleaseVerifier"]
