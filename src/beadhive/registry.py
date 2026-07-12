"""Registry operations over config.yaml: classify, derive prefixes, register rigs,
reconcile against git-workspace, report usage, and (re)generate the labels doc.

Ports scripts/labels.sh (classify/prefix/register/repos-sync/report/allowed/docs).
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import typer
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.scalarstring import DoubleQuotedScalarString as DQ

from . import config, gitworkspace
from .identity import workspace_root
from .run import run

PREFIX_SOFT_MAX = 8  # beads' recommended cap (not enforced; doctor hard limit is 20)

# ---- Factory HQ (kind=hq singleton) -----------------------------------------
# HQ is the one durable central store: the aggregation primary that ALSO holds canonical
# hq-prefixed control-plane beads. It is LOCAL infra (like the hub/cache) registered ONLY in
# the ws registry under a RESERVED SYNTHETIC IDENTITY — never a git-workspace provider, so its
# triplet is not a real remote and its on-disk home is config.hq_dir() (see rig_dir's special
# case), NOT the $GIT_WORKSPACE path-derivation.
HQ_KIND = "hq"
HQ_PREFIX = "hq"
HQ_PROVIDER, HQ_ORG, HQ_REPO = "local", "factory", "hq"
HQ_TRIPLET = (HQ_PROVIDER, HQ_ORG, HQ_REPO)


# ---- registry helpers -------------------------------------------------------


def org_code(cfg, org) -> str:
    return (cfg.get("orgs", {}).get(org, {}) or {}).get("code", "") or ""


def org_policy(cfg, org) -> str:
    return (cfg.get("orgs", {}).get(org, {}) or {}).get("policy", "personal")


# ---- rig resolution (for -a / -r routing) ----------------------------------


def rig_dir(entry) -> Path:
    # kind=hq is the Factory HQ store — LOCAL infra that lives at config.hq_dir(), NOT under
    # $GIT_WORKSPACE. Special-case it before the triplet path-derivation (its local/factory/hq
    # identity is synthetic, so the derived $GIT_WORKSPACE path would not exist).
    if str(entry.get("kind", "")) == HQ_KIND:
        return config.hq_dir()
    return Path(workspace_root()) / str(entry["provider"]) / str(entry["org"]) / str(entry["repo"])


def rig_dir_for(cfg, rig: str) -> Path:
    """The rig directory bd should target for a rig-scoped (bead-less) read: the resolved managed
    rig for `--rig`, else the current directory. The read verbs need to point `bd` at a rig without
    a bead to locate one from."""
    if rig:
        return rig_dir(resolve_rig(cfg, rig))
    return Path.cwd()


def rig_of_kind(cfg, kind):
    """The single managed_repos entry whose ``kind`` matches, or None. The resolver for
    singleton kinds (e.g. kind=hq — the Factory HQ store): locate + guard the one instance.
    Returns the first match if (invalidly) more than one is registered."""
    return next((e for e in cfg.get("managed_repos", []) if str(e.get("kind", "")) == kind), None)


def all_rig_targets(cfg):
    return [(str(e["prefix"]), rig_dir(e)) for e in cfg.get("managed_repos", [])]


def resolve_rig(cfg, rig_id):
    """Find the managed_repos entry for rig_id per `rig_match` (flexible|prefix|triplet)."""
    rigs = cfg.get("managed_repos", []) or []
    mode = str((cfg.get("git_workspace") or {}).get("rig_match", "flexible"))

    def by_prefix():
        return [e for e in rigs if str(e["prefix"]) == rig_id]

    def by_triplet():
        return [e for e in rigs if f"{e['provider']}/{e['org']}/{e['repo']}" == rig_id]

    def by_orgrepo():
        return [e for e in rigs if f"{e['org']}/{e['repo']}" == rig_id]

    def by_repo():
        return [e for e in rigs if str(e["repo"]) == rig_id]

    if mode == "prefix":
        matches = by_prefix()
    elif mode == "triplet":
        matches = by_triplet()
    else:  # flexible: prefix → triplet → org/repo → bare repo (if unique)
        matches = by_prefix() or by_triplet() or by_orgrepo() or by_repo()

    if not matches:
        typer.echo(f"✗ no rig matching '{rig_id}' (rig_match={mode})", err=True)
        raise typer.Exit(1)
    if len(matches) > 1:
        cands = ", ".join(f"{e['org']}/{e['repo']}" for e in matches)
        typer.echo(f"✗ '{rig_id}' is ambiguous: {cands} — qualify with org/repo", err=True)
        raise typer.Exit(1)
    return matches[0]


def effective_providers(cfg):
    """Provider labels from config, unioned with git-workspace's when enabled."""
    from . import gitworkspace

    provs = set(cfg.get("providers", []) or [])
    if gitworkspace.enabled(cfg):
        provs |= gitworkspace.providers(cfg)
    return sorted(provs)


def closed_dimensions(cfg):
    """{dimension: {allowed values}} for every closed dimension the validator enforces.

    Seeded with ws's built-in intake/outbound state vocabulary (``state.STATE_DIMENSIONS``,
    owned in code so it's uniform fleet-wide), then unioned with every per-rig dimension that
    declares `values:` (a closed set). Dimensions without `values:` are open and accept
    anything; config may extend a built-in dimension's value set but never removes a built-in
    value."""
    from .state import STATE_DIMENSIONS  # code-owned intake/outbound state vocabulary

    out = {dim: set(vals) for dim, vals in STATE_DIMENSIONS.items()}
    for dim, spec in (cfg.get("dimensions", {}) or {}).items():
        vals = (spec or {}).get("values")
        if vals is not None:
            out.setdefault(dim, set()).update(str(v) for v in vals)
    return out


def rig_key(entry) -> str:
    """`provider/org/repo` triplet — the ws.metadata cache key for a managed-rig entry."""
    return f"{entry['provider']}/{entry['org']}/{entry['repo']}"


def _key(e) -> str:
    return rig_key(e)


def sanitize(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[^a-z0-9-]", "-", s)
    s = re.sub(r"-+", "-", s)
    return s.strip("-")


def is_excluded(cfg, provider, org, repo) -> bool:
    ex = cfg.get("exclude", {}) or {}
    if org in (ex.get("orgs", []) or []):
        return True
    return f"{provider}/{org}/{repo}" in (ex.get("repos", []) or [])


def prefix_taken(cfg, prefix, skip="") -> bool:
    return any(str(e["prefix"]) == prefix and _key(e) != skip for e in cfg.get("managed_repos", []))


def find_entry(cfg, provider, org, repo):
    """The managed_repos entry already registered for this rig, or None if unregistered.
    The 'already-configured' signal `rig init` reads to stay non-destructive on re-init."""
    key = f"{provider}/{org}/{repo}"
    return next((e for e in cfg.get("managed_repos", []) if _key(e) == key), None)


def prefix_collisions(cfg):
    """Prefixes claimed by more than one rig → ``[{prefix, rigs:[org/repo, …]}]``.

    The structured form of `repos_sync`'s 'Prefix collisions' section, shared by it and
    the `rigs_status` MCP tool so the two never drift."""
    by_prefix: dict[str, list[str]] = {}
    for e in cfg.get("managed_repos", []):
        by_prefix.setdefault(str(e["prefix"]), []).append(f"{e['org']}/{e['repo']}")
    return [{"prefix": pref, "rigs": rigs} for pref, rigs in by_prefix.items() if len(rigs) > 1]


def required_violations(cfg):
    """Required-org repos whose prefix doesn't start with '<code>-'."""
    out = []
    for e in cfg.get("managed_repos", []):
        org = str(e["org"])
        if org_policy(cfg, org) == "required":
            code = org_code(cfg, org)
            if not str(e["prefix"]).startswith(f"{code}-"):
                out.append(f"{org}/{e['repo']}: {e['prefix']} != {code}-*")
    return out


# ---- classify ---------------------------------------------------------------


def classify(provider, org, repo, cfg=None) -> str:
    """excluded | org-native | 'fork upstream=<o>/<r>' | personal-or-prototype."""
    cfg = cfg if cfg is not None else config.load()
    if (provider, org, repo) == HQ_TRIPLET:  # reserved synthetic identity → the HQ singleton
        return HQ_KIND
    if is_excluded(cfg, provider, org, repo):
        return "excluded"
    if org_policy(cfg, org) == "required":
        return "org-native"
    # Offline fork signal first: git-workspace records a fork's parent as [[repo]].upstream in the
    # lockfile — no gh/network needed, and it works when the provider LABEL (path segment) differs
    # from the resolved host (bh-rax6).
    host = provider
    if gitworkspace.enabled(cfg):
        up = gitworkspace.upstreams(cfg).get(f"{provider}/{org}/{repo}")
        if up:
            return f"fork upstream={up}"
        host = gitworkspace.provider_host(cfg, provider) or provider
    # Resolve the real host, not the path segment, before the github probe.
    if host == "github" and shutil.which("gh"):
        res = run(
            ["gh", "repo", "view", f"{org}/{repo}", "--json", "isFork,parent"],
            check=False,
            capture=True,
        )
        if res.returncode == 0:
            info = json.loads(res.stdout or "{}")
            if info.get("isFork"):
                p = info.get("parent") or {}
                owner = (p.get("owner") or {}).get("login", "")
                return f"fork upstream={owner}/{p.get('name', '')}"
    return "personal-or-prototype"


_PUSH_PERMISSIONS = ("ADMIN", "WRITE", "MAINTAIN")


def has_push_access(provider, org, repo) -> bool:
    """True only when we can CONFIRM push access to `<org>/<repo>` — fail-closed everywhere else.

    Probes GitHub's `viewerPermission` via `gh`; write access is ADMIN/WRITE/MAINTAIN. Returns
    False when gh is absent from PATH, the provider isn't github, the probe errors, or the
    permission is read-only (READ/TRIAGE/NONE). Beads must live on a repo we own or nowhere
    (bh-dhl6), so an unconfirmable answer is treated as no-access."""
    if provider != "github" or not shutil.which("gh"):
        return False
    res = run(
        ["gh", "repo", "view", f"{org}/{repo}", "--json", "viewerPermission"],
        check=False,
        capture=True,
    )
    if res.returncode != 0:
        return False
    try:
        perm = str((json.loads(res.stdout or "{}") or {}).get("viewerPermission", "")).upper()
    except json.JSONDecodeError:
        return False
    return perm in _PUSH_PERMISSIONS


# ---- prefix -----------------------------------------------------------------


def derive_prefix(provider, org, repo, kind="", cfg=None):
    """Return (prefix, warnings). Mirrors labels.sh cmd_prefix."""
    cfg = cfg if cfg is not None else config.load()
    code = org_code(cfg, org) or sanitize(org)[:2]
    rs = sanitize(repo)
    if kind == HQ_KIND:
        pref = HQ_PREFIX  # reserved singleton prefix — never derived from the synthetic repo name
    elif kind in ("org-native", "personal"):
        pref = f"{code}-{rs}"
    elif kind == "prototype":
        pref = rs
    elif kind == "fork":
        pref = f"fork-{rs}"
    else:  # no kind: bare if unique, else code-repo
        pref = f"{code}-{rs}" if prefix_taken(cfg, rs) else rs

    warnings = []
    if len(pref) > PREFIX_SOFT_MAX:
        warnings.append(
            f"note: '{pref}' is {len(pref)} chars (>{PREFIX_SOFT_MAX} recommended) "
            f"— consider an override"
        )
    if prefix_taken(cfg, pref, f"{provider}/{org}/{repo}"):
        warnings.append(f"warn: prefix '{pref}' already used by another rig — override needed")
    return pref, warnings


# ---- register ---------------------------------------------------------------


def _entry(provider, org, repo, prefix, kind, upstream=""):
    """A flow-style mapping with double-quoted scalars (matches the existing layout)."""
    m = CommentedMap()
    m[DQ("provider")] = DQ(provider)
    m[DQ("org")] = DQ(org)
    m[DQ("repo")] = DQ(repo)
    m[DQ("prefix")] = DQ(prefix)
    m[DQ("kind")] = DQ(kind)
    if upstream:
        m[DQ("upstream")] = DQ(upstream)
    m.fa.set_flow_style()
    return m


def register(provider, org, repo, prefix, kind, upstream=""):
    from . import guard  # lazy: guard imports registry (avoid an import cycle)
    from .identity import resolve_actor

    # Fleet/managed_repos membership is the director's partition (§2.1); controller is read-only.
    guard.guard_hq_registry_write(guard.HQ_FLEET, resolve_actor())
    cfg = config.load()
    key = f"{provider}/{org}/{repo}"
    kept = [e for e in cfg.get("managed_repos", []) if _key(e) != key]
    kept.append(_entry(provider, org, repo, prefix, kind, upstream))
    kept.sort(key=lambda e: (str(e["org"]), str(e["repo"])))
    cfg["managed_repos"] = CommentedSeq(kept)
    config.save(cfg)
    from . import metadata  # lazy: metadata imports registry (avoid an import cycle)
    metadata.invalidate(cfg, key)  # rig add/init/onboard — warm the new repo in the background
    typer.echo(f"✓ registered {org}/{repo} as prefix '{prefix}' (kind={kind})")


def unregister(provider, org, repo):
    """Drop this rig's managed_repos entry and persist. Registry-scoped (the inverse of
    register): does NOT touch .beads/labels/the repo — purely config. cwd-free."""
    from . import guard  # lazy: guard imports registry (avoid an import cycle)
    from .identity import resolve_actor

    # Fleet/managed_repos membership is the director's partition (§2.1); controller is read-only.
    guard.guard_hq_registry_write(guard.HQ_FLEET, resolve_actor())
    cfg = config.load()
    key = f"{provider}/{org}/{repo}"
    kept = [e for e in cfg.get("managed_repos", []) if _key(e) != key]
    cfg["managed_repos"] = CommentedSeq(kept)
    config.save(cfg)
    from . import metadata  # lazy: metadata imports registry (avoid an import cycle)
    metadata.invalidate(cfg, key, reload=False)  # rig rm/retire — repo is gone; just drop the entry
    typer.echo(f"✓ unregistered {org}/{repo}")


# ---- repos-sync -------------------------------------------------------------


def repos_sync():
    cfg = config.load()
    have = {_key(e) for e in cfg.get("managed_repos", [])}
    ex = cfg.get("exclude", {}) or {}
    exo = set(ex.get("orgs", []) or [])
    exr = set(ex.get("repos", []) or [])

    typer.echo("# Candidates (in git-workspace, not registered, not excluded) — run 'ws rig init'")
    res = run(["git", "workspace", "list"], check=False, capture=True)
    if res.returncode != 0:
        typer.echo("git-workspace not available — skipping candidate scan.", err=True)
    else:
        for line in res.stdout.splitlines():
            parts = line.strip().split("/")
            if len(parts) < 3:
                continue
            provider, org, repo = parts[0], parts[1], parts[2]
            k = f"{provider}/{org}/{repo}"
            if k in have or org in exo or k in exr:
                continue
            typer.echo(f"  {k}")

    typer.echo("# Prefix collisions")
    for col in prefix_collisions(cfg):
        typer.echo(f"  {col['prefix']}: {', '.join(col['rigs'])}")

    typer.echo("# Required-org prefix violations")
    for v in required_violations(cfg):
        typer.echo(f"    {v}")

    from . import metadata  # lazy: metadata imports registry (avoid an import cycle)
    metadata.invalidate(cfg)  # labels sync reconciles the fleet — coarse; next read recomputes


# ---- report -----------------------------------------------------------------


def report():
    cfg = config.load()
    res = run(["bd", "label", "list-all", "--json"], check=False, capture=True)
    labels = json.loads(res.stdout or "[]") if res.returncode == 0 else []
    typer.echo("# Usage by dimension")
    # identity triplet + whatever dimensions the config declares
    for dim in ["provider", "org", "repo", *cfg.get("dimensions", {}).keys()]:
        typer.echo(f"## {dim}:")
        rows = [(x["count"], x["label"]) for x in labels if x["label"].startswith(dim + ":")]
        for count, label in sorted(rows, key=lambda x: -x[0]):
            typer.echo(f"  {count}\t{label}")


# ---- allowed ----------------------------------------------------------------


def allowed():
    cfg = config.load()
    vals = set()
    vals.update(f"provider:{p}" for p in effective_providers(cfg))
    vals.update(f"org:{o}" for o in cfg.get("orgs", {}))
    vals.update(f"repo:{e['repo']}" for e in cfg.get("managed_repos", []))
    for dim, allowed_vals in closed_dimensions(cfg).items():
        vals.update(f"{dim}:{v}" for v in allowed_vals)
    for v in sorted(vals):
        typer.echo(v)


# ---- docs -------------------------------------------------------------------


def docs():
    cfg = config.load()
    out = []
    out.append("# Registry & label taxonomy")
    out.append("")
    out.append(
        f"> Generated from `config.yaml` by `{config.BINARY_ALIAS} labels docs` — "
        "do not edit by hand."
    )
    out.append("")
    out.append(
        "Identity = labels `provider:`/`org:`/`repo:` (full names). "
        "Prefix = short stable handle (provider not included)."
    )
    out.append("")
    out.append("## Providers")
    out.append("")
    for name in effective_providers(cfg):
        out.append(f"- `provider:{name}`")
    out.append("")
    out.append("## Orgs")
    out.append("")
    for k, v in cfg.get("orgs", {}).items():
        out.append(f"- `org:{k}` — code `{v['code']}`, policy **{v['policy']}**")
    out.append("")
    out.append("## Excluded (beads ignores)")
    out.append("")
    for o in cfg.get("exclude", {}).get("orgs", []) or []:
        out.append(f"- org `{o}`")
    out.append("")
    out.append("## Non-identity dimensions")
    out.append("")
    out.append("| Dimension | Values | Description |")
    out.append("|---|---|---|")
    for k, v in cfg.get("dimensions", {}).items():
        vals = v.get("values")
        if vals is None:
            valstr = "_(open)_"
        elif not vals:
            valstr = "_(closed; no values yet)_"
        else:
            valstr = ", ".join(str(x) for x in vals)
        out.append(f"| `{k}:` | {valstr} | {v.get('description', '')} |")
    out.append("")
    rigs = cfg.get("managed_repos", [])
    out.append(f"## Managed rigs ({len(rigs)})")
    out.append("")
    for e in rigs:
        extra = str(e["kind"])
        if e.get("upstream"):
            extra += f", fork of {e['upstream']}"
        out.append(f"- `{e['prefix']}` — {e['provider']}/{e['org']}/{e['repo']} ({extra})")

    config.docs_path().parent.mkdir(parents=True, exist_ok=True)
    config.docs_path().write_text("\n".join(out) + "\n")
    typer.echo(f"wrote {config.docs_path()}")
