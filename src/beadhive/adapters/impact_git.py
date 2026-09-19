"""Git adapter for :class:`~beadhive.modules.work.contracts.impact.TreeDiffPort`.

Core computes the changed-path set itself (a backend is never trusted to report what it was
asked about), so this is the one place impact resolution talks to git. Renames are split into
a deletion plus an addition (``--no-renames``) so rule 1 sees the deleted side too.
"""

from __future__ import annotations

import subprocess

from ..modules.work.domain.impact import ChangedPath


class GitTreeDiff:
    """Resolve revisions to tree ids and list the paths differing between two trees."""

    def __init__(self, git: str = "git") -> None:
        self._git = git

    def _run(self, repo: str, *args: str) -> str:
        proc = subprocess.run(
            [self._git, "-C", repo, *args],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()}"
            )
        return proc.stdout

    def tree_of(self, repo: str, rev: str) -> str:
        return self._run(repo, "rev-parse", "--verify", "--quiet", f"{rev}^{{tree}}").strip()

    def changed_paths(self, repo: str, base_tree: str, head_tree: str) -> tuple[ChangedPath, ...]:
        if base_tree == head_tree:
            return ()
        out = self._run(
            repo, "diff-tree", "-r", "-z", "--no-renames", "--name-status", base_tree, head_tree
        )
        fields = out.split("\0")
        changed: list[ChangedPath] = []
        for status, path in zip(fields[0::2], fields[1::2], strict=False):
            if status and path:
                changed.append(ChangedPath(path=path, status=status[0]))
        return tuple(sorted(changed))


__all__ = ["GitTreeDiff"]
