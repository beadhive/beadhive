"""``GitRepoFacts``: the intake ``RepoFacts`` port over local git, read-only (bh-hrx2r).

What the pre-apply proposal checks ask of the repository, each one plain ``git`` read in the
checkout, copying :mod:`.release_verifier`'s pattern (an injected :data:`GitRunner`, operands
validated before git runs, nothing written: no fetch, tag, ref or object):

* ``is_ancestor``: ``git merge-base --is-ancestor <commit> <branch>``. Only exit 0 is ``YES`` and
  only 1 is ``NO``; any other status (an unknown or unfetched object, a git failure) is
  ``UNKNOWN``, which the checks treat as not on the branch;
* ``first_tag_containing``: ``git tag --list --contains <commit> --sort=version:refname
  [<pattern>]``, its first line, so the fix version is computed and version-sorted (``v0.10.0``
  after ``v0.9.0``). ``None`` for no such tag, or any status but 0;
* ``changed_paths``: ``git diff --name-only --no-renames -z <since>...<until> --``, the paths
  ``until`` changed since it diverged from ``since`` (a rename reports both names, so a cited old
  path still matches). ``None`` for any status but 0.

A value that is not a plain hex sha or ref name is refused before git runs (the answer is
``UNKNOWN`` / ``None``), so a value read from a stored proposal can never be read as a git option.
"""

from __future__ import annotations

import re
from pathlib import Path

from beadhive.modules.intake.contracts import Verdict

from .release_verifier import GitRunner, _subprocess_git

_SHA = re.compile(r"^[0-9a-f]{7,64}$")
_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+/-]*$")
_PATTERN = re.compile(r"^[A-Za-z0-9*?][A-Za-z0-9._+/*?-]*$")


def _is_ref(name: str) -> bool:
    return bool(
        _REF.fullmatch(name)
        and ".." not in name
        and "//" not in name
        and not name.endswith((".", "/", ".lock"))
    )


class GitRepoFacts:
    """Implements :class:`~beadhive.modules.intake.contracts.RepoFacts` over a checkout.

    ``tag_pattern`` narrows the tags a fix version may be (a ``git tag --list`` glob, such as
    ``v*``); empty means every tag."""

    def __init__(
        self, directory: Path | str, *, run: GitRunner | None = None, tag_pattern: str = ""
    ) -> None:
        if tag_pattern and not _PATTERN.fullmatch(tag_pattern):
            raise ValueError(f"not a plain tag pattern: {tag_pattern!r}")
        self._run = run if run is not None else _subprocess_git(Path(directory))
        self._pattern = tag_pattern

    def is_ancestor(self, commit: str, branch: str) -> Verdict:
        if not (_SHA.fullmatch(commit) and _is_ref(branch)):
            return Verdict.UNKNOWN
        code, _ = self._run(["merge-base", "--is-ancestor", commit, branch])
        if code == 0:
            return Verdict.YES
        return Verdict.NO if code == 1 else Verdict.UNKNOWN

    def first_tag_containing(self, commit: str) -> str | None:
        if not _SHA.fullmatch(commit):
            return None
        args = ["tag", "--list", "--contains", commit, "--sort=version:refname"]
        code, out = self._run([*args, self._pattern] if self._pattern else args)
        if code != 0:
            return None
        tags = [line.strip() for line in out.splitlines() if line.strip()]
        return tags[0] if tags else None

    def changed_paths(self, since: str, until: str) -> tuple[str, ...] | None:
        if not ((_SHA.fullmatch(since) or _is_ref(since)) and _is_ref(until)):
            return None
        code, out = self._run(
            ["diff", "--name-only", "--no-renames", "-z", f"{since}...{until}", "--"]
        )
        if code != 0:
            return None
        return tuple(sorted({path for path in out.split("\0") if path}))


__all__ = ["GitRepoFacts"]
