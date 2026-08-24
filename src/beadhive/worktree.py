"""ws-managed git worktrees in a shadow tree outside $GIT_WORKSPACE.

Each worktree is a normal linked `git worktree` of a hive's main clone
($GIT_WORKSPACE/<provider>/<org>/<repo>), but its working dir lives under a single
shadow root (default ~/.beadhive/worktrees, $BH_WORKTREES / config worktrees.root) mirroring
the triplet path:  <root>/<provider>/<org>/<repo>/<leaf>. Living outside the workspace
means no collision with git-workspace repo roots, "ours vs hand-made" is just a
path-prefix test, and bulk cleanup is one subtree.

Every managed branch is prefixed `wt/` (applied once, centrally), so a worktree branch is
obvious at a glance. Each mode only computes the suffix after it (templates configurable):
  --bead ID    -> wt/ + worktrees.bead_branch  (default "bead/{kind}/{id}") -> wt/bead/<type>/<id>
  --branch B   -> wt/ + B                         (not a full override)   -> wt/<B>
  neither      -> wt/ + worktrees.session_branch (default "session/{ts}-{rand}")
The leaf is the sanitized last path segment of the branch (bead ids / session ids are
already unique, so the namespace prefix is dropped for a clean dir name).

Post-create init is declarative config (no scripting): a list of {run, if_exists?} rules.
Global worktrees.init runs first, then the hive's worktree_init. if_exists is a glob
relative to the new worktree; omit it to always run. Failures warn and continue.
"""

from __future__ import annotations

import contextlib
import contextvars
import datetime
import importlib
import json
import os
import secrets  # noqa: F401 - compatibility patch seam
import shlex  # noqa: F401 - compatibility patch seam
import shutil  # noqa: F401 - compatibility patch seam
import subprocess  # noqa: F401 - compatibility patch seam
import tempfile  # noqa: F401 - compatibility patch seam
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import typer

from . import (
    bd,
    config,
    converge,  # noqa: F401 - compatibility patch seam
    ghpr,  # noqa: F401 - compatibility patch seam
    host,  # noqa: F401 - compatibility patch seam
    otel,
    plugins,
    registry,
    test_report,  # noqa: F401 - compatibility patch seam
    triage_store,  # noqa: F401 - compatibility patch seam
    validation_ledger,  # noqa: F401 - compatibility patch seam
    worktree_merge,
    wt_status,
)  # noqa: F401 - compatibility patch seams retained on the facade
from .identity import workspace_identity
from .run import missing_binary, retry_on_index_lock, run  # noqa: F401 - compatibility patch seams

# Re-export the integration-merge tier (in worktree_merge) so ws.worktree.<name> still works.
merge_no_ff = worktree_merge.merge_no_ff
merge_conflict_paths = worktree_merge.merge_conflict_paths
merge_with_union = worktree_merge.merge_with_union
try_merge_rebase = worktree_merge.try_merge_rebase
_all_union_eligible = worktree_merge._all_union_eligible
_ref_sha = worktree_merge._ref_sha
_try_union_tier = worktree_merge._try_union_tier

_RAND_BYTES = 2  # 4 hex chars — collision cover for two sessions in the same second

# Classification is mostly git / bead-store subprocess I/O.  A small fixed ceiling lets a hub
# make progress across a fleet without stampeding a shared filesystem or store service.
_CLASSIFY_MAX_WORKERS = 8


def _run_git(args, **kw):
    """Compatibility facade for ``worktree_git.impl__run_git``."""
    return _worktree_git.impl__run_git(args, **kw)


# ---- naming -----------------------------------------------------------------


WT_PREFIX = "wt/"  # every managed-worktree branch starts here, whatever the mode
VERIFY_LEAF_PREFIX = "verify-"  # ephemeral clean-checkout worktrees (clean_checkout); not a seat
# Per-invocation verify- dirs (bh-nikb): each clean_checkout gets its own
# verify-<branch-leaf>-<rand6> dir, so two processes validating the same branch never share (and
# never destroy) one deterministic path. A liveness marker inside each dir (the merge-slot
# HolderToken analog, work_group._slot_holder) lets a global sweep reap orphans left by a killed
# run without ever touching a live sibling.
VERIFY_MARKER = ".bh-verify.json"  # liveness marker written inside each verify- dir
_VERIFY_RAND_BYTES = 3  # 6 hex chars — per-invocation isolation suffix
_VERIFY_CREATE_ATTEMPTS = 8  # mkdtemp-style retry budget on suffix collision
_VERIFY_GRACE_SECONDS = 5 * 60  # marker missing/unreadable: reap only past this window
_VERIFY_TTL_SECONDS = 24 * 60 * 60  # hard age backstop (reboots / cross-host / shared FS)
# A work-group's shared branch is `wt/batch/<group>`, but its worktree DIR carries a `batch-`
# prefix (`batch-<group>`) so it can never resolve onto a *bead* worktree that shares the group
# name. The load-bearing case: collapsed mode uses the epic id as the group, whose coordinator
# seat is `wt/bead/epic/<epic>` — a bare-`<epic>` leaf, i.e. the SAME dir the batch would want.
# Without the prefix `ensure` returns the pre-existing seat worktree and commits land on the seat
# branch instead of `wt/batch/<epic>`, breaking `merge --group`.
BATCH_BRANCH_PREFIX = "batch/"  # branch namespace: wt/batch/<group>
BATCH_LEAF_PREFIX = "batch-"  # worktree-dir namespace: <root>/.../batch-<group>

# Every bead branch is wt/bead/<type>/<id>. <type> is a legible role assertion in the ref path:
# CONTAINER_TYPES are landing targets — an epic at ANY tier (a workstream is an epic-of-epics, per
# xn3o.7) opens its own container/integration line; a leaf `issue` is never a landing target. The
# integration-target climb probes only the container namespace, so it stays a pure-git string walk
# (no bd call). `mol/<epic>` is retired: a container branch is just wt/bead/epic/<id>, in the one
# universal wt/bead/… namespace.
CONTAINER_TYPES = ("epic",)
BEAD_KINDS = ("epic", "issue")  # container namespace(s) first — the parse/probe order
_BEAD_PREFIX = f"{WT_PREFIX}bead/"  # the wt/bead/ ref prefix; <type>/<id> follows


def _ts_rand(now=None, rand=None):
    """Fixed-width basic-ISO UTC timestamp (YYYYMMDDTHHMMSSZ) + short random hex. The ts
    leads, so lexical sort == chronological; both are git-ref / filesystem safe."""
    now = now or datetime.datetime.now(datetime.UTC)
    ts = now.strftime("%Y%m%dT%H%M%SZ")
    rnd = rand if rand is not None else os.urandom(_RAND_BYTES).hex()
    return ts, rnd


def _session_id(now=None, rand=None) -> str:
    """`<ts>-<rand>` — the session branch/leaf tail (see _ts_rand for sortability)."""
    ts, rnd = _ts_rand(now, rand)
    return f"{ts}-{rnd}"


def _leaf(branch: str) -> str:
    """Worktree-directory leaf for a managed branch/ref. Normally the sanitized last path segment
    ('wt/bead/issue/ag-7' -> 'ag-7'). A batch branch is special-cased to a `batch-<group>` leaf
    ('wt/batch/<group>' -> 'batch-<group>') so the shared batch worktree gets its OWN directory and
    can never resolve onto a bead worktree sharing the group name — in collapsed mode the group IS
    the epic id, whose seat `wt/bead/epic/<epic>` would otherwise be the same dir (ev1l).
    Idempotent on an already-computed leaf (`batch-<group>` has no `batch/` segment).
    """
    body = branch.removeprefix(WT_PREFIX)
    if body.startswith(BATCH_BRANCH_PREFIX):
        return BATCH_LEAF_PREFIX + registry.sanitize(body[len(BATCH_BRANCH_PREFIX) :])
    return registry.sanitize(branch.rsplit("/", 1)[-1])


def _suffix(cfg, bead="", branch="", kind="issue", now=None, rand=None) -> str:
    """The branch suffix (everything after the wt/ prefix) for each creation mode. Adding a
    fourth mode = adding a branch here; the wt/ prefix is applied once in _branch_and_leaf.
    A bead branch carries its `<type>` segment (`bead/{kind}/{id}`); callers resolve `kind`
    (`_bead_kind`) — the leaf default 'issue' keeps a bare template call well-formed."""
    wcfg = config.worktrees_cfg(cfg)
    if bead:
        tmpl = str(wcfg.get("bead_branch", "bead/{kind}/{id}"))
        return tmpl.format(id=bead, kind=kind or "issue")
    if branch:
        return branch
    ts, rnd = _ts_rand(now=now, rand=rand)
    tmpl = str(wcfg.get("session_branch", "session/{ts}-{rand}"))
    return tmpl.format(ts=ts, rand=rnd, id=f"{ts}-{rnd}")


def apply_prefix(suffix: str) -> str:
    """Prepend the managed wt/ prefix to a branch suffix, never doubling an existing wt/."""
    return WT_PREFIX + suffix.removeprefix(WT_PREFIX).lstrip("/")


def _branch_and_leaf(cfg, bead="", branch="", kind="issue", now=None, rand=None):
    """(branch, leaf). Every mode yields a suffix; we always prepend wt/ (so a managed
    worktree is obvious from the branch), normalizing to never double a wt/wt/. The leaf is
    the last path segment — for a bead branch that is `<id>` regardless of `<type>`, so a
    worktree dir is named the same under the new namespace as before."""
    br = apply_prefix(_suffix(cfg, bead=bead, branch=branch, kind=kind, now=now, rand=rand))
    return br, _leaf(br)


def _bead_kind(main: Path, bead: str, kind: str = "") -> str:
    """The `<type>` segment for a bead's branch `wt/bead/<type>/<id>`. An explicit `kind`
    (resolved from the bead's issue_type at a write seam) wins and is authoritative for a
    branch that does not exist yet. Otherwise probe the container namespace by exact ref — an
    already-opened epic/container answers — and fall back to the leaf default 'issue'. At most
    one show-ref, no bd call, so it stays cheap on the read path (`locate`)."""
    if kind:
        return kind
    for t in CONTAINER_TYPES:
        if _branch_exists(main, f"{_BEAD_PREFIX}{t}/{bead}"):
            return t
    return "issue"


def _bead_id_from_branch(branch: str) -> str | None:
    """Parse the bead id out of a real `wt/bead/<type>/<id>` ref (dots preserved). Returns None
    for a non-bead branch (batch/session). Tolerates a legacy tail-less `wt/bead/<id>` ref so a
    pre-migration worktree still classifies."""
    if not branch or not branch.startswith(_BEAD_PREFIX):
        return None
    rest = branch[len(_BEAD_PREFIX) :]
    head, sep, tail = rest.partition("/")
    if sep and head in BEAD_KINDS:
        return tail or None  # wt/bead/<type>/<id>
    return rest or None  # legacy wt/bead/<id>


# ---- hive / path resolution --------------------------------------------------


def wt_dir(entry, leaf: str) -> Path:
    """<root>/<provider>/<org>/<repo>/<leaf> — mirrors registry.hive_dir under the shadow root."""
    root = config.worktrees_root()
    return root / str(entry["provider"]) / str(entry["org"]) / str(entry["repo"]) / leaf


def _resolve_entry(cfg, hive):
    """The managed_repos entry for `hive`, or (when hive is empty) the hive owning cwd, resolved
    through the shared `registry.current_hive` cwd resolver (identity -> shadow-root reverse-map
    -> synthesize; DRY with the `work`/`plan` bead-less defaults). Clear error only when cwd
    belongs to no hive at all."""
    if hive:
        return registry.resolve_hive(cfg, hive)
    entry = registry.current_hive(cfg)
    if entry is None:
        typer.echo("✗ no --hive given and cwd is not a repo under $GIT_WORKSPACE", err=True)
        raise typer.Exit(1)
    return entry


def _entry_for_path(cfg, path: Path):
    """Reverse a worktree path back to its hive entry via the triplet segments under root."""
    root = config.worktrees_root()
    try:
        rel = path.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        typer.echo(f"✗ {path} is not under the managed worktree root {root}", err=True)
        raise typer.Exit(1) from None
    parts = rel.parts
    if len(parts) < 4:
        typer.echo(f"✗ {path} is not a <provider>/<org>/<repo>/<leaf> worktree", err=True)
        raise typer.Exit(1)
    provider, org, repo = parts[0], parts[1], parts[2]
    for e in cfg.get("managed_repos", []) or []:
        if (str(e["provider"]), str(e["org"]), str(e["repo"])) == (provider, org, repo):
            return e
    return {"provider": provider, "org": org, "repo": repo, "prefix": repo}


# ---- init rules -------------------------------------------------------------


def _rules(cfg, entry):
    """Compatibility facade for ``worktree_verify.impl__rules``."""
    return _worktree_verify.impl__rules(cfg, entry)


def run_init(cfg, entry, path: Path, verify_only: bool = False):
    """Compatibility facade for ``worktree_verify.impl_run_init``."""
    return _worktree_verify.impl_run_init(cfg, entry, path, verify_only)


def provision_observaloop(cfg, entry, target: Path) -> None:
    """Best-effort per-hive observaloop profile provisioning + worktree overlay, run on a TRUE
    worktree create (after ``run_init``, from ``_do_add`` — the chokepoint that ``clean_checkout``
    bypasses, so ephemeral ``verify-`` worktrees never reach here).

    Gated and import-cheap by design: the default (observaloop disabled) path is a single
    ``config.observaloop_enabled`` check and imports **no** observaloop module. Only when enabled do
    we lazily import the observaloop seams, derive the per-hive profile name, idempotently
    ``ensure_profile`` + ``up`` (a profile is per-hive, shared across its worktrees), resolve the
    OTLP endpoint, and write ``<worktree>/.ws/otel.env`` so a ``ws`` invocation there exports to the
    hive profile (Phase B loader). Mirrors ``run_init``'s warn-and-continue contract: observaloop
    unavailable / docker down / any exception warns and returns — it NEVER raises and NEVER blocks
    worktree creation."""
    if target.name.startswith(VERIFY_LEAF_PREFIX):
        return  # defensive: ephemeral clean-checkout worktree — not a seat, never provisioned
    if not config.observaloop_enabled(cfg, entry):
        return  # default/off path: no observaloop import, nothing provisioned or written
    try:
        from . import observaloop, observaloop_env  # lazy: confine the surface to the enabled path

        name = config.observaloop_profile_name(cfg, entry)
        if not name:
            typer.echo("  ⚠ observaloop: no profile name for hive — skipping overlay", err=True)
            return
        observaloop.ensure_profile(name, cfg)  # idempotent server-side; best-effort
        observaloop.up(name, cfg)  # idempotent; the hive's worktrees share the one profile
        endpoint = observaloop.endpoint_for(name, config.otel_protocol(cfg), cfg)
        if not endpoint:
            typer.echo(
                "  ⚠ observaloop: no endpoint resolved (unavailable / down) — skipping overlay",
                err=True,
            )
            return
        observaloop_env.write_worktree_env(target, name, endpoint)
        typer.echo(f"  → observaloop profile '{name}' ready; wrote .bh/otel.env → {endpoint}")
    except Exception as exc:  # best-effort: never block worktree creation (mirror run_init)
        typer.echo(f"  ⚠ observaloop: provisioning failed ({exc}) — continuing", err=True)


# ---- operations -------------------------------------------------------------


def _branch_exists(main: Path, branch: str) -> bool:
    """True iff `branch` is a local head in the hive's main clone."""
    return (
        _run_git(
            ["git", "-C", str(main), "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            check=False,
        ).returncode
        == 0
    )


def _container_at(main: Path, parent: str) -> str:
    """The started container branch `wt/bead/<type>/<parent>` for `parent` if one exists (probed
    by exact `show-ref` over CONTAINER_TYPES), else ''."""
    for t in CONTAINER_TYPES:
        branch = f"{_BEAD_PREFIX}{t}/{parent}"
        if _branch_exists(main, branch):
            return branch
    return ""


def _id_prefix_base(main: Path, bead: str, integration: str) -> str:
    """Nearest started container ancestor by the dotted `<parent>.<n>` id chain (pure git + string;
    skips issue-type ancestors for free), falling back to `integration` at the dotless root."""
    node = bead or ""
    while True:
        parent, sep, _ = node.rpartition(".")  # split on the LAST '.'
        if not sep or not parent:
            return integration  # dotless root → the hive integration branch
        branch = _container_at(main, parent)
        if branch:
            return branch  # nearest started container ancestor wins
        node = parent  # climb; a non-container (issue) ancestor is skipped


def _parent_link_base(main: Path, bead: str, integration: str) -> str:
    """Nearest started container ancestor by the bd parent-child link — the source of truth after
    a re-parent/split, where the dotted id keeps its birth prefix but the real parent has moved.
    Climbs `bd show <id>`'s `parent` field, checking for a started container at each hop. Returns
    `integration` on any bd failure (bead/DB absent) or a missing parent, so the caller can fall
    back to the id-prefix climb — byte-identical to the pre-parent-link behavior when bd is silent
    or the two agree."""
    seen: set[str] = set()
    node = bead or ""
    try:
        while node and node not in seen:
            seen.add(node)
            data = bd.show(node, main)
            parent = str((data or {}).get("parent") or "")
            if not parent:
                return integration
            branch = _container_at(main, parent)
            if branch:
                return branch
            node = parent  # climb past a non-container (issue) parent
    except Exception:  # bd unavailable / malformed — defer to the id-prefix climb
        return integration
    return integration


def integration_base(entry, bead: str, integration: str) -> str:
    """Resolve the integration target for a bead's merges — the branch its worktree forks from and
    its merges land on — as the NEAREST started container ancestor, falling back to `integration`
    (the hive branch, main) at the root.

    A container is "started" iff its branch `wt/bead/<type>/<parent>` exists in the hive's main
    clone (only kickoff opens it). Resolution follows the **bd parent-child link first** — the
    source of truth after any re-parent/split (see bh-2m6v / bh-bfoy): a child re-parented under a
    new epic but keeping its original `<oldepic>.<n>` dotted id lands on its parent-link container,
    not the stale prefix container. The dotted-id climb is the fallback: used when bd is silent
    (no DB / synthetic ids) or when the two already agree — so a never-reparented bead (the common
    case) and every bd-free caller stay byte-identical to before. Nearest-first gives the tightest
    isolation: a child lands on its own epic even when a workstream exists above."""
    main = registry.hive_dir(entry)
    id_base = _id_prefix_base(main, bead, integration)
    link_base = _parent_link_base(main, bead, integration)
    # Prefer the parent-link container whenever bd resolves one that differs from the stale prefix.
    if link_base != integration and link_base != id_base:
        return link_base
    return id_base


def container_conflict(entry, bead: str, integration: str) -> tuple[str, str] | None:
    """Return `(id_prefix_base, parent_link_base)` when the dotted-id prefix and the bd parent-link
    resolve to two DIFFERENT started containers — a genuine re-parent/split ambiguity a merge must
    refuse rather than silently pick (see bh-2m6v). Returns None when they agree, or when only one
    side names a real container (the unambiguous re-parent case: the stale prefix container is gone,
    so integration_base's parent-link answer is trusted)."""
    main = registry.hive_dir(entry)
    id_base = _id_prefix_base(main, bead, integration)
    link_base = _parent_link_base(main, bead, integration)
    if id_base != integration and link_base != integration and id_base != link_base:
        return (id_base, link_base)
    return None


def container_epic_closed(entry, base: str) -> bool:
    """True iff `base` is a container branch whose epic is CLOSED — a merge must never resurrect or
    land onto a landed epic's container (see bh-2m6v). False for the integration branch, a
    non-container ref, or when bd cannot resolve the epic (fail open — the merge's other guards
    still apply)."""
    epic = _bead_id_from_branch(base)
    if not epic:
        return False
    try:
        data = bd.show(epic, registry.hive_dir(entry))
    except Exception:
        return False
    return bool(data) and str(data.get("status", "")) == "closed"


# `ensure_integration_branch` retired (xn3o.6): under the collapsed container==seat model the
# container branch IS `wt/bead/epic/<id>` — a first-class managed-worktree branch — so "open the
# container" and "attach a worktree" are one op. `worktree.ensure(cfg, hive, bead=<epic>,
# kind="epic")` opens the branch off `integration_base(<epic>)` AND attaches the seat, subsuming
# the old branch-only seam. `start`/`assign`/`_maybe_open_molecule` all route through `ensure`.


def _record_wt_event(op: str, outcome: str = "ok", *, hive: str = "", leaf: str = "") -> None:
    """Best-effort, gated emission of the ``ws.worktree.events`` metric at a create/remove/prune
    seam. Gated on ``otel.is_active()`` so the off-path is zero-cost + opentelemetry-import-free,
    and wrapped so a telemetry failure NEVER blocks the underlying worktree op. Ephemeral
    ``verify-`` clean-checkout worktrees aren't a seat, so they emit nothing; ``bh.hive`` /
    ``ws.worktree`` are tagged when known."""
    if not otel.is_active() or (leaf and leaf.startswith(VERIFY_LEAF_PREFIX)):
        return
    try:
        attrs: dict[str, str] = {}
        if hive:
            attrs["bh.hive"] = str(hive)
        if leaf:
            attrs["bh.worktree"] = leaf
        otel.record_worktree_event(op, outcome, attrs)
    except Exception:  # best-effort: telemetry must never block a worktree op
        pass


def _record_wt_op_duration(
    op: str, seconds: float, outcome: str = "ok", *, hive: str = "", leaf: str = ""
) -> None:
    """Best-effort, gated emission of the ``ws.worktree.op.duration`` histogram for a worktree git
    op (the wall time of the ``git worktree add|remove`` subprocess). Mirrors ``_record_wt_event``'s
    contract exactly: gated on ``otel.is_active()`` (off-path zero-cost, opentelemetry-import-free),
    ephemeral ``verify-`` clean-checkout worktrees excluded (not a seat), and wrapped so a telemetry
    failure NEVER blocks the op. ``bh.hive`` / ``ws.worktree`` are tagged when known."""
    if not otel.is_active() or (leaf and leaf.startswith(VERIFY_LEAF_PREFIX)):
        return
    try:
        attrs: dict[str, str] = {"bh.worktree.op": op, "bh.worktree.outcome": outcome}
        if hive:
            attrs["bh.hive"] = str(hive)
        if leaf:
            attrs["bh.worktree"] = leaf
        otel.record_worktree_op_duration(seconds, attrs)
    except Exception:  # best-effort: telemetry must never block a worktree op
        pass


def _consult_wt_create(
    cfg, entry, *, main: Path, branch: str, target: Path, start_point: str
) -> Path | None:
    """Generic delegation seam for a worktree *create*: the first enabled plugin (registry
    order) defining ``wt_create`` wins. ``None`` (or no enabled plugin defining the hook) means
    "not handled" — the native `git worktree add` runs instead. A ``typer.Exit`` raised by the
    hook is the plugin's own hard-fail policy and PROPAGATES; any other exception is best-effort
    (warn + fall through to native), mirroring retire.py's plugin-notify fence."""
    for p in plugins.registry():
        if p.wt_create is None or not p.enabled(cfg, entry):
            continue
        try:
            result = p.wt_create(
                cfg, entry, main=main, branch=branch, target=target, start_point=start_point
            )
        except typer.Exit:
            raise
        except Exception as exc:  # noqa: BLE001 - defensive fence: a plugin never aborts create
            typer.echo(
                f"⚠ plugin {p.name} wt_create failed, falling back to native: {exc}", err=True
            )
            continue
        if result is not None:
            return result
    return None


def _notify_wt_create(
    hook: str, cfg, entry, *, main: Path, branch: str, target: Path, start_point: str = ""
) -> None:
    """Run an observing worktree-create hook for every enabled plugin.

    Unlike ``wt_create``, these hooks never take ownership of ``git worktree add``.  They are
    deliberately best-effort: one failed observer must not prevent either creation or a later
    observer from running.
    """
    for p in plugins.registry():
        callback = getattr(p, hook)
        if callback is None or not p.enabled(cfg, entry):
            continue
        try:
            kwargs = {"main": main, "branch": branch, "target": target}
            if hook == "wt_creating":
                kwargs["start_point"] = start_point
            callback(cfg, entry, **kwargs)
        except Exception as exc:  # noqa: BLE001 - observers never abort worktree creation
            typer.echo(f"⚠ plugin {p.name} {hook} failed, continuing: {exc}", err=True)


def _consult_wt_remove(
    cfg, entry, *, main: Path, target: Path, force: bool, keep_branch: bool
) -> bool:
    """Generic delegation seam for a worktree *remove*: the first enabled plugin (registry
    order) defining ``wt_remove`` wins. ``False`` (or no enabled plugin defining the hook) means
    "not handled" — the native `git worktree remove` runs instead. Same propagation contract as
    ``_consult_wt_create``: a ``typer.Exit`` PROPAGATES, any other exception warns and falls
    through to native."""
    for p in plugins.registry():
        if p.wt_remove is None or not p.enabled(cfg, entry):
            continue
        try:
            result = p.wt_remove(
                cfg, entry, main=main, target=target, force=force, keep_branch=keep_branch
            )
        except typer.Exit:
            raise
        except Exception as exc:  # noqa: BLE001 - defensive fence: a plugin never aborts remove
            typer.echo(
                f"⚠ plugin {p.name} wt_remove failed, falling back to native: {exc}", err=True
            )
            continue
        if result:
            return True
    return False


def _do_add(
    cfg, entry, main: Path, br: str, target: Path, *, new_branch: bool, start_point: str = ""
):
    """Create the linked worktree (new `-b` branch, or attach an existing one) + run init.
    Attaching an existing branch prunes stale admin entries first, so a worktree whose dir
    was deleted out-of-band (not via `worktree remove`) doesn't block re-attach.
    `start_point` is only honoured for new-branch creation — it sets the commit the branch
    forks from (e.g. `wt/bead/epic/<epic>` so the bead sees intra-molecule merged work).

    Delegation seam: only the new-branch path may be taken over by a plugin's `wt_create` hook
    (see `_consult_wt_create`) — attach stays native even when a delegating plugin is enabled
    (bh's `wt/` branch conventions are authoritative for an existing branch; there's no naming
    decision left to delegate), with a one-line warning noting the fallthrough."""
    target.parent.mkdir(parents=True, exist_ok=True)
    hive = str(entry.get("prefix", ""))
    started = time.monotonic()
    delegated_target: Path | None = None
    _notify_wt_create(
        "wt_creating", cfg, entry, main=main, branch=br, target=target, start_point=start_point
    )
    if new_branch:
        delegated_target = _consult_wt_create(
            cfg, entry, main=main, branch=br, target=target, start_point=start_point
        )
    elif any(p.wt_create is not None and p.enabled(cfg, entry) for p in plugins.registry()):
        typer.echo(
            "⚠ worktree attach stays native (delegation only covers new-branch create)", err=True
        )

    # Time + tag the create. The error path used to raise BEFORE any emission (always-"ok" gap), so
    # a failed create recorded nothing — now both the events counter AND the op.duration histogram
    # fire with outcome=error before the re-raise. Best-effort + gated (verify- trees never reach
    # this chokepoint; clean_checkout bypasses _do_add entirely).
    if delegated_target is None:
        if new_branch:
            cmd = ["git", "-C", str(main), "worktree", "add", "-b", br, str(target)]
            if start_point:
                cmd.append(start_point)
        else:
            _run_git(["git", "-C", str(main), "worktree", "prune"], check=False)
            cmd = ["git", "-C", str(main), "worktree", "add", str(target), br]
        res = _run_git(cmd, check=False)
        if res.returncode != 0:
            elapsed = time.monotonic() - started
            _record_wt_event("create", "error", hive=hive, leaf=target.name)
            _record_wt_op_duration("create", elapsed, "error", hive=hive, leaf=target.name)
            raise typer.Exit(res.returncode)
    else:
        target = delegated_target
    _notify_wt_create("wt_created", cfg, entry, main=main, branch=br, target=target)
    elapsed = time.monotonic() - started
    _record_wt_op_duration("create", elapsed, "ok", hive=hive, leaf=target.name)
    run_init(cfg, entry, target)
    provision_observaloop(cfg, entry, target)
    _record_wt_event("create", hive=hive, leaf=target.name)


def add(hive="", bead="", branch="", dry_run=False, as_json=False):
    """Create a managed worktree (off the hive's HEAD) + run init ops. `dry_run` (`--preview` is
    an alias) prints the `preview()` contract and changes nothing; `as_json` renders it (or the
    real result) as the machine-readable schema instead of the human lines, so an external
    orchestrator parses both phases with one shape."""
    if bead and branch:
        typer.echo("✗ pass at most one of --bead / --branch", err=True)
        raise typer.Exit(1)
    cfg = config.load()
    if dry_run:
        result = preview(cfg, hive, bead=bead, branch=branch)
        if as_json:
            typer.echo(json.dumps(result, indent=2))
            return
        typer.echo(f"hive {result['hive']}  branch {result['branch']}")
        typer.echo(f"  → {result['path']}")
        typer.echo(f"  would {result['would']}")
        typer.echo("(dry-run — nothing changed)")
        return

    entry, main, target, br = locate(cfg, hive, bead=bead, branch=branch)
    if not (main / ".git").exists():
        typer.echo(f"✗ no clone for hive at {main} — clone it first", err=True)
        raise typer.Exit(1)
    typer.echo(f"hive {registry.hive_key(entry)}  branch {br}")
    typer.echo(f"  → {target}")
    if target.exists():
        typer.echo(f"✗ worktree path already exists: {target}", err=True)
        raise typer.Exit(1)
    _refuse_if_codex_unreachable(cfg, entry, main, target)
    _do_add(cfg, entry, main, br, target, new_branch=True)
    from . import metadata

    metadata.invalidate(cfg, registry.hive_key(entry))  # branch/worktree churn on this hive
    typer.echo(f"✓ worktree ready: {target}")
    if as_json:
        typer.echo(
            json.dumps(
                {
                    "op": "add",
                    "hive": registry.hive_key(entry),
                    "bead": bead,
                    "branch": br,
                    "path": str(target),
                    "created": True,
                },
                indent=2,
            )
        )


# ---- ws work helpers (idempotent provision/re-attach + submit-time git) ------


def clone_for_branch(entry, branch: str) -> Path:
    """The working dir an integration merge/reset for `branch` must run in: the linked worktree
    that currently has `branch` checked out, else the hive's main clone. A branch can only be
    checked out (and thus merged/reset onto) where it lives — under the collapsed container==seat
    model (xn3o.6) the container branch `wt/bead/epic/<id>` lives in the coordinator seat worktree,
    so a child's merge ONTO it runs there, not in the main clone (which holds `main`). For a
    top-level land onto `main` the main clone wins (nothing else has `main` checked out). Merging
    a branch that is checked out elsewhere is fine — only checking it OUT twice is refused — so
    this only matters for the merge/reset *target* (`base`), never the source."""
    main = registry.hive_dir(entry)
    res = _run_git(
        ["git", "-C", str(main), "worktree", "list", "--porcelain"], check=False, capture=True
    )
    if res.returncode != 0:
        return main
    path = None
    for line in (res.stdout or "").splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree ") :]
        elif line.startswith("branch "):
            br = line[len("branch ") :].removeprefix("refs/heads/")
            if br == branch and path:
                return Path(path)
    return main


def locate(cfg, hive, bead="", branch="", kind=""):
    """Resolve (entry, main, target, branch) for a managed worktree — no side effects. Keys on a
    single `bead` (`wt/bead/<type>/<id>`) or a raw `branch` suffix (`wt/<name>`, e.g. a batch
    worktree). `kind` (epic|issue) fixes the bead branch's `<type>` when the caller knows the
    issue_type; otherwise it's resolved by probing (`_bead_kind`) so a read seam stays type-aware
    with no bd call. The worktree dir (leaf = `<id>`) is unaffected by `<type>`."""
    entry = _resolve_entry(cfg, hive)
    main = registry.hive_dir(entry)
    if bead:
        kind = _bead_kind(main, bead, kind)
    br, leaf = _branch_and_leaf(cfg, bead=bead, branch=branch, kind=kind)
    return entry, main, wt_dir(entry, leaf), br


def preview(cfg, hive, bead="", branch="", kind="", op="add") -> dict:
    """Side-effect-free "what would this provisioning call produce" — the JSON contract external
    orchestrators (`add --preview`, `work claim|assign --preview`) parse before committing. Built
    entirely on the read-only resolvers `locate`/`_branch_exists`/`_rules`/`integration_base` — no
    `git worktree add`, no `bd` write. `would` mirrors `ensure`'s reuse-live-dir /
    attach-existing-branch / create-forked-off-integration-base decision tree, whatever op
    ultimately provisions through it."""
    entry, main, target, br = locate(cfg, hive, bead=bead, branch=branch, kind=kind)
    if not (main / ".git").exists():
        typer.echo(f"✗ no clone for hive at {main} — clone it first", err=True)
        raise typer.Exit(1)
    path_exists = target.exists()
    branch_exists = _branch_exists(main, br)
    would = "reuse" if path_exists else "attach" if branch_exists else "create"
    start_point = ""
    if would == "create":
        integration = config.integration_branch(cfg, entry)
        start_point = integration_base(entry, bead, integration)
    return {
        "op": op,
        "hive": registry.hive_key(entry),
        "bead": bead,
        "branch": br,
        "path": str(target),
        "would": would,
        "start_point": start_point,
        "branch_exists": branch_exists,
        "path_exists": path_exists,
        "init": _rules(cfg, entry),
    }


def in_bead_worktree(target: Path, cwd: Path | None = None) -> bool:
    """True iff `cwd` (default: Path.cwd()) resolves to or is inside the bead's managed
    worktree at `target`. Used by claim/check/submit to warn when the caller is operating
    from the main clone instead of the worktree — absolute paths under the hive root resolve
    to the main clone (the wrong tree), not the worktree."""
    try:
        resolved = (cwd or Path.cwd()).resolve()
        t = target.resolve()
        return resolved == t or resolved.is_relative_to(t)
    except OSError:
        return False


def cwd_identity(cfg=None, cwd=None):
    """``((provider, org, repo) | None, leaf)`` for the current location — side-effect free
    (no typer.Exit / echo), so it's safe to stamp telemetry identity. Two resolution paths mirror
    ``_resolve_entry`` but quietly:
      - cwd under the shadow worktree root → triplet + worktree ``leaf`` from the path segments
        ``<root>/<provider>/<org>/<repo>/<leaf>`` (the managed-worktree case, whose path is NOT
        under $GIT_WORKSPACE);
      - else a real hive checkout under $GIT_WORKSPACE → ``workspace_identity`` triplet, leaf ``''``
        (the main clone is not a managed worktree);
      - neither → ``(None, '')``.
    """
    here = Path(cwd) if cwd else Path.cwd()
    root = config.worktrees_root(cfg)
    try:
        parts = here.resolve().relative_to(root.resolve()).parts
    except (ValueError, OSError):
        parts = ()
    if len(parts) >= 4:
        return (parts[0], parts[1], parts[2]), _leaf(parts[3])
    if len(parts) >= 3:
        return (parts[0], parts[1], parts[2]), ""
    return workspace_identity(str(here)), ""


def cwd_worktree_dir(cfg=None, cwd=None) -> Path | None:
    """The managed-worktree ROOT dir containing ``cwd`` (``<root>/<provider>/<org>/<repo>/<leaf>``),
    or ``None`` when ``cwd`` is not inside a managed worktree. Side-effect free (no typer.Exit /
    echo) — the path companion to ``cwd_identity``: where ``cwd_identity`` yields the telemetry
    triplet+leaf, this yields the worktree dir itself so a per-worktree overlay (``.ws/otel.env``)
    can be located even when ``cwd`` is nested below the worktree root. ``None`` for the main clone
    (under $GIT_WORKSPACE, not the shadow root) and anywhere outside the shadow root."""
    here = Path(cwd) if cwd else Path.cwd()
    root = config.worktrees_root(cfg)
    try:
        parts = here.resolve().relative_to(root.resolve()).parts
    except (ValueError, OSError):
        return None
    if len(parts) < 4:
        return None
    return root.resolve().joinpath(*parts[:4])


def _commits_behind(main: Path, branch: str, base: str) -> int:
    """Count of commits on `base` not yet reachable from `branch` (0 when the range can't be
    resolved) — the "how stale is this child" measure `_repoint_if_stale` re-points against."""
    res = _run_git(
        ["git", "-C", str(main), "rev-list", "--count", f"{branch}..{base}"],
        check=False,
        capture=True,
    )
    return int((res.stdout or "0").strip() or "0") if res.returncode == 0 else 0


def _repoint_if_stale(cfg, entry, main, branch, target, base_bead) -> None:
    """Re-point a child branch that was provisioned BEFORE its container was refreshed (bh-4wwi).

    An idempotent re-assign returns the existing worktree as-is, so a child forked off a now-stale
    container tip stays behind. When the child branch has NO unique commits and is behind its
    container tip, fast-forward it (`reset --hard`) to the refreshed tip — a lossless move, since it
    has no work of its own. A child with real commits is NEVER re-pointed (its work is preserved),
    and a dirty / elsewhere-checked-out worktree is left untouched with a warning."""
    integration = config.integration_branch(cfg, entry)
    base = integration_base(entry, base_bead, integration)
    count, _subjects = history(entry, branch, base)
    if count != 0:
        return  # real work on the child branch — never re-point it
    behind = _commits_behind(main, branch, base)
    if behind <= 0:
        return  # already at the container tip (or the range is unresolvable) — nothing to refresh
    if current_branch(target) != branch or not is_clean(target):
        typer.echo(
            f"WARNING: child {branch} is {behind} commit(s) behind {base} but its worktree is "
            f"dirty or checked out elsewhere — reusing it as-is; refresh by hand",
            err=True,
        )
        return
    if reset_hard(target, base) == 0:
        typer.echo(f"✓ re-pointed stale child {branch} to refreshed {base} ({behind} commit(s))")


def pr_base_ref(cfg, entry) -> str:
    """The git ref a NEW bead branch forks from at the hive root (no started container above
    it): for a `kind=external` (contribution) hive, `upstream/<pr_base>` — freshly fetched from
    the `upstream` remote so a contribution's diff is measured against CURRENT upstream work,
    never a stale or locally-diverged `main`. Every other hive is unaffected: the local
    `config.integration_branch` name, unchanged.

    Fetch failure (offline, upstream unreachable) warns and falls back to the local branch name
    rather than hard-failing worktree provisioning — a degraded-but-working base beats a blocked
    claim."""
    base = config.pr_base(cfg, entry)
    if str((entry or {}).get("kind", "")) != "external":
        return base
    main = registry.hive_dir(entry)
    fetched = _run_git(["git", "-C", str(main), "fetch", UPSTREAM_REMOTE, base], check=False)
    if fetched.returncode != 0:
        typer.echo(f"⚠ fetch {UPSTREAM_REMOTE} failed — basing off local {base} instead", err=True)
        return base
    return f"{UPSTREAM_REMOTE}/{base}"


def _refuse_if_codex_unreachable(cfg, entry, main: Path, target: Path) -> None:
    """bh-rpzaj: refuse to (re)provision `target` when THIS invocation is itself running as a
    Codex tool call (`config.codex_sandbox_active`) and `target` sits outside Codex's own
    default sandbox roots (`config.codex_default_sandbox_covers`) — bh's own process can
    write there (it's the one doing the provisioning), but the SAME Codex sub-agent's later
    `apply_patch`/file-edit tool calls, routed through that session's actual sandbox, cannot.
    A current managed project-local Codex grant makes that subtree reachable; a stale
    project-local grant still shadows a global grant, matching `hive_ready`'s precedence.
    Fails fast with the exact remediation instead of leaving a claimed bead the agent can
    never edit."""
    if not config.codex_sandbox_active() or config.codex_default_sandbox_covers(target):
        return
    # `hive` reaches the cross-hive stores, which must stay outside this module's import-time
    # closure (the publish entry point imports worktree). Grant inspection only happens while
    # provisioning from a Codex session, so load those existing helpers lazily at that boundary.
    hive = importlib.import_module(".hive", package=__package__)

    cur = hive.codex_grant_is_current(
        cfg, main, str(entry["provider"]), str(entry["org"]), str(entry["repo"])
    )
    if cur is True:
        return
    # A project-local sandbox table shadows the ambient global table, including when its
    # managed grant is stale. Only a hive with no per-hive grant can use the global fallback.
    if cur is None and hive.global_codex_grant_is_current(cfg):
        return
    wt_root = config.worktrees_root(cfg)
    typer.echo(
        f"✗ worktree root {target} is outside Codex's default sandbox (cwd + $TMPDIR) — "
        "this session cannot write there.\n"
        "  Fix one: set worktrees.ephemeral: true (OS temp, always reachable) · "
        "move worktrees.path under $TMPDIR · "
        f"relaunch codex with --add-dir {wt_root}",
        err=True,
    )
    raise typer.Exit(1)


def ensure(cfg, hive, bead="", branch="", base_bead="", kind=""):
    """Idempotent provision/re-attach for `ws work`. Returns (entry, target, branch): reuse a live
    dir; else attach an existing branch into a fresh dir; else create the branch+dir forked off its
    `integration_base` — the nearest started container (a parent epic/workstream) or `integration`
    (start-point threading). Keys on `bead` (single-bead `wt/bead/<type>/<id>`) or a raw `branch`
    suffix (a work-group's shared `wt/<name>` worktree); `kind` fixes the bead branch's `<type>`
    (epic for a coordinator seat, else issue); `base_bead` names the bead whose container sets the
    start point (defaults to `bead`). Init runs only on a new dir.

    For a `kind=external` hive, `integration` (the root fallback `integration_base` climbs to)
    is `pr_base_ref`'s freshly-fetched `upstream/<branch>` — a contribution worktree is always
    created off upstream, never local main (see `pr_base_ref`).

    Refuses up front (bh-rpzaj) rather than provisioning a dir a Codex sub-agent's own tool
    calls can never reach — see `_refuse_if_codex_unreachable`."""
    entry, main, target, br = locate(cfg, hive, bead=bead, branch=branch, kind=kind)
    if not (main / ".git").exists():
        typer.echo(f"✗ no clone for hive at {main} — clone it first", err=True)
        raise typer.Exit(1)
    _refuse_if_codex_unreachable(cfg, entry, main, target)
    if target.exists():
        if bead:  # only a single-bead child branch tracks a refreshable container tip
            _repoint_if_stale(cfg, entry, main, br, target, base_bead or bead)
        return entry, target, br
    new_branch = not _branch_exists(main, br)
    start_point = ""
    if new_branch:
        integration = pr_base_ref(cfg, entry)
        start_point = integration_base(entry, base_bead or bead, integration)
    _do_add(cfg, entry, main, br, target, new_branch=new_branch, start_point=start_point)
    return entry, target, br


def refresh_container(entry, branch: str, upstream: str) -> None:
    """Refresh a container branch from `upstream` (its own integration base, e.g. `main`) so a
    child provisioned mid-molecule forks from CURRENT upstream work, not the container's stale
    open-time base (: the container opens on the FIRST child dispatch and was
    never refreshed, so fixes landing on main were invisible to later children). Runs in the
    seat worktree holding the branch: `git merge` fast-forwards a strictly-behind container and
    otherwise records a merge commit ON THE CONTAINER — fine, since submit's history rules judge
    `base..child` only. NEVER blocks dispatch: a dirty seat or a conflicting merge warns loudly
    (merge aborted, seat left clean) and provisioning proceeds from the stale base."""
    main = registry.hive_dir(entry)
    res = _run_git(
        ["git", "-C", str(main), "rev-list", "--count", f"{branch}..{upstream}"],
        check=False,
        capture=True,
    )
    behind = int((res.stdout or "0").strip() or "0") if res.returncode == 0 else 0
    if behind == 0:
        return  # container already contains upstream's tip (or the range is unresolvable)
    stale = f"WARNING: container {branch} is {behind} commit(s) behind {upstream}"
    workdir = clone_for_branch(entry, branch)
    if current_branch(workdir) != branch:
        typer.echo(f"{stale} and checked out nowhere — provisioning from the stale base", err=True)
        return
    if not is_clean(workdir):
        typer.echo(
            f"{stale} but its seat worktree is dirty — provisioning from the stale base", err=True
        )
        return
    # Explicit conventional subject (bh-cgxc): a bare `git merge --no-edit` writes git's default
    # "Merge branch …" subject, which a commitizen commit-msg hook rejects on hook-enforcing hives.
    # `chore(merge)` keeps this a no-version-bump merge, mirroring the landing-bubble subjects.
    refresh_subject = f"chore(merge): refresh {branch} from {upstream}"
    merged = _run_git(
        [
            "git",
            "-C",
            str(workdir),
            "-c",
            "rerere.enabled=false",
            "merge",
            "-m",
            refresh_subject,
            upstream,
        ],
        check=False,
        capture=True,
    )
    if merged.returncode != 0:
        _run_git(["git", "-C", str(workdir), "merge", "--abort"], check=False, capture=True)
        typer.echo(
            f"{stale} and merging it CONFLICTS — provisioning from the stale base; "
            f"merge {upstream} into {branch} (the container seat, {workdir}) by hand",
            err=True,
        )
        return
    typer.echo(f"✓ refreshed container {branch} from {upstream} ({behind} commit(s))")


def history(entry, branch, base):
    """Compatibility facade for ``worktree_git.impl_history``."""
    return _worktree_git.impl_history(entry, branch, base)


def signature_status(entry, branch, base) -> list[tuple[str, str, str]]:
    """Compatibility facade for ``worktree_git.impl_signature_status``."""
    return _worktree_git.impl_signature_status(entry, branch, base)


def commit_messages(entry, branch, base) -> list[str]:
    """Compatibility facade for ``worktree_git.impl_commit_messages``."""
    return _worktree_git.impl_commit_messages(entry, branch, base)


def commit_shas(entry, branch, base) -> list[str]:
    """Compatibility facade for ``worktree_git.impl_commit_shas``."""
    return _worktree_git.impl_commit_shas(entry, branch, base)


def _pid_alive(pid: int) -> bool:
    """Compatibility facade for ``worktree_verify.impl__pid_alive``."""
    return _worktree_verify.impl__pid_alive(pid)


def _pid_start(pid: int) -> str:
    """Compatibility facade for ``worktree_verify.impl__pid_start``."""
    return _worktree_verify.impl__pid_start(pid)


def _pid_starts(pids) -> dict:
    """Compatibility facade for ``worktree_verify.impl__pid_starts``."""
    return _worktree_verify.impl__pid_starts(pids)


def _write_verify_marker(tmp: Path, branch: str, cmd: str) -> None:
    """Compatibility facade for ``worktree_verify.impl__write_verify_marker``."""
    return _worktree_verify.impl__write_verify_marker(tmp, branch, cmd)


def _read_verify_marker(d: Path):
    """Compatibility facade for ``worktree_verify.impl__read_verify_marker``."""
    return _worktree_verify.impl__read_verify_marker(d)


def _verify_dir_is_orphan(
    d: Path, now: float, grace: int, ttl: int, pid_starts: dict | None = None
) -> bool:
    """Compatibility facade for ``worktree_verify.impl__verify_dir_is_orphan``."""
    return _worktree_verify.impl__verify_dir_is_orphan(d, now, grace, ttl, pid_starts)


def _verify_dir_candidates(parent: Path) -> list:
    """Compatibility facade for ``worktree_verify.impl__verify_dir_candidates``."""
    return _worktree_verify.impl__verify_dir_candidates(parent)


def _live_marker_pids(dirs) -> set:
    """Compatibility facade for ``worktree_verify.impl__live_marker_pids``."""
    return _worktree_verify.impl__live_marker_pids(dirs)


def sweep_verify_dirs(entry, grace=_VERIFY_GRACE_SECONDS, ttl=_VERIFY_TTL_SECONDS) -> int:
    """Compatibility facade for ``worktree_verify.impl_sweep_verify_dirs``."""
    return _worktree_verify.impl_sweep_verify_dirs(entry, grace, ttl)


def _branch_sha(entry, branch) -> str:
    """Compatibility facade for ``worktree_verify.impl__branch_sha``."""
    return _worktree_verify.impl__branch_sha(entry, branch)


def _create_verify_dir(entry, leaf_base: str) -> Path | None:
    """Compatibility facade for ``worktree_verify.impl__create_verify_dir``."""
    return _worktree_verify.impl__create_verify_dir(entry, leaf_base)


# Color-forcing env stripped from a validation child's environment so a clean-checkout run never
# inherits the operator's terminal color settings. Kept deliberately separate from
# `otel.telemetry_neutral_env` (that scrub is telemetry-specific) — this is a sibling,
# color-hermeticity concern, composed on top of it at the one call site below. FORCE_COLOR /
# CLICOLOR_FORCE are the env vars several color libraries (incl. Rich, which Typer's `--help`
# renders through) honor to force ANSI output even when stdout isn't a TTY; left unscrubbed, an
# operator shell exporting either one leaks ANSI escapes into the validation child's output,
# which can break a plain-substring assert (e.g. `--verbose` in `--help` output) with a false RED
# that has nothing to do with the change under validation.
_COLOR_FORCE_ENV_KEYS = ("FORCE_COLOR", "CLICOLOR_FORCE")


def _color_neutral_env(base: dict[str, str]) -> dict[str, str]:
    """Compatibility facade for ``worktree_verify.impl__color_neutral_env``."""
    return _worktree_verify.impl__color_neutral_env(base)


# Rendered once, centrally, when the validation command fails in a verify checkout — every
# caller (submit / merge / postland / batch / review) inherits it without duplicating the text.
_BARE_CHECKOUT_HINT = (
    "  ↳ the command ran in a bare clean checkout: only worktree init rules flagged "
    "`verify: true` were applied. If this failure doesn't reproduce in your dev worktree, "
    "the checkout likely isn't provisioned — flag the dependency-sync rules (e.g. 'uv sync') "
    "with verify: true under worktrees.init / worktree_init, or make the repo self-provisioning "
    "(uv [dependency-groups] + tool.uv.default-groups make a bare 'uv run' self-sufficient; "
    "extras are NEVER synced by default). See docs/WORKTREES.md (verify-environment contract)."
)


def _reuse_verdict_hit(entry, sha: str, cmd: str, cfg=None) -> bool:
    """Compatibility facade for ``worktree_verify.impl__reuse_verdict_hit``."""
    return _worktree_verify.impl__reuse_verdict_hit(entry, sha, cmd, cfg)


def _prepare_verify_worktree(main: Path, entry, branch: str, cmd: str):
    """Compatibility facade for ``worktree_verify.impl__prepare_verify_worktree``."""
    return _worktree_verify.impl__prepare_verify_worktree(main, entry, branch, cmd)


def clean_checkout(entry, branch, cmd, cfg=None, reuse=False) -> int:
    """Compatibility facade for ``worktree_verify.impl_clean_checkout``."""
    return _worktree_verify.impl_clean_checkout(entry, branch, cmd, cfg, reuse)


#: External hives are pull-only (bh-uxam.1): `upstream` is a read rail, never a write target.
#: The single choke point for a branch push — checked here, not per-caller, so no future push
#: path can silently start writing to a repo we forked from.
UPSTREAM_REMOTE = "upstream"


def push_branch(entry, branch, remote="origin") -> int:
    """Compatibility facade for ``worktree_git.impl_push_branch``."""
    return _worktree_git.impl_push_branch(entry, branch, remote)


def is_clean(target: Path) -> bool:
    """Compatibility facade for ``worktree_git.impl_is_clean``."""
    return _worktree_git.impl_is_clean(target)


def dirty_paths(target: Path) -> list[str]:
    """Compatibility facade for ``worktree_git.impl_dirty_paths``."""
    return _worktree_git.impl_dirty_paths(target)


def current_branch(target: Path) -> str:
    """Compatibility facade for ``worktree_git.impl_current_branch``."""
    return _worktree_git.impl_current_branch(target)


def head_sha(target: Path) -> str:
    """Compatibility facade for ``worktree_git.impl_head_sha``."""
    return _worktree_git.impl_head_sha(target)


def head_full_sha(target: Path) -> str:
    """Compatibility facade for ``worktree_git.impl_head_full_sha``."""
    return _worktree_git.impl_head_full_sha(target)


# ---- show / refine git helpers (all git; no bd — keeps work.py's bd seam intact) ----
#
# `commit_rows` packs each commit into one log line with a record separator (RS) leading the
# format and a unit separator (FS) between fields, so the subject (last field, may contain
# spaces) never needs quoting; --name-only files trail each record until the next RS.
_ROW_RS = "\x1e"
_ROW_FS = "\x1f"
_ROW_FMT = _ROW_RS + _ROW_FS.join(["%H", "%h", "%P", "%an", "%ae", "%ad", "%G?", "%GS", "%s"])


def base_of(entry, branch, integration) -> str:
    """Compatibility facade for ``worktree_git.impl_base_of``."""
    return _worktree_git.impl_base_of(entry, branch, integration)


def commit_rows(entry, base, branch) -> list[dict]:
    """Compatibility facade for ``worktree_git.impl_commit_rows``."""
    return _worktree_git.impl_commit_rows(entry, base, branch)


def backup_branch(entry, branch, ts: str, label: str = "refine") -> str:
    """Compatibility facade for ``worktree_git.impl_backup_branch``."""
    return _worktree_git.impl_backup_branch(entry, branch, ts, label)


def _rebase_env(**extra) -> dict:
    """Compatibility facade for ``worktree_git.impl__rebase_env``."""
    return _worktree_git.impl__rebase_env(**extra)


def rebase_squash(target_wt, base, todo_lines) -> tuple[int, str]:
    """Compatibility facade for ``worktree_git.impl_rebase_squash``."""
    return _worktree_git.impl_rebase_squash(target_wt, base, todo_lines)


def rebase_autosquash(target_wt, base) -> tuple[int, str]:
    """Compatibility facade for ``worktree_git.impl_rebase_autosquash``."""
    return _worktree_git.impl_rebase_autosquash(target_wt, base)


def rebase_onto(target_wt, base) -> tuple[int, str]:
    """Compatibility facade for ``worktree_git.impl_rebase_onto``."""
    return _worktree_git.impl_rebase_onto(target_wt, base)


def rebase_abort(target_wt) -> None:
    """Compatibility facade for ``worktree_git.impl_rebase_abort``."""
    return _worktree_git.impl_rebase_abort(target_wt)


def reset_hard(target_wt, ref) -> int:
    """Compatibility facade for ``worktree_git.impl_reset_hard``."""
    return _worktree_git.impl_reset_hard(target_wt, ref)


def safe_to_rewrite(clone, branch) -> bool:
    """Compatibility facade for ``worktree_git.impl_safe_to_rewrite``."""
    return _worktree_git.impl_safe_to_rewrite(clone, branch)


def same_tree(entry, a, b) -> bool:
    """Compatibility facade for ``worktree_git.impl_same_tree``."""
    return _worktree_git.impl_same_tree(entry, a, b)


def is_merged(entry, branch: str, base: str) -> bool:
    """Compatibility facade for ``worktree_git.impl_is_merged``."""
    return _worktree_git.impl_is_merged(entry, branch, base)


def on_first_parent_chain(entry, branch: str, base: str) -> bool:
    """Compatibility facade for ``worktree_git.impl_on_first_parent_chain``."""
    return _worktree_git.impl_on_first_parent_chain(entry, branch, base)


def landed_via_merge(entry, branch: str, base: str) -> bool:
    """Compatibility facade for ``worktree_git.impl_landed_via_merge``."""
    return _worktree_git.impl_landed_via_merge(entry, branch, base)


def _all_cherry_landed(entry, branch: str, parent: str) -> bool:
    """Compatibility facade for ``worktree_git.impl__all_cherry_landed``."""
    return _worktree_git.impl__all_cherry_landed(entry, branch, parent)


def is_landed(entry, branch: str, parent: str, close_reason: str = "") -> bool:
    """Compatibility facade for ``worktree_git.impl_is_landed``."""
    return _worktree_git.impl_is_landed(entry, branch, parent, close_reason)


def bead_and_parent(entry, path: str, integration: str, branch: str = "") -> tuple[str | None, str]:
    """Compatibility facade for ``worktree_git.impl_bead_and_parent``."""
    return _worktree_git.impl_bead_and_parent(entry, path, integration, branch)


def diff_range(entry, base, branch) -> int:
    """Compatibility facade for ``worktree_git.impl_diff_range``."""
    return _worktree_git.impl_diff_range(entry, base, branch)


def log_range(entry, base, branch) -> str:
    """Compatibility facade for ``worktree_git.impl_log_range``."""
    return _worktree_git.impl_log_range(entry, base, branch)


def _managed_for_entry(e, root: str) -> list:
    """[(prefix, path, branch), ...] visible under `root` for ONE managed_repos entry, parsed
    from `git worktree list --porcelain`. [] when the entry has no local clone or the git call
    fails."""
    main = registry.hive_dir(e)
    if not (main / ".git").exists():
        return []
    res = _run_git(
        ["git", "-C", str(main), "worktree", "list", "--porcelain"],
        check=False,
        capture=True,
    )
    if res.returncode != 0:
        return []
    out: list = []
    path = brref = None
    for line in (res.stdout or "").splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree ") :]
            brref = None
        elif line.startswith("branch "):
            brref = line[len("branch ") :].removeprefix("refs/heads/")
        elif not line.strip() and path:
            _emit(out, e, root, path, brref)
            path = brref = None
    if path:
        _emit(out, e, root, path, brref)
    return out


def managed(cfg):
    """[(prefix, path, branch)] for every linked worktree under the shadow root."""
    root = str(config.worktrees_root().resolve())
    out = []
    for e in cfg.get("managed_repos", []) or []:
        out.extend(_managed_for_entry(e, root))
    return out


def _emit(out, entry, root, path, brref):
    try:
        under = Path(path).resolve().is_relative_to(root)
    except OSError:
        under = path.startswith(root + os.sep)
    if under:
        out.append((str(entry["prefix"]), path, brref or "(detached)"))


def _worktree_branch(path) -> str:
    """The current branch of the worktree at `path` ('(detached)' when HEAD isn't on a branch)."""
    res = _run_git(
        ["git", "-C", str(path), "rev-parse", "--abbrev-ref", "HEAD"], check=False, capture=True
    )
    branch = (res.stdout or "").strip() if res.returncode == 0 else ""
    return branch if branch and branch != "HEAD" else "(detached)"


def unregistered_worktrees(cfg):
    """[(slug, leaf, path, branch)] for git worktrees under the shadow root whose repo is NOT in
    managed_repos (bh-ea1i). The status/list sweep otherwise iterates only the hive registry, so a
    repo with worktrees on disk but no registration is silently omitted. This walks the wt root
    itself (``<root>/<provider>/<org>/<repo>/<leaf>``) so such orphans are surfaced, not dropped."""
    root = config.worktrees_root().resolve()
    if not root.exists():
        return []
    registered = {
        (str(e.get("provider")), str(e.get("org")), str(e.get("repo")))
        for e in (cfg.get("managed_repos", []) or [])
    }
    out = []
    for leaf in sorted(root.glob("*/*/*/*")):
        if not leaf.is_dir():
            continue
        parts = leaf.relative_to(root).parts
        if len(parts) != 4:
            continue
        provider, org, repo, leaf_name = parts
        if (provider, org, repo) in registered:
            continue
        if not (leaf / ".git").exists():
            continue  # a plain dir, not a linked git worktree
        out.append((f"{provider}/{org}/{repo}", leaf_name, str(leaf), _worktree_branch(leaf)))
    return out


def list_cmd():
    cfg = config.load()
    rows = managed(cfg)
    unreg = unregistered_worktrees(cfg)
    if not rows and not unreg:
        typer.echo("no managed worktrees")
        return
    for prefix, path, br in rows:
        typer.echo(f"{prefix}\t{br}\t{path}")
    for slug, _leaf, path, br in unreg:
        typer.echo(f"{slug}\t{br}\t{path}")
    if unreg:
        _warn_unregistered(unreg)


def path_of(hive, ref):
    cfg = config.load()
    entry = _resolve_entry(cfg, hive)
    target = wt_dir(entry, _leaf(ref))
    if not target.exists():
        typer.echo(f"✗ no managed worktree: {target}", err=True)
        raise typer.Exit(1)
    typer.echo(str(target))


def init_existing(path):
    cfg = config.load()
    p = Path(path)
    if not p.exists():
        typer.echo(f"✗ no such path: {p}", err=True)
        raise typer.Exit(1)
    entry = _entry_for_path(cfg, p)
    run_init(cfg, entry, p)
    typer.echo(f"✓ re-ran init for {p}")


def _rmdir_empty_parents(leaf_path, cfg):
    """Climb from a removed worktree's parent toward the shadow root, removing now-empty
    triplet dirs. Path.rmdir only deletes EMPTY dirs (raises otherwise) — that's the safety:
    a non-empty dir (another live worktree) stops the climb, and the root is never removed.
    Disabled by `worktrees.rmdir_empty: false` (absent ⇒ enabled)."""
    if not config.worktrees_cfg(cfg).get("rmdir_empty", True):
        return
    root = config.worktrees_root().resolve()
    d = Path(leaf_path).parent.resolve()
    while root in d.parents and d != root:
        try:
            d.rmdir()
        except OSError:
            break
        d = d.parent


def _refuse_unknown_removal(cfg, entry, target: Path, *, force: bool) -> None:
    """Refuse to remove a worktree whose bead could not be RESOLVED (bh-167s0), unless forced.

    ``rm`` names one target, so "unattended" does not bite here the way it does for ``prune`` —
    but the operator's premise does.  Whoever types ``rm`` decided this seat was disposable, and
    on the hive that produced this bead that decision would have been made against rows reading
    ACTIVE that were neither active nor readable.  An UNKNOWN row means bh cannot say what is on
    that branch; refusing is the only answer that is not a guess.

    ``--force`` still removes it, deliberately: the operator may know exactly what this is (they
    just retired the prefix, say), and a preflight that cannot be overridden becomes a preflight
    people route around.  The refusal names ``--force`` so the escape is not a secret.

    Best-effort by construction — anything that goes wrong deciding this (an unregistered path,
    a git failure) leaves the removal alone rather than blocking it.  A safety check that turns
    an ordinary ``rm`` into a crash is a check that gets deleted.
    """
    if force:
        return
    try:
        rows = [r for r in managed(cfg) if Path(r[1]) == target]
        if not rows:
            return
        statuses = _classify_entry(entry, rows, cfg)
    except Exception:  # noqa: BLE001 — never let the preflight itself fail the verb
        return
    unknown = wt_status.untrustworthy(statuses)
    if not unknown:
        return
    st = unknown[0]
    typer.echo(
        f"✗ refusing to remove {target}: its bead ({st.bead_id}) could NOT BE RESOLVED, so bh "
        f"cannot tell you whether this worktree holds unmerged work.",
        err=True,
    )
    if st.unknown_reason:
        typer.echo(f"    {st.unknown_reason}", err=True)
    typer.echo(
        "  Resolve the hive's bead store (or the branch name) and re-check with "
        f"`{config.BINARY_ALIAS} worktree status`; `--force` removes it anyway.",
        err=True,
    )
    raise typer.Exit(1)


def remove(hive, ref, force=False, as_json=False):
    """Remove one managed worktree. The branch is the durable artifact here (a bead's history
    lives on it), so a delegating plugin's `wt_remove` hook is consulted with `keep_branch=True`
    — never call this for a disposable prune removal (see `prune`). `as_json` (bh-73rz.4) emits
    the same `{op, hive, path, removed}` machine-readable shape an external orchestrator's
    preview→create→…→remove flow parses, mirroring `add --json`."""
    cfg = config.load()
    entry = _resolve_entry(cfg, hive)
    main = registry.hive_dir(entry)
    target = wt_dir(entry, _leaf(ref))
    hive_key = registry.hive_key(entry)
    hive = str(entry.get("prefix", ""))
    _refuse_unknown_removal(cfg, entry, target, force=force)
    started = time.monotonic()
    delegated = _consult_wt_remove(
        cfg, entry, main=main, target=target, force=force, keep_branch=True
    )
    if not delegated:
        cmd = ["git", "-C", str(main), "worktree", "remove", str(target)]
        if force:
            cmd.append("--force")
        res = _run_git(cmd, check=False)
        if res.returncode != 0:
            elapsed = time.monotonic() - started
            _record_wt_event("remove", "error", hive=hive, leaf=target.name)
            _record_wt_op_duration("remove", elapsed, "error", hive=hive, leaf=target.name)
            raise typer.Exit(res.returncode)
    elapsed = time.monotonic() - started
    _rmdir_empty_parents(target, cfg)
    _record_wt_op_duration("remove", elapsed, "ok", hive=hive, leaf=target.name)
    _record_wt_event("remove", hive=hive, leaf=target.name)
    from . import metadata

    metadata.invalidate(cfg, registry.hive_key(entry))  # branch/worktree churn on this hive
    if as_json:
        typer.echo(
            json.dumps(
                {"op": "rm", "hive": hive_key, "path": str(target), "removed": True}, indent=2
            )
        )
        return
    typer.echo(f"✓ removed {target}")


_LANDED_REASONS = ("merged", "molecule landed")  # the close_reasons is_landed treats as landed


def mark_landed(hive: str, ref: str) -> None:
    """Operator escape hatch (bh-v0wu): assert an OUT-OF-BAND landing by stamping the
    authoritative close_reason (`merged`) on the bead, so `prune`'s landed detection reclassifies
    the seat LANDED when no git/gh signal exists to find (hand-squashed from a laptop, landed on
    another machine, a deleted PR, a non-GitHub remote …) and the seat unsticks.

    ``ref`` is a bead id or its ``wt/bead/<type>/<id>`` branch. An open bead is closed with
    reason ``merged``; one already closed with a landed reason is a no-op; one closed for
    another reason is reopened + reclosed so the close_reason becomes authoritative (bd has no
    close-reason edit). This ASSERTS a landing — prefer `work land` when a PR exists to check."""
    cfg = config.load()
    bead = (_bead_id_from_branch(ref) or "") if ref.startswith(WT_PREFIX) else ref
    if not bead:
        typer.echo(f"✗ cannot parse a bead id from {ref}", err=True)
        raise typer.Exit(1)
    entry, main, _target, branch = locate(cfg, hive, bead)
    data = bd.show(bead, main)
    if data is None:
        typer.echo(f"✗ no such bead: {bead}", err=True)
        raise typer.Exit(1)
    if str(data.get("status")) == "closed":
        reason = str(data.get("close_reason") or "")
        if reason in _LANDED_REASONS:
            typer.echo(f"• {bead} already closed with close_reason '{reason}' — nothing to do")
            return
        if bd.run(["reopen", bead], main).returncode != 0:
            typer.echo(f"✗ cannot reopen {bead} to restamp its close_reason", err=True)
            raise typer.Exit(1)
    if bd.run(["close", bead, "--reason", "merged"], main).returncode != 0:
        typer.echo(f"✗ failed to close {bead} with close_reason 'merged'", err=True)
        raise typer.Exit(1)
    typer.echo(
        f"✓ marked {bead} landed (close_reason: merged) — "
        f"`{config.BINARY_ALIAS} worktree prune` can now reap {branch}"
    )


def _prune_load_entries(cfg) -> tuple:
    """`(mains, keys, entries_by_prefix)` lookup tables, keyed by hive prefix, for every
    managed_repos entry — the per-hive tables `prune` needs regardless of `--hive` scoping."""
    mains: dict[str, Path] = {}
    keys: dict[str, str] = {}
    entries_by_prefix: dict[str, dict] = {}
    for e in cfg.get("managed_repos", []) or []:
        p = str(e["prefix"])
        mains[p] = registry.hive_dir(e)
        keys[p] = registry.hive_key(e)
        entries_by_prefix[p] = e
    return mains, keys, entries_by_prefix


def _prune_sweep_orphans(entries_by_prefix: dict, want: str | None) -> int:
    """Reap orphaned ephemeral verify-* clean-checkout dirs across in-scope hives (bh-nikb) before
    classification: they are not classifier rows to prune (detached, no bead) — the marker-based
    liveness sweep shared with `clean_checkout` is what reclaims them. Live ones are always spared
    and simply show up as DETACHED skips. Returns the number reaped."""
    return sum(
        sweep_verify_dirs(e) for p, e in entries_by_prefix.items() if want is None or p == want
    )


def _classify_entries(
    cfg,
    entries: list,
    rows_by_prefix: dict[str, list],
    on_complete=None,
) -> dict[str, list]:
    """Classify populated hives concurrently, optionally reporting each completed hive.

    Every classification has independent git and bead-store subprocesses, so waiting for one
    hive before starting the next only delays the fleet view.  Each worker owns a
    :func:`store_probe_cache` context: cache entries are per main-clone path and one worker owns
    one hive, which preserves the one-probe-per-hive command contract without sharing a mutable
    ``ContextVar`` value between threads.

    Results are keyed rather than appended in completion order.  Callers that return structured
    data can then retain their established deterministic entry ordering, while the human status
    renderer uses ``on_complete`` to show a hive as soon as it is ready.
    """
    jobs = [
        (str(entry.get("prefix", "")), entry, rows_by_prefix.get(str(entry.get("prefix", "")), []))
        for entry in entries
        if rows_by_prefix.get(str(entry.get("prefix", "")), [])
    ]
    if not jobs:
        return {}

    def classify_one(entry, entry_rows):
        # A context is intentionally per worker rather than around the executor: ContextVars do
        # not propagate their values into new threads, and no two workers classify one hive.
        with store_probe_cache():
            return _classify_entry(entry, entry_rows, cfg)

    statuses_by_prefix: dict[str, list] = {}
    if len(jobs) == 1:
        prefix, entry, entry_rows = jobs[0]
        statuses = classify_one(entry, entry_rows)
        statuses_by_prefix[prefix] = statuses
        if on_complete is not None:
            on_complete(prefix, statuses)
        return statuses_by_prefix

    with ThreadPoolExecutor(
        max_workers=min(_CLASSIFY_MAX_WORKERS, len(jobs)),
        thread_name_prefix="bh-worktree-classify",
    ) as executor:
        futures = {
            executor.submit(classify_one, entry, entry_rows): prefix
            for prefix, entry, entry_rows in jobs
        }
        for future in as_completed(futures):
            prefix = futures[future]
            statuses = future.result()
            statuses_by_prefix[prefix] = statuses
            if on_complete is not None:
                on_complete(prefix, statuses)

    return statuses_by_prefix


def _prune_classify(cfg, entries_by_prefix: dict, rows: list) -> tuple:
    """Classify every candidate row concurrently. Returns `(safe_set, skipped)` — the
    SAFE-to-remove and NOT-SAFE ``WtStatus`` lists."""
    prefixes = list(dict.fromkeys(r[0] for r in rows))
    entries = [entries_by_prefix[prefix] for prefix in prefixes if prefix in entries_by_prefix]
    rows_by_prefix: dict[str, list] = {}
    for row in rows:
        rows_by_prefix.setdefault(row[0], []).append(row)
    statuses_by_prefix = _classify_entries(cfg, entries, rows_by_prefix)

    all_statuses = [status for prefix in prefixes for status in statuses_by_prefix.get(prefix, [])]
    safe_set = [s for s in all_statuses if s.safe]
    skipped = [s for s in all_statuses if not s.safe]
    return safe_set, skipped


def _prune_withhold_untrustworthy(safe_set: list, skipped: list) -> tuple[list, list, set[str]]:
    """Drop every hive carrying an UNKNOWN row out of the removal set (bh-167s0).

    UNKNOWN is not ``safe``, so an unresolvable row was never going to be removed — but that is
    not enough, and this is the acceptance criterion that says so: prune must "refuse to run
    unattended over a hive containing UNKNOWN rows".  Whatever stopped one bead resolving —
    a store bd will not open, a retired prefix — stopped every OTHER bead in that hive being
    confirmed too, so the SAFE verdicts from the same pass are not evidence either.  They are
    withheld, not removed, and the caller says why and exits non-zero.

    Scoped to the affected HIVE rather than the whole run: a second, healthy hive in the same
    `bh worktree prune` still prunes, because its answers were never in doubt.
    """
    tainted = {s.hive for s in wt_status.untrustworthy(safe_set + skipped)}
    if not tainted:
        return safe_set, skipped, tainted
    withheld = [s for s in safe_set if s.hive in tainted]
    return (
        [s for s in safe_set if s.hive not in tainted],
        skipped + withheld,
        tainted,
    )


def _prune_report_skipped(skipped: list) -> None:
    """Echo the '<n> skipped (not SAFE)' block `prune` renders both on the early "nothing to
    prune" return and after a real removal pass."""
    if skipped:
        typer.echo(f"  {len(skipped)} skipped (not SAFE):")
        for s in skipped:
            typer.echo(f"    {s.leaf}  {s.classification}")


def _prune_remove_one(cfg, entries_by_prefix: dict, main: Path, st) -> bool:
    """Remove one SAFE worktree (delegated plugin first, native `git worktree remove` fallback),
    recording telemetry and native/delegated branch-deletion parity. Returns True iff removal
    succeeded (outcome == "ok")."""
    prefix = st.hive
    entry = entries_by_prefix.get(prefix)
    started = time.monotonic()
    # SAFE (closed + merged + clean) → the branch is disposable, so keep_branch=False: a
    # delegating plugin owns branch cleanup for its own removals (mirrors the native
    # git-branch-D parity step below).
    delegated = entry is not None and _consult_wt_remove(
        cfg, entry, main=main, target=Path(st.path), force=True, keep_branch=False
    )
    if delegated:
        outcome = "ok"
    else:
        res = _run_git(
            ["git", "-C", str(main), "worktree", "remove", "--force", st.path],
            check=False,
        )
        outcome = "ok" if res.returncode == 0 else "error"
    elapsed = time.monotonic() - started
    if outcome == "ok":
        typer.echo(f"  removed {st.path}  [{st.branch}]")
    else:
        # Only the native fallback can fail (a delegated removal is always "ok" here) — `res`
        # is defined in this branch. Native calls aren't captured, so stderr already printed
        # straight to the console; this line just stops the misleading "removed" claim.
        typer.echo(
            f"  failed to remove {st.path}  [{st.branch}]: "
            f"{res.stderr or 'git worktree remove failed'}"
        )
    _record_wt_event("prune", outcome, hive=prefix, leaf=st.leaf)
    _record_wt_op_duration("prune", elapsed, outcome, hive=prefix, leaf=st.leaf)
    if outcome != "ok":
        return False
    _rmdir_empty_parents(st.path, cfg)
    if not delegated:
        # Native/delegated parity (design delta): a SAFE tree is already merged, so once its
        # worktree is gone the branch is dead weight — delete it the same way a delegated remove
        # would. Best-effort: a stray branch never blocks the prune loop.
        _run_git(["git", "-C", str(main), "branch", "-D", st.branch], check=False)
    return True


def _prune_remove_all(cfg, mains: dict, keys: dict, entries_by_prefix: dict, safe_set: list) -> int:
    """Remove every SAFE worktree, `git worktree prune` each touched main clone, and invalidate
    cached metadata for every hive touched. Returns the removed count."""
    from . import metadata

    removed_main_sets: set[str] = set()
    removed_count = 0
    for st in safe_set:
        main = mains.get(st.hive)
        if main is None:
            continue
        if _prune_remove_one(cfg, entries_by_prefix, main, st):
            removed_count += 1
        removed_main_sets.add(str(main))

    for main_str in removed_main_sets:
        _run_git(["git", "-C", main_str, "worktree", "prune"], check=False)

    for prefix in {s.hive for s in safe_set}:
        if prefix in keys:
            metadata.invalidate(cfg, keys[prefix])

    return removed_count


def prune(hive=""):
    """Remove ONLY managed worktrees classified SAFE (closed + merged + clean).

    Uses the classifier to determine which worktrees are safe to remove on each run — no
    confirmation prompt and no --force flag are exposed: ``ws worktree status`` is the
    operator's pre-flight view.

    Scoping: ``--hive <id>`` limits to one hive; omit to prune all managed hives.

    Mode 1 (shared per-hive observaloop profile): this function deliberately does NOT tear down
    the hive's observaloop profile.  The profile is shared across all of the hive's worktrees and
    must remain up until the hive itself is retired — use ``ws observaloop down`` for that.
    Do NOT add per-worktree or per-prune observaloop teardown here; doing so would break the
    shared-profile contract and stop telemetry routing for any remaining worktrees or processes.

    Removal is consulted through a delegating plugin's `wt_remove` hook (`keep_branch=False` —
    SAFE means merged, so the branch is disposable); when no plugin handles it, native removal
    also deletes the now-merged branch (`git branch -D`) for native/delegated parity — the one
    deliberate behavior change over pre-delegation prune.
    """
    cfg = config.load()
    want = str(registry.resolve_hive(cfg, hive)["prefix"]) if hive else None

    mains, keys, entries_by_prefix = _prune_load_entries(cfg)

    swept = _prune_sweep_orphans(entries_by_prefix, want)
    if swept:
        typer.echo(f"  reaped {swept} orphaned verify-* checkout(s)")

    all_rows = managed(cfg)
    rows = [r for r in all_rows if want is None or r[0] == want]

    # Reap dangling `.git/worktrees/<leaf>` admin entries for every hive in scope BEFORE
    # classifying (bh-exe8): a prior partial-failure prune run can leave a hive with a stale
    # worktree registration whose branch no longer resolves, which fires git fatals both
    # during classification (is_merged) and on removal. `_prune_remove_all`'s own trailing
    # `git worktree prune` call only runs for hives with at least one row in `safe_set` this
    # run — a hive where every row classifies NOT SAFE would never get reaped and would
    # repeat the same fatals on every future run. Prune every touched hive unconditionally so
    # it self-heals regardless of this run's classification outcome.
    for prefix in {r[0] for r in rows}:
        main = mains.get(prefix)
        if main is not None:
            _run_git(["git", "-C", str(main), "worktree", "prune"], check=False)

    safe_set, skipped = _prune_classify(cfg, entries_by_prefix, rows)
    safe_set, skipped, tainted = _prune_withhold_untrustworthy(safe_set, skipped)

    if not safe_set:
        typer.echo("no SAFE worktrees to prune")
        _prune_report_skipped(skipped)
        if tainted:
            _warn_untrustworthy(skipped)
            raise typer.Exit(1)
        return

    removed_count = _prune_remove_all(cfg, mains, keys, entries_by_prefix, safe_set)

    typer.echo(f"✓ pruned {removed_count} SAFE worktree(s)")
    _prune_report_skipped(skipped)
    if tainted:
        # Non-zero even though something WAS pruned: an unattended caller that only reads the
        # exit code must not come away believing the run was complete when a whole hive was
        # withheld. A partial prune reported as success is the same class of lie this bead is
        # about — a failure rendered as a normal result.
        _warn_untrustworthy(skipped)
        raise typer.Exit(1)


# ---- worktree status helpers -----------------------------------------------


def _wt_dirty(path: str) -> bool:
    """True iff the worktree at `path` has uncommitted changes.

    Runs ``git status --porcelain`` directly in the worktree directory — the only reliable
    approach for linked worktrees, since the main clone's ``RepoMetadata.branches`` dirty flag
    only reflects the main clone's checked-out branch.  Best-effort: if the path does not exist
    or git fails, treated as clean (not dirty) so a missing worktree is never blocked by I/O.
    """
    try:
        res = _run_git(["git", "-C", path, "status", "--porcelain"], check=False, capture=True)
        return res.returncode == 0 and bool((res.stdout or "").strip())
    except Exception:
        return False


#: Per-COMMAND memo for :func:`_store_readable`, keyed on ``str(main)``. ``None`` outside a
#: :func:`store_probe_cache` block, which means "probe every time" — the default.
_STORE_PROBE_CACHE: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "bh_store_probe_cache", default=None
)


@contextlib.contextmanager
def store_probe_cache():
    """Resolve each hive's store readability ONCE for the duration of this block (bh-ioub2).

    A CONTEXT rather than a process-lifetime memo, and the distinction is the point.
    ``worktree status``'s own help promises it "repopulates fresh metadata before classifying —
    the pre-flight never uses stale data"; a memo that outlived the command would quietly break
    exactly that promise inside a long-lived process (`bh mcp serve` holds one for days). Scoping
    it to the caller keeps the guarantee — one probe per command, never across commands — the
    same way `bd.strict_reads` scopes strictness to a surface rather than a call site.

    THE COST IT REMOVES, measured shape: `retire.teardown_worktrees` calls `worktree.remove` once
    per worktree, and each removal's UNKNOWN preflight re-asked the SAME hive whether its store
    could be read. Retiring the 28-worktree agentguides/runtime hive — the hive that motivated
    bh-167s0 — therefore paid 28 store probes for one hive-level fact, against a store that is by
    that bead's own premise either slow or refusing. An unbounded `bd` call in a loop is the
    shape bh-toitp exists to eliminate; this is that shape, introduced by the fix next door.
    """
    token = _STORE_PROBE_CACHE.set({})
    try:
        yield
    finally:
        _STORE_PROBE_CACHE.reset(token)


def _store_readable(main: Path) -> str:
    """ "" iff this hive's bead store answers with real issues; otherwise WHY it does not.

    THE PRE-FLIGHT'S OWN PRE-FLIGHT (bh-167s0).  ``worktree status`` promises it "repopulates
    fresh metadata before classifying — the pre-flight never uses stale data", and then accepted
    an unreadable bead store without a word.  A per-bead miss cannot tell the two causes apart:
    on the hive that produced this bead, ``bd show <id>`` came back EMPTY WITH EXIT 0 for every
    id, because bd's schema-fork guard was refusing to open the database — identical on the wire
    to "no such bead".  So the store is asked once, up front, and every subsequent miss is read
    in that light.

    ``bd list`` rather than a bead lookup on purpose: the question is whether the store answers
    AT ALL, and a store holding zero issues is the measured signature of the schema-fork guard
    (``issues=0  schema_blocked=1`` on three of the four agentguides hives, while the fourth
    answered 102).  It is deliberately NOT fatal — an empty store is a legitimate state for a
    fresh hive, and the caller's job is to stop CLASSIFYING confidently, not to refuse to run.

    Memoized per main-clone path inside a :func:`store_probe_cache` block, and only there — see
    that function for why the scope is the command rather than the process.
    """
    cache = _STORE_PROBE_CACHE.get()
    if cache is not None and str(main) in cache:
        return cache[str(main)]
    reason = _probe_store(main)
    if cache is not None:
        cache[str(main)] = reason
    return reason


def _probe_store(main: Path) -> str:
    """The uncached probe itself — split out so the memo above is obviously a memo and nothing
    more, and so a test can count invocations of the thing that actually shells out."""
    issues = bd.json(["list"], str(main))
    if issues is None:
        return (
            f"the bead store at {main} could not be READ (bd exited non-zero or returned "
            "no JSON) — bd absent, a schema-fork guard refusing to open the database, or a "
            "store engine that is down; try `bh bd list` there to see bd's own error"
        )
    if isinstance(issues, list) and not issues:
        return (
            f"the bead store at {main} answered with ZERO issues — an empty hive, or a store "
            "bd is refusing to read (its schema-fork guard reports no issues rather than an "
            "error: `bd migrate schema --inspect` reports the real version skew)"
        )
    return ""


def _bead_statuses_for_entry(
    entry,
    rows: list[tuple[str, str, str]],
) -> tuple[dict[str, str], dict[str, str], dict[str, str], str]:
    """Fetch bead statuses and close_reasons for every bead id in ``rows`` for this entry.

    Uses the same ``bd show`` seam as ``doctor._orphan_container_branches`` (bd.show).  The
    bead id is parsed from the real ``wt/bead/<type>/<id>`` branch ref in each row via
    :func:`_bead_id_from_branch` — this preserves dots that the sanitized directory leaf converts
    to dashes (the same fix as ``bead_and_parent``).  Non-bead worktrees are skipped.

    Returns ``(statuses, close_reasons, unknown_reasons, store_reason)``.  ``close_reasons``
    holds the AGF lifecycle close_reason (e.g. ``"merged"``, ``"molecule landed"``) — used by
    ``is_landed`` to confirm rebase/squash-landed branches.

    ``unknown_reasons`` / ``store_reason`` are bh-167s0: an id that does not resolve is reported
    WITH ITS REASON rather than silently becoming an empty status the classifier reads as "open".
    The reason has to be built HERE because this is the only layer that knows both what was
    asked and what came back; the classifier is pure and would have to guess.
    """

    main = registry.hive_dir(entry)
    store_reason = _store_readable(main)
    statuses: dict[str, str] = {}
    close_reasons: dict[str, str] = {}
    unknown_reasons: dict[str, str] = {}
    for _, _path, branch in rows:
        bead_id = _bead_id_from_branch(branch)
        if not bead_id or bead_id in statuses:
            continue
        bead = bd.show(bead_id, str(main))
        statuses[bead_id] = (bead or {}).get("status", "")
        close_reasons[bead_id] = (bead or {}).get("close_reason", "")
        if not statuses[bead_id] and not store_reason:
            # The store answers, and this ONE id is not in it.  Measured cause on the hive that
            # produced this bead: a retired bead PREFIX.  The store held 96 `ag-run-*` beads and
            # zero `ag-rt-*`, while 21 of 28 worktree branches were named `wt/bead/issue/ag-rt-*`
            # — the ids exist, under a name no longer derivable from the branch, and every one of
            # those beads was CLOSED.  `bh hive repair` reconciles registry<->database and stops,
            # so nothing renames the branches; the row is unclassifiable until something does.
            unknown_reasons[bead_id] = (
                f"the store answers, but bead {bead_id} is not in it — the branch names an id "
                "that no longer exists (a retired bead prefix leaves every worktree created "
                "under the old one unresolvable), or the bead was deleted"
            )
    return statuses, close_reasons, unknown_reasons, store_reason


def _classify_entry(
    entry,
    rows: list[tuple[str, str, str]],
    cfg,
) -> list:
    """Classify all managed worktrees for one hive entry.

    Repopulates fresh metadata (ttl=0) then runs the classifier.  Returns a list of
    ``WtStatus`` objects.
    """
    from . import metadata

    key = registry.hive_key(entry)
    meta_map = metadata.read_fleet(cfg, [key], ttl=0)
    meta = meta_map.get(key)
    meta_branches = meta.branches if meta else []

    integration = config.integration_branch(cfg, entry)
    bead_statuses, bead_close_reasons, unknown_reasons, store_reason = _bead_statuses_for_entry(
        entry, rows
    )
    dirty_by_path = {path: _wt_dirty(path) for _, path, _ in rows}

    # Closures capture the full entry so bead_and_parent / is_merged / is_landed receive
    # the correct provider/org/repo context; the classify signature's `entry` param is ignored.
    def _merged_fn(_e, branch, base):
        return is_merged(entry, branch, base)

    def _parent_fn(_e, path, integ, br=""):
        return bead_and_parent(entry, path, integ, br)

    def _landed_fn(_e, branch, base, close_reason):
        return is_landed(entry, branch, base, close_reason)

    return wt_status.classify(
        hive_prefix=str(entry.get("prefix", "")),
        managed_rows=rows,
        meta_branches=meta_branches,
        bead_statuses=bead_statuses,
        dirty_by_path=dirty_by_path,
        is_merged_fn=_merged_fn,
        parent_fn=_parent_fn,
        integration=integration,
        is_landed_fn=_landed_fn,
        bead_close_reasons=bead_close_reasons,
        bead_unknown_reasons=unknown_reasons,
        store_unreadable_reason=store_reason,
    )


_BOX_PIPE = "│  "
_BOX_BRANCH = "├─ "
_BOX_LAST = "└─ "
_BOX_SPACE = "   "


def _status_tags(st) -> str:
    """The trailing tag run on one rendered row.

    ``? UNKNOWN`` is deliberately the loudest thing on the line and the only class carrying a
    glyph: it is the one classification that means "do not act on this row", and it has to be
    findable by eye in a tree of thirty (bh-167s0 — "visually distinct in the rendered tree").
    A ``DIRTY`` row also shows what it is masking, so a dirty-but-SAFE seat is distinguishable
    from a dirty-and-open one and a dirty row over an unresolvable bead cannot look ordinary.
    """
    tags = ""
    if st.merged:
        tags += "  merged"
    if st.dirty:
        tags += "  dirty"
    if getattr(st, "underlying", None):
        tags += f"  (under: {str(st.underlying).upper()})"
    if st.safe:
        tags += "  SAFE"
    return tags


def _render_status(statuses: list, header: str = "") -> None:
    """Render a list of WtStatus entries as a text tree to stdout.

    Format::

        <header>          (omitted when empty)
        ├─ <leaf>  [<branch>]  <CLASSIFICATION>  <merged>  SAFE
        └─ <leaf>  [<branch>]  <CLASSIFICATION>

    Box-drawing prefixes only; no rich / colour.
    """
    if header:
        typer.echo(header)
    for i, st in enumerate(statuses):
        prefix = _BOX_LAST if i == len(statuses) - 1 else _BOX_BRANCH
        mark = "? " if str(st.classification) == "unknown" else ""
        typer.echo(
            f"{prefix}{mark}{st.leaf}  [{st.branch}]  {st.classification.upper()}{_status_tags(st)}"
        )


def _warn_untrustworthy(statuses: list) -> None:
    """Say plainly, per hive, that these classifications cannot back a removal decision.

    bh-167s0's third acceptance criterion, and the reason the bead is P1 rather than cosmetic:
    an operator asking "can I archive this?" was told 16 worktrees were ACTIVE and reasonably
    concluded there was live work.  The rows are not merely wrong — they are UNKNOWABLE from
    here, and one bad row poisons the hive's whole answer, because whatever stopped that bead
    resolving stopped nothing else being confirmed either.
    """
    by_hive: dict[str, list] = {}
    for s in wt_status.untrustworthy(statuses):
        by_hive.setdefault(s.hive, []).append(s)
    for hive, rows in by_hive.items():
        typer.echo(
            f"\n⚠ {hive}: {len(rows)} worktree(s) UNKNOWN — the bead could not be resolved, so "
            f"this hive's classifications are NOT a basis for a removal decision.",
            err=True,
        )
        for reason in sorted({r.unknown_reason for r in rows if r.unknown_reason}):
            typer.echo(f"    {reason}", err=True)
        typer.echo(
            "  `prune` will refuse this hive and `rm` will refuse these rows until it resolves.",
            err=True,
        )


def _status_scope(cfg, hive: str, all_rows: list) -> tuple:
    """Resolve which managed_repos entries — and their pre-grouped rows — are in scope for
    `status_rows`'s `hive` argument. See `status_rows`'s docstring for the scoping rules.
    Returns `(entries, rows_by_prefix)`."""
    if hive:
        entry = _resolve_entry(cfg, hive)
        target_prefix = str(entry.get("prefix", ""))
        return [entry], {target_prefix: [r for r in all_rows if r[0] == target_prefix]}

    # Try to resolve from cwd; fall through to all-hives on failure
    ident = workspace_identity()
    cwd = Path.cwd()
    root = config.worktrees_root()
    try:
        under_wts = cwd.resolve().is_relative_to(root.resolve())
    except OSError:
        under_wts = False

    entry_from_cwd = None
    if ident is not None:
        provider, org, repo = ident
        for e in cfg.get("managed_repos", []) or []:
            if (str(e["provider"]), str(e["org"]), str(e["repo"])) == (provider, org, repo):
                entry_from_cwd = e
                break
    elif under_wts:
        try:
            entry_from_cwd = _entry_for_path(cfg, cwd)
        except SystemExit:
            entry_from_cwd = None

    if entry_from_cwd is not None:
        target_prefix = str(entry_from_cwd.get("prefix", ""))
        return [entry_from_cwd], {target_prefix: [r for r in all_rows if r[0] == target_prefix]}

    # Hub scope: all managed hives
    entries = list(cfg.get("managed_repos", []) or [])
    rows_by_prefix: dict = {}
    for r in all_rows:
        rows_by_prefix.setdefault(r[0], []).append(r)
    return entries, rows_by_prefix


def _status_classifications(hive: str = "", on_complete=None) -> tuple:
    """Load status inputs and classify their populated hives.

    ``on_complete`` receives ``(prefix, statuses)`` in completion order.  It is used only by the
    human multi-hive CLI path; structured callers collect the returned mapping and retain entry
    order.
    """
    cfg = config.load()
    all_rows = managed(cfg)  # [(prefix, path, branch), ...]
    entries, rows_by_prefix = _status_scope(cfg, hive, all_rows)
    return cfg, entries, _classify_entries(cfg, entries, rows_by_prefix, on_complete=on_complete)


def _ordered_statuses(entries: list, statuses_by_prefix: dict[str, list]) -> list:
    """Flatten completed classifications in managed-repository order, never finish order."""
    return [
        status
        for entry in entries
        for status in statuses_by_prefix.get(str(entry.get("prefix", "")), [])
    ]


def status_rows(hive: str = "") -> list:
    """Return the ``WtStatus`` list for managed worktrees — Typer-free core.

    Repopulates fresh metadata before classifying — never uses stale data.
    Scoping mirrors ``status_cmd``:
      - ``hive`` → that hive only.
      - No ``hive`` and cwd is inside a hive → that hive.
      - No ``hive`` and not in a hive (hub) → all managed hives.

    Called by both ``status_cmd`` (the Typer command) and the MCP
    ``beadhive://worktree/list`` resource.
    """
    _cfg, entries, statuses_by_prefix = _status_classifications(hive)
    return _ordered_statuses(entries, statuses_by_prefix)


def _warn_unregistered(unreg) -> None:
    """Surface unregistered repos that have on-disk managed worktrees (bh-ea1i) — a warning so they
    are never silently omitted from status/list. Lists the repo slug + each orphaned worktree."""
    repos = sorted({slug for slug, *_ in unreg})
    typer.echo(
        f"⚠ {len(unreg)} managed worktree(s) under unregistered repo(s) {', '.join(repos)} — "
        f"register with `{config.BINARY_ALIAS} hive add` to include them fully",
        err=True,
    )
    for slug, leaf, path, br in unreg:
        typer.echo(f"    {slug}  {leaf}  [{br}]  {path}", err=True)


def _render_status_multi(by_hive: dict) -> None:
    """Nested tree-with-hive-headers rendering `status_cmd` uses when statuses span >1 hive."""
    hive_keys = list(by_hive)
    for ri, hive_label in enumerate(hive_keys):
        statuses = by_hive[hive_label]
        is_last_hive = ri == len(hive_keys) - 1
        hive_prefix = _BOX_LAST if is_last_hive else _BOX_BRANCH
        typer.echo(f"{hive_prefix}{hive_label}")
        for i, st in enumerate(statuses):
            indent = _BOX_SPACE if is_last_hive else _BOX_PIPE
            node = _BOX_LAST if i == len(statuses) - 1 else _BOX_BRANCH
            mark = "? " if str(st.classification) == "unknown" else ""
            typer.echo(
                f"{indent}{node}{mark}{st.leaf}  [{st.branch}]  {st.classification.upper()}"
                f"{_status_tags(st)}"
            )


def status_cmd(hive: str = "", as_json: bool = False) -> None:
    """Render per-worktree status for one hive (--hive/-r) or all managed hives.

    Repopulates fresh metadata before classifying — the pre-flight never uses stale data.
    Scoping:
      - ``--hive <id>`` → that hive only.
      - No ``--hive`` and cwd is inside a hive → that hive.
      - No ``--hive`` and not in a hive (hub) → all managed hives.
    """
    import json as _json

    # Streaming is only for the all-hive human view.  JSON is intentionally collected first so
    # consumers retain its deterministic managed-repository ordering, and the established
    # single-hive rendering remains byte-for-byte the same.
    cfg = config.load()
    all_rows = managed(cfg)
    entries, rows_by_prefix = _status_scope(cfg, hive, all_rows)
    populated_entries = [
        entry for entry in entries if rows_by_prefix.get(str(entry.get("prefix", "")), [])
    ]
    stream_multi = not as_json and len(populated_entries) > 1

    def render_completed_hive(prefix: str, statuses: list) -> None:
        # Render a complete one-hive tree immediately.  The standalone tree deliberately uses
        # the same renderer and therefore keeps its hierarchy, UNKNOWN marking, and SAFE tags;
        # only section order now follows completion order.
        _render_status_multi({prefix: statuses})

    statuses_by_prefix = _classify_entries(
        cfg,
        entries,
        rows_by_prefix,
        on_complete=render_completed_hive if stream_multi else None,
    )
    all_statuses = _ordered_statuses(entries, statuses_by_prefix)
    unreg = unregistered_worktrees(cfg) if not hive else []

    if as_json:
        typer.echo(_json.dumps([s.as_dict() for s in all_statuses], indent=2))
        # …and on stderr for the JSON reader too. The payload carries `classification:
        # "unknown"` and its `unknown_reason`, but a consumer that only counts `active` sees a
        # plausible answer either way — the same shape bh-fzh4h closed for the MCP surface.
        _warn_untrustworthy(all_statuses)
        if unreg:
            _warn_unregistered(unreg)
        return

    if not all_statuses:
        if unreg:
            _warn_unregistered(unreg)
        else:
            typer.echo("no managed worktrees")
        return

    if not stream_multi:
        # Group by hive for the tree header when covering multiple hives.  In streaming mode,
        # each completed section has already been rendered by the callback above.
        by_hive: dict[str, list] = {}
        for s in all_statuses:
            by_hive.setdefault(s.hive, []).append(s)

        if len(by_hive) == 1:
            # Single-hive: show a flat tree with no hive header
            hive_label, statuses = next(iter(by_hive.items()))
            typer.echo(f"worktrees: {hive_label}")
            _render_status(statuses)
        else:
            # Multi-hive: nest under a hive header line
            _render_status_multi(by_hive)

    _warn_untrustworthy(all_statuses)

    if unreg:
        _warn_unregistered(unreg)


# Implementation modules load after the facade is defined, avoiding circular imports while
# keeping every historical worktree.* symbol and monkeypatch seam intact.
_worktree_git = importlib.import_module(".worktree_git", __package__)
_worktree_verify = importlib.import_module(".worktree_verify", __package__)
