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

# bd's idempotent re-add refusal — expected on every re-sync, not an error.
_ALREADY_CONFIGURED = "already configured"


def _output(res) -> str:
    """Combined stdout+stderr of a captured CompletedProcess, stripped."""
    return ((res.stdout or "") + (res.stderr or "")).strip()


def _err_line(res) -> str:
    """First non-empty output line — the `Error: …` headline, never bd's usage dump."""
    for line in _output(res).splitlines():
        if line.strip():
            return line.strip()
    return f"exit {res.returncode}"


def ensure_hub():
    hub = config.hub_dir()
    if not (hub / ".beads").is_dir():
        hub.mkdir(parents=True, exist_ok=True)
        cmd = [
            "bd", "init", "--prefix", "hub", "--skip-agents", "--skip-hooks", "--non-interactive"
        ]
        try:
            res = run(cmd, cwd=str(hub), env=_BD_NI, check=False, capture=True)
        except FileNotFoundError:
            typer.echo(
                "✗ `bd` not found on PATH — install beads before running `ws sync`", err=True
            )
            raise typer.Exit(1) from None
        if res.returncode:
            typer.echo(f"✗ bd init failed for hub {hub}: {_err_line(res)}", err=True)
            raise typer.Exit(1)
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
    """Make the hub reflect every registered rig (cloned by path, uncloned via cache).

    `bd repo sync` hydrates the hub only from each rig's `.beads/issues.jsonl`, but
    dolt-backend rigs keep no such file on disk — so export each rig's beads to JSONL first
    (`bd export` is dolt-aware; `.beads/` is gitignored, so this leaves no working-tree noise).
    A rig whose import still fails (e.g. corrupt beads data bd can't round-trip) is reported as
    failed rather than folded into a blanket green. `bd repo add` output is captured: an
    'already configured' refusal is the expected idempotent re-add (silent), any other non-zero
    exit is a real failure — surfaced and excluded from the hydrated count.

    Returns the prefixes that failed to hydrate (empty on full success).
    """
    hub = ensure_hub()
    cfg = config.load()
    added, skipped, failed = [], [], []
    for e in cfg.get("managed_repos", []):
        prefix = str(e["prefix"])
        path = registry.rig_dir(e)
        src = path if (path / ".beads").is_dir() else _fetch_cache(cfg, e)
        if src is None:
            typer.echo(f"  ⚠ skip {prefix}: not cloned and no remote beads data", err=True)
            skipped.append(prefix)
            continue
        export = run(
            ["bd", "-C", str(src), "export", "-o", str(src / ".beads" / "issues.jsonl")],
            env=_BD_NI,
            check=False,
            capture=True,
        )
        if export.returncode:
            # not fatal by itself — repo sync may still hydrate from an existing JSONL
            typer.echo(f"  ⚠ {prefix}: bd export failed: {_err_line(export)}", err=True)
        add = run(["bd", "-C", str(hub), "repo", "add", str(src)], check=False, capture=True)
        if add.returncode and _ALREADY_CONFIGURED not in _output(add):
            typer.echo(f"  ✗ {prefix}: bd repo add failed: {_err_line(add)}", err=True)
            failed.append(prefix)
            continue
        added.append((prefix, str(src)))

    res = run(["bd", "-C", str(hub), "repo", "sync"], check=False, capture=True)
    report = (res.stdout or "") + (res.stderr or "")
    if res.returncode:
        typer.echo(f"  ✗ bd repo sync failed: {_err_line(res)}", err=True)
        failed.extend(prefix for prefix, _ in added)
        added = []
    elif report.strip():
        typer.echo(report.strip(), err=True)
    failed.extend(prefix for prefix, src in added if f"failed to import from {src}" in report)
    hydrated = [prefix for prefix, _ in added if prefix not in failed]

    from . import metadata
    metadata.invalidate(cfg)  # fleet-wide sync — coarse; the next doctor/survey recomputes
    mark = "⚠" if failed else "✓"
    summary = f"{mark} hub synced: {len(hydrated)} hydrated, {len(skipped)} skipped"
    if failed:
        summary += f", {len(failed)} failed to hydrate ({', '.join(failed)})"
    typer.echo(summary + " → query with `ws hub bd ready`")
    return failed


def query(args):
    hub = config.hub_dir()
    if not (hub / ".beads").is_dir():
        typer.echo("✗ hub not initialized — run `ws sync` first", err=True)
        raise typer.Exit(1)
    rc = run(["bd", "-C", str(hub), *args], check=False).returncode
    if rc:
        raise typer.Exit(rc)
