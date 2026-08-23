"""Optional repowise codebase-index integration.

repowise owns an index inside each clone's ``.repowise`` directory.  It shares no state with
the other optional plugins: an absent binary, disabled flag, or unindexed clone is inert.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import typer

from . import config, plugins, registry, run

cli = typer.Typer(no_args_is_help=True, help="repowise local codebase-index integration.")


def _has_cli() -> bool:
    return shutil.which("repowise") is not None


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


def _index(clone: Path) -> int:
    result = run.run(
        [
            "repowise",
            "init",
            str(clone),
            "--mode",
            "fast",
            "--no-prose",
            "--yes",
            "--no-editor-setup",
            "--no-claude-md",
        ],
        check=False,
    )
    return result.returncode


@cli.command("index", help="provision the current hive's repowise index.")
def index(all_hives: bool = typer.Option(False, "--all", help="index every managed hive")) -> None:
    cfg = config.load()
    entries = config.managed_repos(cfg) if all_hives else [registry.current_hive(cfg)]
    clones = [clone for entry in entries if entry and (clone := _clone(entry)) and clone.is_dir()]
    if not clones:
        typer.echo("repowise: no managed clone to index")
        return
    if not _has_cli():
        typer.echo("✗ repowise is not installed", err=True)
        raise typer.Exit(1)
    failed = [clone for clone in clones if _index(clone) != 0]
    if failed:
        typer.echo(f"✗ repowise indexing failed for {len(failed)} clone(s)", err=True)
        raise typer.Exit(1)
    typer.echo(f"✓ repowise indexed {len(clones)} clone(s)")


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
    readiness=readiness,
)
