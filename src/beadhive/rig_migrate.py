"""`bh rig migrate` — upgrade already-onboarded managed repos from `ws` to `bh`.

A repo onboarded before the beadhive/bh rename carries `ws`-era artifacts: the old
`<!-- ws:agf:start/end -->` marker in AGENTS.md/CLAUDE.md, `.claude/settings.json` hooks that
invoke the `ws` binary, and bundled skill files (copied into ./skills by `rig init --skills`)
that still say `ws`. This walks the registry's managed repos and rewrites all of that in place.

Idempotent: `_plan` only returns files whose rewritten content differs from what's on disk, so a
second run finds nothing to do. `--dry-run` runs `_plan` and prints a unified diff per file
without writing anything.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from pathlib import Path

import typer

from . import config, registry
from .rig import _AGF_MARK_END, _AGF_MARK_START  # the current (bh) marker pair

_OLD_MARK_START = "<!-- ws:agf:start"
_OLD_MARK_END = "<!-- ws:agf:end -->"

# Whole-word `ws` only — leaves `workspace`, `answers`, `aws`, etc. untouched.
_WS_TOKEN = re.compile(r"\bws\b")

_DOC_NAMES = ("AGENTS.md", "CLAUDE.md")
_SETTINGS_REL = Path(".claude") / "settings.json"
_SKILLS_DIR = "skills"


@dataclass(frozen=True)
class _Change:
    rel: str  # path relative to the repo root, for display
    path: Path  # absolute path to write
    old: str
    new: str


def _rewrite_ws_tokens(text: str) -> str:
    return _WS_TOKEN.sub("bh", text)


def _upgrade_doc_block(text: str) -> str | None:
    """Rewrite the managed AGF stanza to the canonical (bh) block, upgrading the old `ws`
    marker pair to the new `bh` one along the way. None if no managed block is present."""
    if _AGF_MARK_START in text:
        start_marker, end_marker = _AGF_MARK_START, _AGF_MARK_END
    elif _OLD_MARK_START in text:
        start_marker, end_marker = _OLD_MARK_START, _OLD_MARK_END
    else:
        return None
    block = config.asset("AGF-hint.md").read_text().strip()
    start = text.index(start_marker)
    end = text.index(end_marker, start) + len(end_marker)
    return text[:start] + block + text[end:]


def _read_text(path: Path) -> str | None:
    """Best-effort text read — None for absent or non-UTF8 (binary) files."""
    if not path.is_file():
        return None
    try:
        return path.read_text()
    except UnicodeDecodeError:
        return None


def _maybe_change(base: Path, rel: Path, new: str | None) -> _Change | None:
    if new is None:
        return None
    path = base / rel
    old = path.read_text()
    if new == old:
        return None
    return _Change(str(rel), path, old, new)


def _plan(base: Path) -> list[_Change]:
    """Every file under `base` whose migrated content would differ from what's on disk."""
    changes: list[_Change] = []

    for name in _DOC_NAMES:
        rel = Path(name)
        text = _read_text(base / rel)
        if text is None:
            continue
        change = _maybe_change(base, rel, _upgrade_doc_block(text))
        if change:
            changes.append(change)

    settings_text = _read_text(base / _SETTINGS_REL)
    if settings_text is not None:
        change = _maybe_change(base, _SETTINGS_REL, _rewrite_ws_tokens(settings_text))
        if change:
            changes.append(change)

    skills_dir = base / _SKILLS_DIR
    if skills_dir.is_dir():
        for path in sorted(skills_dir.rglob("*")):
            if not path.is_file():
                continue
            text = _read_text(path)
            if text is None:
                continue
            rel = path.relative_to(base)
            change = _maybe_change(base, rel, _rewrite_ws_tokens(text))
            if change:
                changes.append(change)

    return changes


def _echo_diff(change: _Change) -> None:
    diff = difflib.unified_diff(
        change.old.splitlines(keepends=True),
        change.new.splitlines(keepends=True),
        fromfile=f"a/{change.rel}",
        tofile=f"b/{change.rel}",
    )
    typer.echo("".join(diff))


def migrate_repo(base: Path, dry_run: bool = False) -> list[_Change]:
    """Plan + (unless dry_run) apply the ws->bh migration for one repo. Returns the changes
    found (already applied when not dry_run)."""
    changes = _plan(base)
    for change in changes:
        if dry_run:
            _echo_diff(change)
        else:
            change.path.write_text(change.new)
            typer.echo(f"  ✓ {change.rel}")
    return changes


def migrate(dry_run: bool = False, rig_id: str = "") -> None:
    """`bh rig migrate`: rewrite ws -> bh across every already-onboarded managed repo (or just
    `rig_id` when given). Skips repos that aren't cloned locally."""
    cfg = config.load()
    entries = [registry.resolve_rig(cfg, rig_id)] if rig_id else cfg.get("managed_repos", [])
    if not entries:
        typer.echo("# No registered rigs.")
        return

    total_changed = total_clean = total_skipped = 0
    for entry in entries:
        prefix = str(entry["prefix"])
        base = registry.rig_dir(entry)
        if not (base / ".git").is_dir():
            typer.echo(f"=== {prefix}  {base} ===\n  ⚠ skip: no checkout", err=True)
            total_skipped += 1
            continue
        typer.echo(f"=== {prefix}  {base} ===")
        changes = migrate_repo(base, dry_run=dry_run)
        if not changes:
            typer.echo("  • up to date")
            total_clean += 1
        else:
            total_changed += 1

    verb = "would change" if dry_run else "changed"
    typer.echo(f"\n{total_changed} {verb} / {total_clean} up to date / {total_skipped} skipped")
