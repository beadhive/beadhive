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

import datetime
import os
import shlex
import tempfile
import time
from pathlib import Path

import typer

from . import bd, config, otel, plugins, registry, worktree_merge
from .identity import workspace_identity
from .run import retry_on_index_lock, run

# Re-export the integration-merge tier (in worktree_merge) so ws.worktree.<name> still works.
merge_no_ff = worktree_merge.merge_no_ff
merge_conflict_paths = worktree_merge.merge_conflict_paths
merge_with_union = worktree_merge.merge_with_union
try_merge_rebase = worktree_merge.try_merge_rebase
_all_union_eligible = worktree_merge._all_union_eligible
_ref_sha = worktree_merge._ref_sha
_try_union_tier = worktree_merge._try_union_tier

_RAND_BYTES = 2  # 4 hex chars — collision cover for two sessions in the same second


def _run_git(args, **kw):
    """Run git with ambient GIT_DIR / GIT_INDEX_FILE / GIT_WORK_TREE scrubbed, so our explicit
    `-C <repo>` always wins (those env vars override -C, and a git hook exports them — without
    this, `ws wt …` invoked inside a hook would operate on the wrong repo).

    Every worktree mutation (worktree add/remove, branch -d, reset --hard, push, rebase) funnels
    through here, so this is also where the ``.git/index.lock`` retry is generalized (bh-i6o7): a
    detached ``git maintenance run --auto`` spawned by an earlier commit can transiently hold the
    index, and a mutation racing it must retry, not fail. ``run`` is passed to the retry so the
    per-module subprocess seam tests fake stays intact."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return retry_on_index_lock(run, args, env=env, **kw)


# ---- naming -----------------------------------------------------------------


WT_PREFIX = "wt/"  # every managed-worktree branch starts here, whatever the mode
VERIFY_LEAF_PREFIX = "verify-"  # ephemeral clean-checkout worktrees (clean_checkout); not a seat
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
    """The managed_repos entry for `hive`, or (when hive is empty) the hive owning cwd.
    Resolves cwd two ways before giving up: a real hive checkout under $GIT_WORKSPACE
    (workspace_identity); else — for agents running inside an OS-temp managed worktree, whose
    path is NOT under $GIT_WORKSPACE — by reverse-mapping cwd against the shadow worktrees root
    (_entry_for_path), so no --hive is needed. Synthesizes a minimal entry from the triplet when
    the repo isn't registered; clear error only when cwd belongs to no hive at all."""
    if hive:
        return registry.resolve_hive(cfg, hive)
    ident = workspace_identity()
    if ident is not None:
        provider, org, repo = ident
        for e in cfg.get("managed_repos", []) or []:
            if (str(e["provider"]), str(e["org"]), str(e["repo"])) == (provider, org, repo):
                return e
        return {"provider": provider, "org": org, "repo": repo, "prefix": repo}
    cwd = Path.cwd()
    root = config.worktrees_root()
    try:
        under = cwd.resolve().is_relative_to(root.resolve())
    except OSError:
        under = False
    if under:
        return _entry_for_path(cfg, cwd)
    typer.echo("✗ no --hive given and cwd is not a repo under $GIT_WORKSPACE", err=True)
    raise typer.Exit(1)


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
    """Global worktrees.init then the hive's worktree_init (both lists of {run, if_exists?})."""
    out = list(config.worktrees_cfg(cfg).get("init", []) or [])
    out += list(entry.get("worktree_init", []) or [])
    return out


def run_init(cfg, entry, path: Path):
    """Evaluate init rules in `path`: run each whose if_exists glob matches (or has none).
    Best-effort — a failing/absent command warns and we keep going."""
    for rule in _rules(cfg, entry):
        cmd = (rule or {}).get("run")
        if not cmd:
            continue
        cond = rule.get("if_exists")
        if cond and not any(path.glob(cond)):
            continue
        typer.echo(f"  → {cmd}")
        try:
            res = run(shlex.split(cmd), cwd=str(path), check=False)
        except FileNotFoundError:
            typer.echo(f"  ⚠ init: command not found: {cmd}", err=True)
            continue
        if res.returncode != 0:
            typer.echo(f"  ⚠ init: '{cmd}' exited {res.returncode}", err=True)


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
    elapsed = time.monotonic() - started
    _record_wt_op_duration("create", elapsed, "ok", hive=hive, leaf=target.name)
    run_init(cfg, entry, target)
    provision_observaloop(cfg, entry, target)
    _record_wt_event("create", hive=hive, leaf=target.name)


def add(hive="", bead="", branch="", dry_run=False):
    if bead and branch:
        typer.echo("✗ pass at most one of --bead / --branch", err=True)
        raise typer.Exit(1)
    cfg = config.load()
    entry = _resolve_entry(cfg, hive)
    main = registry.hive_dir(entry)
    if not (main / ".git").exists():
        typer.echo(f"✗ no clone for hive at {main} — clone it first", err=True)
        raise typer.Exit(1)

    br, leaf = _branch_and_leaf(cfg, bead=bead, branch=branch)
    target = wt_dir(entry, leaf)
    typer.echo(f"hive {entry['provider']}/{entry['org']}/{entry['repo']}  branch {br}")
    typer.echo(f"  → {target}")
    if dry_run:
        typer.echo("(dry-run — nothing changed)")
        return
    if target.exists():
        typer.echo(f"✗ worktree path already exists: {target}", err=True)
        raise typer.Exit(1)
    _do_add(cfg, entry, main, br, target, new_branch=True)
    from . import metadata

    metadata.invalidate(cfg, registry.hive_key(entry))  # branch/worktree churn on this hive
    typer.echo(f"✓ worktree ready: {target}")


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
    res = _run_git(
        ["git", "-C", str(main), "rev-list", "--count", f"{branch}..{base}"],
        check=False,
        capture=True,
    )
    behind = int((res.stdout or "0").strip() or "0") if res.returncode == 0 else 0
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


def ensure(cfg, hive, bead="", branch="", base_bead="", kind=""):
    """Idempotent provision/re-attach for `ws work`. Returns (entry, target, branch): reuse a live
    dir; else attach an existing branch into a fresh dir; else create the branch+dir forked off its
    `integration_base` — the nearest started container (a parent epic/workstream) or `integration`
    (start-point threading). Keys on `bead` (single-bead `wt/bead/<type>/<id>`) or a raw `branch`
    suffix (a work-group's shared `wt/<name>` worktree); `kind` fixes the bead branch's `<type>`
    (epic for a coordinator seat, else issue); `base_bead` names the bead whose container sets the
    start point (defaults to `bead`). Init runs only on a new dir."""
    entry, main, target, br = locate(cfg, hive, bead=bead, branch=branch, kind=kind)
    if not (main / ".git").exists():
        typer.echo(f"✗ no clone for hive at {main} — clone it first", err=True)
        raise typer.Exit(1)
    if target.exists():
        if bead:  # only a single-bead child branch tracks a refreshable container tip
            _repoint_if_stale(cfg, entry, main, br, target, base_bead or bead)
        return entry, target, br
    new_branch = not _branch_exists(main, br)
    start_point = ""
    if new_branch:
        integration = config.integration_branch(cfg, entry)
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
    """(count, [subjects]) for commits on `branch` not reachable from `base`.
    (-1, []) when the range can't be computed (e.g. base missing)."""
    main = registry.hive_dir(entry)
    rng = f"{base}..{branch}"
    cres = _run_git(["git", "-C", str(main), "rev-list", "--count", rng], check=False, capture=True)
    if cres.returncode != 0:
        return -1, []
    count = int((cres.stdout or "0").strip() or "0")
    lres = _run_git(["git", "-C", str(main), "log", "--format=%s", rng], check=False, capture=True)
    subjects = [s for s in (lres.stdout or "").splitlines() if s.strip()]
    return count, subjects


def clean_checkout(entry, branch, cmd) -> int:
    """Validate `branch` from a throwaway detached worktree, so the result never depends on
    dirty local state. The validation command runs with a telemetry-neutral env
    (`otel.telemetry_neutral_env`) so its result is independent of the operator's otel config and
    never exports telemetry. Returns the validation command's exit code (or git's, if checkout
    fails)."""
    main = registry.hive_dir(entry)
    leaf = registry.sanitize(f"{VERIFY_LEAF_PREFIX}{branch.rsplit('/', 1)[-1]}")
    tmp = wt_dir(entry, leaf)
    if tmp.exists():
        _run_git(["git", "-C", str(main), "worktree", "remove", "--force", str(tmp)], check=False)
    tmp.parent.mkdir(parents=True, exist_ok=True)
    add_res = _run_git(
        ["git", "-C", str(main), "worktree", "add", "--detach", str(tmp), branch], check=False
    )
    if add_res.returncode != 0:
        return add_res.returncode
    try:
        return run(
            shlex.split(cmd), cwd=str(tmp), check=False, env=otel.telemetry_neutral_env()
        ).returncode
    finally:
        _run_git(["git", "-C", str(main), "worktree", "remove", "--force", str(tmp)], check=False)


def push_branch(entry, branch, remote="origin") -> int:
    """Push `branch` to `remote` (same name both ends). Returns git's exit code."""
    main = registry.hive_dir(entry)
    return _run_git(
        ["git", "-C", str(main), "push", remote, f"{branch}:{branch}"], check=False
    ).returncode


def is_clean(target: Path) -> bool:
    """True iff the worktree at `target` has no staged/unstaged/untracked changes."""
    res = _run_git(["git", "-C", str(target), "status", "--porcelain"], check=False, capture=True)
    return res.returncode == 0 and not (res.stdout or "").strip()


def current_branch(target: Path) -> str:
    """The checked-out branch name in `target` ('' if detached / on error)."""
    res = _run_git(
        ["git", "-C", str(target), "rev-parse", "--abbrev-ref", "HEAD"], check=False, capture=True
    )
    name = (res.stdout or "").strip() if res.returncode == 0 else ""
    return "" if name == "HEAD" else name


def head_sha(target: Path) -> str:
    """Short HEAD sha in `target` ('' on error)."""
    res = _run_git(
        ["git", "-C", str(target), "rev-parse", "--short", "HEAD"], check=False, capture=True
    )
    return (res.stdout or "").strip() if res.returncode == 0 else ""


# ---- show / refine git helpers (all git; no bd — keeps work.py's bd seam intact) ----
#
# `commit_rows` packs each commit into one log line with a record separator (RS) leading the
# format and a unit separator (FS) between fields, so the subject (last field, may contain
# spaces) never needs quoting; --name-only files trail each record until the next RS.
_ROW_RS = "\x1e"
_ROW_FS = "\x1f"
_ROW_FMT = _ROW_RS + _ROW_FS.join(["%H", "%h", "%P", "%an", "%ae", "%ad", "%G?", "%GS", "%s"])


def base_of(entry, branch, integration) -> str:
    """The fork point `git merge-base <integration> <branch>` — base..branch is the bead's
    local history. '' if it can't be computed (e.g. integration branch missing locally)."""
    main = registry.hive_dir(entry)
    res = _run_git(
        ["git", "-C", str(main), "merge-base", integration, branch], check=False, capture=True
    )
    return (res.stdout or "").strip() if res.returncode == 0 else ""


def commit_rows(entry, base, branch) -> list[dict]:
    """Oldest→newest commits in base..branch. Each row: {sha, short, parents, author, email,
    date (author date, iso-strict), subject, files, sig (G/U/B/N), signer}. [] on error."""
    main = registry.hive_dir(entry)
    res = _run_git(
        [
            "git",
            "-C",
            str(main),
            "log",
            f"{base}..{branch}",
            "--reverse",
            "--date=iso-strict",
            "--name-only",
            f"--format={_ROW_FMT}",
        ],
        check=False,
        capture=True,
    )
    if res.returncode != 0:
        return []
    rows = []
    for chunk in (res.stdout or "").split(_ROW_RS):
        chunk = chunk.strip("\n")
        if not chunk:
            continue
        lines = chunk.split("\n")
        f = lines[0].split(_ROW_FS)
        if len(f) < 9:
            continue
        sha, short, parents, an, ae, ad, sig, signer, subj = f[:9]
        rows.append(
            {
                "sha": sha,
                "short": short,
                "parents": parents.split(),
                "author": an,
                "email": ae,
                "date": ad,
                "subject": subj,
                "files": [ln for ln in lines[1:] if ln.strip()],
                "sig": sig,
                "signer": signer,
            }
        )
    return rows


def backup_branch(entry, branch, ts: str, label: str = "refine") -> str:
    """Create the safety branch `<branch>.<label>-<ts>` at `branch`'s tip; return its name.
    Caller supplies `ts` (ws runtime may stamp time freely). `label` distinguishes the operation
    (refine vs. premerge rebase) so concurrent safety refs never collide."""
    main = registry.hive_dir(entry)
    name = f"{branch}.{label}-{ts}"
    res = _run_git(["git", "-C", str(main), "branch", name, branch], check=False, capture=True)
    if res.returncode != 0:
        typer.echo(f"✗ could not create backup branch {name}: {res.stderr or res.stdout}", err=True)
        raise typer.Exit(1)
    return name


def _rebase_env(**extra) -> dict:
    """git env with the dir-pointing GIT_* scrubbed (so `-C` wins) plus our editor overrides —
    `_run_git` can't be reused here because it scrubs ALL GIT_* incl. the ones we must set."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(extra)
    return env


def rebase_squash(target_wt, base, todo_lines) -> tuple[int, str]:
    """Run `git rebase -i <base>` in the WORKTREE (the branch is checked out there) with a
    non-interactive sequence editor that overwrites git's todo with `todo_lines`. GIT_EDITOR is
    pinned to a no-op too (fixup/exec need no editor) so nothing can block. (rc, combined out)."""
    with tempfile.NamedTemporaryFile("w", suffix=".gittodo", delete=False) as f:
        f.write("\n".join(todo_lines) + "\n")
        todo_path = f.name
    env = _rebase_env(GIT_SEQUENCE_EDITOR=f"cp {shlex.quote(todo_path)}", GIT_EDITOR="true")
    try:
        res = run(
            ["git", "-C", str(target_wt), "rebase", "-i", base],
            env=env,
            check=False,
            capture=True,
        )
    finally:
        os.unlink(todo_path)
    return res.returncode, (res.stdout or "") + (res.stderr or "")


def rebase_autosquash(target_wt, base) -> tuple[int, str]:
    """`git rebase -i --autosquash <base>` with no-op editors: git auto-builds the todo placing
    each `fixup!`/`squash!` after its target, and `true` accepts it unedited. (rc, combined)."""
    env = _rebase_env(GIT_SEQUENCE_EDITOR="true", GIT_EDITOR="true")
    res = run(
        ["git", "-C", str(target_wt), "rebase", "-i", "--autosquash", base],
        env=env,
        check=False,
        capture=True,
    )
    return res.returncode, (res.stdout or "") + (res.stderr or "")


def rebase_onto(target_wt, base) -> tuple[int, str]:
    """Plain `git rebase <base>` in the worktree (the branch is checked out there) — replay the
    branch's commits onto a newer base. Used by `try_merge_rebase`'s conflict recovery; a clean
    replay needs no editor, and on conflict git stops non-zero so the caller can abort. (rc, out)"""
    # rerere off for the same reason as merge_no_ff: don't let a cached resolution mask a real
    # replay conflict. Cherry-pick de-duplication (the actual replay win) is independent of rerere.
    res = _run_git(
        ["git", "-C", str(target_wt), "-c", "rerere.enabled=false", "rebase", str(base)],
        check=False,
        capture=True,
    )
    return res.returncode, (res.stdout or "") + (res.stderr or "")


def rebase_abort(target_wt) -> None:
    """Best-effort `git rebase --abort` (no-op if no rebase is in progress)."""
    _run_git(["git", "-C", str(target_wt), "rebase", "--abort"], check=False, capture=True)


def reset_hard(target_wt, ref) -> int:
    """`git reset --hard <ref>` in the worktree. Returns git's exit code."""
    return _run_git(
        ["git", "-C", str(target_wt), "reset", "--hard", ref], check=False, capture=True
    ).returncode


def safe_to_rewrite(clone, branch) -> bool:
    """True iff `branch` may be `reset --hard` without rewriting shared/published history: any
    branch with no configured upstream (not pushed). A private container integration branch
    (`wt/bead/epic/<id>`, any tier) is local/unpushed → safe, so an intermediate tier land rolls
    back losslessly. A pushed integration branch (e.g. `main` tracking `origin/main`) is NOT safe —
    a red landing there must be fixed forward, not rewritten."""
    return (
        _run_git(
            ["git", "-C", str(clone), "rev-parse", "--abbrev-ref", f"{branch}@{{upstream}}"],
            check=False,
            capture=True,
        ).returncode
        != 0
    )


def same_tree(entry, a, b) -> bool:
    """True iff refs `a` and `b` have byte-identical trees — the refine safety gate."""
    main = registry.hive_dir(entry)
    return _run_git(["git", "-C", str(main), "diff", "--quiet", a, b], check=False).returncode == 0


def is_merged(entry, branch: str, base: str) -> bool:
    """True iff every commit on `branch` is already reachable from `base`.

    Uses ``git merge-base --is-ancestor branch base`` which exits 0 when ``branch`` is an
    ancestor of ``base`` (i.e. all its commits are included in ``base``).  This is the
    merge-ancestry primitive that the worktree SAFE classifier depends on — the only call that
    performs a real git ancestry check rather than inferring merged-ness from bead status.
    """
    main = registry.hive_dir(entry)
    return (
        _run_git(
            ["git", "-C", str(main), "merge-base", "--is-ancestor", branch, base],
            check=False,
        ).returncode
        == 0
    )


def _all_cherry_landed(entry, branch: str, parent: str) -> bool:
    """True iff every unique commit on ``branch`` (not in ``parent``) is already present
    in ``parent`` by patch-id equivalence.

    Uses ``git cherry <parent> <branch>``: commits marked ``-`` are already in parent
    (patch-id equivalent from a rebase or cherry-pick); commits marked ``+`` are not.
    Returns ``True`` when all unique commits are ``-`` or there are no unique commits.
    Returns ``False`` on git failure (conservative — prefer UNMERGED over a false positive).

    Limitation: pure squash-merges (N commits collapsed to one) cannot be detected here
    because the squashed commit will not patch-id-match the individual originals.  Use the
    merge-event check (``is_landed``) for squash-landed branches.
    """
    main = registry.hive_dir(entry)
    res = _run_git(
        ["git", "-C", str(main), "cherry", parent, branch],
        check=False,
        capture=True,
    )
    if res.returncode != 0:
        return False
    lines = [ln for ln in (res.stdout or "").splitlines() if ln.strip()]
    # Empty output → branch adds no unique commits (already covered).
    # All "-" lines → every commit already in parent by patch-id.
    return all(ln.startswith("- ") for ln in lines) if lines else True


def is_landed(entry, branch: str, parent: str, close_reason: str = "") -> bool:
    """True iff a closed-but-non-ancestor branch has its content effectively in ``parent``.

    Second-stage check for the closed+non-ancestor set (today's UNMERGED rows).  Runs
    ONLY after the fast-path ``is_merged`` ancestor check has returned ``False``, so the
    git work here is bounded to the cases that actually need it.

    Two checks in priority order:

    1. **Merge-event** (fast, authoritative, squash-proof): if ``close_reason`` is
       ``"merged"`` or ``"molecule landed"``, the AGF lifecycle confirms the work landed
       and the branch is safe to reclaim — regardless of SHA identity.

    2. **Patch-id / cherry equivalence** (fallback for branches without a merge event):
       ``git cherry <parent> <branch>`` marks commits already in parent with ``-``.  If
       every unique commit is so marked, the branch was rebase/cherry-pick landed.  Not
       reliable for pure squash-merges (which have no patch-id match), so those require
       a merge event recorded in close_reason.

    Returns ``False`` on git failure (conservative: prefer UNMERGED over a false positive).
    """
    if close_reason in ("merged", "molecule landed"):
        return True
    return _all_cherry_landed(entry, branch, parent)


def bead_and_parent(entry, path: str, integration: str, branch: str = "") -> tuple[str | None, str]:
    """Map a managed worktree path to ``(bead_id | None, parent_branch)``.

    The bead id is parsed from the real ``wt/bead/<type>/<id>`` branch ref (the ``branch``
    argument from ``managed()``'s row) via :func:`_bead_id_from_branch`, which strips the
    ``wt/bead/<type>/`` prefix.  This is the primary path: the actual ref preserves dots and other
    characters that the sanitized directory leaf loses (e.g. wt/bead/issue/
    vs. the dashed leaf -1).

    When ``branch`` is not supplied (legacy callers), the function falls back to reconstructing
    the branch from the directory leaf, probing each ``wt/bead/<type>/<leaf>`` namespace.

    The parent branch is resolved via :func:`integration_base`: the nearest started container
    ancestor (a parent epic/workstream branch ``wt/bead/epic/<parent>``) up the id chain, else
    ``integration``.
    """
    if branch:
        # Primary path: parse the bead id from the real branch ref supplied by managed().
        # This preserves dots that the sanitized directory leaf converts to dashes.
        bead_id: str | None = _bead_id_from_branch(branch)
    else:
        # Fallback for callers that do not supply the branch ref (legacy / no-op path).
        rel = Path(path).relative_to(config.worktrees_root())
        leaf = rel.parts[-1] if len(rel.parts) >= 4 else ""
        main = registry.hive_dir(entry)
        bead_id = None
        if leaf:
            for t in BEAD_KINDS:
                if _branch_exists(main, f"{_BEAD_PREFIX}{t}/{leaf}"):
                    bead_id = leaf
                    break

    parent = integration_base(entry, bead_id, integration) if bead_id else integration
    return bead_id, parent


def diff_range(entry, base, branch) -> int:
    """Stream `git diff base..branch` to stdout (the net change). Returns git's exit code."""
    main = registry.hive_dir(entry)
    return _run_git(["git", "-C", str(main), "diff", f"{base}..{branch}"], check=False).returncode


def log_range(entry, base, branch) -> str:
    """`git log --oneline base..branch` (oldest→newest) — the post-refine digest summary."""
    main = registry.hive_dir(entry)
    res = _run_git(
        [
            "git",
            "-C",
            str(main),
            "log",
            "--reverse",
            "--format=%h %ad %s",
            "--date=short",
            f"{base}..{branch}",
        ],
        check=False,
        capture=True,
    )
    return (res.stdout or "") if res.returncode == 0 else ""


def managed(cfg):
    """[(prefix, path, branch)] for every linked worktree under the shadow root."""
    root = str(config.worktrees_root().resolve())
    out = []
    for e in cfg.get("managed_repos", []) or []:
        main = registry.hive_dir(e)
        if not (main / ".git").exists():
            continue
        res = _run_git(
            ["git", "-C", str(main), "worktree", "list", "--porcelain"],
            check=False,
            capture=True,
        )
        if res.returncode != 0:
            continue
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


def remove(hive, ref, force=False):
    """Remove one managed worktree. The branch is the durable artifact here (a bead's history
    lives on it), so a delegating plugin's `wt_remove` hook is consulted with `keep_branch=True`
    — never call this for a disposable prune removal (see `prune`)."""
    cfg = config.load()
    entry = _resolve_entry(cfg, hive)
    main = registry.hive_dir(entry)
    target = wt_dir(entry, _leaf(ref))
    hive = str(entry.get("prefix", ""))
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
    typer.echo(f"✓ removed {target}")


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
    all_rows = managed(cfg)
    rows = [r for r in all_rows if want is None or r[0] == want]

    mains: dict[str, Path] = {}
    keys: dict[str, str] = {}
    entries_by_prefix: dict[str, dict] = {}
    for e in cfg.get("managed_repos", []) or []:
        p = str(e["prefix"])
        mains[p] = registry.hive_dir(e)
        keys[p] = registry.hive_key(e)
        entries_by_prefix[p] = e

    # Classify every candidate row (repopulates fresh metadata per entry)
    statuses_by_prefix: dict[str, list] = {}
    for prefix in {r[0] for r in rows}:
        entry = entries_by_prefix.get(prefix)
        if entry is None:
            continue
        entry_rows = [r for r in rows if r[0] == prefix]
        statuses_by_prefix[prefix] = _classify_entry(entry, entry_rows, cfg)

    all_statuses = [s for slist in statuses_by_prefix.values() for s in slist]
    safe_set = [s for s in all_statuses if s.safe]
    skipped = [s for s in all_statuses if not s.safe]

    if not safe_set:
        typer.echo("no SAFE worktrees to prune")
        if skipped:
            typer.echo(f"  {len(skipped)} skipped (not SAFE):")
            for s in skipped:
                typer.echo(f"    {s.leaf}  {s.classification}")
        return

    removed_main_sets: set[str] = set()
    removed_count = 0
    from . import metadata

    for st in safe_set:
        prefix = st.hive
        main = mains.get(prefix)
        if main is None:
            continue
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
        typer.echo(f"  removed {st.path}  [{st.branch}]")
        _record_wt_event("prune", outcome, hive=prefix, leaf=st.leaf)
        _record_wt_op_duration("prune", elapsed, outcome, hive=prefix, leaf=st.leaf)
        if outcome == "ok":
            _rmdir_empty_parents(st.path, cfg)
            removed_count += 1
            if not delegated:
                # Native/delegated parity (design delta): a SAFE tree is already merged, so once
                # its worktree is gone the branch is dead weight — delete it the same way a
                # delegated remove would. Best-effort: a stray branch never blocks the prune loop.
                _run_git(["git", "-C", str(main), "branch", "-D", st.branch], check=False)
        removed_main_sets.add(str(main))

    for main_str in removed_main_sets:
        _run_git(["git", "-C", main_str, "worktree", "prune"], check=False)

    for prefix in {s.hive for s in safe_set}:
        if prefix in keys:
            metadata.invalidate(cfg, keys[prefix])

    typer.echo(f"✓ pruned {removed_count} SAFE worktree(s)")
    if skipped:
        typer.echo(f"  {len(skipped)} skipped (not SAFE):")
        for s in skipped:
            typer.echo(f"    {s.leaf}  {s.classification}")


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


def _bead_statuses_for_entry(
    entry,
    rows: list[tuple[str, str, str]],
) -> tuple[dict[str, str], dict[str, str]]:
    """Fetch bead statuses and close_reasons for every bead id in ``rows`` for this entry.

    Uses the same ``bd show`` seam as ``doctor._orphan_container_branches`` (bd.show).  The
    bead id is parsed from the real ``wt/bead/<type>/<id>`` branch ref in each row via
    :func:`_bead_id_from_branch` — this preserves dots that the sanitized directory leaf converts
    to dashes (the same fix as ``bead_and_parent``).  Non-bead worktrees are skipped.

    Returns ``(statuses, close_reasons)`` where both map ``bead_id -> string``.
    ``close_reasons`` holds the AGF lifecycle close_reason (e.g. ``"merged"``,
    ``"molecule landed"``) — used by ``is_landed`` to confirm rebase/squash-landed branches.
    """

    main = registry.hive_dir(entry)
    statuses: dict[str, str] = {}
    close_reasons: dict[str, str] = {}
    for _, _path, branch in rows:
        bead_id = _bead_id_from_branch(branch)
        if not bead_id or bead_id in statuses:
            continue
        bead = bd.show(bead_id, str(main))
        statuses[bead_id] = (bead or {}).get("status", "")
        close_reasons[bead_id] = (bead or {}).get("close_reason", "")
    return statuses, close_reasons


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
    from .wt_status import classify

    key = registry.hive_key(entry)
    meta_map = metadata.read_fleet(cfg, [key], ttl=0)
    meta = meta_map.get(key)
    meta_branches = meta.branches if meta else []

    integration = config.integration_branch(cfg, entry)
    bead_statuses, bead_close_reasons = _bead_statuses_for_entry(entry, rows)
    dirty_by_path = {path: _wt_dirty(path) for _, path, _ in rows}

    # Closures capture the full entry so bead_and_parent / is_merged / is_landed receive
    # the correct provider/org/repo context; the classify signature's `entry` param is ignored.
    def _merged_fn(_e, branch, base):
        return is_merged(entry, branch, base)

    def _parent_fn(_e, path, integ, br=""):
        return bead_and_parent(entry, path, integ, br)

    def _landed_fn(_e, branch, base, close_reason):
        return is_landed(entry, branch, base, close_reason)

    return classify(
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
    )


_BOX_PIPE = "│  "
_BOX_BRANCH = "├─ "
_BOX_LAST = "└─ "
_BOX_SPACE = "   "


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
        safe_tag = "  SAFE" if st.safe else ""
        merged_tag = "  merged" if st.merged else ""
        dirty_tag = "  dirty" if st.dirty else ""
        typer.echo(
            f"{prefix}{st.leaf}"
            f"  [{st.branch}]"
            f"  {st.classification.upper()}"
            f"{merged_tag}{dirty_tag}{safe_tag}"
        )


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
    cfg = config.load()
    all_rows = managed(cfg)  # [(prefix, path, branch), ...]

    if hive:
        entry = _resolve_entry(cfg, hive)
        target_prefix = str(entry.get("prefix", ""))
        entries = [entry]
        rows_by_prefix = {target_prefix: [r for r in all_rows if r[0] == target_prefix]}
    else:
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
            entries = [entry_from_cwd]
            rows_by_prefix = {target_prefix: [r for r in all_rows if r[0] == target_prefix]}
        else:
            # Hub scope: all managed hives
            entries = list(cfg.get("managed_repos", []) or [])
            rows_by_prefix = {}
            for r in all_rows:
                rows_by_prefix.setdefault(r[0], []).append(r)

    all_statuses: list = []
    for e in entries:
        prefix = str(e.get("prefix", ""))
        rows = rows_by_prefix.get(prefix, [])
        if not rows:
            continue
        statuses = _classify_entry(e, rows, cfg)
        all_statuses.extend(statuses)

    return all_statuses


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


def status_cmd(hive: str = "", as_json: bool = False) -> None:
    """Render per-worktree status for one hive (--hive/-r) or all managed hives.

    Repopulates fresh metadata before classifying — the pre-flight never uses stale data.
    Scoping:
      - ``--hive <id>`` → that hive only.
      - No ``--hive`` and cwd is inside a hive → that hive.
      - No ``--hive`` and not in a hive (hub) → all managed hives.
    """
    import json as _json

    all_statuses = status_rows(hive=hive)
    unreg = unregistered_worktrees(config.load()) if not hive else []

    if as_json:
        typer.echo(_json.dumps([s.as_dict() for s in all_statuses], indent=2))
        if unreg:
            _warn_unregistered(unreg)
        return

    if not all_statuses:
        if unreg:
            _warn_unregistered(unreg)
        else:
            typer.echo("no managed worktrees")
        return

    # Group by hive for the tree header when covering multiple hives
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
        hive_keys = list(by_hive)
        for ri, hive_label in enumerate(hive_keys):
            statuses = by_hive[hive_label]
            is_last_hive = ri == len(hive_keys) - 1
            hive_prefix = _BOX_LAST if is_last_hive else _BOX_BRANCH
            typer.echo(f"{hive_prefix}{hive_label}")
            for i, st in enumerate(statuses):
                indent = _BOX_SPACE if is_last_hive else _BOX_PIPE
                node = _BOX_LAST if i == len(statuses) - 1 else _BOX_BRANCH
                safe_tag = "  SAFE" if st.safe else ""
                merged_tag = "  merged" if st.merged else ""
                dirty_tag = "  dirty" if st.dirty else ""
                typer.echo(
                    f"{indent}{node}{st.leaf}"
                    f"  [{st.branch}]"
                    f"  {st.classification.upper()}"
                    f"{merged_tag}{dirty_tag}{safe_tag}"
                )

    if unreg:
        _warn_unregistered(unreg)
