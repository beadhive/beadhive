"""gitworkspace_plugin.py — git-workspace's `bh plugin`-shaped CLI + readiness surface.

git-workspace is a required DEP (`deps.py`, `required=ALWAYS`), not a `plugins.Plugin`
(bh-hsus.4 — it used to be dual-classified: a plugin gated by `git_workspace.enabled`, which
defaulted to False, while `setup.PROBE_TABLE` required the binary unconditionally). It is
however the one dep with a real `bh plugin`-shaped surface (a sub-app + a hive-ready line), so
this module still carries that surface — mounted and called EXPLICITLY by `cli.py` /
`hive_ready.py` rather than through the generic `plugins.registry()` loop. `gitworkspace.py`
itself stays pure-stdlib (no typer import); this thin module carries the `bh plugin
git-workspace …` sub-app on top of it. Mirrors `orca.PLUGIN`'s sub-app shape, minus the
`plugins.Plugin` registration.
"""

from __future__ import annotations

import typer

from . import gitworkspace
from .identity import workspace_root

cli = typer.Typer(no_args_is_help=True, help="git-workspace repo-group integration.")


@cli.command("groups", help="list repo groups (path/provider/account/filters).")
def _groups_cmd() -> None:
    from . import config

    groups = gitworkspace.groups(config.load())
    if not groups:
        typer.echo("• no repo groups found (no workspace*.toml under $GIT_WORKSPACE)")
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


def readiness(cfg, entry=None) -> tuple[str, str] | None:
    """`bh hive ready` line: is git-workspace itself set up (env, sources, lockfile)?

    Not hive-specific — `entry` is accepted (mirrors the old `plugins.Plugin.readiness`
    signature `hive_ready.py` calls every check with) but unused; git-workspace readiness is a
    workspace-wide signal, not a per-hive one. Called directly by `hive_ready.py`, not through
    a generic plugin loop — git-workspace has no `enabled` gate to loop over any more."""
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
