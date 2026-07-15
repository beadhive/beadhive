"""orca.py — the orca repo-registry integration (the first bh plugin).

orca keeps a registry of repos in ``~/.config/orca/orca-data.json``. This module mirrors
``gitworkspace.py`` (on-disk / JSON reads) and ``run.py`` (best-effort subprocess) to register
git-workspace clones with orca.

**Scope invariant:** bh only ever touches orca's ``repos`` list — never its ``projects`` /
``projectHostSetups`` collections, and never any orchestration database. Reads go through the
``orca repo list`` surface (or the JSON file's repos list); writes go through ``orca repo add``.

**Best-effort:** every function degrades to a warning + a falsy/empty return on failure (missing
orca CLI, unreadable data file, failing subprocess) and NEVER raises, so orca can never abort
onboarding / retire / rig-ready. ``import beadhive.orca`` is always safe.

**Deliberate exception — worktree delegation:** :func:`create_worktree` (the ``wt_create`` hook)
and :func:`remove_worktree` (the ``wt_remove`` hook) HARD FAIL via ``typer.Exit`` by default when
delegated worktree create/remove goes wrong, so a silently-broken orca delegation can't
masquerade as success. This is scoped ONLY to the delegation hooks; setting
``config.orca_worktrees_fallback`` restores the best-effort contract (warn + return
``None``/``False``, native git takes over).

**Second deliberate exception — worktree-base-path wiring:** :func:`_ensure_worktree_base_path`
(the onboard/sync companion to the delegation hooks, driven when
``config.orca_worktrees_enabled`` is set) drives ``orca project setups`` / ``setup-update`` —
the only place this module reaches past the ``repos`` scope. It stays best-effort (warn, never
raise) since it runs during onboard/sync, not the hard-failing hooks, and it never reads/writes
orca-data.json's ``projects``/``projectHostSetups`` directly (CLI-only).
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import typer

from . import config, plugins, run
from .identity import workspace_root


def _has_cli() -> bool:
    return shutil.which("orca") is not None


def is_available(cfg=None) -> bool:
    """orca is usable when its CLI is on PATH OR its data file exists on disk."""
    return _has_cli() or config.orca_data_path(cfg).exists()


def _load(cfg=None) -> dict | None:
    """Read + parse orca-data.json; returns None on any read/parse failure (never raises)."""
    try:
        return json.loads(config.orca_data_path(cfg).read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _repos_from(data) -> list[dict]:
    """Extract ONLY the repos list from a parsed orca payload: the CLI's ``{id, ok,
    result: {repos}}`` envelope, a top-level dict-with-repos, or a bare list.

    Never reads ``projects`` / ``projectHostSetups`` — those are out of bh's scope."""
    if isinstance(data, dict):
        result = data.get("result")
        repos = result.get("repos", []) if isinstance(result, dict) else data.get("repos", [])
    elif isinstance(data, list):
        repos = data
    else:
        repos = []
    return [r for r in repos if isinstance(r, dict)]


def list_repos(cfg=None) -> list[dict]:
    """The repos orca knows about — via ``orca repo list --json`` when the CLI is present, else
    from orca-data.json's repos list. Returns [] on any failure (never raises)."""
    if _has_cli():
        try:
            return _repos_from(json.loads(run.out(["orca", "repo", "list", "--json"])))
        except Exception:  # noqa: BLE001 - best-effort: fall through to the file read
            pass
    data = _load(cfg)
    return _repos_from(data) if data is not None else []


def _repo_paths(cfg=None) -> set[str]:
    return {str(r.get("path")) for r in list_repos(cfg) if r.get("path")}


def add_repo(path, cfg=None) -> bool:
    """Register ``path`` with orca (idempotent). Returns True ONLY when it actually registered a
    new repo; False when the path is already known or the add could not be performed.

    Best-effort: a missing CLI or a failing ``orca repo add`` warns and returns False, never
    raises (mirrors rig._do_observaloop's fence)."""
    path = str(path)
    if path in _repo_paths(cfg):
        return False
    if not _has_cli():
        typer.echo(f"• orca: cannot register {path} — orca CLI not on PATH", err=True)
        return False
    try:
        run.out(["orca", "repo", "add", "--path", path, "--json"])
        return True
    except Exception as exc:  # noqa: BLE001 - best-effort fence
        typer.echo(f"• orca: failed to register {path} ({exc})", err=True)
        return False


def discover_repos(cfg=None) -> list[Path]:
    """Real on-disk clones exactly three levels under $GIT_WORKSPACE (<group>/org/repo — the
    group's `path` segment, not necessarily the provider TYPE; see gitworkspace.RepoGroup) that
    contain a ``.git`` entry. Walks the filesystem — does NOT read workspace-lock.toml (many
    enumerated repos never actually clone).

    DECISION (bh-4y0r.2): this stays a fixed three-level walk — it is NOT generalized to the
    deeper multi-owner nesting `gitworkspace`'s lockfile readers already tolerate (they key off
    `parts[0]`/`parts[-1]`, dropping any middle segments). A clone nested deeper than three
    levels is simply not discovered here; `doctor` warns separately when workspace-lock.toml
    records such a path (see `doctor._data_warnings`), rather than this walk special-casing it."""
    root = Path(workspace_root())
    found: list[Path] = []
    if not root.is_dir():
        return found
    for group in sorted(root.iterdir()):
        if not group.is_dir():
            continue
        for org in sorted(group.iterdir()):
            if not org.is_dir():
                continue
            for repo in sorted(org.iterdir()):
                if repo.is_dir() and (repo / ".git").exists():
                    found.append(repo)
    return found


@dataclass
class OrcaSyncResult:
    """Outcome of ``sync_repos`` — mirrors the printed summary so callers/tests assert on it."""

    checked: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    unavailable: bool = False


def _sync_worktree_wiring(cfg, clone: Path) -> None:
    """sync's per-repo companion to :func:`_on_onboard`'s wiring: only fires for clones that
    are actual bh-managed rigs (``registry.find_entry``) — worktree delegation is a
    bh-rig-scoped feature, not something to apply to every orca-registered clone."""
    from . import registry  # lazy: avoid a module-load cycle

    resolved = cfg if cfg is not None else config.load()
    root = Path(workspace_root())
    try:
        provider, org, repo = clone.relative_to(root).parts[:3]
    except ValueError:
        return
    entry = registry.find_entry(resolved, provider, org, repo)
    if entry is not None:
        _ensure_worktree_base_path(resolved, entry, clone)


def sync_repos(cfg=None, dry_run: bool = False) -> OrcaSyncResult:
    """Register every discovered git-workspace clone with orca (idempotent).

    Returns ``unavailable=True`` (doing nothing else) when orca can't be used. Otherwise each
    discovered repo already known to orca is skipped; the rest are added (or would-be-added
    under ``dry_run``). Idempotent: a second run adds nothing.

    Also (when not ``dry_run``) best-effort wires worktree-delegation's ``worktree-base-path``
    for every discovered clone that is an actual bh-managed rig — see
    :func:`_sync_worktree_wiring`."""
    result = OrcaSyncResult()
    if not is_available(cfg):
        result.unavailable = True
        return result
    known = _repo_paths(cfg)
    for repo in discover_repos(cfg):
        p = str(repo)
        result.checked.append(p)
        if p in known:
            result.skipped.append(p)
        elif dry_run:
            result.added.append(p)  # would register
        elif add_repo(p, cfg):
            result.added.append(p)
        if not dry_run:
            _sync_worktree_wiring(cfg, repo)
    return result


def warn_retire(path, cfg=None) -> None:
    """Print a de-registration reminder on retire — WARN-only, never mutates orca-data.json.

    ``orca project setup-delete --setup <id>`` DOES de-register a repo (confirmed live in the
    zzxt.1 spike — this module's earlier "orca has no de-registration verb" claim predates the
    spike and is now wrong). Retire still only warns rather than calling it: resolving <id> for
    an arbitrary retiring `path` needs its own lookup, and auto-deleting a project-setup on
    retire risks dropping orca state the operator wanted to keep — safer to name the real
    command and let the operator run it deliberately."""
    typer.echo(
        f"• orca: {path} may still be registered with orca — de-register it manually with "
        "`orca project setup-delete --setup <id>` (find <id> via `orca project setups "
        "--json`) if you no longer want it tracked.",
        err=True,
    )


def _on_onboard(ctx) -> None:
    """on_onboard hook: register the freshly onboarded rig's clone with orca, then —
    best-effort, never raising — wire worktree-delegation's ``worktree-base-path`` + the
    auto-rename operator nudge when ``config.orca_worktrees_enabled`` is set for this rig
    (see :func:`_ensure_worktree_base_path`)."""
    add_repo(str(ctx.base), ctx.cfg)
    entry = {"provider": ctx.provider, "org": ctx.org, "repo": ctx.repo}
    _ensure_worktree_base_path(ctx.cfg, entry, ctx.base)


def _entry_triplet(entry):
    """(provider, org, repo) from a managed_repos entry, or None when it lacks the triplet."""
    if not entry:
        return None
    provider, org, repo = entry.get("provider"), entry.get("org"), entry.get("repo")
    if provider and org and repo:
        return str(provider), str(org), str(repo)
    return None


def _runtime_ready(cfg=None) -> bool:
    """Best-effort probe of the orca runtime via ``orca status --json``.

    Healthy iff ``.result.runtime.reachable`` is true AND ``.result.runtime.state`` is
    ``"ready"``. Any non-zero exit, unparsable JSON, or missing CLI is treated as down —
    never raises (mirrors the module's best-effort invariant)."""
    try:
        proc = run.run(["orca", "status", "--json"], check=False, capture=True)
        if proc.returncode != 0:
            return False
        data = json.loads(proc.stdout)
    except Exception:  # noqa: BLE001 - best-effort: any failure means down
        return False
    runtime = ((data or {}).get("result") or {}).get("runtime") or {}
    return bool(runtime.get("reachable")) and runtime.get("state") == "ready"


def _auto_rename_enabled(cfg=None) -> bool:
    """Best-effort parse of orca-data.json's ``settings.autoRenameBranchFromWork``.

    PARSE-AND-WARN ONLY — this never writes the file (the live app owns it). Any read/parse
    failure or missing setting defaults to False, never raises."""
    data = _load(cfg)
    if not isinstance(data, dict):
        return False
    settings = data.get("settings")
    if not isinstance(settings, dict):
        return False
    return bool(settings.get("autoRenameBranchFromWork", False))


def _find_setup_id(clone: Path) -> str | None:
    """Best-effort ``orca project setups --json`` lookup of the project-setup id for ``clone``
    (matched on its ``path`` field) — None on any failure or miss, never raises."""
    data = _run_envelope(["orca", "project", "setups", "--json"])
    if data is None:
        return None
    setups = (data.get("result") or {}).get("setups")
    if not isinstance(setups, list):
        return None
    target = str(clone)
    for setup in setups:
        if isinstance(setup, dict) and str(setup.get("path", "")) == target:
            setup_id = setup.get("id")
            return str(setup_id) if setup_id else None
    return None


def _ensure_worktree_base_path(cfg, entry, clone: Path) -> None:
    """Onboard/sync companion to the ``wt_create``/``wt_remove`` hooks: when worktree
    delegation is enabled for this rig (``config.orca_worktrees_enabled``), make sure orca's
    project-setup for ``clone`` has ``worktree-base-path`` pointed at bh's rig-level shadow dir
    (``config.worktrees_root()/<provider>/<org>`` — orca appends ``<repo-displayName>/<leaf>``
    under its default ``nestWorkspaces: true``, landing delegated trees exactly at
    :func:`worktree.wt_dir`), and nudges the operator when the global auto-rename setting is on.

    Best-effort ONLY (warn + return, never raise) — this is onboarding/sync bookkeeping, not
    the hard-failing delegation hooks themselves."""
    if not config.orca_worktrees_enabled(cfg, entry):
        return
    if _auto_rename_enabled(cfg):
        typer.echo(
            "• orca: settings.autoRenameBranchFromWork is ON — disable 'Auto-Rename Branch "
            "From Work' in Orca's Settings UI before relying on worktree delegation (or run "
            "`bh plugin orca fix-settings` while orca is stopped)",
            err=True,
        )
    if not _has_cli():
        return
    setup_id = _find_setup_id(clone)
    if setup_id is None:
        typer.echo(
            f"• orca: no project-setup found for {clone} — skipping worktree-base-path wiring",
            err=True,
        )
        return
    base_path = config.worktrees_root(cfg) / str(entry["provider"]) / str(entry["org"])
    cmd = [
        "orca", "project", "setup-update", "--setup", setup_id,
        "--worktree-base-path", str(base_path), "--json",
    ]
    if _run_envelope(cmd) is None:
        typer.echo(f"• orca: failed to set worktree-base-path for {clone}", err=True)


_SETTINGS_UI_INSTRUCTION = (
    "✗ orca: runtime is up — disable 'Auto-Rename Branch From Work' by hand in Orca's "
    "Settings UI (fix-settings only writes orca-data.json in the safe window while orca is "
    "stopped)"
)


def fix_settings(cfg=None) -> bool:
    """``bh plugin orca fix-settings``: flip ``settings.autoRenameBranchFromWork`` to False in
    orca-data.json — ONLY while the orca runtime is confirmed down (:func:`_runtime_ready` is
    False), a safe write window where the live app isn't holding the file open. Refuses with
    the Settings-UI instruction (``typer.Exit(1)``) when the runtime is up.

    Reads the whole file, flips the one key, and writes the whole thing back atomically
    (temp file + ``os.replace``) so every other ``settings``/``repos``/``projects`` key is
    preserved untouched. Raises ``typer.Exit(1)`` on refusal or an unreadable/malformed file —
    this is the one deliberately-mutating exception to the module's read-only orca-data.json
    contract, gated on the runtime-down safe window."""
    if _runtime_ready(cfg):
        typer.echo(_SETTINGS_UI_INSTRUCTION, err=True)
        raise typer.Exit(1)

    path = config.orca_data_path(cfg)
    data = _load(cfg)
    if not isinstance(data, dict):
        typer.echo(f"✗ orca: could not read {path} — nothing to fix", err=True)
        raise typer.Exit(1)

    settings = dict(data.get("settings") or {})
    settings["autoRenameBranchFromWork"] = False
    data = {**data, "settings": settings}

    tmp = path.parent / f".{path.name}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)
    typer.echo(f"✓ orca: settings.autoRenameBranchFromWork set to false in {path}")
    return True


def _worktrees_readiness(cfg) -> tuple[str, str]:
    """Extra readiness detail once worktree delegation is enabled: probe the runtime and check
    the auto-rename setting. Returns a warn state describing every problem found, or an ok
    state when both checks pass."""
    problems: list[str] = []
    if not _runtime_ready(cfg):
        problems.append(
            "orca runtime down — worktree delegation will fall back to native git"
            if config.orca_worktrees_fallback(cfg)
            else "orca runtime down — delegated worktree ops will fail"
        )
    if _auto_rename_enabled(cfg):
        problems.append(
            "autoRenameBranchFromWork is on — disable it in orca settings before delegating "
            "worktrees"
        )
    if problems:
        return ("warn", "; ".join(problems))
    return ("ok", "registered; worktree delegation ready")


def _readiness(cfg, entry) -> tuple[str, str] | None:
    """rig-ready hook: is this rig's clone registered with orca? None when entry lacks triplet.

    When worktree delegation is enabled (``config.orca_worktrees_enabled``), additionally
    probes the orca runtime and the auto-rename setting via :func:`_worktrees_readiness`."""
    triplet = _entry_triplet(entry)
    if triplet is None:
        return None
    provider, org, repo = triplet
    clone = Path(workspace_root()) / provider / org / repo
    if str(clone) not in _repo_paths(cfg):
        return ("missing", "not registered — bh plugin orca sync")
    if not config.orca_worktrees_enabled(cfg, entry):
        return ("ok", "registered")
    return _worktrees_readiness(cfg)


def _wt_remove_fail(cfg, message: str) -> bool:
    """Shared failure policy for :func:`remove_worktree`: warn + return False (native removal
    proceeds) when ``config.orca_worktrees_fallback`` is on, else hard-fail via ``typer.Exit(1)``.
    """
    if config.orca_worktrees_fallback(cfg):
        typer.echo(
            f"⚠ orca: {message} — falling back to native removal (orca's registry may go "
            "stale; a later `orca worktree rm` attempt clears it)",
            err=True,
        )
        return False
    typer.echo(
        f"✗ orca: {message} — set orca.worktrees.fallback to fall back to native removal "
        "instead of failing hard",
        err=True,
    )
    raise typer.Exit(1)


def remove_worktree(cfg, entry, *, main, target, force, keep_branch) -> bool:
    """``wt_remove`` hook: delegate a worktree *remove* to ``orca worktree rm``.

    Gated on :func:`config.orca_worktrees_enabled` — returns False immediately (inert) when the
    flag is off, so the native ``git worktree remove`` path runs unchanged.

    orca's ``rm`` DELETES the tree's checked-out branch outright — even without ``--force``,
    even with unmerged commits (verified in the zzxt.1 spike). When ``keep_branch`` is True (the
    branch is the durable artifact, e.g. plain ``worktree.remove()``), this detaches HEAD in the
    target tree FIRST so the branch survives the rm; when False (``prune()``'s SAFE removal —
    the branch is already merged and disposable), the detach is skipped so orca deletes it,
    matching native prune's branch-cleanup parity. A failed detach is itself a removal failure
    — never hand orca a tree whose branch would be lost.

    Runs ``orca worktree rm --worktree path:<target> --json`` (``--force`` unless the caller
    passed ``force=False`` — bh's own callers already gate removal safety, so this only mirrors
    that intent). Returns True ONLY on confirmed success: exit 0 AND a parsed
    ``{ok: true, result: {removed: true}}`` envelope; anything else is failure.

    **Deliberate exception to the module's never-raise invariant:** on failure, with
    ``config.orca_worktrees_fallback`` on, this warns (orca's registry may go stale for a
    natively-removed managed tree — a later ``orca worktree rm`` attempt clears it) and returns
    False so native removal proceeds; otherwise it raises ``typer.Exit(1)`` — HARD FAIL is the
    default failure policy for delegated worktree removal (same pattern as the ``wt_create``
    sibling hook)."""
    if not config.orca_worktrees_enabled(cfg, entry):
        return False

    if keep_branch:
        detach = run.run(["git", "-C", str(target), "checkout", "--detach"], check=False)
        if detach.returncode != 0:
            return _wt_remove_fail(cfg, f"failed to detach HEAD in {target} before orca rm")

    cmd = ["orca", "worktree", "rm", "--worktree", f"path:{target}"]
    if force:
        cmd.append("--force")
    cmd.append("--json")
    try:
        proc = run.run(cmd, check=False, capture=True)
        data = json.loads(proc.stdout) if proc.returncode == 0 else None
    except Exception:  # noqa: BLE001 - any failure (missing CLI, bad JSON) is just "not removed"
        proc, data = None, None

    removed = (
        proc is not None
        and proc.returncode == 0
        and isinstance(data, dict)
        and data.get("ok") is True
        and bool((data.get("result") or {}).get("removed"))
    )
    if removed:
        return True
    return _wt_remove_fail(cfg, f"orca worktree rm failed for {target}")


def _branch_exists(clone: Path, name: str) -> bool:
    """True iff a local branch `name` exists in `clone` — post-create, whether `branch` itself
    already exists so the fixup below doesn't blindly ``checkout -b`` over one."""
    try:
        cmd = ["git", "-C", str(clone), "rev-parse", "--verify", "--quiet", f"refs/heads/{name}"]
        return run.ok(cmd)
    except Exception:  # noqa: BLE001 - defensive: a broken git probe reads as "doesn't exist"
        return False


def _existing_branches(clone: Path) -> set[str]:
    """Snapshot of local branch names in `clone`, taken BEFORE a delegated create — the create
    can hand back a branch name unknown until its response is parsed (orca's global
    branchPrefix means it need not equal the requested ``--name`` leaf), so whether that
    returned branch pre-existed can only be answered against a pre-create snapshot, not a
    single after-the-fact rev-parse."""
    try:
        cmd = ["git", "-C", str(clone), "for-each-ref", "--format=%(refname:short)", "refs/heads/"]
        proc = run.run(cmd, check=False, capture=True)
    except Exception:  # noqa: BLE001 - defensive: a broken git probe reads as "nothing exists"
        return set()
    if proc.returncode != 0:
        return set()
    return {line.strip() for line in proc.stdout.splitlines() if line.strip()}


def _current_branch(path: Path) -> str | None:
    """The tree's live branch name, or None on any failure (detached HEAD, git missing, …)."""
    try:
        cmd = ["git", "-C", str(path), "branch", "--show-current"]
        proc = run.run(cmd, check=False, capture=True)
    except Exception:  # noqa: BLE001
        return None
    return proc.stdout.strip() or None if proc.returncode == 0 else None


def _run_envelope(cmd) -> dict | None:
    """Run an orca CLI command and parse its ``{ok, result|error}`` JSON envelope. Returns the
    parsed dict only on exit 0 + valid JSON + ``ok: true``; None on ANY other outcome (nonzero
    exit, unparseable output, ``ok: false``, missing CLI/runtime down) — never raises."""
    try:
        proc = run.run(cmd, check=False, capture=True)
        if proc.returncode != 0:
            return None
        data = json.loads(proc.stdout)
    except Exception:  # noqa: BLE001 - runtime down / bad JSON is a create failure, not a crash
        return None
    return data if isinstance(data, dict) and data.get("ok") else None


def _cleanup_stray_worktree(path) -> None:
    """Best-effort ``orca worktree rm`` of a just-created tree we're about to reject (path
    mismatch or fixup failure) — failures here are swallowed; we're already on the failure path
    and a cleanup error must never mask the original one."""
    try:
        run.run(
            ["orca", "worktree", "rm", "--worktree", f"path:{path}", "--force", "--json"],
            check=False,
            capture=True,
        )
    except Exception:  # noqa: BLE001
        pass


def _fixup_branch(
    target: Path, actual: str, branch: str, actual_pre_existed: bool, start_point: str
) -> bool:
    """Get the freshly created tree onto ``branch`` without disturbing a pre-existing
    ``<actual>`` branch orca attached (the spike's "existing branch name" finding) — ``actual``
    is the branch the create response reports, NOT necessarily the requested leaf (orca's
    global branchPrefix can prefix it). Returns True iff the tree ends up on ``branch``
    exactly."""
    try:
        if not actual_pre_existed:
            run.run(["git", "-C", str(target), "branch", "-m", actual, branch], capture=True)
        elif _branch_exists(target, branch):
            run.run(["git", "-C", str(target), "checkout", branch], capture=True)
        else:
            cmd = ["git", "-C", str(target), "checkout", "-b", branch]
            if start_point:
                cmd.append(start_point)
            run.run(cmd, capture=True)
    except Exception:  # noqa: BLE001 - any git failure here is a fixup failure, not a crash
        return False
    return _current_branch(target) == branch


def _fail(cfg, reason: str) -> None:
    """Apply the ``wt_create`` failure policy: warn + return None (native fallback takes over)
    when ``config.orca_worktrees_fallback`` is on; otherwise HARD FAIL via ``typer.Exit`` — the
    documented, deliberate exception to this module's never-raise invariant (see the module
    docstring), scoped to the worktree-delegation hooks. Mirrors ``_consult_wt_create``'s
    propagation contract: the ``typer.Exit`` raised here is meant to propagate all the way up."""
    if config.orca_worktrees_fallback(cfg):
        typer.echo(f"⚠ orca: {reason} — falling back to native worktree create", err=True)
        return None
    typer.echo(
        f"✗ orca: {reason} (set orca.worktrees.fallback to fall back to native git instead)",
        err=True,
    )
    raise typer.Exit(1)


def create_worktree(
    cfg, entry, *, main: Path, branch: str, target: Path, start_point: str
) -> Path | None:
    """``wt_create`` hook: delegate a NEW worktree's create subprocess to
    orca worktree create --json (per the spike) while bh's `wt/` branch
    convention stays authoritative — orca only ever sees the sanitized leaf (``target.name``),
    never the full slashed branch name (orca sanitizes slashes anyway).

    Inert (returns None immediately, no subprocess) unless ``config.orca_worktrees_enabled``.
    On success, requires the returned worktree path to equal ``target`` EXACTLY (orca inserts a
    repo-name path segment by default; a mismatch means the repo's worktree-base-path is
    misconfigured) and that the post-create branch fixup lands the tree on ``branch`` exactly —
    anything else is a failure, resolved through :func:`_fail` (HARD FAIL by default; warn +
    None when ``orca.worktrees.fallback`` is on).

    orca's global ``branchPrefix`` setting can rename the created branch out from under the
    requested leaf (e.g. ``<leaf>`` comes back as ``<username>/<leaf>``), so the fixup keys off
    the branch the create response ACTUALLY reports (``result.worktree.branch``), not the leaf
    — see the live-e2e finding."""
    if not config.orca_worktrees_enabled(cfg, entry):
        return None

    leaf = target.name
    existing_before = _existing_branches(main)

    cmd = ["orca", "worktree", "create", "--repo", f"path:{main}", "--name", leaf]
    if start_point:
        cmd += ["--base-branch", start_point]
    cmd += ["--setup", "skip", "--no-parent", "--json"]

    data = _run_envelope(cmd)
    if data is None:
        return _fail(cfg, "orca worktree create failed (runtime down, error result, or bad JSON)")

    worktree_result = ((data.get("result") or {}).get("worktree")) or {}
    result_path = worktree_result.get("path")
    if result_path != str(target):
        _cleanup_stray_worktree(result_path or target)
        reason = (
            f"orca created {result_path!r}, expected {str(target)!r} — check worktree-base-path"
        )
        return _fail(cfg, reason)

    actual = str(worktree_result.get("branch") or "").removeprefix("refs/heads/") or leaf

    if not _fixup_branch(target, actual, branch, actual in existing_before, start_point):
        _cleanup_stray_worktree(target)
        return _fail(cfg, f"post-create branch fixup failed — tree is not on {branch!r}")

    return target


cli = typer.Typer(no_args_is_help=True, help="orca repo-registry integration (register clones).")


@cli.command("sync", help="register every git-workspace clone with orca (idempotent).")
def _sync_cmd(
    dry_run: bool = typer.Option(False, "--dry-run", help="print the plan and change nothing"),
) -> None:
    result = sync_repos(config.load(), dry_run=dry_run)
    if result.unavailable:
        typer.echo("• orca unavailable — install the orca CLI or create orca-data.json.")
        return
    verb = "would register" if dry_run else "registered"
    for p in result.added:
        typer.echo(f"  ✓ {verb} {p}")
    for p in result.skipped:
        typer.echo(f"  • already registered {p}")
    typer.echo(
        f"orca sync: {len(result.added)} {verb}, {len(result.skipped)} already known "
        f"({len(result.checked)} checked)"
    )


@cli.command(
    "fix-settings",
    help="flip settings.autoRenameBranchFromWork off in orca-data.json (only while orca is down).",
)
def _fix_settings_cmd() -> None:
    fix_settings(config.load())


PLUGIN = plugins.Plugin(
    name="orca",
    cli=cli,
    enabled=lambda cfg, entry: config.orca_enabled(cfg, entry),
    on_onboard=_on_onboard,
    on_retire=lambda clone_path, cfg, entry: warn_retire(clone_path, cfg),
    readiness=_readiness,
    wt_create=create_worktree,
    wt_remove=remove_worktree,
)
