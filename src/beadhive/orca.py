"""orca.py — the orca repo-registry integration (the first bh plugin).

orca keeps a registry of repos in ``~/.config/orca/orca-data.json``. This module mirrors
``gitworkspace.py`` (on-disk / JSON reads) and ``run.py`` (best-effort subprocess) to register
git-workspace clones with orca.

**Scope invariant:** bh only ever touches orca's ``repos`` list — never its ``projects`` /
``projectHostSetups`` collections, and never any orchestration database. Reads go through the
``orca repo list`` surface (or the JSON file's repos list); writes go through ``orca repo add``.

**Best-effort:** every function degrades to a warning + a falsy/empty return on failure (missing
orca CLI, unreadable data file, failing subprocess) and NEVER raises, so orca can never abort
onboarding / retire / hive-ready. ``import beadhive.orca`` is always safe.

**Worktree delegation — retired (bh-055ot.1).** Orca used to take over worktree create/remove
through the ``wt_create``/``wt_remove`` hooks when ``orca.worktrees`` was set. That made Orca a
second owner of worktree mechanics, which the worktree-manager ADR
(``docs/design/bh-mr9tk.2-worktree-manager-herdr-binding-adr.md``) forbids: exactly one configured
``worktrees.manager`` (native git) creates, attaches, and removes worktrees. The hooks, their
``worktree-base-path`` onboarding companion, and the hard-fail/fallback policy are gone; setting
``orca.worktrees`` now only produces a config-load warning
(``config.warn_retired_orca_worktrees_if_needed``) and a ``warn`` readiness row, never a change
in mechanics. Orca stays a repo registry.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import typer

from . import plugins, run
from .config_consumer_ports import plugin_settings as config
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
    raises (mirrors hive._do_observaloop's fence)."""
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


def sync_repos(cfg=None, dry_run: bool = False) -> OrcaSyncResult:
    """Register every discovered git-workspace clone with orca (idempotent).

    Returns ``unavailable=True`` (doing nothing else) when orca can't be used. Otherwise each
    discovered repo already known to orca is skipped; the rest are added (or would-be-added
    under ``dry_run``). Idempotent: a second run adds nothing."""
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
    """on_onboard hook: register the freshly onboarded hive's clone with orca."""
    add_repo(str(ctx.base), ctx.cfg)


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


_WORKTREES_RETIRED_READINESS = (
    "registered; orca.worktrees is retired and ignored — worktrees.manager (native git) owns "
    "worktree mechanics, remove orca.worktrees from config"
)


def _readiness(cfg, entry) -> tuple[str, str] | None:
    """hive-ready hook: is this hive's clone registered with orca? None when entry lacks triplet.

    A hive still setting the retired ``orca.worktrees`` flag reads ``warn`` (the flag no longer
    changes worktree mechanics — see the module docstring), so the operator removes it."""
    triplet = _entry_triplet(entry)
    if triplet is None:
        return None
    provider, org, repo = triplet
    clone = Path(workspace_root()) / provider / org / repo
    if str(clone) not in _repo_paths(cfg):
        return ("missing", "not registered — bh plugin orca sync")
    if config.orca_worktrees_enabled(cfg, entry):
        return ("warn", _WORKTREES_RETIRED_READINESS)
    return ("ok", "registered")


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
)
