"""The hydration hub: one aggregated beads DB (under $BH_HOME) holding a cross-rig
view of every registered rig.

`bh sync` builds/refreshes it — cloned rigs are added by local path; uncloned rigs are
fetched into a minimal-clone cache (blobless, no working tree) via `bd bootstrap`, then
added. `bh hub <bd cmd>` queries it. So the aggregate works whether or not a rig's code
is checked out, and `bh` itself needs no repo cloned beyond the caches.
"""

from __future__ import annotations

import os

import typer

from . import bd, config, gitworkspace, guard, registry
from .run import run

_BD_NI = {**os.environ, "BD_NON_INTERACTIVE": "1"}

# bd's idempotent re-add refusal — expected on every re-sync, not an error.
_ALREADY_CONFIGURED = "already configured"


def _output(res) -> str:
    """Combined stdout+stderr of a captured CompletedProcess, stripped."""
    return ((res.stdout or "") + (res.stderr or "")).strip()


def ensure_store(store, prefix):
    """bd-init a local git+bd aggregation store at ``store`` (prefix ``prefix``) if absent, and
    return it. Shared by the legacy disposable hub and the durable Factory HQ — the one place
    the cross-rig aggregate is stood up."""
    if not (store / ".beads").is_dir():
        store.mkdir(parents=True, exist_ok=True)
        cmd = [
            "bd", "init", "--prefix", prefix, "--skip-agents", "--skip-hooks", "--non-interactive"
        ]
        try:
            res = run(cmd, cwd=str(store), env=_BD_NI, check=False, capture=True)
        except FileNotFoundError:
            typer.echo(
                "✗ `bd` not found on PATH — install beads before running "
                f"`{config.BINARY_ALIAS} sync`",
                err=True,
            )
            raise typer.Exit(1) from None
        if res.returncode:
            typer.echo(f"✗ bd init failed for {prefix} store {store}: {bd.err_line(res)}", err=True)
            raise typer.Exit(1)
    return store


def _aggregation_target():
    """``(dir, prefix)`` of the cross-rig aggregate: the durable Factory HQ store (kind=hq) once
    one is registered, else the legacy disposable hub (pre-HQ back-compat). HQ subsumes the hub —
    the aggregation role moves onto it — so hub.py points here, not at ``hub_dir()`` alone."""
    try:
        cfg = config.load()
    except FileNotFoundError:
        cfg = {}
    if registry.rig_of_kind(cfg, registry.HQ_KIND) is not None:
        return config.hq_dir(), registry.HQ_PREFIX
    return config.hub_dir(), "hub"


def ensure_hub():
    store, prefix = _aggregation_target()
    return ensure_store(store, prefix)


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
    (`bd export` is dolt-aware). Under the tracked-beads convention `.beads/issues.jsonl` is
    committed, so this export dirties the working tree; that churn is rig-state bookkeeping
    (discounted by `safety._non_rig_dirty_paths` via its `.beads/` prefix), not a real edit.
    A rig whose import still fails (e.g. corrupt beads data bd can't round-trip) is reported as
    failed rather than folded into a blanket green. `bd repo add` output is captured: an
    'already configured' refusal is the expected idempotent re-add (silent), any other non-zero
    exit is a real failure — surfaced and excluded from the hydrated count.

    Returns the prefixes that failed to hydrate (empty on full success).
    """
    hub = ensure_hub()
    cfg = config.load()
    managed = [
        e for e in cfg.get("managed_repos", [])
        if str(e.get("kind", "")) != registry.HQ_KIND
    ]
    n = len(managed)
    typer.echo(f"starting hub sync ({n} rig(s))…", err=True)
    added, skipped, failed = [], [], []
    for i, e in enumerate(managed, 1):
        prefix = str(e["prefix"])
        typer.echo(f"• syncing {prefix} ({i}/{n})", err=True)
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
            typer.echo(f"  ⚠ {prefix}: bd export failed: {bd.err_line(export)}", err=True)
        add = run(["bd", "-C", str(hub), "repo", "add", str(src)], check=False, capture=True)
        if add.returncode and _ALREADY_CONFIGURED not in _output(add):
            typer.echo(f"  ✗ {prefix}: bd repo add failed: {bd.err_line(add)}", err=True)
            failed.append(prefix)
            continue
        added.append((prefix, str(src)))

    res = run(["bd", "-C", str(hub), "repo", "sync"], check=False, capture=True)
    report = (res.stdout or "") + (res.stderr or "")
    if res.returncode:
        typer.echo(f"  ✗ bd repo sync failed: {bd.err_line(res)}", err=True)
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
    typer.echo(summary + f" → query with `{config.BINARY_ALIAS} hub bd ready`")
    return failed


def query(args):
    guard.guard_hub(args)  # the hub is a READ cache — refuse writes (they strand beads)
    hub, _ = _aggregation_target()
    if not (hub / ".beads").is_dir():
        typer.echo(f"✗ hub not initialized — run `{config.BINARY_ALIAS} sync` first", err=True)
        raise typer.Exit(1)
    rc = run(["bd", "-C", str(hub), *args], check=False).returncode
    if rc:
        raise typer.Exit(rc)


def intake(extra=None):
    """The superintendent's FLEET-WIDE inbox: untriaged intake across every hydrated rig.

    Source-agnostic by construction — the `intake:untriaged` label is set by every source
    (report | github | import), so one filter surfaces the whole fleet's untriaged reports. A read
    against the hub cache (allowlisted by the write-guard); extra `bd list` flags (e.g. `--json`,
    `--assignee`) forward through."""
    from .state import INTAKE_UNTRIAGED

    query(["list", "--label", INTAKE_UNTRIAGED, "--status", "open", *(extra or [])])
