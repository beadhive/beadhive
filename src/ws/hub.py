"""The hydration hub: one aggregated beads DB (under $WS_HOME) holding a cross-rig
view of every registered rig.

`ws sync` builds/refreshes it — cloned rigs are added by local path; uncloned rigs are
fetched into a minimal-clone cache (blobless, no working tree) via `bd bootstrap`, then
added. `ws hub <bd cmd>` queries it. So the aggregate works whether or not a rig's code
is checked out, and `ws` itself needs no repo cloned beyond the caches.
"""

from __future__ import annotations

import os

import typer

from . import config, gitworkspace, registry
from .run import run

_BD_NI = {**os.environ, "BD_NON_INTERACTIVE": "1"}


def ensure_hub():
    hub = config.hub_dir()
    if not (hub / ".beads").is_dir():
        hub.mkdir(parents=True, exist_ok=True)
        run(
            ["bd", "init", "--prefix", "hub", "--skip-agents", "--skip-hooks", "--non-interactive"],
            cwd=str(hub),
            env=_BD_NI,
        )
    return hub


def _rig_url(cfg, entry):
    """Clone URL for a rig: exact from the git-workspace lock, else derive for github/gitlab."""
    key = f"{entry['provider']}/{entry['org']}/{entry['repo']}"
    url = gitworkspace.repo_urls(cfg).get(key)
    if url:
        return url
    provider, org, repo = entry["provider"], entry["org"], entry["repo"]
    if provider == "github":
        return f"git@github.com:{org}/{repo}.git"
    if provider == "gitlab":
        return f"git@gitlab.com:{org}/{repo}.git"
    return None


def _fetch_cache(cfg, entry):
    """Minimal-clone (blobless, no checkout) + bootstrap a rig's beads into the cache.
    Returns the cache path, or None if it couldn't be fetched."""
    cache = config.cache_dir() / entry["provider"] / entry["org"] / entry["repo"]
    if not (cache / ".git").is_dir():
        url = _rig_url(cfg, entry)
        if not url:
            return None
        cache.parent.mkdir(parents=True, exist_ok=True)
        rc = run(
            ["git", "clone", "--filter=blob:none", "--no-checkout", url, str(cache)],
            check=False,
        ).returncode
        if rc:
            return None
    # bootstrap pulls refs/dolt/data (idempotent; refreshes on later syncs)
    run(["bd", "bootstrap", "--non-interactive"], cwd=str(cache), env=_BD_NI, check=False)
    return cache if (cache / ".beads").is_dir() else None


def sync():
    """Make the hub reflect every registered rig (cloned by path, uncloned via cache)."""
    hub = ensure_hub()
    cfg = config.load()
    cloned, cached, skipped = [], [], []
    for e in cfg.get("managed_repos", []):
        prefix = str(e["prefix"])
        path = registry.rig_dir(e)
        if (path / ".beads").is_dir():
            run(["bd", "-C", str(hub), "repo", "add", str(path)], check=False)
            cloned.append(prefix)
            continue
        cache = _fetch_cache(cfg, e)
        if cache is None:
            typer.echo(f"  ⚠ skip {prefix}: not cloned and no remote beads data", err=True)
            skipped.append(prefix)
            continue
        run(["bd", "-C", str(hub), "repo", "add", str(cache)], check=False)
        cached.append(prefix)

    run(["bd", "-C", str(hub), "repo", "sync"], check=False)
    from . import metadata
    metadata.invalidate(cfg)  # fleet-wide sync — coarse; the next doctor/survey recomputes
    typer.echo(
        f"✓ hub synced: {len(cloned)} cloned, {len(cached)} remote-cached, "
        f"{len(skipped)} skipped → query with `ws hub bd ready`"
    )


def query(args):
    hub = config.hub_dir()
    if not (hub / ".beads").is_dir():
        typer.echo("✗ hub not initialized — run `ws sync` first", err=True)
        raise typer.Exit(1)
    rc = run(["bd", "-C", str(hub), *args], check=False).returncode
    if rc:
        raise typer.Exit(rc)
