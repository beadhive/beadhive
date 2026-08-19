"""The hydration hub: one aggregated beads DB (under $BH_HOME) holding a cross-hive
view of every registered hive.

`bh sync` builds/refreshes it — cloned hives are added by local path; uncloned hives are
fetched into a minimal-clone cache (blobless, no working tree) via `bd bootstrap`, then
added. `bh hub <bd cmd>` queries it. So the aggregate works whether or not a hive's code
is checked out, and `bh` itself needs no repo cloned beyond the caches.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import threading
import time
from pathlib import Path

import typer

from . import bd, config, engine, gitworkspace, guard, hive, registry, store_locator
from .run import ChildTimeout, run
from .run import bounded as run_bounded

# Mirrors `storage_migrate.SHARED_SERVER_FLAG` / `.SHARED_SERVER_CONFIG_KEY` — NOT re-imported
# from there: `storage_migrate` imports `hq`, which imports `hub` back (a real, if currently
# import-order-tolerant, cycle). Two short string literals duplicated is a smaller risk than a
# fragile cross-module cycle.
_SHARED_SERVER_FLAG = "--shared-server"
_SHARED_SERVER_CONFIG_KEY = "dolt.shared-server"


def _bd_ni_env() -> dict:
    """`os.environ` + `BD_NON_INTERACTIVE=1`, read FRESH on every call — never a module-level
    snapshot (bh-areg.7's review, round 3). The former `_BD_NI = {**os.environ, ...}` constant
    was computed once at import time, before any per-invocation env override (a test's own
    isolation fixture setting `BEADS_SHARED_SERVER_DIR`/`BEADS_DOLT_SERVER_PORT`; an operator
    changing their shell mid-process) could apply — every real `bd` call below would silently
    fall through to whatever the ambient environment was at first import instead. That is a
    live path back into the operator's production shared server, the exact hazard this
    bead's own `_sandbox_shared_server` test fixture exists to close off; nothing this module
    does should be able to route around it."""
    return {**os.environ, "BD_NON_INTERACTIVE": "1"}


def bootstrap_env() -> dict:
    """`_bd_ni_env()` plus the per-invocation shared-server activation a `bd bootstrap` needs —
    it has no `--shared-server` flag of its own (unlike `bd init`), so activation goes through
    this env var instead (the same seam `onboard.py`'s zero-footprint bootstrap branch already
    uses). Every fresh mint targets the fleet's shared-server mode by default now
    (`docs/design/dolt-server-mode-adr.md` / bh-ukit.4 — not per-hive opt-in), so any hydration
    via `bd bootstrap` needs to activate it too, exactly like a live `bd init --shared-server`
    already does — `_fetch_cache` (bh-hpeye) and `hq.clone` were the two `bd bootstrap` call
    sites that didn't."""
    return {**_bd_ni_env(), "BEADS_DOLT_SHARED_SERVER": "1"}


def landed_on_shared_server(store) -> bool:
    """Whether `store`'s database is REALLY present under bd's shared-server datadir
    (`store_locator.server_store_dir()`) — a filesystem fact, no subprocess and no live probe,
    the same discipline `store_locator` itself keeps.

    Both names a store can resolve through are checked (`dolt_database`, bd's own field, and
    `dolt_server_database`, bh's additive one) because a store mid-bootstrap may carry either.
    Absent/unreadable metadata resolves to the store directory's own name, which no shared
    database is normally called — so the answer fails toward "not on the server", i.e. toward
    leaving bd's own persisted mode alone rather than overwriting it with a guess."""
    root = store_locator.server_store_dir()
    names = {store_locator.dolt_database(store, ""), store_locator.server_database(store, "")}
    return any(name and (root / name).is_dir() for name in names)


def persist_shared_server_mode(store) -> None:
    """Belt-and-suspenders durability after a fresh `bd bootstrap` lands `store` on server mode
    — mirrors `onboard._ensure_server_mode_persisted` (same reasoning: a per-invocation
    activation — `BEADS_DOLT_SHARED_SERVER=1` above — is NOT durable on its own; bd's own
    `main.go:warnSharedServerEmbeddedMismatch` documents exactly this class of drift). Re-
    asserts `dolt_mode` in `.beads/metadata.json` (pure file op, no subprocess — see
    `store_locator.ensure_server_mode_persisted`) and `dolt.shared-server` in
    `.beads/config.yaml` (a real `bd config set`, same belt-and-suspenders step `ensure_store`
    already takes after a fresh `bd init` above).

    REFUSES on a store the bootstrap did not actually put on the shared server (bh-qpa3g). This
    used to assert server mode UNCONDITIONALLY, on the assumption that activating
    `BEADS_DOLT_SHARED_SERVER=1` guaranteed the bootstrap honored it. Measured, it does not: when
    the target database name already exists `bd bootstrap` declines outright (bh-8g6cj), leaving
    the store half-migrated — server metadata and server scaffolding, data still in
    `.beads/embeddeddolt/` — which is precisely the `database "ag_hp" not found` state four cache
    stores on this host were left in. A mode assertion is a RECORD of what happened, never a
    wish: :func:`landed_on_shared_server` checks the database really is under bd's shared-server
    datadir before anything is written."""
    if not landed_on_shared_server(store):
        typer.echo(
            f"⚠ beads: {store} has no database under {store_locator.server_store_dir()} — the "
            "bootstrap did not land it on the shared server, so its dolt_mode is left as bd "
            "actually wrote it (bh-qpa3g).",
            err=True,
        )
        return
    if store_locator.ensure_server_mode_persisted(store):
        # Defensive path only — a fresh, non-`--reinit-local` bootstrap already persists this
        # correctly on its own (measured, bh-areg.4/onboard.py). Fix it visibly, never
        # silently, matching `ensure_store`'s identical warning below.
        typer.echo(
            '⚠ beads: dolt_mode was not persisted by bd bootstrap — wrote dolt_mode="server" '
            "directly to .beads/metadata.json so a restore never trusts a stale mode.",
            err=True,
        )
    run(
        ["bd", "-C", str(store), "config", "set", _SHARED_SERVER_CONFIG_KEY, "true"],
        check=False,
        capture=True,
    )


# bd's idempotent re-add refusal — expected on every re-sync, not an error.
_ALREADY_CONFIGURED = "already configured"


def _output(res) -> str:
    """Combined stdout+stderr of a captured CompletedProcess, stripped."""
    return ((res.stdout or "") + (res.stderr or "")).strip()


# Returned by `sync()` (as the sole "failed" entry) when `hub.bulk_sync` is explicitly disabled.
# NOT a hive prefix — callers that render the failed list must special-case it rather than
# reporting "1 hive(s) failed to hydrate", which would be actively misleading (bh-l7sm8).
BULK_SYNC_DISABLED = "<hub.bulk_sync disabled>"


def _registered_repo_paths(hub) -> list[str]:
    """Additional repo paths registered in the hub (``bd repo list``), excluding the
    primary ``.``. Parses the human listing (``- <path>`` lines); ``--json`` is a no-op
    for this bd verb. Empty on a listing failure — reconcile then no-ops safely."""
    res = run(["bd", "-C", str(hub), "repo", "list"], check=False, capture=True)
    if res.returncode:
        return []
    return [
        line[2:].strip()
        for line in (line.strip() for line in _output(res).splitlines())
        if line.startswith("- ")
    ]


def _managed_repo_paths(cfg, managed) -> set[str]:
    """Every path a managed hive can be registered under — its live checkout (hive_dir)
    and its blobless cache — so a registration matching neither is genuinely stale."""
    desired: set[str] = set()
    for e in managed:
        desired.add(str(registry.hive_dir(e)))
        desired.add(str(config.cache_dir() / e["provider"] / e["org"] / e["repo"]))
    return desired


def _is_cache_path(cfg, src) -> bool:
    """Whether `src` is a `_fetch_cache` hydration artifact rather than a real checkout.

    `sync()` resolves each hive to a checkout OR a cache store and then treats them alike; the
    bulk path must NOT (bh-4o07n). Cache stores predating bh-qpa3g carry `dolt_mode: server`
    plus a `dolt_database` belonging to some other hive, so they look co-located by metadata
    alone while belonging to none — five of them shared one project_id AND one database. They
    carry their own per-hive database now (`cache_database`), but the distinction this answers
    is structural, not a property of what a store happens to carry: a cache is a derived
    hydration artifact and is never an authoritative co-located hive."""
    try:
        Path(src).resolve().relative_to(config.cache_dir().resolve())
    except (ValueError, OSError):
        return False
    return True


def _reconcile_removed(hub, cfg, managed, marks: dict[str, str] | None = None) -> None:
    """Drop hub registrations for repos no longer managed — a stale cache path left
    after a hive switched to its live checkout, or a hive that was removed/retired. Only
    the hub *registration* is dropped (``bd repo remove``); never the repo/hive itself.

    Also the home for pruning `marks` (bh-d5jhc.2's per-hive sync watermark bookkeeping,
    per this bead's own DESIGN note) when given: a retired hive's watermark must not
    silently outlive its registration — if the prefix were ever reused, a leftover mark
    could be misread as "unchanged" for beads it never actually saw."""
    desired = _managed_repo_paths(cfg, managed)
    for path in _registered_repo_paths(hub):
        if path in desired:
            continue
        rm = run(["bd", "-C", str(hub), "repo", "remove", path], check=False, capture=True)
        if rm.returncode:
            typer.echo(f"  ⚠ could not drop stale hub entry {path}: {bd.err_line(rm)}", err=True)
        else:
            typer.echo(f"  ✓ dropped stale hub entry: {path}", err=True)
    if marks is not None:
        live = {str(e["prefix"]) for e in managed}
        for prefix in [p for p in marks if p not in live]:
            del marks[prefix]


def ensure_store(store, prefix):
    """bd-init a local git+bd aggregation store at ``store`` (prefix ``prefix``) if absent, and
    return it. Shared by the legacy disposable hub and the durable Factory HQ — the one place
    the cross-hive aggregate is stood up.

    A FRESH store (this function only ever mints one; an existing one is untouched — see the
    ``.beads``-exists guard) lands on bd's shared server, same as the rest of onboarding
    (`docs/design/dolt-server-mode-adr.md` / `bh-ukit.4`: the default for every newly-minted
    store, HQ included, not per-hive opt-in). ``dolt_mode``/``dolt.shared-server`` are asserted
    durable the same way `onboard._ensure_server_mode_persisted` does — see that function's
    docstring for why a per-invocation flag alone isn't enough.

    A FAILED `bd init` is cleaned up (`hive.cleanup_failed_bd_init`) before this raises, so
    `.beads/` never exists here as wreckage from a prior failed call — a retry's own
    ``.beads``-exists guard is never fooled into skipping a real mint (bh-areg.7's own review
    finding: a busy dolt-server port can otherwise leave `.beads/` behind with no
    `metadata.json`, misreported as already initialized).

    The `bd init` call is NEVER captured (unlike this module's other `bd`/`bd repo` calls,
    which stay `capture=True` + `bd.err_line` for their own short, single-phase commands) —
    `--shared-server` is two-phase (a git-init phase that prints a SUCCESS line to stdout,
    THEN a dolt-server-start phase that can fail on stderr with the actually actionable
    message), and `err_line` reads `stdout + stderr` and returns the FIRST non-empty line —
    which would be the git-phase's own success line, silently swallowing the real error
    (bh-areg.7's own review finding, round 3: "the error above" pointed at nothing, because
    the quoted line was bd's "✓ Initialized git repository"). Streaming lets bd's own already-
    actionable message (port, offending PID, remediation) through untouched, matching
    `onboard._run_bd_mint`'s identical fix for the same two-phase shape."""
    if not (store / ".beads").is_dir():
        store.mkdir(parents=True, exist_ok=True)
        cmd = [
            "bd",
            "init",
            "--prefix",
            prefix,
            _SHARED_SERVER_FLAG,
            "--skip-agents",
            "--skip-hooks",
            "--non-interactive",
        ]
        try:
            res = run(cmd, cwd=str(store), env=_bd_ni_env(), check=False)
        except FileNotFoundError:
            typer.echo(
                "✗ `bd` not found on PATH — install beads before running "
                f"`{config.BINARY_ALIAS} sync`",
                err=True,
            )
            raise typer.Exit(1) from None
        if res.returncode:
            hive.cleanup_failed_bd_init(store)
            typer.echo(
                f"✗ bd init failed for {prefix} store {store} — bd's error is above. The "
                "incomplete .beads/ has been cleaned up; re-run once the underlying issue is "
                "resolved.",
                err=True,
            )
            raise typer.Exit(1)
        if store_locator.ensure_server_mode_persisted(store):
            typer.echo(
                '⚠ beads: dolt_mode was not persisted by bd init — wrote dolt_mode="server" '
                "directly to .beads/metadata.json so a restore never trusts a stale mode.",
                err=True,
            )
        run(
            ["bd", "-C", str(store), "config", "set", _SHARED_SERVER_CONFIG_KEY, "true"],
            check=False,
            capture=True,
        )
    return store


#: The hub store's bd prefix — deliberately NOT a plausible hive prefix (bh-89wxf.1).
#:
#: The hub is a DERIVED aggregate: everything in it arrives hydrated from some hive, carrying
#: that hive's own prefix and `source_repo`. It therefore has no business ISSUING an id, and a
#: store with a plausible prefix is a store whose leaked ids look ordinary. `bd init` demands a
#: prefix string, so the enforcement that matters is :func:`guard.guard_hub`, not the string —
#: but the string is chosen so any id that does leak self-identifies as a bug on sight
#: (`_HUB_ISSUES_NO_IDS-7`).
#:
#: "Obviously invalid" has to live inside bd's own alphabet: measured against bd 1.1.0,
#: `bd init --prefix '!hub'` is refused outright ("Database names must start with a letter or
#: underscore and contain only letters, digits, underscores, or hyphens"), so a punctuation
#: sentinel is not available. A leading underscore plus SHOUTING is: hive prefixes are lowercase
#: slugs derived from repo names (`registry.derive_prefix`), which can never produce this.
HUB_PREFIX = "_HUB_ISSUES_NO_IDS"

#: Prefixes a pre-bh-89wxf.1 hub was bd-init'd with. `"hub"` is exactly the plausible-looking
#: prefix this bead retired: it names a store, reads like a hive, and would have issued
#: `hub-1`-style ids indistinguishable from a real hive's.
_LEGACY_HUB_PREFIXES = frozenset({"hub"})

#: Sentinel for "this store records no database name" — see :func:`_retire_legacy_hub`.
_UNKNOWN_DATABASE = "\0"


def _aggregation_target():
    """``(dir, prefix)`` of the cross-hive aggregate: the durable Factory HQ store (kind=hq) once
    one is registered, else the legacy disposable hub (pre-HQ back-compat). HQ subsumes the hub —
    the aggregation role moves onto it — so hub.py points here, not at ``hub_dir()`` alone."""
    try:
        cfg = config.load()
    except FileNotFoundError:
        cfg = {}
    if registry.hive_of_kind(cfg, registry.HQ_KIND) is not None:
        return config.hq_dir(), registry.HQ_PREFIX
    return config.hub_dir(), HUB_PREFIX


def _retire_legacy_hub(store: Path) -> None:
    """Move a pre-bh-89wxf.1 hub (bd prefix `"hub"`) aside so `ensure_store` re-mints it
    prefix-less. The MIGRATION PATH for an existing host, and it is a REBUILD, not surgery: the
    hub is derived, so the whole cost of discarding one is the `bd repo sync` that follows.

    Detected by the recorded shared-server database name, which bd derives from the prefix —
    the prefix itself is held in the store's Dolt data, not in any file (checked: `.beads/
    config.yaml` carries only a commented-out `issue-prefix`). An embedded/unreadable store
    records no database name at all and is left ALONE: unknown is not the same as legacy, and
    wrongly retiring a store the operator still wants is the one irreversible mistake here.

    Renamed, never deleted — bh does not remove an operator's data unprompted, even data it
    considers disposable. The old shared-server database keeps its disk until they say so."""
    if not (store / ".beads").is_dir():
        return
    # `_UNKNOWN`, not `""`: `dolt_database`'s no-fallback default is the STORE DIRECTORY'S OWN
    # NAME — which is literally "hub" — so an empty fallback would read every metadata-less
    # store as legacy on the strength of its path alone.
    if store_locator.dolt_database(store, _UNKNOWN_DATABASE) not in _LEGACY_HUB_PREFIXES:
        return
    aside = store.with_name(f"{store.name}.legacy-{int(time.time())}")
    store.rename(aside)
    typer.echo(
        f"↻ retired the legacy hub at {store} (bd prefix 'hub' — a prefix-less aggregate is "
        f"the contract now, see docs/HUB.md) → moved to {aside}; rebuilding it from every "
        "hive's own remote. Nothing is lost: the hub is derived. Delete the moved copy (and "
        "its 'hub' shared-server database) to reclaim the space.",
        err=True,
    )


def ensure_hub():
    store, prefix = _aggregation_target()
    if store == config.hub_dir():
        _retire_legacy_hub(store)
    return ensure_store(store, prefix)


def _hive_url(cfg, entry):
    """Clone URL for a hive: exact from the git-workspace lock, else derive for github/gitlab.

    `entry['provider']` is the stored triplet's first segment (the repo-group path — see
    gitworkspace.RepoGroup); the derive-fallback below treats it as the provider TYPE directly
    (no group->type resolution), matching this hive's existing entries where the two coincide."""
    key = f"{entry['provider']}/{entry['org']}/{entry['repo']}"
    url = gitworkspace.repo_urls(cfg).get(key)
    if url:
        return url
    group_path, org, repo = entry["provider"], entry["org"], entry["repo"]
    if group_path == "github":
        return f"git@github.com:{org}/{repo}.git"
    if group_path == "gitlab":
        return f"git@gitlab.com:{org}/{repo}.git"
    return None


# ---------------------------------------------------------------------------
# Cache-store identity (bh-fnn3d / bh-qpa3g / bh-gbmyw — one defect, three beads)
#
# `_fetch_cache` bootstrapped every cache under `BEADS_DOLT_SHARED_SERVER=1` and then asserted
# server mode on the result. The REGISTERED PREFIX — the one identity bh already enforces
# unique fleet-wide — was never an input to the bootstrap's database resolution, so every store
# landed on bd's generic default. Measured on this host 2026-08-09: all five cache stores
# carried `dolt_database: ag_hp` (hermes-plugin's name) and ONE shared project_id, 8b177292.
# Three consequences, filed as three beads and fixed here as one:
#
#   * bh-fnn3d — a fresh host, whose only artifact IS a cache, had no path to a per-hive
#     database at all, so hydration failed (silently, exit 0);
#   * bh-qpa3g — each store's identity is overwritten on every sync;
#   * bh-gbmyw — five stores, one database, one project_id, nothing to tell them apart.
#
# THE DIRECT-TO-SHARED PATH WORKS, and is what bh-fnn3d asked for. Measured against real bd
# 1.1.0 with the fleet server up on 3308: seeding `.beads/metadata.json` with
# `dolt_mode: "server"` + `dolt_database: <sanitized prefix>` BEFORE `bd bootstrap` makes a
# blobless `--no-checkout` clone hydrate straight into `~/.beads/shared-server/dolt/<prefix>`
# ("Bootstrap plan: clone from remote / Database: dxnvh_probe … via server at 127.0.0.1:3308"),
# readable immediately. Seeding the name is the only lever there is: `bd bootstrap` resolves its
# target from that field and has no working `--database` override (bh-8g6cj). Without the seed
# it resolves bd's generic `beads` — re-measured, and it lands on the FLEET server too, which is
# exactly how five caches collapsed onto one database. This is `storage_migrate.migrate_hive`'s
# own repoint-then-bootstrap sequence, applied to the one bootstrap call site that lacked it.
#
# THE RE-STAMP IS bd's, NOT bh's — the mechanism bh-qpa3g measured from the outside ("repointing
# them by hand does not hold; the next `bh sync` re-stamps them"). Measured directly: with a
# stale `project_id` still in metadata.json, `bd bootstrap` prints `Bootstrap metadata repair:
# repaired dolt_database: "dxnvh" -> "ag_hp" using project_id 8b177292…` and puts the WRONG
# database name back, because that id is the one another hive's database already claims on the
# server. So the repair must retire the stale project_id along with the stale name; with it
# dropped, the same store bootstraps cleanly into its own database (measured, both ways).
#
# The `.beads/dolt-server-config.yaml` + rotating-port servers found on those caches are bd's
# OWNED mode, which `docs/design/dolt-server-mode-adr.md` rejects separately (OS-ephemeral port,
# one unbounded server per project directory). Repairing to the fleet server retires them:
# measured, bootstrap rewrites the store's port file 43755 -> 3308.
#
# Both `hub_bulk` guards stay exactly as they are. A cache is a derived hydration artifact and
# is never an authoritative co-located hive, whatever identity it happens to carry.
# ---------------------------------------------------------------------------


def cache_database(entry) -> str:
    """The database name a hive's CACHE store hydrates into on the shared server — its
    registered prefix, run through `store_locator.sanitize_database_name` (so `bc-workspace` ->
    `bc_workspace`), matching what `migrate-storage` derives for a cloned hive.

    The prefix is the hive's own registered identity AND the one name bh already enforces
    unique fleet-wide, so this is collision-free by construction and — the rule bh-g5ujg set —
    RECORDED (`store_locator.ensure_server_database_persisted`), never re-derived at read time.
    :func:`detect_cache_collisions` asserts the uniqueness rather than assuming it."""
    return store_locator.sanitize_database_name(str(entry.get("prefix") or ""))


def detect_cache_collisions(cfg) -> dict[str, list[str]]:
    """``{database: [hive_key, ...]}`` for every cache database name claimed by more than one
    registered hive — empty when the fleet is safe.

    The cache-side counterpart of `storage_migrate.detect_target_collisions`, which this path
    had no equivalent of: migration refuses a plan where two hives resolve to one database,
    while hydration merrily pointed five stores at one. Same shape, same refusal."""
    by_database: dict[str, list[str]] = {}
    for entry in registry.hives(cfg):
        name = cache_database(entry)
        if name:
            by_database.setdefault(name, []).append(registry.hive_key(entry))
    return {name: ids for name, ids in sorted(by_database.items()) if len(ids) > 1}


def _cache_on_shared_server(cache: Path, database: str) -> bool:
    """Whether `cache` ALREADY is what the ADR requires: metadata pinning server mode and
    `database`, and that database really present under bd's shared-server datadir. Pure
    filesystem + metadata facts, no subprocess (`store_locator`'s discipline).

    This is the idempotence gate. True means LEAVE IT ALONE — re-stamping a store that is
    already correct on every sync is the churn bh-qpa3g measured, and the condition is a fixed
    point: once a cache satisfies it, no later sync writes to it again. False covers a fresh
    clone, a store in bd's owned mode (rotating port, empty `.beads/dolt/`), a store left
    embedded, and a store naming a database that belongs to some other hive."""
    return (
        store_locator.dolt_mode(cache) == "server"
        and store_locator.dolt_database(cache, "") == database
        and (store_locator.server_store_dir() / database).is_dir()
    )


def _adopt_cache_identity(cache: Path, database: str) -> str:
    """Point `cache` at its OWN database on the shared server, by writing `dolt_mode: "server"`
    + `dolt_database: <database>` into `.beads/metadata.json` before `bd bootstrap` reads it.
    Returns a short note describing what changed.

    Also RETIRES the stale `project_id`, which is not housekeeping: bd's own "Bootstrap metadata
    repair" resolves a project_id to whichever database on the server already claims it and
    silently overwrites the name being seeded (measured — see the section comment). Leaving it
    is what makes a hand repair evaporate on the next sync. A cache is a derived artifact, so
    the identity it should carry is the one it re-clones from its own remote; bd writes that
    back itself during the bootstrap that follows. Measured, that is also what finally settles
    bh-gbmyw: the next sync prints `Bootstrap metadata repair: backfilled project_id 2ff6c786…
    from database "hl"`, so each store ends up with the DISTINCT id of its own database
    (2ff6c786 for hl, cd6dbccf for dxnvh) instead of one shared 8b177292, and the file is then
    byte-stable across every further sync.

    `dolt_server_database` is dropped for the same reason and re-recorded AFTER bootstrap
    (`_fetch_cache`) — measured, bd rewrites metadata.json during bootstrap and does not
    preserve bh's additive key."""
    beads = Path(cache) / ".beads"
    path = beads / "metadata.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    before = f"{data.get('dolt_mode') or 'unset'}/{data.get('dolt_database') or 'unset'}"
    data["dolt_mode"] = "server"
    data["dolt_database"] = database
    for stale in ("project_id", store_locator.SERVER_DATABASE_KEY):
        data.pop(stale, None)
    beads.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    return f"{before} -> server/{database}"


def _ensure_shared_server_running(cache: Path) -> None:
    """Start bd's shared server if nothing is answering on its endpoint — the step
    `storage_migrate._bootstrap_shared_server` already takes for the same reason, and the one a
    FRESH HOST needs (bh-fnn3d's whole scenario is a host with no prior store): unlike `bd init
    --shared-server`, `bd bootstrap` does NOT auto-start the server, it fails outright with
    `connect: connection refused`. Probe first (`dolt_health.probe_shared_server`, a real
    endpoint connect, no subprocess) and only then attempt a start — an unconditional `bd dolt
    start --global` returns non-zero against a server already running outside bd's own
    bookkeeping (bh-hqmcl / bh-l90xk). Best-effort: a failed start is left for the bootstrap
    below to report against the actual work it was trying to do."""
    from . import dolt_health

    if dolt_health.probe_shared_server().reachable:
        return
    run(
        ["bd", "-C", str(cache), "dolt", "start", "--global"],
        check=False,
        capture=True,
        env={**bootstrap_env()},
    )


def _fetch_cache(cfg, entry):
    """Minimal-clone (blobless, no checkout) + bootstrap a hive's beads into the cache, on the
    fleet's shared server, in the hive's OWN database. Returns the cache path, or None — loudly
    — if it couldn't be hydrated there.

    Identity is written only when the store is not already correct (:func:`_cache_on_shared_
    server`), so a repair survives the next sync instead of being re-stamped, and landing on the
    shared server is ASSERTED afterwards rather than assumed: a bootstrap that didn't get there
    is a failure this reports and returns None for (`sync()` counts it failed and `bh sync`
    exits non-zero), never a quieter fallback onto a mode the ADR retired.

    Safe to re-run — but NOT because nothing here is ever written locally: `report._target`
    creates a bead in this cache and pushes it back (`bh report` against an uncloned hive), so
    the "read-only, never a place a developer creates local beads" claim this docstring used to
    make was false and is retracted (filed as bh-3dyo9). What makes a re-run safe is narrower:
    `bd bootstrap` declines outright on a store that already exists ("Database already exists.
    Nothing to do", measured), and the repair path only runs when the store is not usable as it
    stands."""
    cache = config.cache_dir() / entry["provider"] / entry["org"] / entry["repo"]
    prefix = str(entry.get("prefix") or "?")
    database = cache_database(entry)
    from . import hub_bulk

    if not database or database in hub_bulk.GENERIC_DATABASE_NAMES:
        typer.echo(
            f"  ✗ {prefix}: cannot hydrate — its prefix resolves to "
            f"{database or '(nothing)'!r}, which is not a usable per-hive database name "
            "(bh-fnn3d). Re-register the hive with a distinct --prefix.",
            err=True,
        )
        return None
    collisions = detect_cache_collisions(cfg)
    if database in collisions:
        typer.echo(
            f"  ✗ {prefix}: cannot hydrate — cache database {database!r} is claimed by more "
            f"than one registered hive ({', '.join(collisions[database])}); hydrating would "
            "merge separate bead corpora into one store (bh-gbmyw).",
            err=True,
        )
        return None
    if not (cache / ".git").is_dir():
        url = _hive_url(cfg, entry)
        if not url:
            return None
        cache.parent.mkdir(parents=True, exist_ok=True)
        rc = run(
            ["git", "clone", "--filter=blob:none", "--no-checkout", url, str(cache)],
            check=False,
        ).returncode
        if rc:
            return None
    if not _cache_on_shared_server(cache, database):
        _ensure_shared_server_running(cache)
        typer.echo(
            f"  ↻ {prefix}: cache store repointed at its own shared-server database "
            f"({_adopt_cache_identity(cache, database)})",
            err=True,
        )
    # bootstrap pulls refs/dolt/data into the database named above (a no-op once it exists)
    engine.get_engine(cfg).bootstrap(cache, env=bootstrap_env())
    if not _cache_on_shared_server(cache, database):
        # Deliberately NOT the old `.beads/.is_dir()` check, which the identity stamp above would
        # now satisfy on its own. bh-fnn3d asks for a loud refusal over a cheerful exit 0, and
        # names the fallback an operator has: a real checkout plus `bh hive migrate-storage`.
        typer.echo(
            f"  ✗ {prefix}: bootstrap did not land {database!r} on the shared server at "
            f"{store_locator.server_store_dir()} — refusing to register a store that is not "
            "there. Clone the hive and run `bh hive migrate-storage " + prefix + " --confirm` "
            "instead (bh-fnn3d).",
            err=True,
        )
        return None
    persist_shared_server_mode(cache)
    # Record the server name rather than leaving it derivable (bh-g5ujg) — bd's own bootstrap
    # rewrite drops this additive key, so it goes back afterwards, not before.
    store_locator.ensure_server_database_persisted(cache, database)
    embedded = store_locator.embedded_store_dir(cache)
    if embedded.is_dir():
        typer.echo(
            f"  · {prefix}: {embedded} is now unused (the store is on the shared server) — "
            "remove it to reclaim the space.",
            err=True,
        )
    return cache


# ---------------------------------------------------------------------------
# Per-hive sync watermarks (bh-d5jhc.2) — stop re-exporting a hive whose beads have not
# changed since the last successful hub sync.
#
# `hub.sync()` used to re-run `bd export` for EVERY registered hive on EVERY call, which kept
# the exported `.beads/issues.jsonl`'s mtime perpetually fresh and so permanently defeated
# `bd repo sync`'s OWN incremental skip (it compares that mtime against one it persists in the
# primary store — cmd/bd/repo.go:357-366, vendored). The fix is a bh-side watermark ahead of
# the export subprocess itself (not just relying on bd's downstream mtime check — that still
# pays a full `bd export` spawn per hive every run, per the parent bead's DESIGN note).
# ---------------------------------------------------------------------------

_WATERMARK_VERSION = 2  # 2: + "aggregate_id" (store identity, not just path) — bh-89wxf.1
_WATERMARK_FILENAME = "hub-sync-watermarks.json"


def _watermark_path() -> Path:
    """Where per-hive sync watermarks persist. Deliberately `config.cache_dir()` — bh's own
    local, UNTRACKED scratch area (same file family as `metadata.py`'s `_cache_path()`) — and
    deliberately NEVER inside the hub/HQ store directory itself: that directory is a real git
    working tree with a remote (`hq.py`'s "hq clone"), and this bead's own parent notes record
    exactly this hazard for `metadata.json`'s `dolt_mode` field ("committing it propagates
    server mode to every other host"). Per-host state has no business riding along in a
    fleet-shared, git-tracked repo."""
    return config.cache_dir() / _WATERMARK_FILENAME


def _load_watermarks(aggregate: Path) -> dict[str, str]:
    """``{prefix: last-successfully-synced Dolt commit hash}`` for `aggregate` (the CURRENT
    hub/HQ target dir — see `_aggregation_target`).

    Missing / unparseable / wrong-version / aggregate-mismatch all collapse to an EMPTY dict,
    never raise — a cold cache means "treat every hive as changed", which is always safe
    (CONVERGENCE DISCIPLINE: a missed or corrupt watermark read must fail toward a full
    re-sync, never toward silently trusting a skip it can't justify). The `aggregate` check
    specifically covers the hub->HQ handoff: once a durable HQ is registered, `sync()`'s
    aggregation target moves (`_aggregation_target`) to a store that has never seen any hive
    yet, so a watermark recorded against the OLD disposable hub must never be read as if it
    still applies.

    IDENTITY, NOT JUST PATH (bh-89wxf.1). The `aggregate` path check alone could not see a hub
    WIPED AND RE-MINTED under the same directory — precisely the `rm -rf` the hub's contract
    invites — and would then hand every hive a "unchanged, skip the export" verdict recorded
    against a store that no longer exists. It happened to survive that only because bd's own
    downstream mtime memory lives in the primary store and died with it; correctness resting on
    a second, unrelated mechanism's failure is not correctness. So the store's `project_id`
    (`store_locator.project_id`, new on every fresh `bd init`) is recorded alongside the path
    and must match too. A store recording no id at all reads as a mismatch — unknown fails
    toward the full re-sync, same as every other unreadable case here."""
    path = _watermark_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("version") != _WATERMARK_VERSION:
        return {}
    if data.get("aggregate") != str(aggregate):
        return {}
    identity = store_locator.project_id(aggregate)
    if not identity or data.get("aggregate_id") != identity:
        return {}
    hives = data.get("hives")
    if not isinstance(hives, dict):
        return {}
    return {str(k): str(v) for k, v in hives.items() if isinstance(v, str) and v}


def _store_watermarks(aggregate: Path, marks: dict[str, str]) -> None:
    """Atomic write of the whole file (temp + `os.replace`, mirrors `metadata.store`) so a
    reader never observes a half-written file — a torn read must never be mistaken for
    "this hive is unchanged".

    PER-WRITER scratch name, mirroring `metadata.store` in that too (bh-gc4h1). This file is
    written from `sync()`, and `sync_background` runs `sync()` on a daemon thread while its
    caller keeps going — so a `<pid>`-keyed temp is ONE name shared by every thread in the
    process. Two overlapping writers raced: the first `os.replace` moves the scratch away and
    the loser raises `FileNotFoundError` out of a *cache write*. That race was reproduced under
    parallel load next door in `metadata.store`; the identical shape here is fixed with it
    rather than left to be rediscovered the same expensive way."""
    path = _watermark_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {
            "version": _WATERMARK_VERSION,
            "aggregate": str(aggregate),
            "aggregate_id": store_locator.project_id(aggregate),
            "hives": marks,
        },
        indent=2,
    )
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(payload)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _hive_commit(cfg, src) -> str | None:
    """The hive's current Dolt HEAD commit hash — the watermark value itself (`bd vc status
    --json`'s `commit` field), or `None` when bd genuinely couldn't be asked (missing/timeout/
    non-zero exit/unparseable/missing key). `None` must NEVER be read as "unchanged" by a
    caller — an unreadable watermark forces a sync, it never licenses a skip.

    Chosen over two cheaper-looking alternatives that don't hold up: `refs/dolt/data` is NOT
    reliably a local ref in either embedded or shared-server mode (measured directly —
    `host_fence.transport_lookup`'s docstring: "the local side of the push is a transient
    `refs/dolt/blobstore/...` ref"), and this hive's own working-tree `.git` HEAD says nothing
    about the separate Dolt data store. `bd vc status` is bd's own supported "what commit is
    this store's data on right now" surface in every mode, and it is dramatically cheaper than
    the `bd export` it lets this bead skip: measured on this hive (2.3k issues), ~0.5s vs ~2s,
    and unlike export it does not scale with issue count — it reads one ref, not the whole
    table."""
    data = bd.json(["vc", "status"], src)
    if not isinstance(data, dict):
        return None
    commit = data.get("commit")
    return commit if isinstance(commit, str) and commit else None


def _sync_hive(hub, cfg, src, prefix, *, export: bool = True) -> bool:
    """Export + register ONE hive's beads into the aggregate `hub` (the shared body of the
    fleet-wide loop in `sync()` and the single-hive `sync_one()`).

    `bd repo sync` hydrates the hub only from each hive's `.beads/issues.jsonl`, but
    dolt-backend hives keep no such file on disk — so export each hive's beads to JSONL first
    (`bd export` is dolt-aware). Under the tracked-beads convention `.beads/issues.jsonl` is
    committed, so this export dirties the working tree; that churn is hive-state bookkeeping
    (discounted by `safety._non_hive_dirty_paths` via its `.beads/` prefix), not a real edit.

    `export=False` (bh-d5jhc.2, `sync()`'s fleet-wide loop only — `sync_one()` never passes
    this) skips the `bd export` subprocess entirely for a hive whose watermark says nothing
    changed, leaving `.beads/issues.jsonl`'s mtime untouched — which is exactly what lets
    `bd repo sync`'s OWN mtime-based skip (the mechanism this bead stops defeating) fire for
    that hive too. `bd repo add` still runs even when `export` is False: it is a cheap,
    idempotent, SELF-HEALING re-assertion of hub registration — a commit-hash watermark alone
    cannot see the hub/HQ *store itself* having been wiped/reinitialized under the same path
    (which would silently drop this hive's registration), but re-running `add` every time does.

    `bd repo add` output is captured: an 'already configured' refusal is the expected idempotent
    re-add (silent), any other non-zero exit is a real failure. Returns whether the hive's own
    add succeeded (export failure alone is not fatal — `bd repo sync` may still hydrate from an
    existing JSONL)."""
    if export:
        jsonl = src / ".beads" / "issues.jsonl"
        res = engine.get_engine(cfg).export_jsonl(src, jsonl, env=_bd_ni_env())
        if res.returncode:
            typer.echo(f"  ⚠ {prefix}: bd export failed: {bd.err_line(res)}", err=True)
    add = run(["bd", "-C", str(hub), "repo", "add", str(src)], check=False, capture=True)
    if add.returncode and _ALREADY_CONFIGURED not in _output(add):
        typer.echo(f"  ✗ {prefix}: bd repo add failed: {bd.err_line(add)}", err=True)
        return False
    return True


def sync_one(prefix: str, src) -> bool:
    """Synchronously export + register ONE hive into the aggregate — the cheap, synchronous half
    of `sync()`'s two responsibilities (bh-d5jhc.1). `onboard.py`'s `footprint` step depends on
    this landing before a furnished hive's scaffold commit captures `issues.jsonl`
    (onboard.py:~1373), so this stays on the interactive path; only the fleet-wide `bd repo
    sync` aggregation walk (`sync()` / `sync_background()`) moves off it. Does NOT run `bd repo
    sync` itself — the triggering hive is added to the aggregate's repo list, but the derived
    aggregate is not re-materialized until the next fleet sync (background or explicit)."""
    hub = ensure_hub()
    cfg = config.load()
    return _sync_hive(hub, cfg, Path(src), prefix)


def sync_background(cfg=None):
    """Kick the fleet-wide aggregation walk (`sync()`) in a best-effort daemon thread — the
    mutating op that triggered it (`hive onboard` / `hq push`) returns immediately, and a later
    read against the aggregate (`hub bd`, `hq bd`, `hub intake` — all read-only, gated by
    `guard.guard_hub`) serves whatever the background sync managed to land.

    Mirrors `metadata._spawn_reload` / `config.metadata_background_reload` exactly: one
    throwaway daemon thread (no pool, no daemon service), gated by
    `config.hub_sync_background`, and NEVER raises into the caller — a failed/interrupted
    background sync just leaves the aggregate as it was (mirroring `escalate.py`'s treatment of
    `hub.sync` failures as non-blocking). The work only needs to START before the CLI process
    exits (bh-d5jhc.1's recorded operator decision): a daemon thread dying with a short-lived
    CLI is fine because the aggregate is DERIVED and the next sync reconciles — deliberately NOT
    a detached subprocess (no precedent in this codebase, not warranted here).

    Returns the started `Thread`, or `None` when backgrounding is disabled
    (`hub.background_sync: false`)."""
    cfg = cfg if cfg is not None else config.load()
    if not config.hub_sync_background(cfg):
        return None

    def _reload():
        try:
            sync()
        except Exception:
            pass  # background best-effort — a failed sync just leaves the aggregate as-is

    t = threading.Thread(target=_reload, name="bh-hub-sync", daemon=True)
    t.start()
    return t


def sync():
    """Make the hub reflect every registered hive (cloned by path, uncloned via cache).

    THE FLEET-WIDE HALF of the hub's two responsibilities (bh-d5jhc.1) — walks and reprocesses
    EVERY registered hive; `sync_one()` is the cheap single-hive half kept on the interactive
    path, `sync_background()` is how a mutating op (`hive onboard` / `hq push`) triggers this
    without blocking on it. A hive whose import still fails (e.g. corrupt beads data bd can't
    round-trip) is reported as failed rather than folded into a blanket green.

    PER-HIVE WATERMARK SKIP (bh-d5jhc.2): before exporting each hive, its current Dolt HEAD
    commit (`_hive_commit`) is compared against the commit recorded from the last hive this
    function successfully synced (`_load_watermarks`). A match skips the `bd export`
    subprocess (`_sync_hive(..., export=False)`) — and, because that leaves
    `.beads/issues.jsonl`'s mtime untouched, lets `bd repo sync`'s OWN incremental mtime skip
    fire for that hive too, avoiding the per-issue reimport this bead's parent measured as the
    real cost. `bd repo add` still runs for every hive regardless (cheap, idempotent,
    self-healing — see `_sync_hive`'s docstring).

    BULK CROSS-DATABASE FAST PATH (bh-l7sm8, `config.hub_bulk_sync`, default OFF): when
    enabled, `hub_bulk.run_bulk_pass` bulk-copies every CO-LOCATED hive (one sharing this
    fleet's Dolt server with the aggregate) directly, cross-database, bypassing the `bd repo
    sync` call below entirely for those hives — see that module for the mechanism and the
    curated table list. Everything ELSE in this function (export, `bd repo add`, watermarks,
    `_reconcile_removed`) runs exactly as it did before this bead, for every hive, regardless
    of the flag — the bulk pass only ever changes what the FINAL `bd repo sync` call still has
    left registered to process. A hive whose bulk copy fails, or that isn't co-located at all,
    is simply left registered — `bd repo sync` remains the correctness backstop for it this
    same round, never a silently skipped hive.

    Returns the prefixes that failed to hydrate (empty on full success).
    """
    hub = ensure_hub()
    cfg = config.load()

    # REFUSAL, not a silent fallback (bh-l7sm8, operator decision 2026-08-09). Disabling the
    # bulk path does not mean "use the old path" — the old path is `bd repo sync`, whose per-edge
    # recursive-CTE ancestry check is a known upstream defect (bh-z4z52). Falling through to it
    # silently would charge ~398x with nothing on screen connecting the cost to the cause. So a
    # deliberate opt-out gets a hard stop naming the reason, and `bh sync` exits non-zero.
    if not config.hub_bulk_sync(cfg):
        typer.echo(
            "✗ hub sync refused: `hub.bulk_sync` is set false.\n"
            "  The non-bulk path is `bd repo sync`, which validates dependency ancestry with one\n"
            "  recursive CTE PER EDGE — measured 4212 issues in 655.82s (6.4/sec) against 1.65s\n"
            "  for the cross-database copy, and separately observed exiting 0 while importing\n"
            "  nothing. bh will not run it. See bh-z4z52.\n"
            "  Unset `hub.bulk_sync` (or set it true) to use the fast path; it falls back to\n"
            "  `bd repo sync` automatically, per hive, for any hive not co-located on the\n"
            "  shared Dolt server.",
            err=True,
        )
        return [BULK_SYNC_DISABLED]

    managed = registry.hives(cfg)
    n = len(managed)
    typer.echo(f"starting hub sync ({n} hive(s))…", err=True)
    marks = _load_watermarks(hub)
    commits: dict[str, str | None] = {}
    added, skipped, failed = [], [], []
    bulk_entries: list[tuple[str, Path, bool, bool]] = []
    from . import hub_bulk

    bulk_enabled = config.hub_bulk_sync(cfg)
    # One live `SHOW DATABASES` for the whole pass, queried before the loop so co-location can be
    # decided per hive BEFORE its export runs (bh-4o07n).
    server_dbs = hub_bulk.server_databases(hub) if bulk_enabled else set()
    for i, e in enumerate(managed, 1):
        prefix = str(e["prefix"])
        typer.echo(f"• syncing {prefix} ({i}/{n})", err=True)
        path = registry.hive_dir(e)
        src = path if (path / ".beads").is_dir() else _fetch_cache(cfg, e)
        if src is None:
            typer.echo(f"  ⚠ skip {prefix}: not cloned and no remote beads data", err=True)
            skipped.append(prefix)
            continue
        commit = _hive_commit(cfg, src)
        commits[prefix] = commit
        changed = commit is None or marks.get(prefix) != commit
        if not changed:
            typer.echo(f"  ✓ {prefix}: unchanged since last sync — skipping export", err=True)
        # bh-4o07n: decide co-location BEFORE exporting. A hive the bulk pass will copy must NOT
        # have its `.beads/issues.jsonl` rewritten — leaving the mtime untouched is what lets bd's
        # OWN mtime skip fire for it in the trailing `bd repo sync`, so bd neither re-imports it
        # (re-paying the cost this path exists to avoid) nor loses it. That replaces the
        # de-registration the bulk pass used to do, which deleted the very hives it had copied.
        # `is_cache` is the distinction `src` erases and the bulk path must not guess at.
        is_cache = _is_cache_path(cfg, src)
        will_bulk = bool(
            bulk_enabled
            and hub_bulk.co_located_database(server_dbs, Path(src), prefix, is_cache=is_cache)
        )
        if not _sync_hive(hub, cfg, src, prefix, export=changed and not will_bulk):
            failed.append(prefix)
            continue
        added.append((prefix, str(src)))
        bulk_entries.append((prefix, Path(src), changed, is_cache))

    # Reconcile before hydrating so the sync reflects only still-managed repos — also prunes
    # watermark bookkeeping for anything no longer managed.
    _reconcile_removed(hub, cfg, managed, marks)

    bulk_hydrated: list[str] = []
    if bulk_enabled:
        bulk_hydrated = hub_bulk.run_bulk_pass(hub, bulk_entries)

    remainder = [(prefix, src) for prefix, src in added if prefix not in bulk_hydrated]
    res = run(["bd", "-C", str(hub), "repo", "sync"], check=False, capture=True)
    report = (res.stdout or "") + (res.stderr or "")
    if res.returncode:
        typer.echo(f"  ✗ bd repo sync failed: {bd.err_line(res)}", err=True)
        failed.extend(prefix for prefix, _ in remainder)
        remainder = []
    elif report.strip():
        typer.echo(report.strip(), err=True)
    failed.extend(prefix for prefix, src in remainder if f"failed to import from {src}" in report)
    hydrated = [prefix for prefix, _ in remainder if prefix not in failed] + bulk_hydrated

    # Advance the watermark ONLY for a hive `bd repo sync` actually confirmed hydrated — never
    # merely on `_sync_hive`'s (export + repo add) own success. CONVERGENCE DISCIPLINE: a
    # failed/interrupted `bd repo sync` is the step that actually lands data in the aggregate,
    # so any prefix it didn't confirm must lose its old watermark too — the next run must
    # re-sync it rather than trust a skip it can no longer justify (a missed watermark update
    # fails toward "will re-sync next time", never toward "will silently skip forever").
    for prefix in failed:
        marks.pop(prefix, None)
    for prefix in hydrated:
        commit = commits.get(prefix)
        if commit is not None:
            marks[prefix] = commit
        else:
            # hydrated, but bd couldn't be asked for a commit this round — nothing trustworthy
            # to record, so drop any stale prior mark rather than leave it sitting unused.
            marks.pop(prefix, None)
    _store_watermarks(hub, marks)

    from . import metadata

    metadata.invalidate(cfg)  # fleet-wide sync — coarse; the next doctor/survey recomputes
    mark = "⚠" if failed else "✓"
    summary = f"{mark} hub synced: {len(hydrated)} hydrated, {len(skipped)} skipped"
    if failed:
        summary += f", {len(failed)} failed to hydrate ({', '.join(failed)})"
    typer.echo(summary + f" → query with `{config.BINARY_ALIAS} hub bd ready`")
    return failed


# ---- bounding the cross-hive aggregate read (bh-toitp) ------------------------------------
#
# `query` IS THE SPAWN THAT LEAKED. 31 live `bd -C ~/.beadhive/hq show <~50 ids> --json`
# processes, 9.6 GB RSS, oldest 2h12m, id lists spanning every registered hive prefix — a wave of
# ten spawned ~10s apart, all still alive an hour later, all `ppid=1`. Every one came through
# here, and the call carried NO timeout and NO ceiling: fire-and-forget against the store that is
# already the contention point.
#
# THE CONSEQUENCE IS WHY IT IS P1, and it is not tidiness. Anything that had to WRITE to HQ while
# a wave was in flight silently did not happen — including `bh escalate`, the bottom-rung verb an
# agent uses to report that the tooling is broken. One escalation hung >13 minutes, failed four
# times, and the upstream bug it was carrying was very likely never filed. The factory lost its
# own escalation path to its own read path.
#
# TWO BOUNDS, because they fail differently:
#   * a TIMEOUT on each child (plus PDEATHSIG — see `run.bounded`), so no single call can outlive
#     the caller, whether the caller finished or was killed;
#   * a CEILING on how many run at once, so waves cannot pile up. The bead is explicit that a
#     reaper alone is the wrong fix: "DO NOT fix this by making the caller kill stragglers — that
#     hides an unbounded spawn behind a cleanup. The spawn is the bug."
#
# THE CEILING DELIBERATELY DOES NOT COVER WRITES. `bh escalate` reaches HQ through `bd` directly
# and never through this function, so it is exempt BY CONSTRUCTION rather than by someone
# remembering to exempt it — which is the property the incident actually needed. Demonstrated
# 2026-08-13 under load: ten concurrent aggregate reads fired at once, live `bd -C <hq>` processes
# held at 2 (the ceiling) rather than 10, `bh escalate` returned 0 in 12s DURING the wave, all ten
# reads completed, and the bead's own recovery probe found no survivor.
#
# IS THIS bh-mo5t's ROOT CAUSE WEARING A DIFFERENT VERB? NO — determined, and recorded here
# either way so the next reader does not re-derive it. They are the same FAMILY (an HQ aggregate
# read with `--json`) and share a symptom, but not a cause:
#   * THIS bead is a CALLER defect — an unbounded, un-timed-out, un-reaped spawn. It is present
#     even when the underlying `bd` read is instantaneous, and it is what turns a slow read into
#     31 live processes and 9.6 GB.
#   * bh-mo5t is a COST defect inside bd's own query (`bd swarm list --json` burning ~100% CPU
#     for minutes). Nothing here makes that read faster.
# The bound therefore changes bh-mo5t's shape without fixing it: a query that used to wedge
# forever now fails loudly at AGGREGATE_TIMEOUT and names itself. Step 3 of this bead's own
# ordered plan — "why does a ~50-id `bd show --json` against HQ take >2h at all" — is unanswered
# and stays with bh-mo5t.

#: Seconds any ONE aggregate read may take. Generous on purpose: a legitimate cross-hive `show`
#: of ~50 ids is seconds, and the wedged ones ran for HOURS. No honest call is near this.
AGGREGATE_TIMEOUT = float(os.environ.get("BH_HQ_QUERY_TIMEOUT", "120"))

#: How many aggregate reads this HOST runs at once. Two, not one: an agent and a human
#: overlapping is ordinary; ten waves 10s apart is the bug. `flock`-based, so the kernel releases
#: a slot when its holder dies — a SIGKILLed `bh` cannot wedge one, which a pidfile semaphore
#: could. `BH_HQ_QUERY_SLOTS=0` disables the bound (the other arm of the measurement).
AGGREGATE_SLOTS = int(os.environ.get("BH_HQ_QUERY_SLOTS", "2"))

#: Wait this long for a slot, then FAIL — never queue. Queuing is how ten waves became 31 live
#: processes; a caller that cannot be served now needs to be told, not parked.
AGGREGATE_SLOT_WAIT = float(os.environ.get("BH_HQ_QUERY_SLOT_WAIT", "30"))


class AggregateBusy(RuntimeError):
    """No aggregate-read slot came free (bh-toitp). Loud — never a queue, and never an empty
    result: a wave that could not be served has to be visible to whoever asked for it."""


def aggregate_slot_dir() -> Path:
    """Where the aggregate-read permits live: ONE directory per host.

    HOST-WIDE IS THE WHOLE POINT — do not "fix" this into a per-process or per-run path. The
    thing being bounded is how many `bd -C <hq>` children exist on this machine at once, and
    every one of the 31 leaked processes belonged to a DIFFERENT `bh` invocation. A per-process
    scope would leave the bound looking present and doing nothing, which is worse than not
    having it (bh-toitp).

    `_aggregate_slot(slot_dir=…)` overrides it — for TESTS only, so a test cannot take a real
    host slot or collide with a sibling test under `-n auto` (bh-a4zsr). Inside the fence TMPDIR
    is one directory shared by every xdist worker in the run, so two tests exercising the
    ceiling would genuinely contend for the same `slot-0` and fail each other.
    """
    import tempfile

    return Path(tempfile.gettempdir()) / "bh-hq-query-slots"


@contextlib.contextmanager
def _aggregate_slot(slots: int = -1, wait: float = -1.0, slot_dir: Path | None = None):
    """Hold one of *slots* host-wide permits to read the cross-hive aggregate.

    Raises :class:`AggregateBusy` rather than blocking forever when none frees up within *wait*
    seconds. `flock` on files under the system temp dir, mirroring the pattern
    ``tests/harness/world.py::dolt_server_slot`` already proved for the same shape of problem
    (bh-wa3ch): it bounds EVERY invocation, including ones started by a different process
    entirely, which an in-process semaphore cannot.

    ``slot_dir`` defaults to :func:`aggregate_slot_dir` — the production bound is unchanged by
    its existence; see that function for why the default is host-wide and who may override it.
    """
    import fcntl
    import time

    slots = AGGREGATE_SLOTS if slots < 0 else slots
    wait = AGGREGATE_SLOT_WAIT if wait < 0 else wait
    if slots <= 0:
        yield -1
        return
    slot_dir = aggregate_slot_dir() if slot_dir is None else slot_dir
    slot_dir.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + wait
    while True:
        for index in range(slots):
            handle = (slot_dir / f"slot-{index}").open("a+")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                handle.close()
                continue
            try:
                yield index
                return
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()
        if time.monotonic() >= deadline:
            raise AggregateBusy(
                f"all {slots} cross-hive aggregate read slot(s) are busy after {wait:g}s — "
                "another bh is already querying HQ. This is the ceiling that stops hydration "
                "waves piling up (bh-toitp); retry, or raise BH_HQ_QUERY_SLOTS."
            )
        time.sleep(0.25)


def query(args, *, label: str = "hq"):
    """Run a bd command against the cross-hive aggregate (HQ once registered, else the
    legacy hub — see ``_aggregation_target``). ``label`` is cosmetic: it names which command
    surface the caller invoked (``"hq"`` vs the deprecated ``"hub"`` alias) purely so
    ``guard.guard_hub``'s refusal message names the real command (bh-ohx2) — both surfaces
    resolve to the SAME store and run through the SAME guard.

    BOUNDED on both axes since bh-toitp — see the block comment above for the measurement, and
    for why a reaper alone would have been the wrong fix.
    """
    guard.guard_hub(args, label=label)  # the hub is a READ cache — refuse writes (strands beads)
    hub, _ = _aggregation_target()
    if not (hub / ".beads").is_dir():
        typer.echo(f"✗ hub not initialized — run `{config.BINARY_ALIAS} sync` first", err=True)
        raise typer.Exit(1)
    verb = " ".join(a for a in args if not a.startswith("-"))[:60] or "bd"
    try:
        with _aggregate_slot():
            rc = run_bounded(
                ["bd", "-C", str(hub), *args],
                timeout=AGGREGATE_TIMEOUT,
                label=f"{label} bd {verb} against {hub}",
            ).returncode
    except (ChildTimeout, AggregateBusy) as exc:
        # Names the store AND the verb, and is a FAILURE. What this replaced was a call that
        # neither returned nor reported, so the work depending on it silently did not happen.
        typer.echo(f"✗ {exc}", err=True)
        typer.echo(
            f"  the aggregate is a READ cache — rebuild it with `{config.BINARY_ALIAS} sync`. "
            "Check for leftovers with:\n"
            "    ps -eo pid,etimes,cmd | awk '/[b]d -C .*\\/hq/ && $2 >= 600'",
            err=True,
        )
        raise typer.Exit(1) from None
    if rc:
        raise typer.Exit(rc)


def intake(extra=None):
    """The superintendent's FLEET-WIDE inbox: untriaged intake across every hydrated hive.

    Source-agnostic by construction — the `intake:untriaged` label is set by every source
    (report | github | import), so one filter surfaces the whole fleet's untriaged reports. A read
    against the hub cache (allowlisted by the write-guard); extra `bd list` flags (e.g. `--json`,
    `--assignee`) forward through."""
    from .state import INTAKE_UNTRIAGED

    query(["list", "--label", INTAKE_UNTRIAGED, "--status", "open", *(extra or [])])
