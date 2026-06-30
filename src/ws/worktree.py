"""ws-managed git worktrees in a shadow tree outside $GIT_WORKSPACE.

Each worktree is a normal linked `git worktree` of a rig's main clone
($GIT_WORKSPACE/<provider>/<org>/<repo>), but its working dir lives under a single
shadow root (default ~/.ws/worktrees, $WS_WORKTREES / config worktrees.root) mirroring
the triplet path:  <root>/<provider>/<org>/<repo>/<leaf>. Living outside the workspace
means no collision with git-workspace repo roots, "ours vs hand-made" is just a
path-prefix test, and bulk cleanup is one subtree.

Every managed branch is prefixed `wt/` (applied once, centrally), so a worktree branch is
obvious at a glance. Each mode only computes the suffix after it (templates configurable):
  --bead ID    -> wt/ + worktrees.bead_branch   (default "bead/{id}")    -> wt/bead/<id>
  --branch B   -> wt/ + B                         (not a full override)   -> wt/<B>
  neither      -> wt/ + worktrees.session_branch (default "session/{ts}-{rand}")
The leaf is the sanitized last path segment of the branch (bead ids / session ids are
already unique, so the namespace prefix is dropped for a clean dir name).

Post-create init is declarative config (no scripting): a list of {run, if_exists?} rules.
Global worktrees.init runs first, then the rig's worktree_init. if_exists is a glob
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

from . import config, otel, registry, worktree_merge
from .identity import workspace_identity
from .run import run

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
    this, `ws wt …` invoked inside a hook would operate on the wrong repo)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return run(args, env=env, **kw)


# ---- naming -----------------------------------------------------------------


WT_PREFIX = "wt/"  # every managed-worktree branch starts here, whatever the mode
MOL_PREFIX = "mol/"  # a molecule's integration branch is mol/<epic>
VERIFY_LEAF_PREFIX = "verify-"  # ephemeral clean-checkout worktrees (clean_checkout); not a seat


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
    """Sanitized last path segment of a branch ('wt/bead/ag-infra-7' -> 'ag-infra-7')."""
    return registry.sanitize(branch.rsplit("/", 1)[-1])


def _suffix(cfg, bead="", branch="", now=None, rand=None) -> str:
    """The branch suffix (everything after the wt/ prefix) for each creation mode. Adding a
    fourth mode = adding a branch here; the wt/ prefix is applied once in _branch_and_leaf."""
    wcfg = config.worktrees_cfg(cfg)
    if bead:
        return str(wcfg.get("bead_branch", "bead/{id}")).format(id=bead)
    if branch:
        return branch
    ts, rnd = _ts_rand(now=now, rand=rand)
    tmpl = str(wcfg.get("session_branch", "session/{ts}-{rand}"))
    return tmpl.format(ts=ts, rand=rnd, id=f"{ts}-{rnd}")


def apply_prefix(suffix: str) -> str:
    """Prepend the managed wt/ prefix to a branch suffix, never doubling an existing wt/."""
    return WT_PREFIX + suffix.removeprefix(WT_PREFIX).lstrip("/")


def _branch_and_leaf(cfg, bead="", branch="", now=None, rand=None):
    """(branch, leaf). Every mode yields a suffix; we always prepend wt/ (so a managed
    worktree is obvious from the branch), normalizing to never double a wt/wt/."""
    br = apply_prefix(_suffix(cfg, bead=bead, branch=branch, now=now, rand=rand))
    return br, _leaf(br)


# ---- rig / path resolution --------------------------------------------------


def wt_dir(entry, leaf: str) -> Path:
    """<root>/<provider>/<org>/<repo>/<leaf> — mirrors registry.rig_dir under the shadow root."""
    root = config.worktrees_root()
    return root / str(entry["provider"]) / str(entry["org"]) / str(entry["repo"]) / leaf


def _resolve_entry(cfg, rig):
    """The managed_repos entry for `rig`, or (when rig is empty) the rig owning cwd.
    Resolves cwd two ways before giving up: a real rig checkout under $GIT_WORKSPACE
    (workspace_identity); else — for agents running inside an OS-temp managed worktree, whose
    path is NOT under $GIT_WORKSPACE — by reverse-mapping cwd against the shadow worktrees root
    (_entry_for_path), so no --rig is needed. Synthesizes a minimal entry from the triplet when
    the repo isn't registered; clear error only when cwd belongs to no rig at all."""
    if rig:
        return registry.resolve_rig(cfg, rig)
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
    typer.echo("✗ no --rig given and cwd is not a repo under $GIT_WORKSPACE", err=True)
    raise typer.Exit(1)


def _entry_for_path(cfg, path: Path):
    """Reverse a worktree path back to its rig entry via the triplet segments under root."""
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
    """Global worktrees.init then the rig's worktree_init (both lists of {run, if_exists?})."""
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
    """Best-effort per-rig observaloop profile provisioning + worktree overlay, run on a TRUE
    worktree create (after ``run_init``, from ``_do_add`` — the chokepoint that ``clean_checkout``
    bypasses, so ephemeral ``verify-`` worktrees never reach here).

    Gated and import-cheap by design: the default (observaloop disabled) path is a single
    ``config.observaloop_enabled`` check and imports **no** observaloop module. Only when enabled do
    we lazily import the observaloop seams, derive the per-rig profile name, idempotently
    ``ensure_profile`` + ``up`` (a profile is per-rig, shared across its worktrees), resolve the
    OTLP endpoint, and write ``<worktree>/.ws/otel.env`` so a ``ws`` invocation there exports to the
    rig profile (Phase B loader). Mirrors ``run_init``'s warn-and-continue contract: observaloop
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
            typer.echo("  ⚠ observaloop: no profile name for rig — skipping overlay", err=True)
            return
        observaloop.ensure_profile(name, cfg)  # idempotent server-side; best-effort
        observaloop.up(name, cfg)  # idempotent; the rig's worktrees share the one profile
        endpoint = observaloop.endpoint_for(name, config.otel_protocol(cfg), cfg)
        if not endpoint:
            typer.echo(
                "  ⚠ observaloop: no endpoint resolved (unavailable / down) — skipping overlay",
                err=True,
            )
            return
        observaloop_env.write_worktree_env(target, name, endpoint)
        typer.echo(f"  → observaloop profile '{name}' ready; wrote .ws/otel.env → {endpoint}")
    except Exception as exc:  # best-effort: never block worktree creation (mirror run_init)
        typer.echo(f"  ⚠ observaloop: provisioning failed ({exc}) — continuing", err=True)


# ---- operations -------------------------------------------------------------


def _branch_exists(main: Path, branch: str) -> bool:
    """True iff `branch` is a local head in the rig's main clone."""
    return (
        _run_git(
            ["git", "-C", str(main), "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            check=False,
        ).returncode
        == 0
    )


def molecule_base(entry, bead: str, integration: str) -> str:
    """Resolve the integration target for a bead's merges (two-level AGF integration).
    bd sub-ids are `<epic>.<n>` — split on the LAST '.', so the epic is the molecule. If an
    epic is derivable AND its `mol/<epic>` branch exists in the rig's main clone, that molecule
    was kicked off, so return `mol/<epic>`; otherwise fall back to `integration` (a bead with no
    '.' has no molecule, and an un-kicked-off molecule still targets the rig integration branch).
    Pure git + string (no bd call) — the branch's existence is the signal, keeping work.py's
    bd-only seam intact."""
    epic, sep, _ = bead.rpartition(".")
    if not sep or not epic:
        return integration
    branch = f"{MOL_PREFIX}{epic}"
    main = registry.rig_dir(entry)
    return branch if _branch_exists(main, branch) else integration


def _record_wt_event(op: str, outcome: str = "ok", *, rig: str = "", leaf: str = "") -> None:
    """Best-effort, gated emission of the ``ws.worktree.events`` metric at a create/remove/prune
    seam. Gated on ``otel.is_active()`` so the off-path is zero-cost + opentelemetry-import-free,
    and wrapped so a telemetry failure NEVER blocks the underlying worktree op. Ephemeral
    ``verify-`` clean-checkout worktrees aren't a seat, so they emit nothing; ``ws.rig`` /
    ``ws.worktree`` are tagged when known."""
    if not otel.is_active() or (leaf and leaf.startswith(VERIFY_LEAF_PREFIX)):
        return
    try:
        attrs: dict[str, str] = {}
        if rig:
            attrs["ws.rig"] = str(rig)
        if leaf:
            attrs["ws.worktree"] = leaf
        otel.record_worktree_event(op, outcome, attrs)
    except Exception:  # best-effort: telemetry must never block a worktree op
        pass


def _record_wt_op_duration(
    op: str, seconds: float, outcome: str = "ok", *, rig: str = "", leaf: str = ""
) -> None:
    """Best-effort, gated emission of the ``ws.worktree.op.duration`` histogram for a worktree git
    op (the wall time of the ``git worktree add|remove`` subprocess). Mirrors ``_record_wt_event``'s
    contract exactly: gated on ``otel.is_active()`` (off-path zero-cost, opentelemetry-import-free),
    ephemeral ``verify-`` clean-checkout worktrees excluded (not a seat), and wrapped so a telemetry
    failure NEVER blocks the op. ``ws.rig`` / ``ws.worktree`` are tagged when known."""
    if not otel.is_active() or (leaf and leaf.startswith(VERIFY_LEAF_PREFIX)):
        return
    try:
        attrs: dict[str, str] = {"ws.worktree.op": op, "ws.worktree.outcome": outcome}
        if rig:
            attrs["ws.rig"] = str(rig)
        if leaf:
            attrs["ws.worktree"] = leaf
        otel.record_worktree_op_duration(seconds, attrs)
    except Exception:  # best-effort: telemetry must never block a worktree op
        pass


def _do_add(
    cfg, entry, main: Path, br: str, target: Path, *, new_branch: bool, start_point: str = ""
):
    """Create the linked worktree (new `-b` branch, or attach an existing one) + run init.
    Attaching an existing branch prunes stale admin entries first, so a worktree whose dir
    was deleted out-of-band (not via `worktree remove`) doesn't block re-attach.
    `start_point` is only honoured for new-branch creation — it sets the commit the branch
    forks from (e.g. `mol/<epic>` so the bead sees intra-molecule merged work)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if new_branch:
        cmd = ["git", "-C", str(main), "worktree", "add", "-b", br, str(target)]
        if start_point:
            cmd.append(start_point)
    else:
        _run_git(["git", "-C", str(main), "worktree", "prune"], check=False)
        cmd = ["git", "-C", str(main), "worktree", "add", str(target), br]
    # Time + tag the create. The error path used to raise BEFORE any emission (always-"ok" gap), so
    # a failed create recorded nothing — now both the events counter AND the op.duration histogram
    # fire with outcome=error before the re-raise. Best-effort + gated (verify- trees never reach
    # this chokepoint; clean_checkout bypasses _do_add entirely).
    rig = str(entry.get("prefix", ""))
    started = time.monotonic()
    res = _run_git(cmd, check=False)
    elapsed = time.monotonic() - started
    if res.returncode != 0:
        _record_wt_event("create", "error", rig=rig, leaf=target.name)
        _record_wt_op_duration("create", elapsed, "error", rig=rig, leaf=target.name)
        raise typer.Exit(res.returncode)
    _record_wt_op_duration("create", elapsed, "ok", rig=rig, leaf=target.name)
    run_init(cfg, entry, target)
    provision_observaloop(cfg, entry, target)
    _record_wt_event("create", rig=rig, leaf=target.name)


def add(rig="", bead="", branch="", dry_run=False):
    if bead and branch:
        typer.echo("✗ pass at most one of --bead / --branch", err=True)
        raise typer.Exit(1)
    cfg = config.load()
    entry = _resolve_entry(cfg, rig)
    main = registry.rig_dir(entry)
    if not (main / ".git").exists():
        typer.echo(f"✗ no clone for rig at {main} — clone it first", err=True)
        raise typer.Exit(1)

    br, leaf = _branch_and_leaf(cfg, bead=bead, branch=branch)
    target = wt_dir(entry, leaf)
    typer.echo(f"rig {entry['provider']}/{entry['org']}/{entry['repo']}  branch {br}")
    typer.echo(f"  → {target}")
    if dry_run:
        typer.echo("(dry-run — nothing changed)")
        return
    if target.exists():
        typer.echo(f"✗ worktree path already exists: {target}", err=True)
        raise typer.Exit(1)
    _do_add(cfg, entry, main, br, target, new_branch=True)
    typer.echo(f"✓ worktree ready: {target}")


# ---- ws work helpers (idempotent provision/re-attach + submit-time git) ------


def locate(cfg, rig, bead="", branch=""):
    """Resolve (entry, main, target, branch) for a managed worktree — no side effects. Keys on a
    single `bead` (`wt/bead/<id>`) or a raw `branch` suffix (`wt/<name>`, e.g. a batch worktree)."""
    entry = _resolve_entry(cfg, rig)
    main = registry.rig_dir(entry)
    br, leaf = _branch_and_leaf(cfg, bead=bead, branch=branch)
    return entry, main, wt_dir(entry, leaf), br


def in_bead_worktree(target: Path, cwd: Path | None = None) -> bool:
    """True iff `cwd` (default: Path.cwd()) resolves to or is inside the bead's managed
    worktree at `target`. Used by claim/check/submit to warn when the caller is operating
    from the main clone instead of the worktree — absolute paths under the rig root resolve
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
      - else a real rig checkout under $GIT_WORKSPACE → ``workspace_identity`` triplet, leaf ``''``
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


def ensure(cfg, rig, bead="", branch="", base_bead=""):
    """Idempotent provision/re-attach for `ws work`. Returns (entry, target, branch): reuse a live
    dir; else attach an existing branch into a fresh dir; else create the branch+dir forked off
    mol/<epic> when present (start-point threading). Keys on `bead` (single-bead `wt/bead/<id>`)
    or a raw `branch` suffix (a work-group's shared `wt/<name>` worktree); `base_bead` names the
    bead whose molecule sets the start point (defaults to `bead`). Init runs only on a new dir."""
    entry, main, target, br = locate(cfg, rig, bead=bead, branch=branch)
    if not (main / ".git").exists():
        typer.echo(f"✗ no clone for rig at {main} — clone it first", err=True)
        raise typer.Exit(1)
    if target.exists():
        return entry, target, br
    new_branch = not _branch_exists(main, br)
    start_point = ""
    if new_branch:
        integration = config.integration_branch(cfg, entry)
        start_point = molecule_base(entry, base_bead or bead, integration)
    _do_add(cfg, entry, main, br, target, new_branch=new_branch, start_point=start_point)
    return entry, target, br


def history(entry, branch, base):
    """(count, [subjects]) for commits on `branch` not reachable from `base`.
    (-1, []) when the range can't be computed (e.g. base missing)."""
    main = registry.rig_dir(entry)
    rng = f"{base}..{branch}"
    cres = _run_git(
        ["git", "-C", str(main), "rev-list", "--count", rng], check=False, capture=True
    )
    if cres.returncode != 0:
        return -1, []
    count = int((cres.stdout or "0").strip() or "0")
    lres = _run_git(
        ["git", "-C", str(main), "log", "--format=%s", rng], check=False, capture=True
    )
    subjects = [s for s in (lres.stdout or "").splitlines() if s.strip()]
    return count, subjects


def clean_checkout(entry, branch, cmd) -> int:
    """Validate `branch` from a throwaway detached worktree, so the result never depends on
    dirty local state. The validation command runs with a telemetry-neutral env
    (`otel.telemetry_neutral_env`) so its result is independent of the operator's otel config and
    never exports telemetry. Returns the validation command's exit code (or git's, if checkout
    fails)."""
    main = registry.rig_dir(entry)
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
        _run_git(
            ["git", "-C", str(main), "worktree", "remove", "--force", str(tmp)], check=False
        )


def push_branch(entry, branch, remote="origin") -> int:
    """Push `branch` to `remote` (same name both ends). Returns git's exit code."""
    main = registry.rig_dir(entry)
    return _run_git(
        ["git", "-C", str(main), "push", remote, f"{branch}:{branch}"], check=False
    ).returncode


def is_clean(target: Path) -> bool:
    """True iff the worktree at `target` has no staged/unstaged/untracked changes."""
    res = _run_git(
        ["git", "-C", str(target), "status", "--porcelain"], check=False, capture=True
    )
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
    main = registry.rig_dir(entry)
    res = _run_git(
        ["git", "-C", str(main), "merge-base", integration, branch], check=False, capture=True
    )
    return (res.stdout or "").strip() if res.returncode == 0 else ""


def commit_rows(entry, base, branch) -> list[dict]:
    """Oldest→newest commits in base..branch. Each row: {sha, short, parents, author, email,
    date (author date, iso-strict), subject, files, sig (G/U/B/N), signer}. [] on error."""
    main = registry.rig_dir(entry)
    res = _run_git(
        [
            "git", "-C", str(main), "log", f"{base}..{branch}",
            "--reverse", "--date=iso-strict", "--name-only", f"--format={_ROW_FMT}",
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
    main = registry.rig_dir(entry)
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


def same_tree(entry, a, b) -> bool:
    """True iff refs `a` and `b` have byte-identical trees — the refine safety gate."""
    main = registry.rig_dir(entry)
    return (
        _run_git(["git", "-C", str(main), "diff", "--quiet", a, b], check=False).returncode == 0
    )


def diff_range(entry, base, branch) -> int:
    """Stream `git diff base..branch` to stdout (the net change). Returns git's exit code."""
    main = registry.rig_dir(entry)
    return _run_git(
        ["git", "-C", str(main), "diff", f"{base}..{branch}"], check=False
    ).returncode


def log_range(entry, base, branch) -> str:
    """`git log --oneline base..branch` (oldest→newest) — the post-refine digest summary."""
    main = registry.rig_dir(entry)
    res = _run_git(
        ["git", "-C", str(main), "log", "--reverse", "--format=%h %ad %s", "--date=short",
         f"{base}..{branch}"],
        check=False,
        capture=True,
    )
    return (res.stdout or "") if res.returncode == 0 else ""


def managed(cfg):
    """[(prefix, path, branch)] for every linked worktree under the shadow root."""
    root = str(config.worktrees_root().resolve())
    out = []
    for e in cfg.get("managed_repos", []) or []:
        main = registry.rig_dir(e)
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


def list_cmd():
    cfg = config.load()
    rows = managed(cfg)
    if not rows:
        typer.echo("no managed worktrees")
        return
    for prefix, path, br in rows:
        typer.echo(f"{prefix}\t{br}\t{path}")


def path_of(rig, ref):
    cfg = config.load()
    entry = _resolve_entry(cfg, rig)
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


def remove(rig, ref, force=False):
    cfg = config.load()
    entry = _resolve_entry(cfg, rig)
    main = registry.rig_dir(entry)
    target = wt_dir(entry, _leaf(ref))
    cmd = ["git", "-C", str(main), "worktree", "remove", str(target)]
    if force:
        cmd.append("--force")
    rig = str(entry.get("prefix", ""))
    started = time.monotonic()
    res = _run_git(cmd, check=False)
    elapsed = time.monotonic() - started
    if res.returncode != 0:
        _record_wt_event("remove", "error", rig=rig, leaf=target.name)
        _record_wt_op_duration("remove", elapsed, "error", rig=rig, leaf=target.name)
        raise typer.Exit(res.returncode)
    _rmdir_empty_parents(target, cfg)
    _record_wt_op_duration("remove", elapsed, "ok", rig=rig, leaf=target.name)
    _record_wt_event("remove", rig=rig, leaf=target.name)
    typer.echo(f"✓ removed {target}")


def prune(rig=""):
    """Remove every managed worktree (optionally just one rig's) + prune stale admin files.

    Mode 1 (shared per-rig observaloop profile): this function deliberately does NOT tear down
    the rig's observaloop profile.  The profile is shared across all of the rig's worktrees and
    must remain up until the rig itself is retired — use ``ws observaloop down`` for that.
    Do NOT add per-worktree or per-prune observaloop teardown here; doing so would break the
    shared-profile contract and stop telemetry routing for any remaining worktrees or processes.
    """
    cfg = config.load()
    want = str(registry.resolve_rig(cfg, rig)["prefix"]) if rig else None
    rows = [r for r in managed(cfg) if want is None or r[0] == want]
    mains = {}
    for e in cfg.get("managed_repos", []) or []:
        mains[str(e["prefix"])] = registry.rig_dir(e)
    for prefix, path, _ in rows:
        started = time.monotonic()
        res = _run_git(
            ["git", "-C", str(mains[prefix]), "worktree", "remove", "--force", path],
            check=False,
        )
        elapsed = time.monotonic() - started
        outcome = "ok" if res.returncode == 0 else "error"  # close the always-ok gap on prune too
        typer.echo(f"  removed {path}")
        _record_wt_event("prune", outcome, rig=prefix, leaf=Path(path).name)
        _record_wt_op_duration("prune", elapsed, outcome, rig=prefix, leaf=Path(path).name)
    for main in {str(mains[r[0]]) for r in rows}:
        _run_git(["git", "-C", main, "worktree", "prune"], check=False)
    for _, path, _ in rows:
        _rmdir_empty_parents(path, cfg)
    typer.echo(f"✓ pruned {len(rows)} managed worktree(s)")
