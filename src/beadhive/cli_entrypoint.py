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


def main() -> None:
    """Run the cheap eager path or lazily compose the full command tree."""

    if _requests_root_version(sys.argv[1:]):
        print(importlib.metadata.version("beadhive"))
        return

    from . import cli

    cli.main()
