"""``GitReleaseVerifier``: the intake ``ReleaseVerifier`` port over local git, read-only.

``resolve`` answers which commit a tag names (``git rev-parse --verify`` of ``refs/tags/<tag>``
peeled to a commit) and ``contains`` whether a release commit has a merge commit in its history
(``git merge-base --is-ancestor``, the ``release._remote_refs_containing`` precedent). Both only
read: no fetch, no tag, no ref and no object is ever written. Only an exit status of 0 is a
``YES`` and only 1 a ``NO``; any other answer (an unknown or unfetched object, a git failure) is
``UNKNOWN`` and is reported as not verified, never assumed.

A tag or sha that is not a plain ref/hex name is refused before git runs, so an operator-typed
value can never be read as a git option.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from beadhive.modules.intake.contracts import Verdict

_TAG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+/-]*$")
_SHA = re.compile(r"^[0-9a-f]{7,64}$")

#: ``(argv after "git -C <directory>") -> (exit status, stdout)``: injected so a test can script
#: git without a repository.
GitRunner = Callable[[Sequence[str]], tuple[int, str]]


def _subprocess_git(directory: Path) -> GitRunner:
    def run(args: Sequence[str]) -> tuple[int, str]:
        done = subprocess.run(  # noqa: S603 - fixed argv, validated operands, no shell
            ["git", "-C", str(directory), *args],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
        )
        return done.returncode, done.stdout

    return run


class GitReleaseVerifier:
    """Implements :class:`~beadhive.modules.intake.contracts.ReleaseVerifier` over a checkout."""

    def __init__(self, directory: Path | str, *, run: GitRunner | None = None) -> None:
        self._run = run if run is not None else _subprocess_git(Path(directory))

    def resolve(self, tag: str) -> str | None:
        if not _TAG.fullmatch(tag) or ".." in tag or tag.endswith((".", "/", ".lock")):
            return None
        code, out = self._run(["rev-parse", "--verify", "--quiet", f"refs/tags/{tag}^{{commit}}"])
        commit = out.strip()
        return commit if code == 0 and _SHA.fullmatch(commit) else None

    def contains(self, release_commit: str, merge_commit: str) -> Verdict:
        if not (_SHA.fullmatch(release_commit) and _SHA.fullmatch(merge_commit)):
            return Verdict.UNKNOWN
        code, _ = self._run(["merge-base", "--is-ancestor", merge_commit, release_commit])
        if code == 0:
            return Verdict.YES
        return Verdict.NO if code == 1 else Verdict.UNKNOWN


__all__ = ["GitReleaseVerifier", "GitRunner"]
