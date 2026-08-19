"""`ws doctor` — status + diagnostics.

Shows providers / orgs / hives / repo counts (config + git-workspace), then warns
about config drift and untracked or unrecognized folders under the workspace root.
Informational: always exits 0.

Data-collection is separated from rendering section by section: each section has a
pure ``_data_*`` builder that returns a structured (JSON-able) fragment and a
``_render_*`` that echoes it verbatim. ``doctor_payload()`` assembles the whole
structured dict (exposed as the ``beadhive://doctor`` MCP resource); ``doctor()`` renders
the SAME builders, so the human text output is unchanged.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import re
import time
from pathlib import Path

import typer

from . import (
    bd,
    channels,
    config,
    dolt_health,
    fleet,
    gitauth,
    gitworkspace,
    guard,
    hitch_plugin,
    hive,
    hive_repair,
    hive_schema,
    host_fence,
    install_plane,
    jsonout,
    metadata,
    otel,
    registry,
    safety,
    store_locator,
    validate_probe,
    worktree,
)
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


def _data_config(cfg, root) -> dict:
    """Config section: config path, workspace root, git-workspace sources.

    `git_workspace.enabled` was a manual on/off flag; bh-hsus.4 deleted it (git-workspace is
    now a required dep, always active), so `"enabled"` here means "at least one
    `workspace*.toml` source resolved" rather than a config toggle — the JSON shape is
    unchanged, only what the field measures is."""
    sources = [str(p) for p in gitworkspace.config_paths(cfg)]
    return {
        "config_path": str(config.config_path()),
        "workspace_root": str(root),
        "git_workspace": {"enabled": bool(sources), "sources": sources},
    }


def _render_config(d: dict) -> None:
    typer.echo("# Config")
    typer.echo(f"  config: {d['config_path']}")
    typer.echo(f"  workspace root: {d['workspace_root']}")
    if d["git_workspace"]["enabled"]:
        src = ", ".join(d["git_workspace"]["sources"])
        typer.echo(f"  git-workspace: {src}")
    else:
        typer.echo("  git-workspace: NO workspace*.toml found")


def _data_providers(cfg) -> list[dict]:
    """Providers section: effective providers with their source (config / git-workspace / both)."""
    cfg_provs = set(cfg.get("providers", []) or [])
    gw_provs = gitworkspace.providers(cfg)
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
    gw_orgs = gitworkspace.orgs(cfg)
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


def _data_hives(cfg) -> list[dict]:
    """Hives section: the registered hives as prefix + provider/org/repo + kind."""
    hives = cfg.get("managed_repos", []) or []
    return [
        {
            "prefix": e["prefix"],
            "provider": e["provider"],
            "org": e["org"],
            "repo": e["repo"],
            "kind": e["kind"],
        }
        for e in hives
    ]


def _render_hives(items: list[dict]) -> None:
    typer.echo(f"\n# Hives ({len(items)})")
    for e in items:
        typer.echo(f"  {e['prefix']}\t{e['provider']}/{e['org']}/{e['repo']} ({e['kind']})")


def _overview(cfg, root):
    """The Config/Providers/Orgs/Hives header — the part doctor and `config show` share."""
    _render_config(_data_config(cfg, root))
    _render_providers(_data_providers(cfg))
    _render_orgs(_data_orgs(cfg))
    _render_hives(_data_hives(cfg))


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
    typer.echo(f"  backend: {_render_literal_value('dolt.backend', cfg)}")


def _render_literal_value(dotted: str, cfg) -> str:
    """*dotted*'s declared value, annotated ``(INVALID ...; effective: ...)`` when it falls
    outside its schema Literal's range (bh-aidze) — never rendered plainly as if it were in
    effect, the exact confusion that let `dolt.backend: shared-server` read as applied while
    doing nothing."""
    from . import config_schema

    found, value = config._descend(cfg, dotted.split("."))
    declared = value if found else "(unset)"
    if not found:
        return declared
    choices = config_schema.literal_choices(dotted)
    if choices is None or value in choices:
        return str(declared)
    effective = config_schema.field_default(dotted)
    allowed = "/".join(str(c) for c in choices)
    return f"{declared}  (INVALID — not one of {allowed}; effective: {effective!r})"


def _section_config_problems(cfg):
    """`# Config problems` (bh-aidze, `config show`-only): every persisted value outside its
    schema Literal's declared range, in one place — the generalization of `_section_dolt`'s
    inline annotation, covering every OTHER Literal-typed key a hand-edit or a pre-fix
    `bh config set` could have drifted the same way `dolt.backend` did. Silent (renders
    nothing, not even the header) when the config is fully clean, matching the
    silent-unless-drifted convention `_section_store_engine` already uses."""
    violations = config.literal_violations(cfg)
    if not violations:
        return
    typer.echo(f"\n# Config problems ({len(violations)})")
    for v in violations:
        allowed = "/".join(str(c) for c in v["choices"])
        typer.echo(
            f"  ⚠ {v['key']} = {v['value']!r}  (INVALID — not one of {allowed}; "
            f"effective: {v['default']!r})"
        )


def _section_provenance():
    """`# Provenance` (bh-e0y8.6, `config show`-only): label every fleet/host key with its
    origin layer, so a surprising value in the merged view is traceable back to the file that
    set it — the confusion layered config invites without this."""
    provenance = config.key_provenance()
    typer.echo(f"\n# Provenance ({len(provenance)})")
    if not provenance:
        typer.echo("  (no fleet.yaml or host config.yaml keys)")
        return
    width = max(len(k) for k in provenance)
    for key in sorted(provenance):
        typer.echo(f"  {key:<{width}}  {provenance[key]}")


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

# Bead-id charset observed across the live corpus (bh-xi0m1 review): lowercase
# letters/digits, hyphen-joined words (`bh-merge-slot`), dot-joined child suffixes
# (`bh-5sizy.3`). Epic ids come from git branch names — free text a hostile branch
# could set to something like `bh-x\` to break out of the quoted string literal this
# server treats backslash as an escape in (no NO_BACKSLASH_ESCAPES sql_mode). Anything
# outside this charset is dropped rather than quoted, so nothing unvetted reaches the
# query text — same rule as store_locator.sanitize_database_name / bulk_schema_versions.
_EPIC_ID_RE = re.compile(r"[a-z0-9]+(?:[-.][a-z0-9]+)*")


def _bulk_epic_closed(
    candidates: list[tuple[dict, Path, str]], prefix: str
) -> dict[tuple[str, str], bool]:
    """SHAPE A (bh-xi0m1) for stage 2: every server-mode hive's epic-closed check in ONE
    cross-database query, keyed by ``(str(hive_dir), branch)`` so the per-item fallback below
    can thread the answer straight in — the same ``probed=`` shape
    ``dolt_health.bulk_schema_versions`` -> ``hive_schema.refresh_with_detail`` uses.

    CLASSIFICATION (fleet.py's step 2, done here rather than assumed): `bh bd sql -q "DESCRIBE
    issues"` shows `status` as a plain, indexed `varchar(32)` column with `Extra: ""` — no
    generated/virtual expression — and `bd close` mutates it directly. Nothing server-side
    derives it, so this is the same value `bd show` already reads, asked once per hive instead
    of once per branch. `fleet.py` itself names `issues.status` as the canonical shape-A stored
    column.

    PARTIAL BY CONSTRUCTION, like `bulk_schema_versions`: embedded-mode hives have no database
    on the shared server to qualify (`bd sql` refuses them outright), so they're excluded here
    and the caller falls back to the per-hive `bd show` path for them — same for a hive whose
    recorded database name doesn't round-trip through `sanitize_database_name` (unrecorded or
    unsafe), or an epic id outside `_EPIC_ID_RE` (branch names are free text, so a hostile one
    falls back to shape B rather than being quoted into the query). A missing key means
    UNANSWERED, never "open"."""
    server_items = [
        (e, main, branch)
        for e, main, branch in candidates
        if not store_locator.is_embedded_mode(main)
    ]
    if not server_items:
        return {}
    by_db: dict[str, tuple[Path, list[tuple[Path, str, str]]]] = {}
    for _e, main, branch in server_items:
        db = store_locator.recorded_server_database(main)
        if not db or db != store_locator.sanitize_database_name(db):
            continue  # unrecorded / unsafe name — falls back to shape B for this branch
        epic_id = branch[len(prefix) :]
        if not _EPIC_ID_RE.fullmatch(epic_id):
            continue  # outside the vetted charset — falls back to shape B for this branch
        by_db.setdefault(db, (main, []))[1].append((main, branch, epic_id))
    if not by_db:
        return {}
    conn = next(iter(by_db.values()))[0]
    clauses = []
    for db, (_main, rows) in by_db.items():
        ids = sorted({eid for _m, _b, eid in rows})
        quoted = ",".join("'" + i.replace("'", "''") + "'" for i in ids)
        clauses.append(f"SELECT '{db}' AS db, id, status FROM {db}.issues WHERE id IN ({quoted})")
    rows = fleet.sql_rows(fleet.sql(conn, " UNION ALL ".join(clauses)))
    if not isinstance(rows, list):
        return {}
    status_by_db_id = {
        (str(r["db"]), str(r["id"])): r.get("status") == "closed"
        for r in rows
        if isinstance(r, dict) and "db" in r and "id" in r
    }
    return {
        (str(main), branch): status_by_db_id[(db, epic_id)]
        for db, (_main, rows) in by_db.items()
        for main, branch, epic_id in rows
        if (db, epic_id) in status_by_db_id
    }


def _orphan_container_branches(cfg):
    """Container branches `wt/bead/epic/<epic>` whose epic is closed — i.e. a molecule landed but
    its branch wasn't deleted. `ws work merge --molecule` / `finish` deletes the branch best-effort
    (warns, never fails), so a rare delete failure leaves a stale ref. Returns
    [(hive_prefix, branch), …]. A branch whose epic is still open is an active molecule, not an
    orphan, so it's skipped."""
    prefix = f"{worktree._BEAD_PREFIX}epic/"  # wt/bead/epic/

    def _branches(e) -> list[tuple[dict, Path, str]]:
        """One hive's container branches. Cheap (bh-7fen2 measured 20 for-each-ref at 0.12s
        total) — fanned out only so the expensive per-branch stage below gets a flat list.
        STAYS SHAPE B (bh-xi0m1): this is per-hive git, not a bead-store read at all, so no
        bulk query covers it — fleet.py's shape A is scoped to reads the shared Dolt server can
        answer, and a local `refs/heads/...` listing isn't one."""
        main = registry.hive_dir(e)
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
            return []
        return [(e, main, branch) for branch in (res.stdout or "").split()]

    entries = cfg.get("managed_repos", []) or []
    candidates = [item for group in fleet.fanout(_branches, entries) for item in group]

    # SHAPE A (bh-xi0m1): resolves every server-mode hive's epic-closed check in ONE query.
    bulk = _bulk_epic_closed(candidates, prefix)

    def _closed(item: tuple[dict, Path, str]) -> tuple[str, str] | None:
        e, main, branch = item
        answer = bulk.get((str(main), branch))
        if answer is None:
            # unanswered by the bulk pass (embedded-mode hive, or excluded above) — SHAPE B
            # fallback, same as before this bead.
            bead = bd.show(branch[len(prefix) :], main)
            answer = bool(bead and bead.get("status") == "closed")
        return (str(e["prefix"]), branch) if answer else None

    # Only items the bulk pass left unanswered still spawn a `bd show`; everything else is a
    # dict lookup inside `_closed`. SHAPE B (`fleet.fanout`) still runs over every candidate so
    # order is preserved (`orphans` stays in registry order) — bulk-answered items just return
    # instantly instead of forking a process.
    return [hit for hit in fleet.fanout(_closed, candidates) if hit is not None]


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


# ---- prefix mismatches section (bh-6h1m) ------------------------------------
# A hive's registry prefix (managed_repos[*].prefix) and its beads-DB issue prefix (`bd config
# get issue_prefix`) are tracked separately — nothing keeps them in sync, and the generic
# Warnings bucket buries this actionable case among unrelated noise. This is its OWN section
# with the exact `hive repair` remediation command, per hive, so it can't get lost.


def _data_prefix_mismatches(cfg) -> list[dict]:
    """Registered hives whose registry prefix disagrees with their beads-DB issue_prefix.

    Only checked for hives with a local checkout carrying `.beads/` — a missing checkout or an
    unreadable issue_prefix is silently skipped here (the generic Warnings section already flags
    a missing checkout; there is nothing to compare without one).

    SHAPE A (bh-a8sox), with a shape-B fallback — re-tested against bh-0gvs3's classification,
    which had applied "config resolution is layered" to config reads AS A CLASS and never
    checked it against this key: `bd config get issue_prefix` (cmd/bd/config.go) is not a
    YAML-only key and has no derivation layered on top — it falls straight through to
    `SELECT value FROM config WHERE key = 'issue_prefix'` against the sql-server-backed store
    (see `dolt_health.bulk_issue_prefixes`'s classification note for the full trace through
    bd's own source). `issue_prefix` is a stored row, the same shape as `schema_migrations`'s
    version, so ONE cross-database query replaces what was 14 `bd config get` spawns
    (~565ms each, 7.91s summed — this section's own measured share of that; see
    docs/design/read-path-source-measurement.md for the before/after).
    Embedded-mode hives have no database on the shared server to qualify and stay on the
    per-hive `bd config get` under shape B, same fallback shape as `bulk_schema_versions`."""
    root = Path(workspace_root())
    entries = [
        (e, root / e["provider"] / e["org"] / e["repo"]) for e in cfg.get("managed_repos", []) or []
    ]
    entries = [(e, path) for e, path in entries if (path / ".beads").is_dir()]
    if not entries:
        return []

    # RECORDED, never derived (bh-g5ujg's rule — see `bulk_schema_versions`'s caller in this
    # module for the same reasoning): `server_database` falls back to a guess for a keyless
    # hive, and a guessed name that names no database on the server fails the UNION for every
    # hive in it. Asking only for what is written down keeps one broken hive's blast radius to
    # that hive; it drops to the per-hive fallback below like any other unanswered hive.
    bulk = dolt_health.bulk_issue_prefixes(
        [
            (path, store_locator.recorded_server_database(path))
            for _e, path in entries
            if not store_locator.is_embedded_mode(path)
        ]
    )

    def _classify(e: dict, raw_db_prefix: str) -> dict | None:
        try:
            registry_prefix = hive_repair.normalize_prefix(str(e["prefix"]))
            db_prefix = hive_repair.normalize_prefix(raw_db_prefix)
        except hive_repair.RepairError:
            return None  # an unparseable prefix on either side isn't THIS check's problem to fix
        if registry_prefix == db_prefix:
            return None
        hive_id = f"{e['provider']}/{e['org']}/{e['repo']}"
        remediation = (
            f"{config.BINARY_ALIAS} hive repair --hive {hive_id} --prefix {registry_prefix} --yes"
        )
        return {
            "hive": hive_id,
            "registry_prefix": registry_prefix,
            "db_prefix": db_prefix,
            # Suggests reconciling the DB onto the registry's (deliberately configured)
            # value — still just a suggestion: --prefix can target either side, or a third.
            "remediation": remediation,
        }

    def _one_fallback(item: tuple[dict, Path]) -> dict | None:
        e, path = item
        db = bd.json(["config", "get", "issue_prefix"], path)
        if not isinstance(db, dict) or "value" not in db:
            return None
        return _classify(e, str(db["value"]))

    fallback = [(e, path) for e, path in entries if path not in bulk]
    # The remaining `bd config get` spawns (embedded-mode hives, plus any server-mode hive the
    # bulk query didn't answer for) are independent, read-only, and store-bound (bh-3qo60: 15
    # hives = 4.39s sequential) — shape B, same as before, just over a smaller `entries`.
    fallback_paths = [path for _e, path in fallback]
    fallback_results = dict(zip(fallback_paths, fleet.fanout(_one_fallback, fallback), strict=True))

    # Input order is preserved (`entries` order), so the reported list stays deterministic
    # regardless of which hives came from the bulk read vs. the fallback fan-out.
    results = [
        _classify(e, bulk[path]) if path in bulk else fallback_results[path] for e, path in entries
    ]
    return [m for m in results if m is not None]


def _render_prefix_mismatches(mismatches: list[dict]) -> None:
    typer.echo(f"\n# Prefix mismatches ({len(mismatches)})")
    if not mismatches:
        typer.echo("  ✓ none")
        return
    for m in mismatches:
        typer.echo(f"  ⚠ {m['hive']}: registry='{m['registry_prefix']}' db='{m['db_prefix']}'")
        typer.echo(f"    fix: {m['remediation']}")


def _section_prefix_mismatches(cfg):
    """Render the prefix-mismatches section."""
    _render_prefix_mismatches(_data_prefix_mismatches(cfg))


# ---- node_id section (bh-y85rj) ----------------------------------------------
# bd's own `bd reclaim --help`: a lease is only meaningful on the replica that granted it, and
# reclaim SKIPS a lease another replica granted (unless --any-replica) — but only when node_id
# is set. Unset, that cross-replica guard is simply off. node_id is per-HOST (this hive runs
# `dolt.shared-server = true`, and every client of ONE shared sql-server is ONE replica that
# must share a value), lives in `~/.config/bd/config.yaml` (never a hive-tracked file), and is
# only worth guarding once a hive's store can actually reach a second host — i.e. has a Dolt
# remote wired (`sync.remote`). A host with no remote-wired hive sees a quiet ✓ line, matching
# the Store Engine section's "no new noise for a fleet that never needed this" bar.


def _data_node_id(cfg) -> dict:
    """This host's resolved node_id (env override first, then the persisted config value —
    mirroring how bd itself resolves it) plus every registered hive whose store has a Dolt
    remote wired, i.e. is reachable from a second host and so actually needs the guard."""
    remote_hives: list[str] = []
    probe_cwd: Path | None = None
    for e in cfg.get("managed_repos", []) or []:
        path = registry.hive_dir(e)
        if not (path / ".beads").is_dir():
            continue
        probe_cwd = probe_cwd or path
        # `bd.sync_remote` reads `.beads/config.yaml` directly — the same answer `bd config get
        # sync.remote --json` gave, without the ~470 ms process start it charged per hive
        # (bh-i6e5g: 15 of those spawns were 7.0 s of a 46 s warm doctor).
        if bd.sync_remote(path):
            remote_hives.append(f"{e['provider']}/{e['org']}/{e['repo']}")

    env_value = os.environ.get("BEADS_NODE_ID", "").strip()
    cfg_value = ""
    if not env_value:
        data = bd.json(["config", "get", "node_id"], probe_cwd or Path(workspace_root()))
        if isinstance(data, dict):
            cfg_value = str(data.get("value") or "").strip()
    resolved = env_value or cfg_value

    remediation = ""
    if not resolved and remote_hives:
        remediation = f"{config.BINARY_ALIAS} hive repair --hive {remote_hives[0]} --node-id --yes"
    return {
        "resolved": resolved,
        "source": "BEADS_NODE_ID" if env_value else ("config" if cfg_value else ""),
        "remote_hives": remote_hives,
        "remediation": remediation,
    }


def _render_node_id(d: dict) -> None:
    typer.echo("\n# Node ID")
    if d["resolved"]:
        typer.echo(f"  ✓ node_id='{d['resolved']}' ({d['source']})")
        return
    if not d["remote_hives"]:
        typer.echo("  ✓ unset (no remote-wired hive on this host needs it yet)")
        return
    typer.echo("  ✗ node_id UNSET — the reclaim replica guard is OFF for these remote-wired hives:")
    for h in d["remote_hives"]:
        typer.echo(f"    - {h}")
    typer.echo(f"    fix: {d['remediation']}")


def _section_node_id(cfg):
    """Render the node_id section."""
    _render_node_id(_data_node_id(cfg))


# ---- beads.role section (bh-f3blt) -------------------------------------------
# bd routes on `beads.role` (git config) and falls back to a remote-shape heuristic when it's
# unset — its own doctor already warns "beads.role not configured (GH#2950)". bh knows the
# authoritative answer per hive (the registry `kind`, not remote shape), so this section maps
# it explicitly (`hive_repair.expected_role`) and flags a hive whose actual value disagrees or
# is missing. Only DRIFT is listed (an already-correct hive is silent), same shape as the
# Prefix Mismatches section above.


def _data_beads_role(cfg) -> list[dict]:
    """Registered hives whose `beads.role` is unset or disagrees with what their registry
    `kind` maps to. A hive with no local checkout/`.beads` is reported UNRESOLVABLE
    (`actual=None`) rather than silently skipped or left to borrow whatever value the process
    happens to be sitting in — that misreport (bh-s08me evidence #5) is exactly the bug this
    function used to have.

    The read goes through `bd.beads_role`, which asks git for the git-config-backed key
    directly (6 ms) instead of paying a ~470 ms `bd config get` per hive (bh-i6e5g). Its
    process cwd is pinned to the hive exactly as `bd.run(pin_process_cwd=True)` pinned it:
    `-C <path>` ALONE resolves git config off the process's REAL cwd, so every hive but the one
    `bh doctor` runs from would otherwise report the RUNNER's own value (bh-s08me)."""
    findings = []
    for e in cfg.get("managed_repos", []) or []:
        path = registry.hive_dir(e)
        hive_id = f"{e['provider']}/{e['org']}/{e['repo']}"
        kind = str(e.get("kind", ""))
        expected = hive_repair.expected_role(kind)
        if not (path / ".beads").is_dir():
            findings.append(
                {
                    "hive": hive_id,
                    "kind": kind,
                    "expected": expected,
                    "actual": None,
                    "remediation": f"no local checkout at {path} — clone it before repairing",
                }
            )
            continue
        actual = bd.beads_role(path)
        if actual is None:  # unreadable (not a git repo, no git) — same skip as before
            continue
        if actual == expected:
            continue
        findings.append(
            {
                "hive": hive_id,
                "kind": kind,
                "expected": expected,
                "actual": actual,
                "remediation": f"{config.BINARY_ALIAS} hive repair --hive {hive_id} --role --yes",
            }
        )
    return findings


def _render_beads_role(findings: list[dict]) -> None:
    typer.echo(f"\n# beads.role ({len(findings)})")
    if not findings:
        typer.echo("  ✓ none")
        return
    for f in findings:
        if f["actual"] is None:
            actual = "unresolvable (no local checkout)"
        elif f["actual"]:
            actual = f"'{f['actual']}'"
        else:
            actual = "unset"
        typer.echo(f"  ⚠ {f['hive']}: kind={f['kind']} expected='{f['expected']}' actual={actual}")
        typer.echo(f"    fix: {f['remediation']}")


def _section_beads_role(cfg):
    """Render the beads.role section."""
    _render_beads_role(_data_beads_role(cfg))


# ---- store engine section (bh-areg.3) ----------------------------------------
# "Nothing in bh knows a store engine can be DOWN" — embedded mode has no liveness question
# (in-process engine); mode (a) — bd's shared `dolt sql-server` — can be down, wedged, or on
# the wrong port, and nothing reported that before this. Silent (renders NOTHING, not even the
# header) when every registered hive is embedded and nothing has drifted, so a fleet that has
# never migrated sees byte-identical `bh doctor` output — the acceptance bar this section is
# held to ("no new noise for users who never migrate"), matching the Seats section's own "an
# optional integration that complains when unused is not optional" precedent.
#
# Down-behavior, CHOSEN and documented here (bh-areg.3's acceptance bar): a down/unreachable
# shared server is reported LOUDLY (⚠ + the exact remedy) but bh does NOT auto-start it and
# does NOT fall back to embedded — falling back would silently point two engines (bd's server,
# and a resurrected embedded store) at what an operator believes is ONE store, with no way to
# tell which one wrote last. bh has no daemon in the dolt lifecycle path under mode (a)
# (bh-u562.1's GO verdict) — `bd dolt start` is the correct remedy, and bd's own verbs already
# hard-fail with that exact hint (bh-u562.1 finding 2, shared mode: 0.32s, legible error). This
# section only reports; it never mutates or restarts anything. See docs/DOLT.md's "Store
# engine liveness" section for the full per-verb-class writeup.


def _data_store_engine(cfg) -> dict:
    """Store-engine liveness across registered hives.

    Reads each hive's PERSISTED `dolt_mode` (`store_locator`, zero subprocess — never a
    `bd dolt status` probe, per this bead's own constraint) and probes the shared endpoint AT
    MOST ONCE regardless of how many hives are server-mode: mode (a) is one `dolt sql-server`
    per HOST, shared by every hive's own database on it, so N server-mode hives cost one probe,
    not N.
    """
    server_mode_hives: list[str] = []
    mismatches: list[dict] = []
    for e in cfg.get("managed_repos", []) or []:
        path = registry.hive_dir(e)
        if not (path / ".beads").is_dir():
            continue
        if store_locator.dolt_mode(path) == "server":
            server_mode_hives.append(str(e["prefix"]))
        reason = dolt_health.mismatch_reason(path)
        if reason:
            mismatches.append({"prefix": str(e["prefix"]), "reason": reason})

    # A zombie makes this section relevant on its own (bh-hqmcl): the orphan on beadhive-factory
    # survived a host wipe, so it outlived every hive that would otherwise have put a row here.
    # A server nothing claims is exactly the one that must not be silent.
    running = _data_running_servers()
    if not server_mode_hives and not mismatches and not running["zombies"]:
        return {"relevant": False}

    probe = dolt_health.probe_shared_server() if server_mode_hives else None
    srv_host, port = dolt_health.server_endpoint()
    return {
        "relevant": True,
        "endpoint": {"host": srv_host, "port": port},
        "server_mode_hives": server_mode_hives,
        "reachable": probe.reachable if probe else None,
        "detail": probe.detail if probe else None,
        "mismatches": mismatches,
        "running": running,
    }


def _render_store_engine(d: dict) -> None:
    if not d["relevant"]:
        return
    typer.echo("\n# Store Engine")
    if d["server_mode_hives"]:
        host, port = d["endpoint"]["host"], d["endpoint"]["port"]
        hives = ", ".join(d["server_mode_hives"])
        if d["reachable"]:
            typer.echo(f"  shared server {host}:{port}: ✓ reachable  (hives: {hives})")
        else:
            typer.echo(f"  shared server {host}:{port}: ✗ UNREACHABLE  (hives: {hives})")
            typer.echo(f"    {d['detail']}")
            typer.echo(
                "    bd verbs against these hives will hard-fail until it's back — start it "
                "with `bd dolt start` (bh does not auto-start it or fall back to embedded)"
            )
    for m in d["mismatches"]:
        typer.echo(f"  ⚠ {m['prefix']}: {m['reason']}")
    _render_running_servers(d.get("running") or {})


# A ZOMBIE ANSWERS THE PROBE ABOVE (bh-hqmcl). The reachability line is a fact about a PORT, and
# the orphan on this host kept LISTENing on 3308 for three days with its datadir unlinked — so
# "✓ reachable" was true and useless. These lines answer what the operator actually needs: WHICH
# servers are running, what each one serves, whether that directory still exists, and — when the
# three sources of truth disagree — which one bh believes.
#
# `bh doctor` because that is where the bead's own notes put it ("WHAT WOULD HAVE HELPED: a bh
# doctor line reporting every running dolt server, which datadir it serves, whether that datadir
# still exists, and whether bd considers it managed or external"), and because a fact nobody
# routinely looks at is a fact nobody has. The two orphans found on 2026-08-08 were found only
# because stopping the shared server required enumerating processes by hand.


def _data_running_servers() -> dict:
    """Inventory + three-way reconciliation of this host's dolt servers (bh-hqmcl)."""
    rec = dolt_health.reconcile()
    return {
        "backend": rec.backend,
        "shared_server_dir": rec.shared_server_dir,
        "shared_server_dir_exists": rec.shared_server_dir_exists,
        "authoritative": rec.authoritative,
        "detail": rec.detail,
        "servers": [s.as_dict() for s in rec.servers],
        "zombies": [s.as_dict() for s in rec.servers if not s.datadir_exists],
    }


def _render_running_servers(d: dict) -> None:
    if not d:
        return
    servers = d.get("servers") or []
    typer.echo(f"  dolt servers on this host: {len(servers)}")
    for s in servers:
        mark = "✓" if s["datadir_exists"] else "✗"
        gone = "" if s["datadir_exists"] else "  ← DATADIR IS GONE (zombie: it still answers)"
        typer.echo(f"    {mark} pid {s['pid']:>7}  [{s['role']}]  {s['datadir'] or '?'}{gone}")
        if not s["datadir_exists"] and s["config_path"]:
            typer.echo(f"        started from: {s['config_path']}")
    typer.echo(
        f"  reconciliation: dolt.backend={d['backend']}, shared-server dir "
        f"{'present' if d['shared_server_dir_exists'] else 'ABSENT'} ({d['shared_server_dir']})"
    )
    typer.echo(f"    authoritative: {d['authoritative']} — {d['detail']}")
    if d.get("zombies"):
        typer.echo(
            "    a zombie is NOT stopped by `bd dolt stop` (bd calls the shared server "
            '"external" and refuses) — identify it by /proc/<pid>/cwd and SIGTERM the pid.'
        )


def _section_store_engine(cfg):
    """Render the store-engine section."""
    _render_store_engine(_data_store_engine(cfg))


# ---- dispatch section (bh-e7r9q.6) --------------------------------------------
# "bh host list prints one word — stale — and both hosts on this fleet show it right now."
# Under HUMAN operation staleness is harmless (the next verb renews); under UNATTENDED
# operation a stale executor is EITHER a dead dispatcher OR a lease that lapsed while work was
# in flight, and those need different operator responses. This section is what tells them
# apart, reading through `dispatch_status.compute_status` — the SAME function `bh host dispatch
# status` renders — so doctor never re-derives "is it healthy" and the two can never disagree.
#
# Silent (renders NOTHING, not even the header) when dispatch has never been enabled on this
# host for any registered hive, matching the Store Engine / Seats sections' own
# no-noise-for-non-users bar.


def _data_dispatch(cfg) -> dict:
    """One row per hive where a dispatch backend is (or was) installed, each carrying the four
    checks bh-e7r9q.6 exists to make: `dead` (supervised but not running), `lease_lost`
    (running but this host does not hold the lease), `lease_expiring_soon`, and `stalled`
    (running, leased, but no pass recorded within `host.dispatch.stale_after_seconds`)."""
    from . import dispatch_status

    stale_after = config.dispatch_stale_after_seconds(cfg)
    rows = []
    for status in dispatch_status.compute_status_all(cfg):
        if not status.installed:
            continue
        stale_seconds = status.stale_since_seconds()
        stalled = (
            status.running
            and status.state == dispatch_status.STATE_RUNNING_HEALTHY
            and stale_seconds is not None
            and stale_seconds > stale_after
        )
        rows.append(
            {
                "hive": status.hive,
                "state": status.state,
                "dead": status.state == dispatch_status.STATE_ENABLED_STOPPED,
                "lease_lost": status.state == dispatch_status.STATE_RUNNING_WITHOUT_LEASE,
                "lease_expiring_soon": status.lease_expiring_soon,
                "stalled": stalled,
                "stale_seconds": stale_seconds,
                "last_pass_at": status.last_pass_at,
                "detail": status.detail,
            }
        )
    return {"relevant": bool(rows), "hives": rows}


def _render_dispatch(d: dict) -> None:
    if not d["relevant"]:
        return
    typer.echo("\n# Unattended Dispatch")
    for row in d["hives"]:
        if row["dead"]:
            typer.echo(
                f"  ✗ {row['hive']}: DEAD — supervised but not running "
                f"(`bh host dispatch status --hive {row['hive']}` for detail; "
                f"`bh host dispatch enable --hive {row['hive']}` to restart it)"
            )
            continue
        if row["lease_lost"]:
            typer.echo(
                f"  ✗ {row['hive']}: RUNNING WITHOUT THE LEASE — either a correct multi-host "
                f"handoff or a lapsed lease mid-flight; `bh host dispatch status --hive "
                f"{row['hive']}` distinguishes them"
            )
            continue
        if row["stalled"]:
            minutes = int((row["stale_seconds"] or 0) // 60)
            typer.echo(
                f"  ⚠ {row['hive']}: no pass recorded in {minutes}m — "
                f"`bh host dispatch logs --hive {row['hive']}` to see what it last did"
            )
        if row["lease_expiring_soon"]:
            typer.echo(f"  ⚠ {row['hive']}: lease expiring soon")
        if not (row["stalled"] or row["lease_expiring_soon"]):
            typer.echo(f"  ✓ {row['hive']}: healthy")


def _section_dispatch(cfg):
    """Render the dispatch section."""
    _render_dispatch(_data_dispatch(cfg))


# ---- per-group auth section (bh-4y0r.3) -------------------------------------


def _data_group_auth(cfg) -> dict:
    """Per-repo-group auth section: a read-only git-config introspection table (identity,
    signing key, insteadOf alias, includeIf scoping) plus warnings for missing/shared auth.
    bh never writes global git config — see `gitauth`."""
    rows = gitauth.group_auth_table(cfg)
    return {"groups": rows, "warnings": gitauth.group_auth_warnings(rows)}


def _render_group_auth(d: dict) -> None:
    typer.echo(f"\n# Repo-group auth ({len(d['groups'])} groups)")
    for r in d["groups"]:
        scoped = "scoped" if r["scoped"] else "global (unscoped)"
        signing = r["signingkey"] or "(none)"
        alias = r["insteadof_alias"] or "(none)"
        typer.echo(
            f"  {r['path']}/{r['account']}: {r['name']} <{r['email']}>  "
            f"signingkey={signing}  insteadOf={alias}  [{scoped}]"
        )
    for w in d["warnings"]:
        typer.echo(f"  ⚠ {w}")


def _section_group_auth(cfg):
    """Render the per-group auth section."""
    _render_group_auth(_data_group_auth(cfg))


# ---- MCP section ------------------------------------------------------------


def _plugin_declares_server(cfg) -> bool:
    """True when the installed marketplace clone's bh plugin declares mcpServers.bh."""
    try:
        mcp_path = config._plugin_root(cfg) / ".mcp.json"
        if not mcp_path.is_file():
            return False
        data = json.loads(mcp_path.read_text())
        return "bh" in (data.get("mcpServers") or {})
    except Exception:  # noqa: BLE001
        return False


def _data_mcp(cfg=None) -> dict:
    """MCP section: fastmcp (core dep) importability, plugin declaration, and preflight hints."""
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
        # fastmcp_available from the beadhive://doctor payload.
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
        # Same seam as the stale-snapshot hint below (bh-jmw0). Two call sites emitting a
        # hand-written command is precisely how bh-tccp became the fifth instance of its shape:
        # one got fixed, the other kept the old wording. Fix the seam, not the string.
        for line in install_plane.describe(install_plane.detect()):
            typer.echo(f"  {line}")
        typer.echo("  hint: fastmcp is a core dependency — a broken install makes the")
        typer.echo("        bundled ws server silently fail to register")
        if d["plugin_declares_server"]:
            typer.echo("  plugin declares server: yes")


def _section_mcp(cfg=None):
    """Report MCP extra availability and plugin server declaration."""
    _render_mcp(_data_mcp(cfg))


# ---- seats section (bh-og0q.4) -----------------------------------------------
# "Which seats can THIS host run" — rides hitch_plugin's Plugin.readiness hook (the SAME hook
# `bh hive ready` consumes via `hive_ready._plugin_checks`), not a bespoke doctor-only check.
# hitch is optional (ADR Amendment 2): disabled or absent, this section renders NOTHING — no
# header, no line — stronger than `bh hive ready`'s own "na" convention, per this bead's own
# acceptance bar ("an optional integration that complains when unused is not optional").


def _data_seats(cfg, *, full: bool = False) -> dict | None:
    """Seats section data: the hitch plugin's own `(state, detail)` readiness reading, or
    ``None`` when hitch is disabled/absent — the render step stays silent in that case.

    ``full=False`` (the default, bh-gqfrm) skips the 7-way `hitch profile preflight` fanout
    (~2.7s, the largest warm section of `bh doctor` — docs/BH_DATA_PIPELINE.md §4.1) and only
    checks that hitch itself is usable (on PATH, repo configured, catalog present); the ``detail``
    string says explicitly that per-seat checks were skipped and how to get them (`--seats`), so
    a clean HUMAN report never reads as "and every seat was checked". Pass ``full=True`` for the
    complete per-seat breakdown (bh doctor --seats).

    ``seats_checked`` (bh-gqfrm, review round 2) is the MACHINE-readable twin of that same fact —
    a bare ``state: "ok"`` cannot distinguish "hitch usable, every seat verified" from "hitch
    usable, seats never looked at", and `detail` is prose an MCP/JSON consumer should not have to
    string-match to recover a meaning this payload already knows. It is exactly ``full`` — the
    caller's own request, not a re-derived guess — so `bh doctor --json` and `--seats --json`
    always carry a field a consumer can branch on instead of parsing English.

    ``state`` deliberately stays ``"ok"`` when ``seats_checked`` is ``False``: it already meant
    "nothing WRONG in what was checked", not "everything possible was checked", before this bead
    — an empty `seat_reports()` (no seat-aligned profiles configured) has always produced
    ``"ok"`` with no per-seat detail either. Reusing that existing reading for "skipped by
    request" keeps one meaning for `state` instead of adding a second, and `seats_checked` is the
    field that now carries the "how thoroughly" question `state` was never able to answer alone.

    No `schema_version` bump (bh-gqfrm): `jsonout`'s own convention is "add a field → same
    version (consumers ignore unknown keys); remove/retype/re-mean a field → bump" — this only
    adds `seats_checked` and leaves `state`/`detail`'s existing meaning untouched."""
    if not hitch_plugin.PLUGIN.enabled(cfg, None):
        return None
    result = (
        hitch_plugin.PLUGIN.readiness(cfg, None)
        if full
        else hitch_plugin.PLUGIN.readiness(cfg, None, full=False)
    )
    if result is None:
        return None
    state, detail = result
    return {"state": state, "detail": detail, "seats_checked": full}


def _render_seats(d: dict | None) -> None:
    """Render the Seats section — entirely absent when `_data_seats` returned None."""
    if d is None:
        return
    typer.echo("\n# Seats (hitch)")
    for line in d["detail"].splitlines():
        typer.echo(f"  {line}")


def _section_seats(cfg, *, full: bool = False):
    """Render the seats section (doctor entry point)."""
    _render_seats(_data_seats(cfg, full=full))


# ---- install-staleness section (bh-9plr) ------------------------------------
# The uv-tool snapshot of beadhive is a point-in-time copy: a src change merged to the source
# checkout does NOT reach the installed `bh` until it is reinstalled, so lifecycle verbs can
# silently run old code. This section compares the RUNNING package against the self-hive source
# and flags the drift, pointing at the one-command reinstall.


def _running_pkg_dir() -> Path:
    """Directory of the RUNNING beadhive package (installed snapshot or dev src checkout)."""
    return Path(__file__).resolve().parent


def _source_pkg_dir(cfg) -> Path | None:
    """`src/beadhive` inside the self-hive checkout, or None.

    The self-hive is the registered hive whose checkout IS the beadhive source repo — detected by
    a `src/beadhive/` package dir plus a pyproject declaring `name = "beadhive"` (no hardcoded
    provider/org/repo). Returns its package dir so its .py can be compared to the running one.
    """
    for e in cfg.get("managed_repos", []) or []:
        main = registry.hive_dir(e)
        pkg = main / "src" / "beadhive"
        pyproj = main / "pyproject.toml"
        if not (pkg.is_dir() and pyproj.is_file()):
            continue
        try:
            if 'name = "beadhive"' in pyproj.read_text():
                return pkg.resolve()
        except OSError:
            continue
    return None


def _hash_pkg(d: Path) -> str:
    """Order-stable content hash of a package's *.py files (path + bytes). A wheel install copies
    sources verbatim, so an in-sync install hashes identically to its source; any src edit diverges.
    """
    h = hashlib.sha256()
    for f in sorted(d.rglob("*.py")):
        h.update(f.relative_to(d).as_posix().encode())
        h.update(f.read_bytes())
    return h.hexdigest()


def _data_install(cfg) -> dict:
    """Install section: installed version + whether the running snapshot lags the self-hive source.

    `stale` is True only when a self-hive source dir is found, we are NOT running from it, and its
    .py content hash differs from the running package's. Running from source (uv run / editable) is
    always current; a missing source checkout means we cannot judge (stale stays False).
    """
    running = _running_pkg_dir()
    source = _source_pkg_dir(cfg)
    from_source = source is not None and running == source
    stale = False
    if source is not None and not from_source:
        try:
            stale = _hash_pkg(running) != _hash_pkg(source)
        except OSError:
            stale = False
    try:
        version = importlib.metadata.version("beadhive")
    except importlib.metadata.PackageNotFoundError:
        version = "unknown"
    return {
        "version": version,
        "running_from": str(running),
        "source_dir": str(source) if source is not None else None,
        "from_source": from_source,
        "stale": stale,
        "pin": _source_pin(source),
        "legacy": _legacy_plane(),
    }


def _legacy_plane() -> dict | None:
    """Posture-2 advice, or None when there is nothing to say (bh-vmdq.4).

    JOINS SIGNALS THAT ALREADY EXIST rather than adding detection: `install_plane.detect()`
    answers which plane, `setup.probe_tools()` answers which of the four infra tools this
    machine actually has. Neither is new; nothing joined them up.

    Returns None on every plane except PYPI — a managed, containerised or editable install must
    stay SILENT here. Advice that fires where it does not apply is how a report stops being read.

    `unmanaged` names the tools MISSING ON THIS MACHINE, so the recommendation is about the
    operator's own box rather than a generic pitch. An empty list is meaningful and NOT a reason
    to suppress the advice: the tools being present says nothing about their being pinned, which
    is the property the managed path actually adds.
    """
    if install_plane.detect() != install_plane.PYPI:
        return None
    from . import setup as _setup  # lazy: setup pulls in the probe table

    try:
        probed = _setup.probe_tools()
    except OSError:
        probed = {}
    return {"unmanaged": sorted(n for n, r in probed.items() if not r.get("found"))}


def _source_pin(source: Path | None) -> str:
    """The version the SOURCE checkout declares — the pin a reinstall should land on.

    Deliberately not the RUNNING version: in the stale case that is the old one, and pinning to it
    would reinstall the staleness this section exists to report. Mirrors what
    `scripts/release-pin.sh` does — derive the version from the tree rather than type it — so bh
    and the install path cannot disagree about what "the pin" means (bh-jmw0).

    Empty string when unreadable, which `install_plane.describe` renders as an UNPINNED command:
    honest, since bh cannot preserve a pin it could not determine.
    """
    if source is None:
        return ""
    pyproject = source.parents[1] / "pyproject.toml"
    try:
        import tomllib

        return str(tomllib.loads(pyproject.read_text()).get("project", {}).get("version", ""))
    except (OSError, ValueError, KeyError, IndexError):
        return ""


def _render_install(d: dict) -> None:
    typer.echo("\n# Install")
    typer.echo(f"  version: {d['version']}")
    typer.echo(f"  running from: {d['running_from']}")
    if d["from_source"]:
        typer.echo("  ✓ running from source checkout (always current)")
    elif d["source_dir"] is None:
        typer.echo("  source checkout: not found (staleness not checked)")
    elif d["stale"]:
        typer.echo("  ⚠ installed snapshot is STALE — the source checkout has newer changes")
        typer.echo(f"    source: {d['source_dir']}")
        # Was an unconditional `uv tool install --force 'beadhive[otel]'` (bh-jmw0). The TOOL was
        # right — bh comes from uv on every plane — but the advice was not: it dropped the version
        # pin `local-install` derives from the tag, and named one step of the TWO a provisioned
        # host needs, where flake.lock pins the toolchain independently of the release pin.
        for line in install_plane.describe(install_plane.detect(), pin=d["pin"]):
            typer.echo(f"    {line}")
    else:
        typer.echo("  ✓ installed snapshot matches source")
    _render_legacy_plane(d.get("legacy"))


def _render_legacy_plane(legacy: dict | None) -> None:
    """The posture-2 line: you are on the unmanaged path, here is what managed buys YOU.

    ONE place, ONCE per invocation — it hangs off the Install section rather than repeating
    per-section, because advice a reader meets three times reads as nagging and gets skipped the
    first time on the next run. `None` (any plane but PYPI) prints nothing at all.

    It does NOT promise an automatic migration: the workspace half is `bh-cgcg.3` and is
    unlanded, so the text points at UPGRADING.md, which is honest about which steps are manual.
    Claiming a command that does not exist is worse than admitting a manual step.
    """
    if legacy is None:
        return
    typer.echo("  ⚠ unmanaged install path (PyPI) — bh is installed, its dependencies are not")
    if legacy["unmanaged"]:
        typer.echo(f"    missing on this machine: {', '.join(legacy['unmanaged'])}")
    else:
        # Present is not the same as pinned, and saying so prevents "I have all four, so this
        # does not apply to me" — which is the reading that keeps a partially-migrated host
        # where it is.
        typer.echo("    all four present, but unpinned — versions are whatever this machine has")
    typer.echo("    the managed path installs and pins bd, dolt, gh and git-workspace together")
    typer.echo("    migrate: docs/UPGRADING.md — 'Ad-hoc PyPI → the managed path'")


def _section_install(cfg):
    """Render the install-staleness section."""
    _render_install(_data_install(cfg))


# ---- observability section --------------------------------------------------


def _data_observability(cfg) -> dict:
    """Observability section: resolved log settings, OTel enablement + library availability."""
    otel_libs = otel.sdk_importable()
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
        typer.echo("  otel libs: unavailable (install: pip install 'beadhive[otel]')")
    typer.echo(f"  endpoint: {d['endpoint'] or '(not set)'}")


def _section_observability(cfg):
    """Report resolved log settings and OTel enablement / library availability."""
    _render_observability(_data_observability(cfg))


# ---- fleet health section ---------------------------------------------------


def _data_fleet_health(records: dict[str, metadata.RepoMetadata], git_repos: set[str]) -> dict:
    """Fleet-wide safety and reclamation summary rolled up from the workspace-metadata cache.

    Reads pre-measured ``records`` (one per git repo under recognized provider dirs; the same
    aggregation the Disk Usage section consumes, so each repo is measured at most once per
    ``ws doctor`` — the disk-walk double-scan is gone) and tallies:
    - dirty repos (uncommitted working-tree changes on any branch)
    - repos with unpushed branches (any branch ahead > 0 vs its upstream)
    - repos with unpushed Dolt state (``refs/dolt/data`` ahead/diverged, or local-only with
      no remote copy — Beads' Dolt-backed issue state; tallied separately from git-unpushed
      since a hive can be branch-clean yet still carry unbacked bead state, see safety.py).
    - unknown-state repos (no cache record, or a Dolt state that genuinely could not be
      verified — ``dolt_ref`` status ``"unknown"``, see safety.DoltRefInfo.reason). Counted
      honestly in their own bucket: never folded into clean/green, never rendered as
      confirmed-unpushed.
    - no-origin repos (no remote named ``origin`` — local-only, cannot be re-cloned)
    - stale clones (last commit older than ``safety.MATURITY_STALE_DAYS`` days)
    - reclaimable space (disk_bytes of no-origin OR stale repos; counted once each)

    A repo that is both no-origin and stale is counted in disk only once.
    """
    dirty_count = 0
    unpushed_count = 0
    dolt_unpushed_count = 0
    unknown_count = 0
    no_origin_count = 0
    stale_count = 0
    reclaimable_bytes = 0

    for key in git_repos:
        rec = records.get(key)
        if rec is None:
            # No cache record (e.g. path vanished after scan) — unverifiable, not green.
            unknown_count += 1
            continue

        is_no_origin = not rec.has_origin
        is_dirty = any(b["dirty"] for b in rec.branches)
        has_unpushed = any(b["ahead"] > 0 for b in rec.branches)
        # A record with no dolt_ref at all is unverified, not green.
        dolt_status = rec.dolt_ref.get("status", "unknown")
        has_dolt_unpushed = dolt_status in ("ahead", "diverged") or (
            dolt_status == "no-remote" and rec.has_origin
        )
        # Cache stores age_days=None for a no-commit repo (inf) — inf >= threshold ⇒ stale.
        is_stale = rec.age_days is None or rec.age_days >= safety.MATURITY_STALE_DAYS

        if is_dirty:
            dirty_count += 1
        if has_unpushed:
            unpushed_count += 1
        if has_dolt_unpushed:
            dolt_unpushed_count += 1
        if dolt_status == "unknown":
            unknown_count += 1
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
        "dolt_unpushed": dolt_unpushed_count,
        "unknown": unknown_count,
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
    typer.echo(f"  unpushed dolt state:  {d['dolt_unpushed']}")
    unknown_note = "(no cache record or unverifiable dolt status)"
    typer.echo(f"  unknown state:        {d['unknown']}  {unknown_note}")
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
    typer.echo(f"  hives registered:        {d['hives_registered']}")
    typer.echo(f"  git repos on disk:      {d['git_repos_on_disk']}")
    typer.echo(f"  onboarding candidates:  {d['onboarding_candidates']}")
    typer.echo(f"  excluded:               {d['excluded']}")
    if d["untracked_git_repos"] is not None:
        typer.echo(f"  untracked git repos:    {d['untracked_git_repos']}")
    typer.echo(f"  non-repo folders:       {d['non_repo_folders']}")
    typer.echo(f"  unrecognized top dirs:  {d['unrecognized_top_dirs']}")


def _data_disk_usage(hives, root: Path, records) -> dict:
    """Disk-usage section: per-hive disk_bytes (or missing) + the total across present hives."""
    entries = []
    total_bytes = 0
    for e in hives:
        path = root / e["provider"] / e["org"] / e["repo"]
        if not path.exists():
            entries.append({"prefix": str(e["prefix"]), "missing": True, "disk_bytes": None})
            continue
        rec = records.get(f"{e['provider']}/{e['org']}/{e['repo']}")
        disk_bytes = rec.disk_bytes if rec is not None else 0
        total_bytes += disk_bytes
        entries.append({"prefix": str(e["prefix"]), "missing": False, "disk_bytes": disk_bytes})
    return {"hives": entries, "total_bytes": total_bytes}


def _render_disk_usage(d: dict) -> None:
    typer.echo("\n# Disk Usage (by hive)")
    for e in d["hives"]:
        if e["missing"]:
            typer.echo(f"  {e['prefix']:<12}  (missing)")
            continue
        typer.echo(f"  {e['prefix']:<12}  {safety.format_bytes(e['disk_bytes'])}")
    if d["hives"]:
        typer.echo(f"  {'total':<12}  {safety.format_bytes(d['total_bytes'])}")


# ---- warnings section -------------------------------------------------------


def _local_commits_while_not_primary(cfg, entry, path: Path) -> tuple[int, str]:
    """``(count, holder)`` of local ``refs/dolt/data`` commits made after the CURRENT lease
    holder's adoption, for a hive `entry` this host does not currently hold the lease for —
    the doctor-side counterpart of ``prepush.check_fence``'s pre-push refusal (bh-ytbb.12),
    sharing the SAME local-only primacy read (``guard.primary_state``) so the two can never
    disagree about who is primary.

    Direct ``bd`` — invoked outside ``bh`` entirely — bypasses the multi-host guard
    completely; bh cannot intercept it. ``bh bd`` no longer shares that bypass
    (bh-edvs' :func:`guard.bd_write_refusal` gates it at the passthrough seam with the same
    lease check ``guard_primary`` uses for ``bh work``'s own write verbs, carved out only for
    the intake tier's bare, unparented ``bd create``, bh-lkbas), so this warning's remaining
    reason to exist is RAW ``bd``: surface local writes an operator built on a host that was
    already not primary, before they discover it only at push time (bounded exposure, but an
    hour of wasted work either way — see the bead description).

    Bounded on purpose: only commits dated after the current holder's ``adopted_at`` count —
    i.e. commits made strictly after primacy demonstrably passed elsewhere, not "everything
    ever committed locally that outran the last push" (which a healthy single-host workflow
    racks up constantly between pushes and would false-positive on every hive). Checked in
    both places ``refs/dolt/data`` can live (``prepush.py``'s module docstring): the hive's
    own checkout (a non-embedded Dolt storage shape) and any existing bd-embedded
    git-transport bare repo (``host_fence.transport_repos`` — absent until a first push,
    which is fine: see that module's docstring for why the gap is harmless). Both are purely
    local reads — no ``ls-remote``, no fetch, no HQ round trip."""
    state = guard.primary_state(cfg=cfg, entry=entry)
    if state is None:
        return 0, ""  # multi-host not in force for this hive — nothing to check
    _prefix, this_host, lease = state
    if lease.held_by(this_host):
        return 0, ""  # this host IS primary — its local writes are the authoritative ones
    total = 0
    for repo in (path, *host_fence.transport_repos(path)):
        res = hive.run(
            ["git", "rev-list", "--count", f"--since={lease.adopted_at}", host_fence.DATA_REF],
            cwd=str(repo),
            check=False,
            capture=True,
        )
        if getattr(res, "returncode", 1) != 0:
            continue  # no local refs/dolt/data here (the common embedded-engine checkout case)
        try:
            total += int((getattr(res, "stdout", "") or "0").strip() or 0)
        except ValueError:
            pass
    return total, (lease.host_id or "nobody")


# ---- home layout drift (bh-cmqp.3) -------------------------------------------
# docs/design/beadhive-home-layout-contract.md is the source of truth for what belongs at the
# top level of `config.home()` and how each entry is classified (durable / regenerable /
# machine-local / artifact). This is the code half of that contract: the fixed entries below,
# plus the independently relocatable stores resolved from their own accessors, are the ONLY
# things `config.home()` should hold — anything else is drift the doc hasn't accounted for.
# Update the doc and this set together when the layout changes.
_KNOWN_HOME_ENTRIES = frozenset(
    {
        "config.yaml",
        "config.yaml.bak",
        "host.yaml",
        "labels.md",
        "docker-compose.yml",
        "docker-compose.otel.yml",
        ".env",
        ".env.example",
        "setup-state.json",
        "backups",
        "retros",
        # migrate-storage's own bookkeeping — small, machine-local, written at the top level.
        # Classified here (and in the contract doc) by bh-5009a: relocating
        # `storage-migrate-backups/` under `backups/` left these two as the only unexplained
        # entries doctor was flagging on a real host, which is a doc gap, not drift.
        "storage-migrate-locks",
        "storage-migrate-state.json",
        # Pre-bh-5009a backup roots. Still READ (`backup.legacy_roots`) and relocated on demand
        # by `bh backup migrate-layout`, so a host that hasn't run it yet isn't drift either.
        "hq-backups",
        "storage-migrate-backups",
    }
)


def _configurable_home_entries(cfg) -> set[str]:
    """Basenames of the independently relocatable stores (hq, hub, cache, hitch, and —
    persistent mode only — worktrees) that currently resolve under `config.home()`. Each
    honours its own override (`$BH_HQ`, `hitch.root`, `worktrees.path`, …), so its expected
    name is read from the accessor rather than assumed; one relocated elsewhere just drops out
    of this set instead of tripping a false positive."""
    home = config.home()
    candidates = [
        config.hq_dir(),
        config.hub_dir(),
        config.cache_dir(),
        config.hitch_config_dir_root(cfg),
    ]
    if not config.worktrees_ephemeral(cfg):
        candidates.append(config.worktrees_root(cfg))
    names = set()
    for p in candidates:
        try:
            names.add(p.relative_to(home).parts[0])
        except ValueError:
            pass  # relocated outside home() by an override — not a home entry at all
    return names


def _legacy_worktrees_root(cfg) -> Path | None:
    """The pre-`worktrees.path` default root (`config.home() / "worktrees"` —
    `worktrees_root`'s own fallback) when it still exists on disk but the CURRENTLY active
    persistent root is a different directory: orphaned content left behind when a host
    set/changed `worktrees.path` without migrating what was already there. See
    docs/design/beadhive-home-layout-contract.md's Migration section for cleanup steps."""
    if config.worktrees_ephemeral(cfg):
        return None
    legacy = config.home() / "worktrees"
    active = config.worktrees_root(cfg)
    return legacy if legacy != active and legacy.is_dir() else None


def _data_layout(cfg) -> dict:
    """Layout-drift findings against docs/design/beadhive-home-layout-contract.md: top-level
    `config.home()` entries the doc doesn't account for, and the specific wt/-vs-worktrees/
    drift a `worktrees.path` change can leave orphaned on disk."""
    home = config.home()
    legacy = _legacy_worktrees_root(cfg)
    unclassified: list[str] = []
    if home.is_dir():
        known = _KNOWN_HOME_ENTRIES | _configurable_home_entries(cfg)
        unclassified = sorted(
            p.name
            for p in home.iterdir()
            # the legacy worktrees root gets its own, more actionable warning below —
            # don't also report it as a generic unclassified entry
            if p.name not in known and p != legacy
        )
    return {
        "unclassified": unclassified,
        "legacy_worktrees_root": str(legacy) if legacy else None,
    }


def _data_warnings(cfg, root: Path, hives, git_repos, nonrepo, unknown_top, untracked):
    """Warnings section: config drift, prefix collisions, untracked/unrecognized folders,
    and per-hive checkout/beads/grant issues. Excluded orgs are out of scope — skipped."""
    cfg_orgs = cfg.get("orgs", {}) or {}
    gw_orgs = gitworkspace.orgs(cfg)
    excluded_orgs = set((cfg.get("exclude", {}) or {}).get("orgs", []) or [])

    def _not_excluded(key):
        return not registry.is_excluded(cfg, *key.split("/"))

    warns = []
    warns += [
        f"config: {v['key']} = {v['value']!r} is not one of "
        f"{'/'.join(str(c) for c in v['choices'])} (using default {v['default']!r})"
        for v in config.literal_violations(cfg)
    ]
    layout = _data_layout(cfg)
    warns += [
        f"unrecognized ~/.beadhive entry not in the layout contract "
        f"(docs/design/beadhive-home-layout-contract.md): {name}"
        for name in layout["unclassified"]
    ]
    if layout["legacy_worktrees_root"]:
        warns.append(
            f"legacy worktrees root {layout['legacy_worktrees_root']} still exists but "
            f"worktrees.path now points elsewhere ({config.worktrees_root(cfg)}) — see "
            "docs/design/beadhive-home-layout-contract.md (Migration: wt/ vs worktrees/ "
            "drift) for cleanup steps"
        )
    for o in sorted(gw_orgs - set(cfg_orgs) - excluded_orgs):
        warns.append(
            f"org '{o}' from git-workspace not in config.yaml "
            f"(using auto code '{registry.sanitize(o)[:2]}', policy personal)"
        )
    warns += [f"required-org prefix: {v}" for v in registry.required_violations(cfg)]
    by_prefix = {}
    for e in hives:
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
    warns += [
        f"workspace-lock path nested deeper than <group>/<org>/<repo> "
        f"(orca discover_repos won't find it): {p}"
        for p in sorted(gitworkspace.deep_nested_paths(cfg))
    ]
    for e in hives:
        path = root / e["provider"] / e["org"] / e["repo"]
        if not config.validate_cmd_is_configured(cfg, e):
            cmd = config.validate_cmd(cfg, e)
            # bh-l44i: RESOLVE the default (follow `just <recipe>`'s own justfile deps) instead
            # of pattern-matching the string — only a fully-resolved, provably test-free graph
            # warns; unresolvable (no checkout yet, no justfile, non-`just` command) stays quiet.
            probe = validate_probe.probe_validate_cmd(cmd, path if path.exists() else None)
            if probe is True:
                warns.append(
                    f"hive '{e['prefix']}' validate_cmd defaults to {cmd!r}, which does not "
                    "look like it runs tests — set work.validate_cmd explicitly if that's "
                    "intentional (a compile-only default silently lets test regressions "
                    "merge clean)"
                )
        if not path.exists():
            warns.append(f"hive '{e['prefix']}' has no local checkout at {path}")
        elif not (path / ".beads").is_dir():
            warns.append(f"hive '{e['prefix']}' has no .beads/ (not initialized)")
        elif (
            not config.worktrees_ephemeral(cfg)
            and hive.grant_is_current(cfg, path, e["provider"], e["org"], e["repo"]) is False
        ):
            warns.append(
                f"hive '{e['prefix']}' sandbox grant is stale (worktrees root moved) "
                f"— re-run: {config.BINARY_ALIAS} hive init --claude"
            )
        if path.exists() and registry.furnish_of(e) == "none":
            # Furnish drift: a declared zero-footprint hive whose scaffolding is nonetheless
            # tracked in git (e.g. furnished before the declaration flipped, or committed
            # by hand) — the declaration and the repo disagree.
            tracked = hive.run(
                ["git", "ls-files", "--", ".beads"],
                cwd=str(path),
                check=False,
                capture=True,
            )
            if (getattr(tracked, "stdout", "") or "").strip():
                warns.append(
                    f"hive '{e['prefix']}' declared zero-footprint (furnish: none) but "
                    f".beads/ is tracked in git — declare it with "
                    f"`{config.BINARY_ALIAS} hive onboard --furnish`, or untrack .beads/"
                )
        if path.exists():
            n_commits, holder = _local_commits_while_not_primary(cfg, e, path)
            if n_commits:
                warns.append(
                    f"hive '{e['prefix']}': {n_commits} local commits made while not primary "
                    f"(current primary: {holder}) — direct `bd` bypasses the multi-host guard "
                    "(bh-ytbb.9); the push-time fence (refs/bh/epoch, bh-ytbb.7) will refuse "
                    "these, so treat this local state as unconfirmed until you re-adopt this "
                    f"host or coordinate with {holder}"
                )
    # First: a missing required binary makes everything derived from it untrustworthy, so the
    # operator should read that before any finding it could have manufactured (bh-7m2h9).
    warns = _missing_required_dep_warnings() + warns
    warns += _hq_ahead_warnings(cfg)
    warns += _bd_dolt_fix_warnings()
    warns += _bd_schema_skew_warnings(cfg, hives, root)
    warns += _devshell_only_warnings()
    warns += _disarmed_signing_gate_warnings(cfg, hives)
    warns += _orphaned_dolt_server_warnings()
    warns += _channel_drift_warnings(cfg, hives)
    return warns


_ADR = "docs/design/release-channel-branches-adr.md"


def _channel_off_tag_warning(prefix: str, name: str, d: dict) -> str:
    """The unambiguous half: a channel branch sitting where no release tag is (bh-7daa6.6).

    No threshold, no tuning, no judgement call. Both workflows only ever fast-forward the branch to
    a tag they have just verified is published, so a channel that is not ON a release tag was moved
    by something that is not the automation — a hand-push, or a promotion that pushed the wrong
    thing — and every guarantee the channel makes to an installer is void until someone reconciles
    it. The message says exactly that, names the sha so it can be diffed against `git tag`, and
    stops: the ADR's Decision 1 makes these branches forward-only, so bh has no correct automatic
    repair to offer and inventing one would be the second thing to keep correct.

    Deliberately NOT flagged as a false positive: the publish-gate window (ADR Consequence 2) leaves
    `latest` naming the PREVIOUS release, which is still a release tag. This never fires on it.
    """
    return (
        f"hive '{prefix}': release channel '{name}' points at {d['channels'][name]['sha'][:9]}, "
        f"which carries no release tag — a channel is only ever fast-forwarded to a published "
        f"release, so this was moved outside the automation and its guarantee to anyone installing "
        f"from '{name}' is void; reconcile it out of band (see {_ADR}, Decision 1 — the channels "
        f"are forward-only, so bh will not move it for you)"
    )


def _channel_stale_warning(prefix: str, d: dict, why: str) -> str:
    """The tunable half: `stable` has trailed `latest` long enough to stop looking like a soak."""
    ch = d["channels"]
    return (
        f"hive '{prefix}': release channel 'stable' is {d['behind_releases']} release(s) behind "
        f"'latest' ({ch['stable']['tag']} → {ch['latest']['tag']}) and {why} — promote it "
        f"(`promote-stable.yml`) or decide not to; an unpromoted 'stable' is the hardcoded-pin "
        f"problem relocated rather than solved ({_ADR}, Decision 2)"
    )


def _channel_drift_warnings(cfg, hives) -> list[str]:
    """Report a channel branch that has stopped tracking its release line (bh-7daa6.6).

    **Why `bh doctor` and not a scheduled CI job** — the bead asked for one, with a reason:

    1. **The ADR already decided it** (Decision 2: "`bh-7daa6.6` puts it in `bh doctor`"), and
       Consequence 5 leans on that. Re-litigating a binding record inside its own implementation
       bead is how a decision quietly becomes a suggestion.
    2. **A scheduled workflow is itself a thing that rots silently** — GitHub disables `schedule:`
       triggers in a public repo after 60 days without repository activity, and a scheduled run's
       failure notifications go to the last committer, who is not necessarily anyone who can
       promote. A rot detector whose own failure mode is "stopped running, told nobody" is a
       peculiar answer to rot.
    3. **doctor structurally cannot gate.** This module always exits 0 (see its docstring), so
       "reports, never gates" is a property of the placement rather than a promise in a comment. A
       CI job is one `required_status_check` away from blocking a release — and blocking on a
       lagging `stable` would block on the channel doing its job.
    4. **It is already the right audience.** Only someone with a beadhive checkout can promote
       `stable`; a downstream installer told `stable` is 20 days stale can do nothing with that.
       Iterating registered hives (rather than hardcoding beadhive) means the check follows the
       convention, not this repo — and `channels.scan` returns None for every repo not using it, so
       a workspace with no channel repo pays two `for-each-ref` calls and prints nothing.

    The acknowledged cost is that doctor is pull-based: nobody is *told*, they have to look. That is
    the trade the ADR took, and the off-tag half is durable under it — an off-tag channel stays
    off-tag until someone fixes it, so the finding waits rather than expiring.

    Lives in the warnings section rather than as its own `# Release channels` block on purpose:
    warnings is the aggregated, counted list a reader actually scans, and a state-only section
    would print for every user on every run to say nothing is wrong.
    """
    warns: list[str] = []
    for e in hives:
        path = registry.hive_dir(e)
        if not (path / ".git").exists():
            continue
        d = channels.scan(path)
        if d is None:
            continue
        prefix = str(e.get("prefix", "")) or f"{e.get('org')}/{e.get('repo')}"
        warns += [_channel_off_tag_warning(prefix, name, d) for name in d["off_tag"]]
        if d["off_tag"] or not d["behind_releases"]:
            # Off-tag already reported and there is no meaningful position in the release order to
            # measure lag from; a channel that is level is nothing to say.
            continue
        max_days = config.release_channel_stale_days(cfg, e)
        max_releases = config.release_channel_stale_releases(cfg, e)
        reasons = []
        if max_days and d["behind_days"] >= max_days:
            reasons.append(
                f"the oldest unpromoted release {d['oldest_unpromoted']} has been sitting for "
                f"{int(d['behind_days'])} days (release.channel_stale_days={max_days})"
            )
        if max_releases and d["behind_releases"] >= max_releases:
            reasons.append(f"release.channel_stale_releases={max_releases}")
        if reasons:
            warns.append(_channel_stale_warning(prefix, d, " and ".join(reasons)))
    return warns


def _disarmed_signing_gate_warnings(cfg, hives) -> list[str]:
    """Name hives whose signing gate is OFF on a host that could actually enforce it (bh-y3lp).

    Defect A of that bead: bh HAS a merge-time signature gate, `work.enforce_signing` defaults
    FALSE, and so the protection never ran — the only thing that noticed two unsigned commits
    was GitHub's branch rule, at push time, 31 commits and roughly a day later.

    The default is NOT flipped, and that stays deliberate: verification needs
    `gpg.ssh.allowedsignersfile` pointing at a real file with this host's key enrolled, and
    without one git reports even a perfectly-signed commit as N or U — never G — so a default
    of `true` would refuse EVERY merge on an unprepared fleet (see
    `config_schema.WorkConfig.enforce_signing`). The gap this closes is narrower: a host that
    HAS the full trust chain is one for which the gate costs nothing, and leaving it disarmed
    THERE is the state that produced the bug. So this fires only when
    `git_identity.signing_summary()` says this host's commits would verify as G — never as
    noise on a host that could not enforce it anyway."""
    from . import git_identity

    ok, _detail = git_identity.signing_summary()
    if not ok:
        return []  # this host can't verify its own signatures; arming the gate would only block
    disarmed = sorted(
        str(e.get("prefix") or "?")
        for e in hives
        if isinstance(e, dict) and not config.enforce_signing(cfg, e)
    )
    if not disarmed:
        return []
    return [
        f"{len(disarmed)} hive(s) have `work.enforce_signing` OFF on a host whose signatures "
        f"verify as G: {', '.join(disarmed)} — the merge-time signature gate exists and is "
        f"disarmed, so an unsigned commit is caught by the forge at push time (if at all) "
        f"instead of at merge. Arm it: "
        f"`{config.BINARY_ALIAS} config set work.enforce_signing true`"
    ]


def _orphaned_dolt_server_warnings() -> list[str]:
    """Name a running dolt sql-server whose data directory has been DELETED (bh-xonqg).

    Measured on beadhive-factory: a server started 2026-08-05 survived a deliberate host
    wipe-and-reinstall, its datadir was unlinked underneath it, and it kept LISTENING on
    127.0.0.1:3308 for ~30 hours. Every client that checks liveness by connecting saw a
    healthy server and then operated against files that no longer exist — surfacing as a hang,
    or as "no beads database found", neither of which points at the process.

    The check is deliberately NOT "does the port answer", because that is the thing that
    lied. Linux keeps a deleted cwd visible as ``/proc/<pid>/cwd -> <path> (deleted)``, which
    is a direct filesystem fact about the running process rather than an inference from its
    behaviour. Best-effort and Linux-only: a platform without ``/proc`` returns nothing rather
    than guessing, and this stays silent on a healthy host.

    Detection only — this does NOT reconcile config/filesystem/process or make provisioning
    idempotent across a wipe (both still open on bh-xonqg). Detection is the part that turns
    30 hours into a `bh doctor` line."""
    import re
    import shutil
    import subprocess

    proc = Path("/proc")
    if not proc.is_dir() or not shutil.which("pgrep"):
        return []  # not Linux, or no way to enumerate — say nothing rather than guess
    res = subprocess.run(  # noqa: S603 — fixed argv, no shell
        ["pgrep", "-f", "dolt sql-server"], capture_output=True, text=True, check=False
    )
    orphans: list[str] = []
    for pid in (res.stdout or "").split():
        if not re.fullmatch(r"\d+", pid):
            continue
        try:
            cwd = (proc / pid / "cwd").resolve(strict=False)
        except OSError:
            continue  # another user's process, or it exited between listing and reading
        # A deleted cwd resolves to a path that no longer exists — the zombie's signature.
        if not cwd.exists():
            orphans.append(f"pid {pid} (datadir {cwd} is gone)")
    if not orphans:
        return []
    return [
        f"{len(orphans)} dolt sql-server process(es) are running on a DELETED data directory: "
        f"{'; '.join(orphans)} — the port still accepts connections, so clients see a healthy "
        f"server and then operate against files that no longer exist (hangs, or 'no beads "
        f"database found'). This survives a host wipe. Stop the process, then re-provision "
        f"the store: `{config.BINARY_ALIAS} host provision`"
    ]


def _missing_required_dep_warnings() -> list[str]:
    """Name every ALWAYS-required dep that is not resolvable on PATH (bh-7m2h9).

    Doctor used to die here instead of reporting it: `bd` off PATH raised FileNotFoundError out
    of the first read that went through it, so the one tool whose job is diagnosing a broken seat
    was the one that failed with a traceback on a broken seat. `run.run` now hands a `check=False`
    caller exit 127 rather than raising, which stops the crash — but a silent 127 would only turn
    a loud failure into a quiet wrong answer, and that already cost a survey a FALSE finding (a
    hive reported as missing a checkout, which was really doctor dying before it could evaluate
    the check). So the absence is stated outright, ahead of anything derived from it.

    Local and cheap, matching this section's rule that nothing here does a remote round trip.
    """
    import shutil

    from . import deps

    missing = [d for d in deps.always_required() if not shutil.which(d.binary)]
    if not missing:
        return []
    profile_bin = Path.home() / ".nix-profile" / "bin"
    out = []
    for d in missing:
        where = (
            f" — it IS installed at {profile_bin / d.binary}, so this is a PATH problem, not a "
            f"missing install: add {profile_bin} to PATH"
            if (profile_bin / d.binary).exists()
            else " — install it (`bh setup check` lists the toolchain and how to get it)"
        )
        out.append(
            f"required dep '{d.binary}' is NOT on PATH{where}. Every check that reads through "
            f"it is unreliable until this is fixed — treat the rest of this report as partial"
        )
    return out


def _devshell_only_warnings() -> list[str]:
    """Surface deps reachable ONLY from inside this `nix develop` shell (bh-ytqc).

    Measured on beadhive-factory 2026-08-05, right after a SUCCESSFUL `just local-install`:
    `bh setup check` reported 4 of 4 inside `nix develop` and 0 OF 4 outside it. That state is
    invisible from where you are standing — the tools are plainly there — and surfaces much
    later as an unattended job (cron, systemd, `ssh host bh sync`) failing with tools "missing"
    that a human can see. `nix develop` stays a supported entry point for local-install, so bh
    has to NAME this rather than leave it to be rediscovered.

    Local and cheap, matching this section's rule that nothing here does a remote round trip:
    `shutil.which` against the current PATH plus one `.exists()` per dep. `~/.nix-profile/bin`
    is the directory that SURVIVES leaving the shell — it is already on a provisioned host's
    PATH — so a dep resolvable now and absent from there is exactly one that vanishes on exit.
    """
    import os
    import shutil

    from . import deps

    if not os.environ.get("IN_NIX_SHELL"):
        return []
    profile_bin = Path.home() / ".nix-profile" / "bin"
    stranded = [
        d.binary
        for d in deps.always_required()
        if shutil.which(d.binary) and not (profile_bin / d.binary).exists()
    ]
    if not stranded:
        return []
    return [
        f"{len(stranded)} dep(s) are visible only inside this `nix develop` shell and NOT in "
        f"{profile_bin}: {', '.join(stranded)} — cron, systemd units and `ssh <host> "
        f"{config.BINARY_ALIAS} sync` all get the bare PATH and will report them missing. "
        f"Install the toolchain into the user profile: `nix profile install .#default` "
        f"(what `just local-install` step 1 now runs)"
    ]


def _bd_schema_skew_warnings(cfg, hives, root: Path) -> list[str]:
    """Surface a hive whose recorded bd schema version is AHEAD of what THIS host's bd
    supports (`bh-wnly`) — the preflight bh-00cq's own 306MB-clone-then-fails-to-open incident
    argued for.

    `bh doctor` is this bead's chosen refresh trigger (see `hive_schema`'s module docstring for
    why, over `bh hive sync` / every write-shaped `bh work` verb): for every registered hive
    with a local checkout — this loop already exists just above, for other per-hive checks —
    probe its real schema version (`dolt_health.probe_raw_schema_version`, never the two decoy
    `schema_version` fields the module docstring warns about) and persist it
    (`hive_schema.refresh`). A probe failure this run still reads back whatever was PREVIOUSLY
    recorded (`hive_schema.try_load`) rather than going silent — `is_stale` marks that reading
    as unverified-since rather than a fresh green light (AC4).

    Local and cheap (module docstring's cost note): the embedded-mode probe is a direct `dolt
    sql` against an on-disk directory this host already has, no network involved — the same
    cost class as the `git ls-files` / grant checks this loop already pays per hive.

    Guarded on a real HQ existing FIRST, before anything else runs: `hive_schema` reads/writes
    live under `hq_dir`, so with no HQ there is nowhere to persist or compare against and this
    whole check is a no-op by construction — same "na" posture `hive_ready`'s sibling check
    takes. This also keeps every OTHER `bh doctor` test (furnish drift, validate_cmd, ...) that
    doesn't set up an HQ from paying a real `bd`/`dolt` subprocess cost it has nothing to do
    with.

    `is_stale(record)` is consulted UNCONDITIONALLY, not only when an advisory already fired —
    a review-caught AC4 regression: staleness matters most precisely when it's the ONLY signal
    (this run's re-probe failed, and the last CONFIRMED value showed no skew) — that is exactly
    "a newer bd on another host may have advanced the real store since, and we can't tell right
    now", which must never render as silence (a false all-clear), not just as a footnote on an
    already-detected skew.

    ENTRIES ARE FILTERED *BEFORE* THE LOCAL PROBE (bh-zzoek): with nothing to compare against —
    no registered hive has a local checkout — there is nothing this whole check can say, so it
    must not pay `local_bd_schema_version`'s cold cost (the ~5-7s throwaway `bd init`,
    `dolt_health._scratch_probe_local_version`) to learn an answer no comparison will use. This
    is the literal "does doctor need the value at all" question `bh-zzoek` asked: only when
    there's a hive to compare against does it. A live fleet with a real HQ almost always has at
    least one such hive (this repo's own fleet does, 20/20 as of that bead), so this is a
    correctness fix for the genuinely-empty case, not a measured win on today's numbers."""
    hq_dir = config.hq_dir()
    if not (hq_dir / ".beads").is_dir():
        return []

    _paths = [(e, root / e["provider"] / e["org"] / e["repo"]) for e in hives]
    entries = [(e, path) for e, path in _paths if (path / ".beads").is_dir()]
    if not entries:
        return []

    local = dolt_health.local_bd_schema_version()
    if local.version is None:
        return []  # can't judge what THIS bd supports — nothing to compare (dolt_fix_advisory's
        # own precedent: stay silent rather than warn off an unconfirmed premise)

    # SHAPE A (bh-0gvs3): every server-mode hive's `schema_migrations` version in ONE
    # cross-database query — measured 14 hives in 0.267s against ~280ms per `bd sql` spawn.
    # `schema_migrations` is a stored table and the per-hive path already reads it with
    # `bd sql`, so this is the same query asked once instead of N times, with no bd-side
    # derivation to reimplement (see dolt_health.bulk_schema_versions' classification note).
    # PARTIAL BY CONSTRUCTION: embedded-mode hives are absent and fall back to shape B below.
    # RECORDED, never derived (bh-g5ujg's rule, and bh-td8t9 backfilled the fleet so this is
    # not a narrower set in practice): `server_database` FALLS BACK to `dolt_database` for a
    # keyless hive, and for a hive whose metadata is unreadable that fallback is the directory
    # name — a guess. A guessed name that names no database on the server fails the UNION, and
    # one bad hive would drop every hive back to the per-hive path. Asking only for what is
    # written down keeps a broken hive's blast radius to that hive.
    bulk = dolt_health.bulk_schema_versions(
        [
            (path, store_locator.recorded_server_database(path))
            for _e, path in entries
            if not store_locator.is_embedded_mode(path)
        ]
    )

    def _probe(item: tuple[dict, Path]) -> tuple[hive_schema.HiveSchemaRecord | None, str | None]:
        e, path = item
        dolt_mode = safety._bd_dolt_mode(str(path))
        # WRITE PATH: `refresh` -> `hive_schema.save` writes ONE file per hive
        # (hives/<provider>/<org>/<repo>.yaml) — no two workers ever target the same path, so
        # there is no file-level race to serialize. The shared, mutable state that IS on this
        # path: `hive_schema`'s module-level `ruamel.yaml.YAML()` (lock-guarded, mirroring
        # config.py's bh-3qo60 fix) AND `host.py`'s own `ruamel.yaml.YAML()` singleton, reached
        # via `refresh` -> `host.host_id()` -> `load()` — a THIRD instance of the same
        # bh-3qo60 pattern, now lock-guarded the same way (`host.py`'s `_yaml_lock`). Both are
        # safe to call from a pool as-is.
        _refreshed, fail_detail = hive_schema.refresh_with_detail(
            path,
            e["provider"],
            e["org"],
            e["repo"],
            hq_dir=hq_dir,
            dolt_mode=dolt_mode,
            probed=bulk.get(path),
        )
        # ALWAYS read back, even on a failed refresh: refresh writes nothing when the probe
        # fails, but a PRIOR record may already be on disk from an earlier successful run —
        # that prior record is still the last known-true observation and must be reported
        # (possibly stale), not discarded just because THIS run's probe didn't confirm it.
        record = hive_schema.try_load(hq_dir, e["provider"], e["org"], e["repo"])
        if record is not None:
            return record, None
        if fail_detail is None and _refreshed is not None:
            # The probe itself succeeded and refresh wrote a record, but the read-back came up
            # empty anyway (e.g. a lost write) — say so instead of rendering "(no detail)" next
            # to a message that claims the hive was never probed.
            fail_detail = (
                "probe succeeded and wrote a record, but reading it back immediately failed"
            )
        return record, fail_detail

    # `_bd_dolt_mode` (`bd dolt status`) and `refresh`'s probe (`bd sql schema_migrations`) are
    # independent, per-hive subprocess calls (bh-ti7ws: 15 hives, 1.84s + 2.41s sequential).
    # Neither goes through `bd.run`/`bd.json` (bh's in-process wrapper) — both call
    # `run.run`/`subprocess.run` directly — so this loop never reads `bd._STRICT_READS`
    # (a ContextVar, invisible to pool workers) and can't silently defeat `bd.strict_reads()`.
    # SHAPE B (`fleet.fanout`): `bd dolt status` is bd's own report on a store, not a row the
    # server serves, so the bulk shape does not apply.
    results = fleet.fanout(_probe, entries)

    warns: list[str] = []
    for (e, _path), (record, fail_detail) in zip(entries, results, strict=True):
        if record is None:
            # NEVER successfully probed for this hive — `hive_schema.refresh` writes nothing on
            # a failed probe, so with no prior record either there is nothing to compare
            # against AND nothing under hq/hives for this hive. Silence here is exactly the
            # bh-j50yv failure mode: a fully-onboarded hive that reads as "doesn't exist" to
            # anything (e.g. QM) that enumerates the fleet from hq/hives. Say so.
            warns.append(
                f"\u26a0 hive '{e['prefix']}': schema version could not be probed "
                f"({fail_detail or 'no detail'}) and no prior observation is recorded \u2014 "
                "this hive is NOT represented under hq/hives yet"
            )
            continue

        advisory = dolt_health.schema_skew_advisory(str(e["prefix"]), local, record.schema_version)
        stale = hive_schema.is_stale(record)
        if not advisory and not stale:
            continue  # confirmed recently, no skew — a genuine, timestamped all-clear

        age_days = hive_schema.age_seconds(record) / 86400.0
        if advisory:
            if stale:
                advisory += (
                    f" [recorded {age_days:.1f}d ago and this run's re-probe failed — "
                    "unverified since; the real gap may be larger]"
                )
            warns.append(advisory)
            continue

        # No CONFIRMED skew, but the record is stale (this run's re-probe failed, and the last
        # verified read is old) — the central AC4 case: say so, don't stay silent.
        warns.append(
            f"⚠ hive '{e['prefix']}': recorded schema version v{record.schema_version} was "
            f"last confirmed {age_days:.1f}d ago and this run's re-probe failed — unverified "
            f"since against this bd's v{local.version}; the real gap may be larger than known"
        )
    return warns


def _bd_dolt_fix_warnings() -> list[str]:
    """Surface a `bd` whose EMBEDDED dolt predates the beads#4770 fix (bh-gnqc).

    Also reported by `bh setup check`, and deliberately repeated here: setup check is a
    once-then-cached gate, so an operator who passed it before upgrading bd never sees that
    warning again. The bug shows up as bead sync HANGING, and `doctor` is what someone runs
    when something is stuck — so it has to be visible from here too.

    Local and cheap: reuses the same probe/parse `setup` already performs (a single
    `bd --version`), no network and no store access, matching this section's rule that nothing
    here does a remote round trip."""
    from . import deps
    from . import setup as setup_mod

    # The bd ROW, not a hand-written copy of it (bh-hsus.3): a doctor that re-declares how to
    # probe bd is one more place for that fact to drift from the table everything else reads.
    probe = setup_mod.probe_one(*setup_mod.probe_row(deps.by_name("bd")))
    advisory = setup_mod.dolt_fix_advisory(probe.get("version"))
    if not advisory:
        return []
    # Collapse to one line: this section is a flat list, and the multi-line form belongs to
    # `setup check`, where there is room to lay the escapes out.
    return [
        f"bd {probe['version']} embeds dolt < {setup_mod.DOLT_FIX_VERSION} — `bd dolt pull` can "
        f"hang indefinitely on a large store (beads#4770); run "
        f"`{config.BINARY_ALIAS} setup check` for the fix options"
    ]


def _hq_ahead_warnings(cfg) -> list[str]:
    """Surface HQ's git half being ahead of its wired remote (bh-z9hl's acceptance: `bh doctor`
    or `bh hive ready` must show a drifted HQ, not just `bh hq status`). Read-only, no network:
    `safety.scan(hq_dir)` (default `fetch=False`) reads cached remote-tracking refs only —
    matching every other check in this section (see `_local_commits_while_not_primary`'s own
    "no ls-remote, no fetch, no HQ round trip" rule). The Dolt half needs a real network fetch
    to verify (`bd federation status`) and is deliberately left to `bh hq status`/`bh hq push`
    instead of paying that cost on every `bh doctor` run."""
    if registry.hive_of_kind(cfg, registry.HQ_KIND) is None:
        return []
    hq_dir = config.hq_dir()
    if not (hq_dir / ".git").exists():
        return []
    branch = next((b for b in safety.scan(hq_dir).branches if b.name == "main"), None)
    if branch is None or not branch.has_upstream or not branch.ahead:
        return []
    return [
        f"HQ ({hq_dir}): main is {branch.ahead} commit(s) ahead of origin/main (as of the last "
        f"fetch) — run `{config.BINARY_ALIAS} hq push`"
    ]


def _render_warnings(warns: list[str]) -> None:
    typer.echo(f"\n# Warnings ({len(warns)})")
    for w in warns:
        typer.echo(f"  ⚠ {w}")
    if not warns:
        typer.echo("  ✓ none")


# ---- collect + payload + render ---------------------------------------------


def _timed(timings: dict, key: str, fn, *args, **kwargs):
    """Call ``fn`` and record its wall-clock cost into ``timings[key]`` (ms, monotonic clock).

    The only instrumentation `_collect` gets: no spans, no context, just a stopwatch around
    each section builder so `bh-13spb.1` can attribute doctor's total without guessing.
    """
    t0 = time.monotonic()
    result = fn(*args, **kwargs)
    timings[key] = round((time.monotonic() - t0) * 1000, 3)
    return result


def _collect(cfg, *, full_seats: bool = False) -> dict:
    """Gather the full diagnostics dict, section by section, from the shared inputs.

    Reuses ``metadata.read_fleet`` / ``registry.*`` / ``gitworkspace.*`` and runs the metadata
    rollup ONCE (Disk Usage + Fleet Health share it, so no repo is disk-walked twice). Pure data:
    makes no ``typer.echo`` calls, returns a JSON-able dict keyed by section, plus a
    ``timings`` key (section name -> milliseconds, plus ``total``) from a monotonic clock.
    """
    root = Path(workspace_root())
    hives = cfg.get("managed_repos", []) or []
    timings: dict[str, float] = {}
    t_start = time.monotonic()

    # ---- inventory intermediates (also feed disk usage, fleet health, warnings) ----
    hive_keys = {f"{e['provider']}/{e['org']}/{e['repo']}" for e in hives}
    git_repos, nonrepo, unknown_top = _timed(
        timings, "scan", _scan, root, registry.effective_providers(cfg)
    )
    tracked = _timed(timings, "tracked", _tracked, root)
    universe = tracked if tracked is not None else git_repos
    excluded = {k for k in git_repos if registry.is_excluded(cfg, *k.split("/"))}
    candidates = {
        k for k in universe if k not in hive_keys and not registry.is_excluded(cfg, *k.split("/"))
    }
    untracked = (git_repos - tracked) if tracked is not None else set()

    inventory = {
        "hives_registered": len(hive_keys),
        "git_repos_on_disk": len(git_repos),
        "onboarding_candidates": len(candidates),
        "excluded": len(excluded),
        "untracked_git_repos": (len(untracked) if tracked is not None else None),
        "non_repo_folders": len(nonrepo),
        "unrecognized_top_dirs": len(unknown_top),
    }

    # ---- single metadata rollup (Disk Usage + Fleet Health share it) ----
    hive_keys_on_disk = {
        f"{e['provider']}/{e['org']}/{e['repo']}"
        for e in hives
        if (root / e["provider"] / e["org"] / e["repo"]).exists()
    }
    records = _timed(
        timings,
        "metadata_rollup",
        metadata.read_fleet,
        cfg,
        sorted(git_repos | hive_keys_on_disk),
        ttl=metadata.ttl(cfg),
    )

    data = {
        "config": _timed(timings, "config", _data_config, cfg, root),
        "providers": _timed(timings, "providers", _data_providers, cfg),
        "orgs": _timed(timings, "orgs", _data_orgs, cfg),
        "hives": _timed(timings, "hives", _data_hives, cfg),
        "inventory": inventory,
        "disk_usage": _timed(timings, "disk_usage", _data_disk_usage, hives, root, records),
        "fleet_health": _timed(timings, "fleet_health", _data_fleet_health, records, git_repos),
        "worktrees": _timed(timings, "worktrees", _data_worktrees, cfg),
        "molecules": _timed(timings, "molecules", _data_molecules, cfg),
        "prefix_mismatches": _timed(timings, "prefix_mismatches", _data_prefix_mismatches, cfg),
        "node_id": _timed(timings, "node_id", _data_node_id, cfg),
        "beads_role": _timed(timings, "beads_role", _data_beads_role, cfg),
        "store_engine": _timed(timings, "store_engine", _data_store_engine, cfg),
        "dispatch": _timed(timings, "dispatch", _data_dispatch, cfg),
        "group_auth": _timed(timings, "group_auth", _data_group_auth, cfg),
        "mcp": _timed(timings, "mcp", _data_mcp, cfg),
        "seats": _timed(timings, "seats", _data_seats, cfg, full=full_seats),
        "install": _timed(timings, "install", _data_install, cfg),
        "observability": _timed(timings, "observability", _data_observability, cfg),
        "warnings": _timed(
            timings,
            "warnings",
            _data_warnings,
            cfg,
            root,
            hives,
            git_repos,
            nonrepo,
            unknown_top,
            untracked,
        ),
    }
    timings["total"] = round((time.monotonic() - t_start) * 1000, 3)
    data["timings"] = timings
    return data


def doctor_payload(*, full_seats: bool = False) -> dict:
    """Structured `ws doctor` diagnostics — the data layer beneath the text render.

    Returns a JSON-able dict keyed by section (``config``, ``providers``, ``orgs``, ``hives``,
    ``inventory``, ``disk_usage``, ``fleet_health``, ``worktrees``, ``molecules``,
    ``prefix_mismatches``, ``node_id``, ``beads_role``, ``group_auth``, ``mcp``, ``seats``,
    ``install``, ``observability``, ``warnings``), plus ``timings`` (section name -> milliseconds
    from a monotonic clock, plus ``total`` — bh-8nnh7, metadata for attributing doctor's cost,
    always present regardless of ``--json``/verbosity), under the ``schema_version`` / ``command``
    envelope (:mod:`beadhive.jsonout`). ``seats`` is ``None`` when hitch is disabled/absent
    (bh-og0q.4's silent-when-unused bar) — every other key is always present. By default
    ``seats`` does NOT run the per-seat `hitch profile preflight` fanout (bh-gqfrm) — pass
    ``full_seats=True`` for the complete per-seat breakdown; the cheap default still says
    explicitly that per-seat detail was skipped. Exposed as the
    ``beadhive://doctor`` MCP resource; ``doctor()`` renders the same builders so the text
    output never drifts from this payload.

    THE ENVELOPE IS ADDED HERE, NOT AT THE CLI EDGE (bh-0olv9.2). Wrapping it inside
    ``doctor_cmd`` would give ``bh doctor --json`` a schema version and leave the MCP resource
    — the same object, read by the same kind of consumer — without one, which is two shapes for
    one payload. This is the only place the object is built, so it is the only place that can
    carry the version.
    """
    return jsonout.envelope(
        "doctor", jsonout.DOCTOR_SCHEMA, _collect(config.load(), full_seats=full_seats)
    )


def show():
    """Pretty-print the resolved config: the doctor overview + config-only sections."""
    cfg = config.load()
    root = Path(workspace_root())
    _overview(cfg, root)
    _section_dimensions(cfg)
    _section_exclude(cfg)
    _section_dolt(cfg)
    _section_worktrees(cfg)
    _section_group_auth(cfg)
    _section_provenance()
    _section_config_problems(cfg)


def _render_timings(timings: dict) -> None:
    """Verbose-only: per-section wall-clock cost from `_collect`'s monotonic stopwatch
    (bh-8nnh7). Silent by default — printing a column of numbers nobody asked for is the
    thing `bh-13spb.1`'s stakes note explicitly rules out."""
    typer.echo("\n# Timings (ms)")
    total = timings.get("total")
    for key, ms in sorted(timings.items(), key=lambda kv: kv[1], reverse=True):
        if key == "total":
            continue
        typer.echo(f"  {ms:>8.1f}  {key}")
    if total is not None:
        typer.echo(f"  {total:>8.1f}  total")


def doctor(as_json: bool = False, verbose: bool = False, seats: bool = False):
    """Render the full `ws doctor` report from the structured payload.

    ``as_json`` emits :func:`doctor_payload` — the SAME object the renders below consume, not a
    parallel assembly of it — and nothing else on stdout (it always carries ``timings``).
    ``verbose`` additionally prints the per-section timings breakdown in text mode; the default
    text report is unchanged (bh-8nnh7). ``seats`` (bh-gqfrm) opts into the full per-seat
    `hitch profile preflight` fanout (~2.7s) — the default report checks that hitch itself is
    usable and says explicitly that per-seat detail was skipped, rather than silently omitting it.
    """
    data = doctor_payload(full_seats=seats)
    if as_json:
        jsonout.emit(data)
        return
    _render_config(data["config"])
    _render_providers(data["providers"])
    _render_orgs(data["orgs"])
    _render_hives(data["hives"])
    _render_inventory(data["inventory"])
    _render_disk_usage(data["disk_usage"])
    _render_fleet_health(data["fleet_health"])
    _render_worktrees(data["worktrees"])
    _render_molecules(data["molecules"])
    _render_prefix_mismatches(data["prefix_mismatches"])
    _render_node_id(data["node_id"])
    _render_beads_role(data["beads_role"])
    _render_store_engine(data["store_engine"])
    _render_dispatch(data["dispatch"])
    _render_group_auth(data["group_auth"])
    _render_mcp(data["mcp"])
    _render_seats(data["seats"])
    _render_install(data["install"])
    _render_observability(data["observability"])
    _render_warnings(data["warnings"])
    if verbose:
        _render_timings(data["timings"])
