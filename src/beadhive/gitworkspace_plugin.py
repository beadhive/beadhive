"""gitworkspace_plugin.py — promotes git-workspace to a proper `bh` plugin (bh-4y0r.4).

Mirrors `orca.PLUGIN`: `gitworkspace.py` itself stays pure-stdlib (no typer / plugins import),
so this thin module carries the `bh plugin git-workspace …` sub-app + the `plugins.Plugin`
registration on top of it.
"""

from __future__ import annotations

import typer

from . import gitworkspace, plugins
from .identity import workspace_mode, workspace_root

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
    """hive-ready hook: is the workspace root itself set up (sources, lockfile)?

    Not hive-specific — `entry` is accepted (per the generic `plugins.Plugin` contract) but
    unused; git-workspace readiness is a workspace-wide signal, not a per-hive one.

    Internal mode (bh-cgcg.2) owns its root — there is nothing for the user to set, so the
    old "GIT_WORKSPACE not set" warning is simply wrong there; the check instead is whether
    the managed root has been created and seeded (`bh doctor` offers to do both). External
    mode keeps the original env-var-aware warning: the root there is the user's, resolved
    from `$GIT_WORKSPACE` or the legacy `~/workspace` default."""
    from pathlib import Path

    root = Path(workspace_root())
    if workspace_mode(str(root)) == "internal":
        if not gitworkspace.is_seeded(root):
            return (
                "missing",
                f"internal workspace root not created/seeded: {root}"
                " — `bh doctor` offers to create it",
            )
    else:
        import os

        if not os.environ.get("GIT_WORKSPACE"):
            return ("warn", f"GIT_WORKSPACE not set — defaulting to {root}")
    sources = gitworkspace.config_paths(cfg)
    if not sources:
        return ("missing", f"no workspace*.toml found under {root}")
    lock = root / "workspace-lock.toml"
    if not lock.exists():
        return ("warn", "no workspace-lock.toml — run `git workspace update`")
    return ("ok", f"{len(gitworkspace.groups(cfg))} repo groups; lockfile present")


PLUGIN = plugins.Plugin(
    name="git-workspace",
    cli=cli,
    enabled=lambda cfg, entry: gitworkspace.enabled(cfg),
    readiness=_readiness,
)
