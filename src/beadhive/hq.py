"""Factory HQ — the one durable central store.

HQ is the aggregation primary (the cross-hive view that supersedes the disposable ``~/.ws/hub``)
that ALSO holds canonical hq-prefixed control-plane beads. A SINGLETON (kind=hq), registered
ONLY in the ws registry under the RESERVED SYNTHETIC IDENTITY ``local/factory/hq`` — LOCAL infra
like the hub/cache (no remote, never a git-workspace provider). It lives at ``config.hq_dir()``.

`ws hq init` stands it up: bd-init the store (prefix ``hq``), register the synthetic identity,
then move the aggregation role onto it (``bd repo add`` every registered hive + sync). The old
``~/.ws/hub`` is subsumed — rebuildable, no data migration (re-add + sync at the new location).

bh-e0y8.2 extends the SAME verb to turn the local-only store into a *distributable* repo: once
the store exists, ``init`` also (idempotently) scaffolds the documented layout (``fleet.yaml`` +
``workspace.toml`` + ``hosts/``), wires the configured ``hq.remote``, and — after a VERIFIED
three-level backup — pushes ``main`` and ``refs/dolt/data`` for the first time. A re-run once the
remote is already configured is a clean no-op (superseding the old "refuse a second `hq init`"
posture, which predates the remote/distribution story): the singleton guard now only prevents
create-time double-registration, not a harmless idempotent re-run. See ``_wire_remote``.

bh-e0y8.4 adds ``hq clone`` — the mirror image, for a SECOND host that has no local HQ at all:
``git clone`` the remote's ``main`` + hydrate bead state from ``refs/dolt/data`` via ``bd
bootstrap`` (the same seam ``hub._fetch_cache`` already uses to hydrate an uncloned hive), then
register the synthetic identity so ``bh hq bd ready`` resolves to it. See ``clone``.
"""

from __future__ import annotations

import json
import re
import sys
import tarfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import typer

from . import config, engine, git_identity, gitworkspace, hub, registry, safety, store_locator
from .bd import err_line
from .run import run

GIT_TIMEOUT = 30.0  # seconds — bounds a git ls-remote/fetch/push so a wedged remote can't hang
BD_TIMEOUT = 120.0  # seconds — matches engine.STATE_TIMEOUT for the bd dolt/status calls below

# fleet.yaml carries only the shared/fleet-truth keys (docs/design/multi-host-model-adr.md):
# host-local keys (worktrees.path, otel.endpoint, identity, dispatch budgets, …) stay out.
_FLEET_KEYS = (
    "schema_version",
    "delimiter",
    "orgs",
    "dimensions",
    "exclude",
    "managed_repos",
    "work",
    "passthrough",
)
# regenerable — pure waste in a backup (bh-e0y8.2 acceptance amendment).
_DOLT_EXCLUDE_DIR = "git-remote-cache"
# The connection-oriented full-fidelity level's own directory name under a dated backup dir
# (bh-areg.1) — shared with `hq_restore` (imported lazily there, same precedent as
# `hq_restore._backup_root`), so backup and restore never drift on the artifact name.
_DOLT_NATIVE_DIRNAME = "hq-dolt-native"


def init_store() -> list:
    """Core of ``bh hq init`` (no singleton gate, no ``typer.Exit``): stand up the store.

    Reuses ``hub.ensure_store`` to bd-init a durable git+bd store at ``config.hq_dir()`` with
    prefix ``hq``, registers the reserved synthetic identity, and reuses ``hub.sync`` to
    ``bd repo add`` every registered hive + sync (the aggregation role moves off the disposable
    hub to HQ). Returns ``hub.sync``'s failed list. Callers own the singleton check — this is
    the seam ``escalate``'s consent-prompted auto-init calls directly (bh-ufne), never a
    subprocess."""
    # Create the durable store FIRST (prefix hq) — so a bd-init failure never leaves a dangling
    # registration — then register the synthetic identity in the ws registry.
    hq = hub.ensure_store(config.hq_dir(), registry.HQ_PREFIX)
    registry.register(
        registry.HQ_PROVIDER,
        registry.HQ_ORG,
        registry.HQ_REPO,
        registry.HQ_PREFIX,
        registry.HQ_KIND,
    )
    typer.echo(f"✓ Factory HQ store initialized at {hq} (prefix '{registry.HQ_PREFIX}', kind=hq)")

    # Aggregation moves onto HQ: hub.sync now resolves the target to HQ (it is registered), so
    # this bd repo add's every registered hive into HQ and syncs. Reuse over a parallel mechanism.
    return hub.sync()


def init(*, dry_run: bool = False, auto: bool = False, create: bool = False) -> None:
    """Stand up the Factory HQ store (first call only) and — idempotently — scaffold its
    distributable layout, wire the configured remote, and push (bh-e0y8.2).

    First call ever (no HQ registered): creates the store (``init_store``), then wires the
    remote. Every later call: the store already exists — skip straight to (idempotent) remote
    wiring, which itself no-ops once the remote is configured. ``--dry-run`` previews the
    pre-push backup plan with zero mutation; against a not-yet-created store it can only report
    that the store itself would be created (there is nothing local yet to back up)."""
    # bh-17eb: self-heal a stale un-migrated host config before validating — an idempotent
    # re-run on a host that already joined a fleet must not hard-fail here, before this host's
    # own `_wire_remote`/`scaffold_layout` even get a chance to run.
    cfg = config.load_reconciling()
    existing = registry.hive_of_kind(cfg, registry.HQ_KIND)
    if existing is None:
        if dry_run:
            typer.echo(
                f"DRY-RUN would stand up Factory HQ store at {config.hq_dir()} "
                f"(prefix '{registry.HQ_PREFIX}')"
            )
            return
        failed = init_store()
        if failed:
            raise typer.Exit(1)
        cfg = config.load()  # init_store just registered HQ — reload before remote wiring
    else:
        triplet = f"{existing['provider']}/{existing['org']}/{existing['repo']}"
        typer.echo(f"✓ Factory HQ already initialized: {triplet} → {config.hq_dir()} (no-op)")

    _wire_remote(cfg, dry_run=dry_run, auto=auto, create=create)


# ---- bh hq push / bh hq status: publish + report after the first push (bh-z9hl) ----------
#
# `_wire_remote`'s ``engine.push_state`` call above is ONE-SHOT — it fires only on the first-
# wiring path and every later ``bh hq init`` hits the "remote already configured" no-op. There
# was no verb to publish HQ again: keeping it current meant knowing to hand-run ``bh sync`` +
# ``git -C ~/.beadhive/hq push`` + ``cd ~/.beadhive/hq && bd dolt push``, in that order, with
# no CLI surface saying so. ``push`` is that verb; ``status`` is its read-only counterpart.
#
# Both reuse ``safety.scan(hq_dir, fetch=True)`` — the SAME ahead/behind machinery
# ``bh hive sync-remote``/``bh doctor`` already trust — rather than hand-rolling a second ahead/
# behind computation. ``fetch=True`` pays for one real network call (``bd federation status``)
# so the Dolt half's ahead/behind is verified, not guessed; the git half is read from cached
# remote-tracking refs (no implicit ``git fetch`` — matches `doctor`'s zero-surprise-network
# ethos) and is only meaningful once ``main`` carries upstream tracking, which is exactly what
# ``_wire_remote``'s ``-u`` fix above guarantees.


def _hq_dir_or_exit() -> Path:
    hq_dir = config.hq_dir()
    if not (hq_dir / ".beads").is_dir():
        typer.echo(
            f"✗ Factory HQ is not initialized — run `{config.BINARY_ALIAS} hq init` first",
            err=True,
        )
        raise typer.Exit(1)
    return hq_dir


def _hq_main_branch(result: safety.ScanResult):
    return next((b for b in result.branches if b.name == "main"), None)


def _branch_status_line(branch) -> str:
    if branch is None:
        return "no local `main` branch"
    if not branch.has_upstream:
        return "`main` has no upstream tracking configured"
    if branch.ahead and branch.behind:
        return f"`main` diverged from origin/main ({branch.ahead} ahead, {branch.behind} behind)"
    if branch.ahead:
        return f"`main` is {branch.ahead} commit(s) ahead of origin/main"
    if branch.behind:
        return f"`main` is {branch.behind} commit(s) behind origin/main"
    return "`main` is up to date with origin/main"


def _dolt_status_line(dolt) -> str:
    if dolt.status == "clean":
        return "refs/dolt/data is up to date with origin"
    if dolt.status == "ahead":
        return f"refs/dolt/data is {dolt.ahead} commit(s) ahead of origin"
    if dolt.status == "behind":
        return f"refs/dolt/data is {dolt.behind} commit(s) behind origin"
    if dolt.status == "diverged":
        return f"refs/dolt/data diverged from origin ({dolt.ahead} ahead, {dolt.behind} behind)"
    if dolt.status == "no-remote":
        return "no Dolt remote configured"
    if dolt.status == "unknown":
        return f"could not be verified ({dolt.reason or 'unreachable'})"
    return f"refs/dolt/data: {dolt.status}"


# dolt states worth attempting a push for — mirrors `sync_remote._DOLT_PUSHABLE` (the SAME
# vocabulary `bh hive sync-remote` already trusts): "unknown" is bd's embedded engine's default
# (no read-only ahead/behind primitive without `fetch=True`, which `status`/`push` both pass),
# treated as "attempt the idempotent `bd dolt push` and trust its own success/failure".
_DOLT_PUSHABLE = frozenset({"ahead", "diverged", "no-remote", "unknown"})


def status() -> None:
    """`bh hq status`: read-only ahead/behind report for BOTH halves of HQ against its wired
    remote (bh-z9hl) — the status view nothing previously surfaced (an operator had to `git
    rev-parse main` vs `git ls-remote origin` by hand to find drift). Pays for one real network
    call (`bd federation status`, via `fetch=True`) so the Dolt half's counts are verified."""
    hq_dir = _hq_dir_or_exit()
    result = safety.scan(hq_dir, fetch=True)
    if not result.has_origin:
        typer.echo(
            f"HQ ({hq_dir}) has no remote configured — run `{config.BINARY_ALIAS} hq init` "
            "to wire one."
        )
        return

    branch = _hq_main_branch(result)
    dolt = result.dolt_ref
    dirty = any(b.dirty for b in result.branches)

    typer.echo(f"HQ ({hq_dir})")
    typer.echo(f"  git:  {_branch_status_line(branch)}")
    typer.echo(f"  dolt: {_dolt_status_line(dolt)}")
    if dirty:
        typer.echo(
            f"  ⚠ working tree is dirty — `{config.BINARY_ALIAS} hq push` commits it before "
            "publishing"
        )

    git_clean = (
        branch is not None and branch.has_upstream and not branch.ahead and not branch.behind
    )
    dolt_clean = dolt.status == "clean"
    if git_clean and dolt_clean and not dirty:
        typer.echo("✓ HQ is up to date with its remote")
    else:
        typer.echo(f"→ run `{config.BINARY_ALIAS} hq push` to publish")


def push(*, dry_run: bool = False, sync: bool = True, git_only: bool = False) -> None:
    """`bh hq push`: refresh the aggregate, then publish BOTH halves of HQ to its wired remote
    (bh-z9hl) — the discoverable, repeatable counterpart to `_wire_remote`'s one-shot first
    push. Idempotent: reports "nothing to push" cleanly when there's nothing new on either
    half. Any local dirtiness is committed first (mirroring `_wire_remote`'s own
    scaffold-commit precedent) — HQ's tracked content is fleet configuration
    (fleet.yaml/workspace.toml/hosts/, see HQ.md#fleet-writes-after-init), safe to auto-commit,
    unlike a hive's arbitrary uncommitted work.

    ``sync``/``git_only`` (bh-d5jhc.1): the refresh (`hub.sync()`) is the SAME fleet-wide walk
    that blocks `hive onboard` for 18+ minutes — an operator publishing only fleet config
    (fleet.yaml/hosts/, the git half) should not pay it. ``sync=False`` (``--no-sync``) skips
    the refresh but still publishes whatever git+Dolt state HQ already has. ``git_only=True``
    (``--git-only``) additionally skips the Dolt half entirely — the git-only publish this flag
    names — since there is then nothing fresh to push there anyway."""
    hq_dir = _hq_dir_or_exit()
    already = _git(["remote", "get-url", "origin"], hq_dir)
    if already.returncode != 0:
        typer.echo(
            f"✗ HQ has no remote configured — run `{config.BINARY_ALIAS} hq init` to wire one",
            err=True,
        )
        raise typer.Exit(1)

    do_sync = sync and not git_only
    tag = "DRY-RUN " if dry_run else ""
    typer.echo(f"{tag}hq push: refreshing aggregate…")
    if not do_sync:
        typer.echo("  skipping aggregate refresh (--no-sync/--git-only)")
    elif dry_run:
        typer.echo("  DRY-RUN: would run `bh sync`")
    else:
        failed = hub.sync()
        if failed:
            typer.echo(
                f"  ⚠ {len(failed)} hive(s) failed to hydrate — continuing to publish anyway",
                err=True,
            )
    if not dry_run:
        _commit_if_dirty(hq_dir, "chore(hq): sync local changes")

    result = safety.scan(hq_dir, fetch=True)
    branch = _hq_main_branch(result)
    dolt = result.dolt_ref
    moved = False

    if branch is not None and branch.has_upstream and branch.ahead:
        if dry_run:
            typer.echo(f"  DRY-RUN: would push git main ({branch.ahead} commit(s))")
        else:
            pushed_main = _git(["push", "origin", "main"], hq_dir)
            if pushed_main.returncode:
                typer.echo(f"✗ git push origin main failed: {err_line(pushed_main)}", err=True)
                raise typer.Exit(1)
            typer.echo(f"  ✓ git: pushed main ({branch.ahead} commit(s))")
            moved = True
    else:
        typer.echo("  git: nothing to push (up to date)")

    if git_only:
        typer.echo("  dolt: skipped (--git-only)")
    elif dolt.status in _DOLT_PUSHABLE:
        if dry_run:
            typer.echo("  DRY-RUN: would run `bd dolt push`")
        else:
            pushed_dolt = engine.get_engine(config.load()).push_state(hq_dir, message="hq push")
            if pushed_dolt.returncode:
                typer.echo(f"✗ bd dolt push failed: {err_line(pushed_dolt)}", err=True)
                raise typer.Exit(1)
            typer.echo("  ✓ dolt: pushed refs/dolt/data")
            moved = True
    else:
        typer.echo("  dolt: nothing to push (up to date)")

    if dry_run:
        typer.echo("  DRY-RUN: no writes made")
        return
    typer.echo("✓ HQ published" if moved else "✓ HQ already up to date — nothing to push")


# ---- bh hq clone: bootstrap a host with no local HQ (bh-e0y8.4) -------------


def clone(*, auto: bool = False) -> None:
    """Bootstrap a fresh host that has no local HQ: clone ``main`` (fleet.yaml/workspace.toml/
    hosts/) from the configured ``hq.remote`` and hydrate bead state from ``refs/dolt/data`` via
    ``bd bootstrap`` — the mirror image of ``init``'s remote-wiring path. Refuses (never
    clobbers) when ``config.hq_dir()`` already exists.

    Reuses ``_remote_urls``/``config.hq_remote`` for the URL and the SAME ``bd bootstrap`` seam
    ``hub._fetch_cache`` already relies on to hydrate an uncloned hive's beads from a fresh git
    clone (``engine.Engine.bootstrap``) — no hand-rolled ``refs/dolt/data`` fetch. Registers the
    reserved synthetic HQ identity on success (mirroring ``init_store``'s create-then-register
    order), so ``bh hq bd ready`` resolves to this store afterward.

    Also reuses ``hub.bootstrap_env()``/``hub.persist_shared_server_mode`` (bh-hpeye) — a second
    host cloning HQ is exactly the "second-host case" ``onboard.py``'s own zero-footprint
    bootstrap branch already activates ``BEADS_DOLT_SHARED_SERVER=1`` for, and HQ is a fleet
    store like any other (`docs/design/dolt-server-mode-adr.md` / bh-ukit.4). Without it this
    bootstrap landed embedded on the cloning host — the same drift ``_fetch_cache`` had."""
    hq_dir = config.hq_dir()
    if hq_dir.exists():
        typer.echo(
            f"✗ {hq_dir} already exists — refusing to clobber it; `{config.BINARY_ALIAS} hq "
            "clone` is only for a host with no local HQ",
            err=True,
        )
        raise typer.Exit(1)

    cfg = config.load()
    remote = _confirm_remote(cfg, auto=auto)
    if not remote:
        typer.echo(
            "✗ hq.remote is unset and unresolvable — nothing to clone from; set one with "
            f"`{config.BINARY_ALIAS} config set hq.remote <owner>/beadhive-hq`",
            err=True,
        )
        raise typer.Exit(1)

    git_url, _dolt_url = _remote_urls(remote)
    typer.echo(f"hq clone: cloning {git_url} → {hq_dir}")
    cloned = run(
        ["git", "clone", git_url, str(hq_dir)], check=False, capture=True, timeout=GIT_TIMEOUT
    )
    if cloned.returncode:
        typer.echo(f"✗ git clone {git_url} failed: {err_line(cloned)}", err=True)
        raise typer.Exit(1)

    bootstrapped = engine.get_engine(cfg).bootstrap(hq_dir, env=hub.bootstrap_env())
    if bootstrapped.returncode:
        typer.echo(f"✗ bd bootstrap failed: {err_line(bootstrapped)}", err=True)
        raise typer.Exit(1)
    hub.persist_shared_server_mode(hq_dir)

    registry.register(
        registry.HQ_PROVIDER,
        registry.HQ_ORG,
        registry.HQ_REPO,
        registry.HQ_PREFIX,
        registry.HQ_KIND,
    )
    typer.echo(f"✓ Factory HQ cloned from {git_url} → {hq_dir}")

    # A fleet.yaml just landed, so any FLEET-classified key the host's own config.yaml still
    # carries now COLLIDES with it, making every later `config.load()` raise — i.e. breaking
    # effectively every bh command on a host that just joined a fleet (bh-w2u9). The template
    # ships those keys live because it is written for the host that FOUNDS a fleet via
    # `bh hq init`; a host that CLONES one inherits someone else's fleet.yaml, so its own
    # copies are stale by definition. Reconcile here, at the moment the conflict is created,
    # instead of leaving the operator to discover it on their next unrelated command.
    dropped = config.reconcile_host_after_fleet()
    if dropped:
        typer.echo(
            f"  reconciled host config against the cloned fleet.yaml — dropped "
            f"{len(dropped)} stale fleet key(s): {', '.join(dropped)}"
        )


# ---- remote wiring: scaffold + backup + push (bh-e0y8.2) --------------------


def _git(args: list[str], cwd: Path):
    return run(["git", *args], cwd=str(cwd), check=False, capture=True, timeout=GIT_TIMEOUT)


def _bd(args: list[str], cwd: Path):
    return run(["bd", "-C", str(cwd), *args], check=False, capture=True, timeout=BD_TIMEOUT)


def _remote_urls(remote: str) -> tuple[str, str]:
    """(git clone URL, bd Dolt remote URL) for ``<owner>/<repo>`` — GitHub, the only provider
    ``config.hq_remote`` can derive today (mirrors ``hub._hive_url``'s github fallback). bd's
    Dolt-on-git-ref transport needs its own ``git+ssh://`` scheme (verified against a real bd
    binary), distinct from git's scp-like clone form."""
    return f"git@github.com:{remote}.git", f"git+ssh://git@github.com/{remote}.git"


def _confirm_remote(cfg: dict, *, auto: bool) -> str:
    """Resolve HQ's remote, CONFIRMING it with the operator rather than acting on a guess.

    Wiring HQ's remote is a one-way fleet decision — it pushes ``main`` + ``refs/dolt/data``
    and fixes which HQ this fleet answers to — so the derived owner is a SUGGESTION offered as
    the prompt default, never a value taken silently (bh-mw97). ``--auto`` (and any non-TTY,
    where there is nobody to ask) takes the derived value as-is, which is the CI/headless
    path. Returns "" only when derivation missed AND we could not ask."""
    derived = config.hq_remote(cfg)
    if auto or not sys.stdin.isatty():
        return derived
    typer.echo(
        "HQ remote — the fleet-wide Factory HQ repo this host pushes to. It must already "
        "exist and be EMPTY."
    )
    answer = typer.prompt("  <owner>/<repo>", default=derived or None)
    return str(answer or "").strip()


# A 404 is the ONLY probe failure `--create` may answer by creating the repo. Auth failures
# ("Permission denied (publickey)") and network failures ("Could not resolve hostname") must
# keep failing loudly — creating a repo because the network blinked would be the wrong repair.
_MISSING_REPO_MARKERS = ("repository not found", "does not exist", "not found")


def _remote_missing(probe) -> bool:
    """True when the ls-remote failure specifically means "no such repo"."""
    blob = f"{probe.stdout or ''}{probe.stderr or ''}".lower()
    return any(marker in blob for marker in _MISSING_REPO_MARKERS)


def _should_create(remote: str, *, auto: bool, create: bool) -> bool:
    """Whether to create the missing repo: explicit ``--create``, else an interactive offer.

    ``--auto`` never creates on its own (bh-aee3) — a headless run that invents repositories
    because a name was typo'd is strictly worse than one that fails. Non-TTY likewise falls
    through to the existing hard failure: there is nobody to ask."""
    if create:
        return True
    if auto or not sys.stdin.isatty():
        return False
    return typer.confirm(f"  {remote} does not exist — create it private and empty?", default=False)


def _create_repo(remote: str, *, dry_run: bool) -> bool:
    """Create ``remote`` as a PRIVATE, EMPTY GitHub repo. Returns False on failure.

    Deliberately no ``--source``/``--push`` (bh-aee3). Empty is the requirement, not a
    shortcut: ``_wire_remote`` below owns the first push — backup, scaffold, ``remote add``,
    ``push main``, ``bd dolt push`` — and it refuses any remote that already carries refs. A
    seeded repo would abort that path, skip ``_take_backup``, and (because ``--source`` adds
    ``origin`` itself) make the whole wiring step a silent no-op."""
    if dry_run:
        typer.echo(f"  DRY-RUN: would create {remote} as a private, empty repo")
        return False
    typer.echo(f"  creating {remote} (private, empty)…")
    done = run(
        ["gh", "repo", "create", remote, "--private"],
        check=False,
        capture=True,
        timeout=GIT_TIMEOUT,
    )
    if done.returncode != 0:
        typer.echo(f"✗ gh repo create {remote} failed: {err_line(done)}", err=True)
        return False
    typer.echo(f"  ✓ created {remote}")
    return True


def _wire_remote(
    cfg: dict, *, dry_run: bool = False, auto: bool = False, create: bool = False
) -> None:
    """Idempotently scaffold HQ's distributable layout, add its git remote, and push ``main`` +
    ``refs/dolt/data`` — the FIRST time only. A no-op once the remote is already configured;
    refuses (never force-pushes) when the remote is unreachable or already carries content.
    Before the very first push, takes and verifies a three-level backup (``_take_backup``) — the
    highest-value backup point in HQ's lifecycle, since giving a database a remote is what makes
    a schema migration a one-way fleet decision (bh-e0y8.2's acceptance amendment)."""
    hq_dir = config.hq_dir()
    if not (hq_dir / ".beads").is_dir():
        return  # no local store yet (fresh --dry-run) — nothing to wire

    already = _git(["remote", "get-url", "origin"], hq_dir)
    if already.returncode == 0:
        typer.echo(f"✓ HQ remote already configured ({(already.stdout or '').strip()}) — no-op")
        return

    remote = _confirm_remote(cfg, auto=auto)
    if not remote:
        typer.echo(
            "  (hq.remote is unset and unresolvable — skipping remote wiring; set one with "
            f"`{config.BINARY_ALIAS} config set hq.remote <owner>/beadhive-hq`)"
        )
        return

    git_url, dolt_url = _remote_urls(remote)
    tag = "DRY-RUN " if dry_run else ""
    typer.echo(f"{tag}hq init: wiring remote {git_url}")

    # Reachability + diverging-content refusal — BEFORE any backup/push; never force-pushes.
    # Scoped to refs/heads/* (the code branch our non-force `git push origin main` could
    # actually conflict with) — NOT refs/dolt/data, which lives outside refs/heads/* entirely
    # and is exactly what backup level 3 below exists to protect when it's already present on
    # an otherwise-fresh remote (bh-e0y8.2 amendment).
    probe = _git(["ls-remote", "--heads", git_url], hq_dir)
    if probe.returncode != 0 and _remote_missing(probe):
        if _should_create(remote, auto=auto, create=create):
            created = _create_repo(remote, dry_run=dry_run)
            if dry_run:
                # Nothing was created, so there is no reachable remote to go on previewing
                # against — stop at 0 (a preview that reports failure is a lie).
                typer.echo("  DRY-RUN: stopping here — the remote does not exist yet")
                return
            if not created:
                raise typer.Exit(1)
            probe = _git(["ls-remote", "--heads", git_url], hq_dir)
    if probe.returncode != 0:
        hint = ""
        if _remote_missing(probe):
            hint = " — pass --create to create it private and empty"
        typer.echo(
            f"✗ HQ remote {git_url} is unreachable — refusing to push: {err_line(probe)}{hint}",
            err=True,
        )
        raise typer.Exit(1)
    if (probe.stdout or "").strip():
        typer.echo(
            f"✗ HQ remote {git_url} already has content — refusing to push over it (never "
            "force-pushes); point hq.remote at an empty repo, or reconcile manually.",
            err=True,
        )
        raise typer.Exit(1)

    plan = _take_backup(hq_dir, git_url, cfg, dry_run=dry_run)
    _print_backup_plan(plan)
    if dry_run:
        typer.echo(
            "  DRY-RUN: would write fleet.yaml/workspace.toml/hosts/, add remote origin, "
            "push main + refs/dolt/data"
        )
        return
    if not plan.ok:
        typer.echo("✗ pre-push backup could not be verified — refusing to push", err=True)
        raise typer.Exit(1)
    _prune_hq_backups_best_effort(cfg)

    for path in scaffold_layout(hq_dir, cfg):
        typer.echo(f"  ✓ wrote {path.relative_to(hq_dir)}")
    _commit_if_dirty(hq_dir, "chore(hq): scaffold fleet.yaml/workspace.toml/hosts/")

    add_origin = _git(["remote", "add", "origin", git_url], hq_dir)
    if add_origin.returncode:
        typer.echo(f"✗ git remote add origin failed: {err_line(add_origin)}", err=True)
        raise typer.Exit(1)
    # -u: this is the FIRST push ever for this remote, so main has no upstream tracking yet —
    # without it, a bare `git push`/`git pull` in ~/.beadhive/hq fails until someone runs
    # `push -u` by hand (bh-z9hl), and `safety.scan`'s ahead/behind detection (which every
    # `bh hq status`/`bh hq push` call below and `bh doctor`'s fleet-health section reads)
    # depends on `%(upstream:short)` being set at all — with no upstream it reports
    # `has_upstream=False` forever, silently hiding drift.
    push_main = _git(["push", "-u", "origin", "main"], hq_dir)
    if push_main.returncode:
        typer.echo(f"✗ git push origin main failed: {err_line(push_main)}", err=True)
        raise typer.Exit(1)

    remote_add = _bd(["dolt", "remote", "add", "origin", dolt_url], hq_dir)
    if remote_add.returncode:
        typer.echo(f"✗ bd dolt remote add failed: {err_line(remote_add)}", err=True)
        raise typer.Exit(1)
    pushed = engine.get_engine(cfg).push_state(hq_dir, message="hq init: first push")
    if pushed.returncode:
        typer.echo(f"✗ bd dolt push failed: {err_line(pushed)}", err=True)
        raise typer.Exit(1)

    typer.echo(f"✓ HQ remote wired ({git_url}) — pushed main + refs/dolt/data")


def _commit_if_dirty(hq_dir: Path, message: str) -> None:
    status = _git(["status", "--porcelain"], hq_dir)
    if not (status.stdout or "").strip():
        return
    _git(["add", "-A"], hq_dir)
    _git(["commit", "-m", message], hq_dir)


# ---- layout scaffold: fleet.yaml + workspace.toml + hosts/ ------------------


def scaffold_layout(hq_dir: Path, cfg: dict) -> list[Path]:
    """Write fleet.yaml + workspace.toml + allowed_signers + hosts/ into ``hq_dir`` —
    idempotent, only writes what's missing. Returns the paths actually written (empty ⇒ layout
    already complete).

    ``allowed_signers`` (bh-ijd4) is the fleet's trusted PUBLIC SSH keys, and HQ is its only
    sane home: it is the operator's by construction and is already the durable central store
    every host clones. Scaffolded EMPTY (comment header only) — bh never invents a trusted key;
    each host enrolls its own public key here when it runs ``bh host identity``."""
    written: list[Path] = []
    fleet = hq_dir / "fleet.yaml"
    if not fleet.exists():
        fleet.write_text(_fleet_yaml(cfg))
        written.append(fleet)
    workspace = hq_dir / "workspace.toml"
    if not workspace.exists():
        workspace.write_text(_workspace_toml(cfg))
        written.append(workspace)
    signers = hq_dir / git_identity.ALLOWED_SIGNERS
    if not signers.exists():
        signers.write_text(
            "# Fleet-wide trusted SSH signers (bh). One `<principal> <key>` per line;\n"
            "# hosts append their own PUBLIC key here as they are provisioned.\n"
        )
        written.append(signers)
    hosts = hq_dir / "hosts"
    hosts.mkdir(exist_ok=True)
    readme = hosts / "README.md"
    if not readme.exists():
        readme.write_text(
            "# hosts/\n\nPer-host manifests land here as `<host_id>.yaml` — the fleet/host "
            "config split (sub-molecule II).\n"
        )
        written.append(readme)
    return written


def _fleet_yaml(cfg: dict) -> str:
    import io

    from ruamel.yaml import YAML

    subset = {k: cfg[k] for k in _FLEET_KEYS if k in cfg}
    yaml = YAML()
    yaml.indent(mapping=2, sequence=4, offset=2)
    buf = io.StringIO()
    yaml.dump(subset, buf)
    return buf.getvalue()


def _workspace_toml(cfg: dict) -> str:
    """git-workspace providers are fleet truth (the clone PATH stays host-local) — copy the
    operator's own workspace*.toml content when resolvable, else a placeholder a later host can
    fill in. git-workspace is a required dep (bh-hsus.4), not a config toggle, so this reads
    `config_paths` unconditionally rather than gating on an enabled flag."""
    for path in gitworkspace.config_paths(cfg):
        try:
            return path.read_text()
        except OSError:
            continue
    return "# git-workspace providers — none configured on this host yet.\n"


# ---- three-level pre-push backup (bh-e0y8.2 acceptance amendment) -----------


@dataclass
class BackupTarget:
    """One backup level's outcome (or plan preview, on ``--dry-run``)."""

    name: str
    path: str = ""
    size_bytes: int = 0
    verified: bool = False
    detail: str = ""


@dataclass
class BackupPlan:
    dry_run: bool
    targets: list[BackupTarget] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.targets) and all(t.verified for t in self.targets)


def _backup_root(cfg: dict) -> Path:
    return config.home() / "hq-backups"


def _take_backup(hq_dir: Path, git_url: str, cfg: dict, *, dry_run: bool) -> BackupPlan:
    """Three-level pre-push backup: (1) a portable JSONL interchange export, (2) a full-
    fidelity copy of the local Dolt store (branches, history, working set), and (3) — only
    when the remote already carries one — a copy of its existing ``refs/dolt/data`` kept
    outside ``refs/dolt/`` so dolt's own ref-globbing never sees it. Each level is VERIFIED,
    not merely written. ``dry_run`` previews targets/sizes with zero writes.

    Level (2) picks its OWN mechanism from a FILESYSTEM FACT, never a ``bd dolt status`` mode
    inference (bh-areg.1): when the embedded store directory is actually there
    (:func:`store_locator.has_embedded_store`), tar it — exactly as before, byte-for-byte
    unchanged for an embedded HQ. Otherwise (owned/shared/external — the store isn't under
    ``.beads/`` at all, bh-u562.1 finding 8) take the backup OVER THE CONNECTION instead
    (:func:`_backup_dolt_native`) rather than trying to locate a directory to tar: a
    directory-tar approach hard-blocks a future mode where the store lives on another host
    entirely, and a connection-oriented backup costs nothing extra here (bh-areg.1's design
    constraint)."""
    backup_dir = _backup_root(cfg) / datetime.now(UTC).strftime("%Y-%m-%d")
    plan = BackupPlan(dry_run=dry_run)
    plan.targets.append(_backup_jsonl(hq_dir, backup_dir, cfg, dry_run=dry_run))
    if store_locator.has_embedded_store(hq_dir):
        plan.targets.append(_backup_tar(hq_dir, backup_dir, dry_run=dry_run))
    else:
        plan.targets.append(_backup_dolt_native(hq_dir, backup_dir, cfg, dry_run=dry_run))
    plan.targets.append(_backup_remote_ref(hq_dir, git_url, dry_run=dry_run))
    return plan


def _print_backup_plan(plan: BackupPlan) -> None:
    tag = "DRY-RUN " if plan.dry_run else ""
    typer.echo(f"  {tag}pre-push backup:")
    for t in plan.targets:
        mark = "○" if plan.dry_run else ("✓" if t.verified else "✗")
        size = f" ({t.size_bytes:,}B)" if t.size_bytes else ""
        where = f" {t.path}" if t.path else ""
        typer.echo(f"    {mark} {t.name}{where}{size} — {t.detail}")
    # A backup nobody knows how to consume is a false sense of safety: three green checkmarks
    # at the most dangerous moment in HQ's lifecycle, with recovery left to a future operator
    # under duress. Name the restore path here, while the artifacts are on screen (bh-cmqp.1).
    if not plan.dry_run:
        typer.echo(
            f"    restore with `{config.BINARY_ALIAS} hq restore --list`, then "
            f"`{config.BINARY_ALIAS} hq restore --confirm`"
        )


def _prune_hq_backups_best_effort(cfg: dict) -> None:
    """Keep-N prune of ``_backup_root``'s dated directories, right after a NEW one is taken
    and verified (bh-cmqp.2) — never before ``plan.ok`` is confirmed. Best-effort: a pruning
    failure (permissions, a half-removed dir) must never turn a successful, verified backup +
    push into a hard failure, so this only ever echoes what happened, never raises."""
    from . import backup as backup_mod

    try:
        result = backup_mod.prune_hq_backups(cfg)
    except OSError as exc:
        typer.echo(f"  (hq-backups prune skipped: {exc})")
        return
    if result.removed:
        typer.echo(
            f"  pruned {len(result.removed)} old hq-backups "
            f"({', '.join(result.removed)}) — {result.reclaimed_bytes:,}B reclaimed"
        )


def _issue_count(hq_dir: Path) -> int:
    """HQ's own reported issue count (``bd status``), independent of the JSONL export itself —
    the cross-check the amendment requires. -1 when it can't be determined."""
    res = _bd(["status", "--json", "--no-activity"], hq_dir)
    try:
        data = json.loads(res.stdout or "{}")
    except ValueError:
        return -1
    return int((data.get("summary") or {}).get("total_issues", -1))


def _backup_jsonl(hq_dir: Path, backup_dir: Path, cfg: dict, *, dry_run: bool) -> BackupTarget:
    out = backup_dir / "hq-issues.jsonl"
    if dry_run:
        return BackupTarget(
            name="jsonl-export",
            path=str(out),
            detail="would `bd export` + verify line count == reported issue count",
        )
    backup_dir.mkdir(parents=True, exist_ok=True)
    res = engine.get_engine(cfg).export_jsonl(hq_dir, out)
    if res.returncode or not out.exists():
        return BackupTarget(
            name="jsonl-export", path=str(out), detail=f"bd export failed: {err_line(res)}"
        )
    lines = sum(1 for _ in out.open())
    count = _issue_count(hq_dir)
    verified = count >= 0 and lines == count
    detail = (
        f"{lines} lines (bd reports {count} issues)"
        if count >= 0
        else f"{lines} lines (issue count unverifiable)"
    )
    return BackupTarget(
        name="jsonl-export",
        path=str(out),
        size_bytes=out.stat().st_size,
        verified=verified,
        detail=detail,
    )


def _absent_store_reason(hq_dir: Path) -> str:
    """Why the embedded store directory isn't there — the difference between a refusal that
    explains itself and one that reads as a bug.

    A plain filesystem-fact message, never a ``bd dolt status`` mode probe (bh-areg.1): that
    probe's own JSON shape is ambiguous by mode (bh-u562.1 finding 9 — it omits the ``mode``
    key entirely for owned and local-external), so asking it here would either wrongly claim
    "bd could not report" for a mode that in fact answered fine, or mislabel one non-embedded
    mode as another. Only ever reached on the miss path — this bead's caller
    (``_take_backup``) already routes a non-embedded HQ to :func:`_backup_dolt_native`
    instead, so this now only fires for an HQ that looks embedded from the outside but whose
    store directory is missing or unreadable (or a direct/test call)."""
    return (
        f"no {store_locator.embedded_store_dir(hq_dir)} directory — either HQ's dolt engine "
        "is not in embedded mode, or the embedded store is missing/broken"
    )


def _backup_tar(hq_dir: Path, backup_dir: Path, *, dry_run: bool) -> BackupTarget:
    src = store_locator.embedded_store_dir(hq_dir)
    out = backup_dir / "hq-embeddeddolt.tar.gz"
    # NOT verified, and checked before the dry-run branch so the preview can't promise a
    # tarball the real run would refuse to take. This is the only level carrying branches,
    # history and working set — the JSONL level is the format-independent FLOOR, not a
    # substitute (see hq_restore's module docstring). Reporting verified=True here handed the
    # operator a green checkmark on an empty backup and let `plan.ok` wave the first push
    # through, which is exactly the "three green checkmarks and nothing to restore from"
    # failure this backup exists to prevent (bh-kobw).
    if not src.is_dir():
        return BackupTarget(
            name="embeddeddolt-tar", path=str(out), detail=_absent_store_reason(hq_dir)
        )
    if dry_run:
        size = sum(f.stat().st_size for f in src.rglob("*") if f.is_file())
        return BackupTarget(
            name="embeddeddolt-tar",
            path=str(out),
            size_bytes=size,
            detail=f"would tar {src} (excluding {_DOLT_EXCLUDE_DIR}/) + verify listing",
        )
    backup_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(out, "w:gz") as tf:
        tf.add(
            src,
            arcname="embeddeddolt",
            filter=lambda ti: None if _DOLT_EXCLUDE_DIR in Path(ti.name).parts else ti,
        )
    try:
        with tarfile.open(out, "r:gz") as tf:
            names = tf.getnames()
        verified = bool(names)
    except tarfile.TarError:
        names, verified = [], False
    return BackupTarget(
        name="embeddeddolt-tar",
        path=str(out),
        size_bytes=out.stat().st_size,
        verified=verified,
        detail=f"{len(names)} entries",
    )


def _backup_dolt_native(
    hq_dir: Path, backup_dir: Path, cfg: dict, *, dry_run: bool
) -> BackupTarget:
    """The full-fidelity level for a NON-embedded dolt engine (owned/shared/external,
    bh-u562.1) — over the CONNECTION (``bd backup add`` + ``bd backup sync``, wrapping Dolt's
    own ``CALL DOLT_BACKUP``) rather than locating and tarring a directory on local disk. This
    is bh-areg.1's binding design constraint: a directory-tar approach works for embedded/
    owned/(future local-colocated) but hard-blocks a future mode where the store lives on
    another host entirely — you cannot tar a directory on a machine you can't reach. Asking
    the connected engine for a backup costs nothing extra for THIS bead's target mode (shared)
    and keeps working unchanged if the store ever moves off this host.

    Verified the same way the tar level is: NOT by trusting ``bd``'s exit code alone (measured
    against a real bd binary — ``bd backup add``/``sync`` both fail cleanly, non-zero exit,
    against an empty or missing store, but "the command exited 0" and "something restorable
    actually landed on disk" are two different claims, and conflating them is exactly bh-kobw's
    shape) — by re-checking the destination actually holds real content afterward."""
    out = backup_dir / _DOLT_NATIVE_DIRNAME
    if dry_run:
        return BackupTarget(
            name="dolt-native-backup",
            path=str(out),
            detail="would `bd backup add` + `bd backup sync` (Dolt-native, over the connection)",
        )
    backup_dir.mkdir(parents=True, exist_ok=True)
    res = engine.get_engine(cfg).backup(hq_dir, out)
    if res.returncode:
        return BackupTarget(
            name="dolt-native-backup", path=str(out), detail=f"bd backup failed: {err_line(res)}"
        )
    size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file()) if out.is_dir() else 0
    verified = size > 0
    detail = f"{size:,}B" if verified else "bd backup reported success but wrote nothing"
    return BackupTarget(
        name="dolt-native-backup", path=str(out), size_bytes=size, verified=verified, detail=detail
    )


def _ref_safe(text: str) -> str:
    """Sanitize a string into a valid single git ref *component* (no `~^: ?*[\\`, no leading
    dot, collapsed runs of disallowed chars)."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-.") or "unknown"


def _bd_version() -> str:
    res = run(["bd", "--version"], check=False, capture=True, timeout=GIT_TIMEOUT)
    tokens = (res.stdout or res.stderr or "").split()
    # "bd version HEAD-af076b6 (Homebrew)" -> "HEAD-af076b6"; tolerate any other shape.
    raw = tokens[2] if len(tokens) >= 3 else (tokens[-1] if tokens else "unknown")
    return _ref_safe(raw)


def _schema_version(hq_dir: Path) -> str:
    """Best-effort schema marker for the backup ref name: bd exposes no dedicated read-only
    "Dolt migration counter" in embedded mode, so this reads the ``schema_version`` field ``bd
    doctor --json`` reports even from its embedded-mode "unsupported" response (verified against
    a real bd binary) — stable and machine-readable, if not the exact migration ordinal."""
    res = _bd(["doctor", "--json"], hq_dir)
    try:
        data = json.loads(res.stdout or "{}")
    except ValueError:
        data = {}
    return _ref_safe(str(data.get("schema_version", "unknown")))


def _backup_ref_name(hq_dir: Path) -> str:
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    return f"refs/backup/dolt-data-schema-{_schema_version(hq_dir)}-bd-{_bd_version()}-{today}"


def _backup_remote_ref(hq_dir: Path, git_url: str, *, dry_run: bool) -> BackupTarget:
    """Copy any PRE-EXISTING ``refs/dolt/data`` on the remote to a backup ref kept outside
    ``refs/dolt/`` before it could ever be overwritten. A freshly-created remote (the common
    case for HQ's first push) has none — that's trivially "verified", nothing to do."""
    probe = _git(["ls-remote", git_url, "refs/dolt/data"], hq_dir)
    sha = (
        (probe.stdout or "").split()[0]
        if probe.returncode == 0 and (probe.stdout or "").strip()
        else ""
    )
    if not sha:
        return BackupTarget(
            name="remote-dolt-data-ref",
            verified=True,
            detail="no pre-existing refs/dolt/data on the remote — nothing to back up",
        )
    backup_ref = _backup_ref_name(hq_dir)
    if dry_run:
        return BackupTarget(
            name="remote-dolt-data-ref",
            path=backup_ref,
            detail=f"would copy refs/dolt/data ({sha[:12]}) → {backup_ref}",
        )
    tmp_ref = "refs/hq-backup-tmp"
    fetched = _git(["fetch", git_url, f"refs/dolt/data:{tmp_ref}"], hq_dir)
    if fetched.returncode:
        return BackupTarget(
            name="remote-dolt-data-ref",
            path=backup_ref,
            detail=f"fetch of existing refs/dolt/data failed: {err_line(fetched)}",
        )
    pushed = _git(["push", git_url, f"{tmp_ref}:{backup_ref}"], hq_dir)
    _git(["update-ref", "-d", tmp_ref], hq_dir)  # local temp ref is scratch — always clean up
    if pushed.returncode:
        return BackupTarget(
            name="remote-dolt-data-ref",
            path=backup_ref,
            detail=f"push of backup ref failed: {err_line(pushed)}",
        )
    verify = _git(["ls-remote", git_url, backup_ref], hq_dir)
    verified = verify.returncode == 0 and sha in (verify.stdout or "")
    return BackupTarget(
        name="remote-dolt-data-ref",
        path=backup_ref,
        verified=verified,
        detail=f"copied {sha[:12]} → {backup_ref}",
    )
