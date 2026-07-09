"""Rig routing for ws's passthrough commands (`bd`, `git`).

Routing comes from the global `-a/--all` / `-r/--rig` flags on the root callback and is
resolved to a set of target rigs. `-a`/`-r` require git_workspace enabled; the default
(no flag) targets the current directory and needs neither config nor git-workspace.
"""

from __future__ import annotations

from pathlib import Path

import typer

from . import gitworkspace, registry
from .identity import workspace_root

_INLINE_FLAGS = {"-a", "--all", "-r", "--rig"}


def reject_inline_flags(args):
    """Routing flags are global (before the subcommand); hint if one appears after it."""
    if args and args[0] in _INLINE_FLAGS:
        typer.echo(
            f"✗ routing flags go before the subcommand — e.g. `ws {args[0]} … git <cmd>`",
            err=True,
        )
        raise typer.Exit(1)


def targets(cfg, mode, target):
    """[(label, cwd)] — label None / cwd None means 'the current directory'."""
    if mode == "cwd":
        return [(None, None)]
    if not gitworkspace.enabled(cfg):
        typer.echo("✗ this feature requires git_workspace enabled in config", err=True)
        raise typer.Exit(1)
    if mode == "rig":
        entry = registry.resolve_rig(cfg, target)
        return [(str(entry["prefix"]), registry.rig_dir(entry))]
    return registry.all_rig_targets(cfg)


def invalidate_targets(cfg, tgts):
    """Invalidate the metadata cache for the rig(s) a `git`/`bd` passthrough just ran against.

    A current-dir passthrough (cwd ``None``) is skipped — the fingerprint probe self-heals any
    out-of-band git-state change. A single routed rig (`-r`) is invalidated per-repo (cheap +
    obvious, warmed in the background); a fleet fan-out (`-a`) invalidates coarsely.
    """
    from . import metadata

    cwds = [cwd for _label, cwd in tgts if cwd is not None]
    if not cwds:
        return
    if len(cwds) > 1:
        metadata.invalidate(cfg)
        return
    root = Path(workspace_root()).resolve()
    try:
        key = str(Path(cwds[0]).resolve().relative_to(root))
    except ValueError:
        return
    metadata.invalidate(cfg, key)


def fan_out(tgts, runner):
    """Run runner(label, cwd) over targets. Single CWD run propagates the exact exit code;
    multiplexed runs label each, continue on failure, summarize, and exit 1 if any failed."""
    if len(tgts) == 1 and tgts[0][0] is None:
        rc = runner(*tgts[0])
        if rc:
            raise typer.Exit(rc)
        return

    ok = failed = skipped = 0
    for label, cwd in tgts:
        loc = f"  {cwd}" if cwd else ""
        typer.echo(f"=== {label}{loc} ===")
        if cwd is not None and not Path(cwd).exists():
            typer.echo(f"  ⚠ skip: no checkout at {cwd}", err=True)
            skipped += 1
            continue
        if runner(label, cwd) == 0:
            ok += 1
        else:
            failed += 1
    typer.echo(f"\n{ok} ok / {failed} failed / {skipped} skipped")
    if failed:
        raise typer.Exit(1)
