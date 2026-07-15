"""gitworkspace_plugin.py — promotes git-workspace to a proper `bh` plugin (bh-4y0r.4).

Mirrors `orca.PLUGIN`: `gitworkspace.py` itself stays pure-stdlib (no typer / plugins import),
so this thin module carries the `bh plugin git-workspace …` sub-app + the `plugins.Plugin`
registration on top of it.
"""

from __future__ import annotations

import typer

from . import gitworkspace, plugins
from .identity import workspace_root

cli = typer.Typer(no_args_is_help=True, help="git-workspace repo-group integration.")


@cli.command("groups", help="list repo groups (path/provider/account/filters).")
def _groups_cmd() -> None:
    from . import config

    groups = gitworkspace.groups(config.load())
    if not groups:
        typer.echo("• no repo groups found (git-workspace disabled, or no workspace*.toml)")
        return
    for g in groups:
        filters = []
        if g.skip_forks:
            filters.append("skip_forks")
        if g.include:
            filters.append(f"include={list(g.include)}")
        if g.exclude:
            filters.append(f"exclude={list(g.exclude)}")
        suffix = f"  ({', '.join(filters)})" if filters else ""
        typer.echo(f"  {g.path}\tprovider={g.provider_type}\taccount={g.account}{suffix}")


def _readiness(cfg, entry) -> tuple[str, str] | None:
    """rig-ready hook: is git-workspace itself set up (env, sources, lockfile)?

    Not rig-specific — `entry` is accepted (per the generic `plugins.Plugin` contract) but
    unused; git-workspace readiness is a workspace-wide signal, not a per-rig one."""
    import os

    if not os.environ.get("GIT_WORKSPACE"):
        return ("warn", f"GIT_WORKSPACE not set — defaulting to {workspace_root()}")
    sources = gitworkspace.config_paths(cfg)
    if not sources:
        return ("missing", f"no workspace*.toml found under {workspace_root()}")
    from pathlib import Path

    lock = Path(workspace_root()) / "workspace-lock.toml"
    if not lock.exists():
        return ("warn", "no workspace-lock.toml — run `git workspace update`")
    return ("ok", f"{len(gitworkspace.groups(cfg))} repo groups; lockfile present")


PLUGIN = plugins.Plugin(
    name="git-workspace",
    cli=cli,
    enabled=lambda cfg, entry: gitworkspace.enabled(cfg),
    readiness=_readiness,
)
