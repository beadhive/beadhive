"""`ws bd …` — a workspace-aware passthrough to beads, with optional rig routing.

Plain: forwards to `bd` in the current dir, intercepting `create` to auto-apply the
provider/org/repo triplet (ports bdc). `-a`/`-r` route across rigs (requires git_workspace).
"""

from __future__ import annotations

import typer

from . import config, route, validate
from .identity import workspace_identity
from .run import run


def triplet_label_args(cwd) -> list[str]:
    """`-l provider:…,org:…,repo:…` for `cwd`'s managed identity, or [] outside one.

    Typer-free core: the identity-triplet labels `ws bd create` auto-applies, shared with
    the future MCP entrypoint so both build the same label set."""
    ident = workspace_identity(cwd)
    if ident is None:
        return []
    provider, org, repo = ident
    return ["-l", f"provider:{provider},org:{org},repo:{repo}"]


def create(create_args, cwd) -> tuple[int, str]:
    """Run `bd create` for `cwd`'s rig with its identity triplet appended. Typer-free core.

    Returns `(exit_code, error)`: when the rig has label violations, returns `(1, msg)` and
    runs nothing; otherwise `(bd's exit code, "")`. Callers render `error` to the user."""
    if validate.has_violations(cwd=cwd):
        return 1, "rig has label violations — fix with 'ws labels validate' before creating."
    extra = triplet_label_args(cwd)
    return run(["bd", "create", *create_args, *extra], check=False, cwd=cwd).returncode, ""


def _create(create_args, cwd):
    """CLI wrapper over `create`: echo the violation error to stderr, return the exit code."""
    code, error = create(create_args, cwd)
    if error:
        typer.echo(f"✗ {error}", err=True)
    return code


def _run_one(args, cwd):
    if args and args[0] == "create":
        return _create(args[1:], cwd)
    return run(["bd", *args], check=False, cwd=cwd).returncode


def passthrough(mode, target, args):
    route.reject_inline_flags(args)
    cfg = config.load() if mode != "cwd" else {}
    tgts = route.targets(cfg, mode, target)
    route.fan_out(tgts, lambda _label, cwd: _run_one(args, cwd))
