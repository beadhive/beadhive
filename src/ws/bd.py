"""`ws bd …` — a workspace-aware passthrough to beads, with optional rig routing.

Plain: forwards to `bd` in the current dir, intercepting `create` to auto-apply the
provider/org/repo triplet (ports bdc). `-a`/`-r` route across rigs (requires git_workspace).
"""

from __future__ import annotations

import typer

from . import config, route, validate
from .identity import workspace_identity
from .run import run


def _create(create_args, cwd):
    """bd create with the target rig's triplet. Returns exit code; 1 if rig has violations."""
    if validate.has_violations(cwd=cwd):
        typer.echo(
            "✗ rig has label violations — fix with 'ws labels validate' before creating.",
            err=True,
        )
        return 1
    ident = workspace_identity(cwd)
    extra = []
    if ident is not None:
        provider, org, repo = ident
        extra = ["-l", f"provider:{provider},org:{org},repo:{repo}"]
    return run(["bd", "create", *create_args, *extra], check=False, cwd=cwd).returncode


def _run_one(args, cwd):
    if args and args[0] == "create":
        return _create(args[1:], cwd)
    return run(["bd", *args], check=False, cwd=cwd).returncode


def passthrough(mode, target, args):
    route.reject_inline_flags(args)
    cfg = config.load() if mode != "cwd" else {}
    tgts = route.targets(cfg, mode, target)
    route.fan_out(tgts, lambda _label, cwd: _run_one(args, cwd))
