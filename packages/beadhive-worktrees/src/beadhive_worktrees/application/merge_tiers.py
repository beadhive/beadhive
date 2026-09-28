"""Pure decision/parsing fragments of the integration-boundary merge tiers (bh-qdezo.8).

Moved verbatim (byte-identical behavior) from ``worktree_merge.py``. The actual merge
mechanics — the `--no-ff` merge itself, the rebase attempt, the transient
`.git/info/attributes` file, and every hard-reset recovery step — are real git-plumbing
EXECUTION and stay in the shell (``worktree_merge.py`` / ``worktree.py``), per this bead's
design constraint: merge serialization and `--no-ff` behavior are not moved. What lives here
is the surrounding pure logic those mechanics consult: parsing a probe merge's conflicted-path
output, deciding whether every such path is union-eligible, composing the union merge driver's
attribute lines, and recognising a rebase that dropped every reviewed commit as already applied.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Sequence

__all__ = [
    "all_union_eligible",
    "is_zero_delta_rebase",
    "parse_conflict_paths",
    "union_attributes_text",
]


def parse_conflict_paths(diff_output: str) -> list[str]:
    """Conflicted paths from a `git diff --name-only --diff-filter=U` probe's stdout.

    One path per non-blank line; moved verbatim from the inline list comprehension in
    ``merge_conflict_paths``.
    """
    return [p for p in (diff_output or "").splitlines() if p.strip()]


def all_union_eligible(paths: Sequence[str], union_globs: Sequence[str]) -> bool:
    """True iff EVERY path matches at least one glob in `union_globs` (fnmatch). An empty
    `paths` is not eligible — there is nothing for the union driver to resolve.

    Moved verbatim from ``worktree_merge._all_union_eligible``.
    """
    if not paths:
        return False
    return all(any(fnmatch.fnmatch(p, g) for g in union_globs) for p in paths)


def union_attributes_text(union_globs: Sequence[str]) -> str:
    """The transient `.git/info/attributes` content activating git's `union` merge driver for
    every glob in `union_globs` — one `<glob> merge=union` line per entry, trailing newline.

    Moved verbatim from the inline construction in ``merge_with_union``.
    """
    return "\n".join(f"{g} merge=union" for g in union_globs) + "\n"


def is_zero_delta_rebase(commit_shas: Sequence[object]) -> bool:
    """True iff a rebase reported success but replayed away every reviewed commit as already
    applied on the newer base — the case ``try_merge_rebase`` must treat as a recoverable
    bounce rather than a bubble-less close, since there is no distinct history left to
    attribute. Moved verbatim from the inline ``not worktree.commit_shas(...)`` check.
    """
    return not commit_shas
