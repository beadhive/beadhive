"""`ws doctor` — status + diagnostics.

Shows providers / orgs / rigs / repo counts (config + git-workspace), then warns
about config drift and untracked or unrecognized folders under the workspace root.
Informational: always exits 0.

Data-collection is separated from rendering section by section: each section has a
pure ``_data_*`` builder that returns a structured (JSON-able) fragment and a
``_render_*`` that echoes it verbatim. ``doctor_payload()`` assembles the whole
structured dict (exposed as the ``ws://doctor`` MCP resource); ``doctor()`` renders
the SAME builders, so the human text output is unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from . import config, gitworkspace, metadata, registry, rig, safety, worktree
from .identity import workspace_root
from .run import run


def _tracked(root: Path):
    """'provider/org/repo' set tracked by git-workspace, or None if unavailable."""
    res = run(["git", "workspace", "list"], check=False, capture=True)
    if res.returncode != 0:
        return None
    out = set()
    prefix = str(root) + "/"
    for line in res.stdout.splitlines():
        s = line.strip()
        if s.startswith(prefix):
            s = s[len(prefix) :]
        parts = s.split("/")
        if len(parts) >= 3:
            out.add("/".join(parts[:3]))
    return out


def _scan(root: Path, providers):
    """Walk <provider>/<org>/<repo> under recognized provider dirs.

    Returns (git_repos, nonrepo_dirs, unknown_top) — the first two as 'p/o/r' keys.
    """
    git_repos, nonrepo, unknown_top = set(), set(), []
    if not root.is_dir():
        return git_repos, nonrepo, unknown_top

    def dirs(p):
        return sorted(c for c in p.iterdir() if c.is_dir() and not c.name.startswith("."))

    for prov in dirs(root):
        if prov.name not in providers:
            unknown_top.append(prov.name)
            continue
        for org in dirs(prov):
            for repo in dirs(org):
                key = f"{prov.name}/{org.name}/{repo.name}"
                (git_repos if (repo / ".git").exists() else nonrepo).add(key)
    return git_repos, nonrepo, unknown_top


# ---- overview sections (shared by `doctor` and `config show`) ----------------
# Each section is a pure `_data_*` builder + a `_render_*` echoer, so the payload
# and the text render consume the SAME data.


def _data_config(cfg, root, gw_on) -> dict:
    """Config section: config path, workspace root, git-workspace enablement + sources."""
    sources = [str(p) for p in gitworkspace.config_paths(cfg)] if gw_on else []
    return {
        "config_path": str(config.config_path()),
        "workspace_root": str(root),
        "git_workspace": {"enabled": bool(gw_on), "sources": sources},
    }


def _render_config(d: dict) -> None:
    typer.echo("# Config")
    typer.echo(f"  config: {d['config_path']}")
    typer.echo(f"  workspace root: {d['workspace_root']}")
    if d["git_workspace"]["enabled"]:
        src = ", ".join(d["git_workspace"]["sources"]) or "NO workspace*.toml found"
        typer.echo(f"  git-workspace: enabled ({src})")
    else:
        typer.echo("  git-workspace: disabled")


def _data_providers(cfg) -> list[dict]:
    """Providers section: effective providers with their source (config / git-workspace / both)."""
    cfg_provs = set(cfg.get("providers", []) or [])
    gw_provs = gitworkspace.providers(cfg) if gitworkspace.enabled(cfg) else set()
    items = []
    for p in registry.effective_providers(cfg):
        src = (
            "both"
            if p in cfg_provs and p in gw_provs
            else "config"
            if p in cfg_provs
            else "git-workspace"
        )
        items.append({"name": p, "source": src})
    return items


def _render_providers(items: list[dict]) -> None:
    typer.echo("\n# Providers")
    for p in items:
        typer.echo(f"  provider:{p['name']}  ({p['source']})")


def _data_orgs(cfg) -> list[dict]:
    """Orgs section: each org's code label, policy, source, and exclusion flag."""
    cfg_orgs = cfg.get("orgs", {}) or {}
    gw_orgs = gitworkspace.orgs(cfg) if gitworkspace.enabled(cfg) else set()
    excluded_orgs = set((cfg.get("exclude", {}) or {}).get("orgs", []) or [])
    items = []
    for o in sorted(set(cfg_orgs) | gw_orgs):
        code = registry.org_code(cfg, o)
        code_str = f"{code} (explicit)" if code else f"{registry.sanitize(o)[:2]} (auto)"
        src = (
            "both"
            if o in cfg_orgs and o in gw_orgs
            else "config"
            if o in cfg_orgs
            else "git-workspace"
        )
        items.append(
            {
                "org": o,
                "code": code_str,
                "policy": registry.org_policy(cfg, o),
                "source": src,
                "excluded": o in excluded_orgs,
            }
        )
    return items


def _render_orgs(items: list[dict]) -> None:
    typer.echo("\n# Orgs")
    for o in items:
        excl = " [excluded]" if o["excluded"] else ""
        typer.echo(
            f"  org:{o['org']}  code={o['code']}  policy={o['policy']}  ({o['source']}){excl}"
        )


def _data_rigs(cfg) -> list[dict]:
    """Rigs section: the registered rigs as prefix + provider/org/repo + kind."""
    rigs = cfg.get("managed_repos", []) or []
    return [
        {
            "prefix": e["prefix"],
            "provider": e["provider"],
            "org": e["org"],
            "repo": e["repo"],
            "kind": e["kind"],
        }
        for e in rigs
    ]


def _render_rigs(items: list[dict]) -> None:
    typer.echo(f"\n# Rigs ({len(items)})")
    for e in items:
        typer.echo(f"  {e['prefix']}\t{e['provider']}/{e['org']}/{e['repo']} ({e['kind']})")


def _overview(cfg, root, gw_on):
    """The Config/Providers/Orgs/Rigs header — the part doctor and `config show` share."""
    _render_config(_data_config(cfg, root, gw_on))
    _render_providers(_data_providers(cfg))
    _render_orgs(_data_orgs(cfg))
    _render_rigs(_data_rigs(cfg))


# ---- config-only render sections (just `config show`) -----------------------


def _section_dimensions(cfg):
    dims = cfg.get("dimensions", {}) or {}
    typer.echo(f"\n# Dimensions ({len(dims)})")
    for k, v in dims.items():
        v = v or {}
        vals = v.get("values")
        if vals is None:
            kind = "open"
        elif vals:
            kind = f"closed: {', '.join(str(x) for x in vals)}"
        else:
            kind = "closed (reserved)"
        desc = v.get("description", "")
        typer.echo(f"  {k}:  {kind}" + (f"  — {desc}" if desc else ""))


def _section_exclude(cfg):
    ex = cfg.get("exclude", {}) or {}
    typer.echo("\n# Exclude")
    typer.echo(f"  orgs:  {', '.join(ex.get('orgs', []) or []) or '(none)'}")
    typer.echo(f"  repos: {', '.join(ex.get('repos', []) or []) or '(none)'}")


def _section_dolt(cfg):
    typer.echo("\n# Dolt")
    typer.echo(f"  backend: {config.dolt_cfg(cfg).get('backend', '(unset)')}")


# ---- worktrees section (shared by `doctor` and `config show`) ---------------


def _data_worktrees(cfg) -> dict:
    """Worktrees section: effective bead/session branches, root, ephemerality, init-rule count."""
    w = config.worktrees_cfg(cfg)
    # Show the EFFECTIVE branches (templates are suffixes; wt/ is always prepended).
    bead = worktree.apply_prefix(w.get("bead_branch", "bead/{kind}/{id}"))
    session = worktree.apply_prefix(w.get("session_branch", "session/{ts}-{rand}"))
    return {
        "ephemeral": config.worktrees_ephemeral(cfg),
        "root": str(config.worktrees_root(cfg)),
        "bead": bead,
        "session": session,
        "rmdir_empty": w.get("rmdir_empty", True),
        "init_rules": len(w.get("init", []) or []),
    }


def _render_worktrees(d: dict) -> None:
    typer.echo("\n# Worktrees")
    typer.echo(f"  ephemeral: {str(d['ephemeral']).lower()}")
    note = "  (OS temp, session-scoped)" if d["ephemeral"] else "  (persistent; sandbox grants on)"
    typer.echo(f"  root: {d['root']}{note}")
    typer.echo("  branch prefix: wt/  (all managed worktree branches)")
    typer.echo(f"  bead:    {d['bead']}")
    typer.echo("  branch:  wt/<name>  (--branch is prefixed, not a full override)")
    typer.echo(f"  session: {d['session']}")
    typer.echo(f"  rmdir_empty: {str(d['rmdir_empty']).lower()}")
    typer.echo(f"  init rules: {d['init_rules']} global")


def _section_worktrees(cfg):
    """Render the worktrees section (config show + doctor entry point)."""
    _render_worktrees(_data_worktrees(cfg))


# ---- molecule branches section ----------------------------------------------


def _orphan_container_branches(cfg):
    """Container branches `wt/bead/epic/<epic>` whose epic is closed — i.e. a molecule landed but
    its branch wasn't deleted. `ws work merge --molecule` / `finish` deletes the branch best-effort
    (warns, never fails), so a rare delete failure leaves a stale ref. Returns
    [(rig_prefix, branch), …]. A branch whose epic is still open is an active molecule, not an
    orphan, so it's skipped."""
    from .work import _show  # local: avoids a load-time cycle, reuses work's bd seam

    prefix = f"{worktree._BEAD_PREFIX}epic/"  # wt/bead/epic/
    orphans = []
    for e in cfg.get("managed_repos", []) or []:
        main = registry.rig_dir(e)
        res = run(
            [
                "git",
                "-C",
                str(main),
                "for-each-ref",
                "--format=%(refname:short)",
                f"refs/heads/{prefix}",
            ],
            check=False,
            capture=True,
        )
        if res.returncode != 0:
            continue
        for branch in (res.stdout or "").split():
            epic = branch[len(prefix) :]
            bead = _show(epic, main)
            if bead and bead.get("status") == "closed":
                orphans.append((str(e["prefix"]), branch))
    return orphans


def _data_molecules(cfg) -> dict:
    """Molecule-branches section: orphaned container branches (closed epic, undeleted ref)."""
    return {"orphaned": [{"prefix": p, "branch": b} for p, b in _orphan_container_branches(cfg)]}


def _render_molecules(d: dict) -> None:
    orphaned = d["orphaned"]
    typer.echo(f"\n# Molecule branches ({len(orphaned)} orphaned)")
    if not orphaned:
        typer.echo("  ✓ none")
        return
    for o in orphaned:
        typer.echo(f"  ⚠ {o['prefix']}\t{o['branch']} (epic closed — delete manually)")


def _section_molecules(cfg):
    """Render the molecule-branches section."""
    _render_molecules(_data_molecules(cfg))


# ---- MCP section ------------------------------------------------------------


def _plugin_declares_server(cfg) -> bool:
    """True when the agf marketplace clone's plugins/agf/.mcp.json declares mcpServers.ws."""
    try:
        root = config._marketplace_root(cfg, config.claude_plugin_name(cfg))
        mcp_path = root / "plugins" / "agf" / ".mcp.json"
        if not mcp_path.is_file():
            return False
        data = json.loads(mcp_path.read_text())
        return "ws" in (data.get("mcpServers") or {})
    except Exception:  # noqa: BLE001
        return False


def _data_mcp(cfg=None) -> dict:
    """MCP section: [mcp] extra availability, plugin server declaration, and preflight hints."""
    try:
        import fastmcp  # noqa: F401

        mcp_extra = True
    except ImportError:
        mcp_extra = False
    plugin_declares = _plugin_declares_server(cfg)
    return {
        "mcp_extra": mcp_extra,
        "plugin_declares_server": plugin_declares,
        # Legacy alias kept for backward compatibility with callers that read
        # fastmcp_available from the ws://doctor payload.
        "fastmcp_available": mcp_extra,
    }


def _render_mcp(d: dict) -> None:
    typer.echo("\n# MCP")
    if d["mcp_extra"] and d["plugin_declares_server"]:
        typer.echo("  fastmcp: available")
        typer.echo("  plugin declares server: yes")
    elif d["mcp_extra"] and not d["plugin_declares_server"]:
        typer.echo("  fastmcp: available")
        typer.echo("  plugin declares server: no (run: claude plugin update)")
    else:
        typer.echo("  fastmcp: unavailable")
        typer.echo("  install: uv tool install 'ws[otel,mcp]'  (or: pip install 'ws[otel,mcp]')")
        typer.echo("  hint: without [mcp] the bundled ws server will silently fail to register")
        if d["plugin_declares_server"]:
            typer.echo("  plugin declares server: yes")


def _section_mcp(cfg=None):
    """Report MCP extra availability and plugin server declaration."""
    _render_mcp(_data_mcp(cfg))


# ---- observability section --------------------------------------------------


def _data_observability(cfg) -> dict:
    """Observability section: resolved log settings, OTel enablement + library availability."""
    try:
        import opentelemetry  # noqa: F401

        otel_libs = True
    except ImportError:
        otel_libs = False
    return {
        "log_format": config.log_format(cfg),
        "log_level": config.log_level(cfg),
        "otel_enabled": config.otel_enabled(cfg),
        "otel_libs": otel_libs,
        "endpoint": config.otel_endpoint(cfg) or None,
    }


def _render_observability(d: dict) -> None:
    typer.echo("\n# Observability")
    typer.echo(f"  log.format: {d['log_format']}")
    typer.echo(f"  log.level: {d['log_level']}")
    typer.echo(f"  otel.enabled: {str(d['otel_enabled']).lower()}")
    if d["otel_libs"]:
        typer.echo("  otel libs: available")
    else:
        typer.echo("  otel libs: unavailable (install: pip install 'ws[otel]')")
    typer.echo(f"  endpoint: {d['endpoint'] or '(not set)'}")


def _section_observability(cfg):
    """Report resolved log settings and OTel enablement / library availability."""
    _render_observability(_data_observability(cfg))


# ---- fleet health section ---------------------------------------------------


def _data_fleet_health(
    records: dict[str, metadata.RepoMetadata], git_repos: set[str]
) -> dict:
    """Fleet-wide safety and reclamation summary rolled up from the workspace-metadata cache.

    Reads pre-measured ``records`` (one per git repo under recognized provider dirs; the same
    aggregation the Disk Usage section consumes, so each repo is measured at most once per
    ``ws doctor`` — the disk-walk double-scan is gone) and tallies:
    - dirty repos (uncommitted working-tree changes on any branch)
    - repos with unpushed branches (any branch ahead > 0 vs its upstream)
    - no-origin repos (no remote named ``origin`` — local-only, cannot be re-cloned)
    - stale clones (last commit older than ``safety.MATURITY_STALE_DAYS`` days)
    - reclaimable space (disk_bytes of no-origin OR stale repos; counted once each)

    A repo that is both no-origin and stale is counted in disk only once.
    """
    dirty_count = 0
    unpushed_count = 0
    no_origin_count = 0
    stale_count = 0
    reclaimable_bytes = 0

    for key in git_repos:
        rec = records.get(key)
        if rec is None:
            continue

        is_no_origin = not rec.has_origin
        is_dirty = any(b["dirty"] for b in rec.branches)
        has_unpushed = any(b["ahead"] > 0 for b in rec.branches)
        # Cache stores age_days=None for a no-commit repo (inf) — inf >= threshold ⇒ stale.
        is_stale = rec.age_days is None or rec.age_days >= safety.MATURITY_STALE_DAYS

        if is_dirty:
            dirty_count += 1
        if has_unpushed:
            unpushed_count += 1
        if is_no_origin:
            no_origin_count += 1
        if is_stale:
            stale_count += 1
        if is_no_origin or is_stale:
            reclaimable_bytes += rec.disk_bytes

    return {
        "repos_scanned": len(git_repos),
        "dirty": dirty_count,
        "unpushed": unpushed_count,
        "no_origin": no_origin_count,
        "stale": stale_count,
        "reclaimable_bytes": reclaimable_bytes,
        "stale_threshold_days": safety.MATURITY_STALE_DAYS,
    }


def _render_fleet_health(d: dict) -> None:
    stale_threshold = f"{d['stale_threshold_days']:.0f}d"
    typer.echo(f"\n# Fleet Health ({d['repos_scanned']} repos scanned)")
    typer.echo(f"  dirty repos:          {d['dirty']}")
    typer.echo(f"  unpushed branches:    {d['unpushed']}")
    typer.echo(f"  no-origin repos:      {d['no_origin']}")
    typer.echo(f"  stale clones:         {d['stale']}  (>{stale_threshold} since last commit)")
    reclaimable_str = safety.format_bytes(d["reclaimable_bytes"])
    typer.echo(f"  reclaimable space:    {reclaimable_str}  (no-origin or stale)")


def _section_fleet_health(records: dict[str, metadata.RepoMetadata], git_repos: set[str]) -> None:
    """Render the fleet-health section from pre-measured metadata records."""
    _render_fleet_health(_data_fleet_health(records, git_repos))


# ---- inventory + disk usage sections ----------------------------------------


def _render_inventory(d: dict) -> None:
    typer.echo("\n# Inventory (under recognized provider dirs)")
    typer.echo(f"  rigs registered:        {d['rigs_registered']}")
    typer.echo(f"  git repos on disk:      {d['git_repos_on_disk']}")
    typer.echo(f"  onboarding candidates:  {d['onboarding_candidates']}")
    typer.echo(f"  excluded:               {d['excluded']}")
    if d["untracked_git_repos"] is not None:
        typer.echo(f"  untracked git repos:    {d['untracked_git_repos']}")
    typer.echo(f"  non-repo folders:       {d['non_repo_folders']}")
    typer.echo(f"  unrecognized top dirs:  {d['unrecognized_top_dirs']}")


def _data_disk_usage(rigs, root: Path, records) -> dict:
    """Disk-usage section: per-rig disk_bytes (or missing) + the total across present rigs."""
    entries = []
    total_bytes = 0
    for e in rigs:
        path = root / e["provider"] / e["org"] / e["repo"]
        if not path.exists():
            entries.append({"prefix": str(e["prefix"]), "missing": True, "disk_bytes": None})
            continue
        rec = records.get(f"{e['provider']}/{e['org']}/{e['repo']}")
        disk_bytes = rec.disk_bytes if rec is not None else 0
        total_bytes += disk_bytes
        entries.append({"prefix": str(e["prefix"]), "missing": False, "disk_bytes": disk_bytes})
    return {"rigs": entries, "total_bytes": total_bytes}


def _render_disk_usage(d: dict) -> None:
    typer.echo("\n# Disk Usage (by rig)")
    for e in d["rigs"]:
        if e["missing"]:
            typer.echo(f"  {e['prefix']:<12}  (missing)")
            continue
        typer.echo(f"  {e['prefix']:<12}  {safety.format_bytes(e['disk_bytes'])}")
    if d["rigs"]:
        typer.echo(f"  {'total':<12}  {safety.format_bytes(d['total_bytes'])}")


# ---- warnings section -------------------------------------------------------


def _data_warnings(cfg, root: Path, rigs, gw_on, git_repos, nonrepo, unknown_top, untracked):
    """Warnings section: config drift, prefix collisions, untracked/unrecognized folders,
    and per-rig checkout/beads/grant issues. Excluded orgs are out of scope — skipped."""
    cfg_orgs = cfg.get("orgs", {}) or {}
    gw_orgs = gitworkspace.orgs(cfg) if gw_on else set()
    excluded_orgs = set((cfg.get("exclude", {}) or {}).get("orgs", []) or [])

    def _not_excluded(key):
        return not registry.is_excluded(cfg, *key.split("/"))

    warns = []
    for o in sorted(gw_orgs - set(cfg_orgs) - excluded_orgs):
        warns.append(
            f"org '{o}' from git-workspace not in config.yaml "
            f"(using auto code '{registry.sanitize(o)[:2]}', policy personal)"
        )
    warns += [f"required-org prefix: {v}" for v in registry.required_violations(cfg)]
    by_prefix = {}
    for e in rigs:
        by_prefix.setdefault(str(e["prefix"]), []).append(f"{e['org']}/{e['repo']}")
    warns += [
        f"prefix collision '{pref}': {', '.join(rs)}"
        for pref, rs in by_prefix.items()
        if len(rs) > 1
    ]
    warns += [
        f"git repo not tracked by git-workspace: {k}" for k in sorted(untracked) if _not_excluded(k)
    ]
    warns += [f"folder with no git repo: {k}" for k in sorted(nonrepo) if _not_excluded(k)]
    warns += [
        f"unrecognized top-level folder (not a known provider): {d}" for d in sorted(unknown_top)
    ]
    for e in rigs:
        path = root / e["provider"] / e["org"] / e["repo"]
        if not path.exists():
            warns.append(f"rig '{e['prefix']}' has no local checkout at {path}")
        elif not (path / ".beads").is_dir():
            warns.append(f"rig '{e['prefix']}' has no .beads/ (not initialized)")
        elif (
            not config.worktrees_ephemeral(cfg)
            and rig.grant_is_current(cfg, path, e["provider"], e["org"], e["repo"]) is False
        ):
            warns.append(
                f"rig '{e['prefix']}' sandbox grant is stale (worktrees root moved) "
                f"— re-run: ws rig init --claude"
            )
    return warns


def _render_warnings(warns: list[str]) -> None:
    typer.echo(f"\n# Warnings ({len(warns)})")
    for w in warns:
        typer.echo(f"  ⚠ {w}")
    if not warns:
        typer.echo("  ✓ none")


# ---- collect + payload + render ---------------------------------------------


def _collect(cfg) -> dict:
    """Gather the full diagnostics dict, section by section, from the shared inputs.

    Reuses ``metadata.read_fleet`` / ``registry.*`` / ``gitworkspace.*`` and runs the metadata
    rollup ONCE (Disk Usage + Fleet Health share it, so no repo is disk-walked twice). Pure data:
    makes no ``typer.echo`` calls, returns a JSON-able dict keyed by section.
    """
    root = Path(workspace_root())
    gw_on = gitworkspace.enabled(cfg)
    rigs = cfg.get("managed_repos", []) or []

    # ---- inventory intermediates (also feed disk usage, fleet health, warnings) ----
    rig_keys = {f"{e['provider']}/{e['org']}/{e['repo']}" for e in rigs}
    git_repos, nonrepo, unknown_top = _scan(root, registry.effective_providers(cfg))
    tracked = _tracked(root)
    universe = tracked if tracked is not None else git_repos
    excluded = {k for k in git_repos if registry.is_excluded(cfg, *k.split("/"))}
    candidates = {
        k for k in universe if k not in rig_keys and not registry.is_excluded(cfg, *k.split("/"))
    }
    untracked = (git_repos - tracked) if tracked is not None else set()

    inventory = {
        "rigs_registered": len(rig_keys),
        "git_repos_on_disk": len(git_repos),
        "onboarding_candidates": len(candidates),
        "excluded": len(excluded),
        "untracked_git_repos": (len(untracked) if tracked is not None else None),
        "non_repo_folders": len(nonrepo),
        "unrecognized_top_dirs": len(unknown_top),
    }

    # ---- single metadata rollup (Disk Usage + Fleet Health share it) ----
    rig_keys_on_disk = {
        f"{e['provider']}/{e['org']}/{e['repo']}"
        for e in rigs
        if (root / e["provider"] / e["org"] / e["repo"]).exists()
    }
    records = metadata.read_fleet(
        cfg, sorted(git_repos | rig_keys_on_disk), ttl=metadata.ttl(cfg)
    )

    return {
        "config": _data_config(cfg, root, gw_on),
        "providers": _data_providers(cfg),
        "orgs": _data_orgs(cfg),
        "rigs": _data_rigs(cfg),
        "inventory": inventory,
        "disk_usage": _data_disk_usage(rigs, root, records),
        "fleet_health": _data_fleet_health(records, git_repos),
        "worktrees": _data_worktrees(cfg),
        "molecules": _data_molecules(cfg),
        "mcp": _data_mcp(cfg),
        "observability": _data_observability(cfg),
        "warnings": _data_warnings(
            cfg, root, rigs, gw_on, git_repos, nonrepo, unknown_top, untracked
        ),
    }


def doctor_payload() -> dict:
    """Structured `ws doctor` diagnostics — the data layer beneath the text render.

    Returns a JSON-able dict keyed by section (``config``, ``providers``, ``orgs``, ``rigs``,
    ``inventory``, ``disk_usage``, ``fleet_health``, ``worktrees``, ``molecules``, ``mcp``,
    ``observability``, ``warnings``). Exposed as the ``ws://doctor`` MCP resource; ``doctor()``
    renders the same builders so the text output never drifts from this payload.
    """
    return _collect(config.load())


def show():
    """Pretty-print the resolved config: the doctor overview + config-only sections."""
    cfg = config.load()
    root = Path(workspace_root())
    gw_on = gitworkspace.enabled(cfg)
    _overview(cfg, root, gw_on)
    _section_dimensions(cfg)
    _section_exclude(cfg)
    _section_dolt(cfg)
    _section_worktrees(cfg)


def doctor():
    """Render the full `ws doctor` report from the structured `_collect` payload."""
    data = _collect(config.load())
    _render_config(data["config"])
    _render_providers(data["providers"])
    _render_orgs(data["orgs"])
    _render_rigs(data["rigs"])
    _render_inventory(data["inventory"])
    _render_disk_usage(data["disk_usage"])
    _render_fleet_health(data["fleet_health"])
    _render_worktrees(data["worktrees"])
    _render_molecules(data["molecules"])
    _render_mcp(data["mcp"])
    _render_observability(data["observability"])
    _render_warnings(data["warnings"])
