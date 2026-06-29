"""`ws doctor` — status + diagnostics.

Shows providers / orgs / rigs / repo counts (config + git-workspace), then warns
about config drift and untracked or unrecognized folders under the workspace root.
Informational: always exits 0.
"""

from __future__ import annotations

from pathlib import Path

import typer

from . import config, gitworkspace, registry, rig, worktree
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


# ---- shared render sections (used by `doctor` and `config show`) ------------


def _section_config(cfg, root, gw_on):
    typer.echo("# Config")
    typer.echo(f"  config: {config.config_path()}")
    typer.echo(f"  workspace root: {root}")
    if gw_on:
        paths = gitworkspace.config_paths(cfg)
        src = ", ".join(str(p) for p in paths) or "NO workspace*.toml found"
        typer.echo(f"  git-workspace: enabled ({src})")
    else:
        typer.echo("  git-workspace: disabled")


def _section_providers(cfg):
    cfg_provs = set(cfg.get("providers", []) or [])
    gw_provs = gitworkspace.providers(cfg) if gitworkspace.enabled(cfg) else set()
    typer.echo("\n# Providers")
    for p in registry.effective_providers(cfg):
        src = (
            "both"
            if p in cfg_provs and p in gw_provs
            else "config"
            if p in cfg_provs
            else "git-workspace"
        )
        typer.echo(f"  provider:{p}  ({src})")


def _section_orgs(cfg):
    cfg_orgs = cfg.get("orgs", {}) or {}
    gw_orgs = gitworkspace.orgs(cfg) if gitworkspace.enabled(cfg) else set()
    excluded_orgs = set((cfg.get("exclude", {}) or {}).get("orgs", []) or [])
    typer.echo("\n# Orgs")
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
        excl = " [excluded]" if o in excluded_orgs else ""
        typer.echo(
            f"  org:{o}  code={code_str}  policy={registry.org_policy(cfg, o)}  ({src}){excl}"
        )


def _section_rigs(cfg):
    rigs = cfg.get("managed_repos", []) or []
    typer.echo(f"\n# Rigs ({len(rigs)})")
    for e in rigs:
        typer.echo(f"  {e['prefix']}\t{e['provider']}/{e['org']}/{e['repo']} ({e['kind']})")


def _overview(cfg, root, gw_on):
    """The Config/Providers/Orgs/Rigs header — the part doctor and `config show` share."""
    _section_config(cfg, root, gw_on)
    _section_providers(cfg)
    _section_orgs(cfg)
    _section_rigs(cfg)


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


def _section_worktrees(cfg):
    w = config.worktrees_cfg(cfg)
    # Show the EFFECTIVE branches (templates are suffixes; wt/ is always prepended).
    bead = worktree.apply_prefix(w.get("bead_branch", "bead/{id}"))
    session = worktree.apply_prefix(w.get("session_branch", "session/{ts}-{rand}"))
    n_init = len(w.get("init", []) or [])
    ephemeral = config.worktrees_ephemeral(cfg)
    typer.echo("\n# Worktrees")
    typer.echo(f"  ephemeral: {str(ephemeral).lower()}")
    note = "  (OS temp, session-scoped)" if ephemeral else "  (persistent; sandbox grants on)"
    typer.echo(f"  root: {config.worktrees_root(cfg)}{note}")
    typer.echo("  branch prefix: wt/  (all managed worktree branches)")
    typer.echo(f"  bead:    {bead}")
    typer.echo("  branch:  wt/<name>  (--branch is prefixed, not a full override)")
    typer.echo(f"  session: {session}")
    typer.echo(f"  rmdir_empty: {str(w.get('rmdir_empty', True)).lower()}")
    typer.echo(f"  init rules: {n_init} global")


def _orphan_mol_branches(cfg):
    """mol/<epic> branches whose epic is closed — i.e. a molecule landed but its branch wasn't
    deleted. `ws work merge --molecule` deletes the branch best-effort (warns, never fails), so a
    rare delete failure leaves a stale ref. Returns [(rig_prefix, branch), …]. A branch whose epic
    is still open is an active molecule, not an orphan, so it's skipped."""
    from .work import _show  # local: avoids a load-time cycle, reuses work's bd seam

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
                f"refs/heads/{worktree.MOL_PREFIX}",
            ],
            check=False,
            capture=True,
        )
        if res.returncode != 0:
            continue
        for branch in (res.stdout or "").split():
            epic = branch[len(worktree.MOL_PREFIX) :]
            bead = _show(epic, main)
            if bead and bead.get("status") == "closed":
                orphans.append((str(e["prefix"]), branch))
    return orphans


def _section_mcp():
    """Report whether the optional `[mcp]` extra (fastmcp) is installed."""
    try:
        import fastmcp  # noqa: F401

        available = True
    except ImportError:
        available = False

    typer.echo("\n# MCP")
    if available:
        typer.echo("  fastmcp: available")
    else:
        typer.echo("  fastmcp: unavailable")
        typer.echo("  install: uv tool install 'ws[mcp]'  (or: pip install 'ws[mcp]')")


def _section_molecules(cfg):
    orphans = _orphan_mol_branches(cfg)
    typer.echo(f"\n# Molecule branches ({len(orphans)} orphaned)")
    if not orphans:
        typer.echo("  ✓ none")
        return
    for prefix, branch in orphans:
        typer.echo(f"  ⚠ {prefix}\t{branch} (epic closed — delete manually)")


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
    cfg = config.load()
    root = Path(workspace_root())
    gw_on = gitworkspace.enabled(cfg)
    cfg_orgs = cfg.get("orgs", {}) or {}
    gw_orgs = gitworkspace.orgs(cfg) if gw_on else set()
    excluded_orgs = set((cfg.get("exclude", {}) or {}).get("orgs", []) or [])
    rigs = cfg.get("managed_repos", []) or []

    _overview(cfg, root, gw_on)

    # ---- inventory ----
    rig_keys = {f"{e['provider']}/{e['org']}/{e['repo']}" for e in rigs}
    git_repos, nonrepo, unknown_top = _scan(root, registry.effective_providers(cfg))
    tracked = _tracked(root)
    universe = tracked if tracked is not None else git_repos
    excluded = {k for k in git_repos if registry.is_excluded(cfg, *k.split("/"))}
    candidates = {
        k for k in universe if k not in rig_keys and not registry.is_excluded(cfg, *k.split("/"))
    }
    untracked = (git_repos - tracked) if tracked is not None else set()

    typer.echo("\n# Inventory (under recognized provider dirs)")
    typer.echo(f"  rigs registered:        {len(rig_keys)}")
    typer.echo(f"  git repos on disk:      {len(git_repos)}")
    typer.echo(f"  onboarding candidates:  {len(candidates)}")
    typer.echo(f"  excluded:               {len(excluded)}")
    if tracked is not None:
        typer.echo(f"  untracked git repos:    {len(untracked)}")
    typer.echo(f"  non-repo folders:       {len(nonrepo)}")
    typer.echo(f"  unrecognized top dirs:  {len(unknown_top)}")

    _section_worktrees(cfg)
    _section_molecules(cfg)
    _section_mcp()

    # ---- warnings (excluded orgs are out of scope — skipped) ----
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

    typer.echo(f"\n# Warnings ({len(warns)})")
    for w in warns:
        typer.echo(f"  ⚠ {w}")
    if not warns:
        typer.echo("  ✓ none")
