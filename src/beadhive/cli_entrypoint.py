"""Import-lazy process entry for the ``bh`` command.

Keep process-only decisions here, ahead of the compatibility CLI's intentionally broad command
tree.  In particular, Click's eager root version option does not need that tree to answer.
"""

from __future__ import annotations

import importlib.metadata
import sys


def _requests_root_version(argv: list[str]) -> bool:
    """Whether *argv* selects the eager root version option before a subcommand."""

    for argument in argv:
        if argument in {"--version", "-V"}:
            return True
        if argument == "--":
            return False
        if not argument.startswith("-"):
            return False
    return False


_WORKTREE_GROUPS = frozenset({"worktree", "wt"})
LOCAL_RECLAIM_VERB = "local-reclaim"


def _local_reclaim_argv(argv: list[str]) -> list[str] | None:
    """The verb's own argv when *argv* selects ``worktree local-reclaim`` (bh-o01m6).

    That verb must run — ``--help`` included — while HQ SQL / runtime config is unavailable, so
    it is selected here from raw argv, before the CLI tree (and its config-reading root callback)
    is imported at all."""

    if len(argv) >= 2 and argv[0] in _WORKTREE_GROUPS and argv[1] == LOCAL_RECLAIM_VERB:
        return argv[2:]
    return None


def main() -> None:
    """Run the cheap eager paths or lazily compose the full command tree."""

    if _requests_root_version(sys.argv[1:]):
        print(importlib.metadata.version("beadhive"))
        return
    local_reclaim = _local_reclaim_argv(sys.argv[1:])
    if local_reclaim is not None:
        from .worktree_local_reclaim import main as local_reclaim_main

        raise SystemExit(local_reclaim_main(local_reclaim))

    from . import cli

    cli.main()
