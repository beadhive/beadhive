"""`ws git …` — passthrough to git, with optional hive routing.

Plain: runs `git <args>` in the current directory (covers `git status`, `git log`, and
`git workspace <cmd>`). `-a`/`-r` route across hives (requires git_workspace enabled).
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
                f"✗ -a/-r can't be used with `{config.BINARY_ALIAS} git workspace …` "
                "(it runs centrally)",
                err=True,
            )
            raise typer.Exit(1)
        # git hijacks --help for subcommands; route help to the git-workspace binary.
        cmd = ["git-workspace", *args[1:]] if ("-h" in args or "--help" in args) else ["git", *args]
        # `github_token=True` — the passthrough must hand git-workspace the SAME environment
        # `bh host provision` step 6 constructs for it (bh-ajnkx). Without it, `bh git workspace
        # update` / `archive` die on "Missing GITHUB_TOKEN environment variable" while the
        # identical step inside provision succeeds, and the workaround an operator reaches for
        # — `export GITHUB_TOKEN=$(gh auth token)` — puts a live token in a shell's environment
        # and history, which is exactly what `run._fill_github_token` exists to avoid (derived
        # fresh, one dict, one child process, never persisted).
        #
        # Enumerated per the bead rather than fixed only where it surfaced: git-workspace is
        # the ONLY caller that needs this. It is a standalone binary that reads the token from
        # the environment, whereas every other GitHub-authenticated thing bh shells out to
        # reaches credentials another way — `gh` has its own keyring, and git (hq clone/push,
        # `bh sync`'s dolt push over git transport) uses the credential helper, neither of which
        # consults GITHUB_TOKEN. `git workspace list` (doctor.py, registry.py) is local and
        # needs no token, but this branch does not special-case it: one `gh auth token`
        # subprocess on an explicit operator passthrough is cheaper than a subcommand
        # allowlist that drifts the next time git-workspace grows a verb.
        rc = run(cmd, check=False, github_token=True).returncode
        if rc:
            raise typer.Exit(rc)
        return

    cfg = config.load() if mode != "cwd" else {}
    tgts = route.targets(cfg, mode, target)

    def _runner(_label, cwd):
        return run(["git", *args], check=False, cwd=cwd).returncode

    try:
        route.fan_out(tgts, _runner)
    finally:
        route.invalidate_targets(cfg, tgts)  # a passthrough may have mutated the hive
