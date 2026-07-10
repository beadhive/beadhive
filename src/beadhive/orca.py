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
"""

from __future__ import annotations

import json
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
    """Extract ONLY the repos list from a parsed orca payload (dict-with-repos or bare list).

    Never reads ``projects`` / ``projectHostSetups`` — those are out of bh's scope."""
    if isinstance(data, dict):
        repos = data.get("repos", [])
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
    """Real on-disk clones exactly three levels under $GIT_WORKSPACE (provider/org/repo) that
    contain a ``.git`` entry. Walks the filesystem — does NOT read workspace-lock.toml (many
    enumerated repos never actually clone)."""
    root = Path(workspace_root())
    found: list[Path] = []
    if not root.is_dir():
        return found
    for provider in sorted(root.iterdir()):
        if not provider.is_dir():
            continue
        for org in sorted(provider.iterdir()):
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
    """Print a manual-removal reminder on retire — orca has NO de-registration verb, so bh
    never mutates orca-data.json to fake a removal."""
    typer.echo(
        f"• orca: {path} may still be registered with orca — orca has no de-registration "
        "verb, so remove it manually from orca-data.json if you no longer want it tracked.",
        err=True,
    )


def _on_onboard(ctx) -> None:
    """on_onboard hook: register the freshly onboarded rig's clone with orca."""
    add_repo(str(ctx.base), ctx.cfg)


def _entry_triplet(entry):
    """(provider, org, repo) from a managed_repos entry, or None when it lacks the triplet."""
    if not entry:
        return None
    provider, org, repo = entry.get("provider"), entry.get("org"), entry.get("repo")
    if provider and org and repo:
        return str(provider), str(org), str(repo)
    return None


def _readiness(cfg, entry) -> tuple[str, str] | None:
    """rig-ready hook: is this rig's clone registered with orca? None when entry lacks triplet."""
    triplet = _entry_triplet(entry)
    if triplet is None:
        return None
    provider, org, repo = triplet
    clone = Path(workspace_root()) / provider / org / repo
    if str(clone) in _repo_paths(cfg):
        return ("ok", "registered")
    return ("missing", "not registered — bh plugin orca sync")


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


PLUGIN = plugins.Plugin(
    name="orca",
    cli=cli,
    enabled=lambda cfg, entry: config.orca_enabled(cfg, entry),
    on_onboard=_on_onboard,
    on_retire=lambda clone_path, cfg, entry: warn_retire(clone_path, cfg),
    readiness=_readiness,
)
