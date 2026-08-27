"""Optional repowise codebase-index integration.

repowise owns an index inside each clone's ``.repowise`` directory.  It shares no state with
the other optional plugins: an absent binary, disabled flag, or unindexed clone is inert.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from functools import lru_cache
from pathlib import Path

import typer
from ruamel.yaml import YAML

from . import config, gitworkspace, plugins, registry, run
from .identity import workspace_root

cli = typer.Typer(no_args_is_help=True, help="repowise local codebase-index integration.")

_REQUIRED_INIT_FLAGS = frozenset(
    {
        "--all",
        "--mode",
        "--no-claude-md",
        "--no-codex",
        "--no-mcp-json",
        "--no-prose",
        "--no-vscode",
        "--no-workspace",
        "-y",
    }
)
_REQUIRED_UPDATE_FLAGS = frozenset({"--index-only", "--no-workspace"})
_REQUIRED_FLAGS = {"init": _REQUIRED_INIT_FLAGS, "update": _REQUIRED_UPDATE_FLAGS}
_FLAG_TOKEN = re.compile(r"(?<![\w-])--?[A-Za-z0-9][A-Za-z0-9-]*")
_OPTION_DECLARATION_ROW = re.compile(r"^  (?P<declarations>-\S(?:.*?\S)?) {2,}\S")

_OPERATIONAL_ENV = {
    "COMSPEC",
    "HOME",
    "HOMEDRIVE",
    "HOMEPATH",
    "LANG",
    "LANGUAGE",
    "LOGNAME",
    "NO_COLOR",
    "PATH",
    "PATHEXT",
    "PYTHONIOENCODING",
    "PYTHONUTF8",
    "SYSTEMROOT",
    "TEMP",
    "TERM",
    "TMP",
    "TMPDIR",
    "TZ",
    "USER",
    "USERPROFILE",
    "WINDIR",
}


def _repowise_env() -> dict[str, str]:
    """Inherited execution environment, minus unrelated repo routing and secret values.

    ``run.run`` treats an explicit ``env`` as its complete base rather than an overlay, so this
    must retain operational values such as ``PATH``. Repowise's Beadhive modes are local,
    no-prose indexing and need neither provider credentials nor fleet/database passwords.
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if key in _OPERATIONAL_ENV or key.startswith("LC_")
    }
    env["REPOWISE_SKIP_EDITOR_SETUP"] = "1"
    return env


def _option_flags(help_text: str) -> frozenset[str]:
    """Parse only Click option declaration rows; prose may name removed/deprecated flags."""
    flags = set()
    in_options = False
    for line in help_text.splitlines():
        if not in_options:
            in_options = line.strip() == "Options:"
            continue
        if line and not line[0].isspace():
            break
        if match := _OPTION_DECLARATION_ROW.match(line):
            flags.update(_FLAG_TOKEN.findall(match.group("declarations")))
    return frozenset(flags)


def _has_cli() -> bool:
    return shutil.which("repowise") is not None


@lru_cache(maxsize=2)
def capabilities(command: str) -> frozenset[str] | None:
    """Return exact flags advertised by one command, or ``None`` when its probe failed."""
    if command not in _REQUIRED_FLAGS:
        raise ValueError(f"unsupported repowise capability command: {command}")
    if not _has_cli():
        return None
    try:
        result = run.run(
            ["repowise", command, "--help"],
            check=False,
            capture=True,
            env=_repowise_env(),
            exact_env=True,
        )
    except Exception:  # noqa: BLE001 - a capability probe must never break a hook
        return None
    if result.returncode != 0:
        return None
    help_text = f"{result.stdout or ''}\n{result.stderr or ''}"
    advertised = _option_flags(help_text)
    return _REQUIRED_FLAGS[command] & advertised


def capability_error(command: str | None = None) -> str | None:
    """Return an actionable reason when the configured plugin cannot be safely used."""
    if not _has_cli():
        return "repowise is not installed"
    commands = (command,) if command is not None else tuple(_REQUIRED_FLAGS)
    problems = []
    for name in commands:
        advertised = capabilities(name)
        if advertised is None:
            problems.append(f"{name} --help capability probe failed")
            continue
        missing = sorted(_REQUIRED_FLAGS[name] - advertised)
        if missing:
            problems.append(f"{name} missing {', '.join(missing)}")
    if problems:
        return (
            "repowise is present but "
            + "; ".join(problems)
            + "; install briancripe/repowise@feat/no-mcp-json-no-vscode-flags"
        )
    return None


def _require_capabilities(command: str) -> None:
    if (error := capability_error(command)) is not None:
        raise RuntimeError(f"unsupported repowise {command} capability: {error}")


def enabled(cfg, entry) -> bool:
    """Only enable when configured *and* the user-provided binary is available."""
    return config.repowise_enabled(cfg, entry) and _has_cli()


def _clone(entry) -> Path | None:
    if not entry or not all(entry.get(key) for key in ("provider", "org", "repo")):
        return None
    return registry.hive_dir(entry)


def _state(clone: Path) -> dict:
    try:
        value = json.loads((clone / ".repowise" / "state.json").read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def readiness(cfg, entry) -> tuple[str, str] | None:
    """Report index presence, git drift, and index size without probing unavailable clones."""
    if config.repowise_enabled(cfg, entry) and (error := capability_error()) is not None:
        return ("warn", error)
    clone = _clone(entry)
    if clone is None:
        return None
    state_path = clone / ".repowise" / "state.json"
    if not state_path.is_file():
        return ("missing", "no index — bh plugin repowise index")

    index_dir = clone / ".repowise"
    size = sum(path.stat().st_size for path in index_dir.rglob("*") if path.is_file())
    last = str(_state(clone).get("last_sync_commit") or "")
    if not last:
        return ("warn", f"index state has no last_sync_commit ({size / 1024 / 1024:.1f} MiB)")
    try:
        drift = run.out(["git", "-C", str(clone), "rev-list", "--count", f"{last}..HEAD"])
        commits = int(drift.strip())
    except Exception:  # noqa: BLE001 - a readiness probe must not break hive ready
        return ("warn", f"index present; sync position unavailable ({size / 1024 / 1024:.1f} MiB)")
    state = "ok" if commits == 0 else "warn"
    return (state, f"{commits} commits behind; {size / 1024 / 1024:.1f} MiB")


_BASE_ARGS = [
    "--mode",
    "fast",
    "--no-prose",
    "--no-claude-md",
    "--no-codex",
    "--no-mcp-json",
    "--no-vscode",
]


def _backfill_vscode_config(path: Path, *, workspace: bool) -> None:
    """Persist the VS Code opt-out in existing base index configurations.

    ``--no-vscode`` protects init, but ``repowise update`` intentionally relies on
    its persisted editor configuration.  A workspace invocation therefore updates
    every existing base config beneath the workspace; a single-repo invocation
    changes only that clone's config.
    """
    configs = path.rglob(".repowise/config.yaml") if workspace else [path / ".repowise/config.yaml"]
    yaml = YAML()
    for config_path in configs:
        if not config_path.is_file():
            continue
        data = yaml.load(config_path.read_text()) or {}
        if not isinstance(data, dict):
            continue
        editor_files = data.get("editor_files")
        if editor_files is None:
            editor_files = data["editor_files"] = {}
        if not isinstance(editor_files, dict) or editor_files.get("vscode") is False:
            continue
        editor_files["vscode"] = False
        with config_path.open("w") as stream:
            yaml.dump(data, stream)


def _index(path: Path, *, workspace: bool) -> int:
    """Initialize an index without touching repowise's machine-wide editor settings."""
    if (error := capability_error("init")) is not None:
        typer.echo(f"⚠ repowise disabled: {error}; skipping index", err=True)
        return 0
    _backfill_vscode_config(path, workspace=workspace)
    args = ["repowise", "init", str(path), *_BASE_ARGS]
    if workspace:
        args.append("--all")
    else:
        # A managed repo may contain fixture repositories.  Do not let repowise silently
        # promote this one-repo request to a workspace scan.
        args.append("--no-workspace")
    args.append("-y")
    result = run.run(
        args,
        check=False,
        env=_repowise_env(),
        exact_env=True,
    )
    return result.returncode


def _workspace(cfg) -> Path:
    """Resolve the configured git-workspace root rather than reading its env var here.

    ``config_paths`` owns the git-workspace layering (explicit path, host workspace, then HQ).
    Its selected config lives at the root that a host-level repowise invocation should index.
    The normal resolver remains the graceful fallback for an unconfigured workspace.
    """
    paths = gitworkspace.config_paths(cfg)
    return paths[0].parent if paths else Path(workspace_root())


def _on_onboard(ctx) -> None:
    """Seed the freshly onboarded base checkout for future worktree copies."""
    if not _has_cli():
        typer.echo("• repowise: skipped — repowise is not installed")
        return
    if _index(Path(ctx.base), workspace=False) != 0:
        raise RuntimeError(f"repowise indexing failed for {ctx.base}")
    typer.echo(f"✓ repowise indexed {ctx.base}")


def _branch_point(main: Path, start_point: str) -> str:
    return run.out(["git", "-C", str(main), "rev-parse", start_point or "HEAD"]).strip()


def _refresh_base(cfg, entry, *, main: Path, branch: str, target: Path, start_point: str) -> None:
    """Refresh the seed source before git fixes the new worktree's branch point."""
    del cfg, entry, branch, target
    _backfill_vscode_config(main, workspace=False)
    state = _state(main)
    last_sync = str(state.get("last_sync_commit") or "")
    if not last_sync:
        typer.echo("• repowise: base has no index; skipping refresh")
        return
    branch_point = _branch_point(main, start_point)
    if last_sync == branch_point:
        typer.echo("• repowise: base index already current")
        return

    _require_capabilities("update")
    started = time.monotonic()
    result = run.run(
        ["repowise", "update", str(main), "--index-only", "--no-workspace"],
        check=False,
        env=_repowise_env(),
        exact_env=True,
    )
    elapsed = time.monotonic() - started
    if result.returncode:
        raise RuntimeError(f"base refresh failed after {elapsed:.1f}s")
    typer.echo(f"✓ repowise refreshed base in {elapsed:.1f}s")


def _install_workspace_overlay(cfg, target: Path) -> None:
    """Expose the host overlay without copying it, with paths valid from the worktree."""
    root = _workspace(cfg)
    source_manifest = root / ".repowise-workspace.yaml"
    source_overlay = root / ".repowise-workspace"
    if not source_manifest.is_file() or not source_overlay.is_dir():
        return

    yaml = YAML()
    data = yaml.load(source_manifest.read_text()) or {}
    for repo in data.get("repos", []):
        path = Path(str(repo.get("path", "")))
        if path and not path.is_absolute():
            repo["path"] = str((root / path).resolve())
    with (target / ".repowise-workspace.yaml").open("w") as stream:
        yaml.dump(data, stream)
    (target / ".repowise-workspace").symlink_to(source_overlay, target_is_directory=True)


def _seed_worktree(cfg, entry, *, main: Path, branch: str, target: Path) -> None:
    """Let repowise auto-detect the linked worktree's validated base and seed from it."""
    del entry, main, branch
    _require_capabilities("init")
    started = time.monotonic()
    result = run.run(
        ["repowise", "init", *_BASE_ARGS, "-y"],
        check=False,
        cwd=target,
        env=_repowise_env(),
        exact_env=True,
    )
    elapsed = time.monotonic() - started
    if result.returncode:
        raise RuntimeError(f"worktree seed/full-init fallback failed after {elapsed:.1f}s")
    _install_workspace_overlay(cfg, target)
    typer.echo(f"✓ repowise seeded worktree in {elapsed:.1f}s")


@cli.command(
    "index",
    help="provision a base index, or --all the workspace (~12 min / ~400 MiB for 34 repos).",
)
def index(all_hives: bool = typer.Option(False, "--all", help="index every managed hive")) -> None:
    cfg = config.load()
    if not _has_cli():
        typer.echo("• repowise: skipped — repowise is not installed")
        return
    if all_hives:
        root = _workspace(cfg)
        if not root.is_dir():
            typer.echo(f"✗ repowise workspace does not exist: {root}", err=True)
            raise typer.Exit(1)
        if _index(root, workspace=True) != 0:
            typer.echo("✗ repowise workspace indexing failed", err=True)
            raise typer.Exit(1)
        typer.echo(f"✓ repowise indexed workspace {root}")
        return

    clone = _clone(registry.current_hive(cfg))
    if clone is None or not clone.is_dir():
        typer.echo("repowise: no managed clone to index")
        return
    if _index(clone, workspace=False) != 0:
        typer.echo("✗ repowise indexing failed", err=True)
        raise typer.Exit(1)
    typer.echo(f"✓ repowise indexed {clone}")


@cli.command("status", help="show the current hive's repowise index freshness and size.")
def status() -> None:
    cfg = config.load()
    entry = registry.current_hive(cfg)
    result = readiness(cfg, entry)
    if result is None:
        typer.echo("repowise: no managed clone for this directory")
        return
    state, detail = result
    typer.echo(f"repowise: {state} — {detail}")


PLUGIN = plugins.Plugin(
    name="repowise",
    cli=cli,
    enabled=enabled,
    on_onboard=_on_onboard,
    readiness=readiness,
    wt_creating=_refresh_base,
    wt_created=_seed_worktree,
)
