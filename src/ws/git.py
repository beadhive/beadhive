"""`ws git …` — passthrough to git, with optional rig routing.

Plain: runs `git <args>` in the current directory (covers `git status`, `git log`, and
`git workspace <cmd>`). `-a`/`-r` route across rigs (requires git_workspace enabled).
git hijacks `--help` for subcommands, so `git workspace … --help` is rewritten to the
`git-workspace` binary.
"""

from __future__ import annotations

import typer

from . import config, route
from .run import run


def passthrough(mode, target, args):
    route.reject_inline_flags(args)

    # git-workspace's own subcommand runs centrally — routing is not allowed.
    if args and args[0] == "workspace":
        if mode != "cwd":
            typer.echo(
                "✗ -a/-r can't be used with `ws git workspace …` (it runs centrally)",
                err=True,
            )
            raise typer.Exit(1)
        # git hijacks --help for subcommands; route help to the git-workspace binary.
        cmd = ["git-workspace", *args[1:]] if ("-h" in args or "--help" in args) else ["git", *args]
        rc = run(cmd, check=False).returncode
        if rc:
            raise typer.Exit(rc)
        return

    cfg = config.load() if mode != "cwd" else {}
    tgts = route.targets(cfg, mode, target)
    route.fan_out(tgts, lambda _label, cwd: run(["git", *args], check=False, cwd=cwd).returncode)
