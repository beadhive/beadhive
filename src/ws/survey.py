"""ws rig survey — operator-facing, read-only fleet table for onboarding triage.

Prints one row per on-disk repo (registered + tracked) with safety classification,
git health signals, disk usage, and onboarding difficulty sourced from safety.py.

Public API
----------
- ``collect_rows(cfg)``   — build all survey rows (read-only, pure)
- ``survey(...)``         — render the fleet table; called from the CLI
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import typer

from . import config, gitworkspace, metadata, registry, safety
from .identity import workspace_root

# Difficulty ordering for --sort difficulty (ascending: easiest first)
_DIFFICULTY_RANK: dict[str, int] = {
    "easy": 0,
    "medium": 1,
    "hard": 2,
    "not-a-candidate": 3,
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _scan_from_meta(rec: metadata.RepoMetadata) -> safety.ScanResult:
    """Rebuild the ``safety.ScanResult`` view the row + difficulty derivation need, from a cached
    metadata record — so no repo is re-scanned (the disk walk already ran once in ``metadata``)."""
    return safety.ScanResult(
        category=safety.Category(rec.category),
        has_origin=rec.has_origin,
        stash_count=rec.stash_count,
        disk_bytes=rec.disk_bytes,
        branches=[safety.BranchInfo(**b) for b in rec.branches],
        worktrees=list(rec.worktrees),
    )


def _all_repo_keys(cfg: dict) -> dict[str, bool]:
    """Union of registered + tracked repos → {triplet: is_registered}.

    Always reads the lock file (even when git_workspace.enabled is false)
    so that every on-disk tracked repo appears regardless of config state.
    """
    registered: dict[str, bool] = {
        f"{e['provider']}/{e['org']}/{e['repo']}": True
        for e in cfg.get("managed_repos", []) or []
    }
    all_keys: dict[str, bool] = dict(registered)
    for (p, o, r) in gitworkspace.tracked_repos(cfg):
        key = f"{p}/{o}/{r}"
        if key not in all_keys:
            all_keys[key] = False
    return all_keys


def _build_row(
    key: str, path: Path, rec: metadata.RepoMetadata, is_registered: bool, cfg: dict
) -> dict:
    """Build one survey row for a single on-disk repo.

    Sources all repo state from the workspace-metadata cache record (``rec``) — the union of
    ``safety.ScanResult`` + commit age / maturity / last-commit-date measured once by ``metadata``.
    ``difficulty`` is the sanctioned cheap on-read derivation (docs/METADATA-CACHE.md §2): it is
    recomputed from the reconstructed scan + ``registry.classify``, never re-scanning the tree.
    """
    parts = key.split("/")
    provider, org, repo_name = parts[0], parts[1], parts[2]
    cls = registry.classify(provider, org, repo_name, cfg)
    scan = _scan_from_meta(rec)
    commit_count = rec.commit_count
    # Cache stores age_days=None / last_commit=None for a no-commit repo; restore the prior
    # sentinels (inf / "(none)") so downstream formatting + sorting are byte-for-byte unchanged.
    age_days = float("inf") if rec.age_days is None else rec.age_days
    last_commit = "(none)" if rec.last_commit is None else rec.last_commit
    diff = safety.difficulty(scan, repo_path=str(path), classify=cls)

    # Compute ahead/behind: display string for human table, raw ints for JSON.
    # Display "(n/a)" only when there are no branches at all (consistent with
    # last-commit "(none)"). Raw ints are null when no branch has an upstream.
    if scan.branches:
        total_ahead = sum(b.ahead for b in scan.branches)
        total_behind = sum(b.behind for b in scan.branches)
        ab_display = f"+{total_ahead}/-{total_behind}"
    else:
        total_ahead = 0
        total_behind = 0
        ab_display = "(n/a)"
    has_upstream = any(b.has_upstream for b in scan.branches)

    return {
        "repo": key,
        "registered": is_registered,
        "classification": cls,
        "commits": commit_count,
        "last_commit": last_commit,
        "age_days": age_days,
        "ahead_behind": ab_display,
        "ahead": total_ahead if has_upstream else None,
        "behind": total_behind if has_upstream else None,
        "dirty_branches": sum(1 for b in scan.branches if b.dirty),
        "disk_bytes": scan.disk_bytes,
        "difficulty": diff.verdict,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def collect_rows(cfg: dict | None = None) -> list[dict]:
    """Collect survey rows for all on-disk repos (registered + tracked).

    Read-only: no mutation of any repo or registry state. Repos that are
    registered or tracked but not yet cloned locally are silently skipped.
    """
    if cfg is None:
        cfg = config.load()
    root = Path(workspace_root())
    repo_keys = _all_repo_keys(cfg)

    # On-disk repos only (registered/tracked-but-uncloned are silently skipped), in sorted order.
    present: list[tuple[str, Path]] = []
    for key in sorted(repo_keys):
        parts = key.split("/")
        if len(parts) != 3:
            continue
        path = root / parts[0] / parts[1] / parts[2]
        if not path.exists():
            continue
        present.append((key, path))

    # Single aggregation path: one read-through over the metadata cache measures each repo at most
    # once, replacing the per-row safety.scan (the disk-walk double-scan is gone — see .3 / §1).
    records = metadata.read_fleet(cfg, [k for k, _ in present], ttl=metadata.ttl(cfg))
    rows: list[dict] = []
    for key, path in present:
        rec = records.get(key)
        if rec is None:
            continue
        rows.append(_build_row(key, path, rec, repo_keys[key], cfg))
    return rows


def survey(
    available: bool = False,
    json_out: bool = False,
    sort: str = "",
) -> None:
    """Render the fleet table.

    Parameters
    ----------
    available:
        When True, filter to unregistered candidate repos only.
    json_out:
        When True, emit machine-readable JSON (one object per repo).
    sort:
        Optional sort key: ``"disk"`` | ``"age"`` | ``"difficulty"``.
    """
    cfg = config.load()
    rows = collect_rows(cfg)

    if available:
        rows = [r for r in rows if not r["registered"]]

    if sort == "disk":
        rows.sort(key=lambda r: r["disk_bytes"])
    elif sort == "age":
        rows.sort(key=lambda r: r["age_days"])
    elif sort == "difficulty":
        rows.sort(key=lambda r: _DIFFICULTY_RANK.get(r["difficulty"], 9))

    if json_out:
        _emit_json(rows)
        return

    if not rows:
        typer.echo("(no repos found)")
        return

    _print_table(rows)


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def _emit_json(rows: list[dict]) -> None:
    """Serialise rows as a JSON array.

    age_days=inf and last_commit='(none)' are rendered as null.
    ahead_behind is split into typed integer fields ahead/behind (null when no upstream).
    """
    output = []
    for r in rows:
        age = r["age_days"]
        last_commit = r["last_commit"]
        output.append(
            {
                "repo": r["repo"],
                "registered": r["registered"],
                "classification": r["classification"],
                "commits": r["commits"],
                "last_commit": None if last_commit == "(none)" else last_commit,
                "age_days": None if math.isinf(age) else round(age, 1),
                "ahead": r["ahead"],
                "behind": r["behind"],
                "dirty_branches": r["dirty_branches"],
                "disk": safety.format_bytes(r["disk_bytes"]),
                "disk_bytes": r["disk_bytes"],
                "difficulty": r["difficulty"],
            }
        )
    typer.echo(json.dumps(output))


# Column spec: (header, width, align)
_COLS: list[tuple[str, int, str]] = [
    ("REPO", 35, "left"),
    ("REG", 3, "right"),
    ("CLASS", 22, "left"),
    ("COMMITS", 7, "right"),
    ("LAST-COMMIT", 11, "right"),
    ("AHEAD/BEHIND", 12, "right"),
    ("DIRTY", 5, "right"),
    ("DISK", 10, "right"),
    ("DIFFICULTY", 14, "left"),
]


def _cell(value: str, width: int, align: str) -> str:
    return value.ljust(width) if align == "left" else value.rjust(width)


def _print_table(rows: list[dict]) -> None:
    """Render the human-readable fleet table to stdout."""
    header = "  ".join(_cell(col, w, a) for col, w, a in _COLS)
    typer.echo(header)
    typer.echo("-" * len(header))
    for r in rows:
        difficulty_cell = (
            "(n/a)" if r["difficulty"] == "not-a-candidate" else r["difficulty"].upper()
        )
        values = [
            r["repo"],
            "yes" if r["registered"] else "no",
            r["classification"],
            str(r["commits"]),
            r["last_commit"],
            r["ahead_behind"],
            str(r["dirty_branches"]),
            safety.format_bytes(r["disk_bytes"]),
            difficulty_cell,
        ]
        row_str = "  ".join(_cell(v, w, a) for v, (_, w, a) in zip(values, _COLS, strict=False))
        typer.echo(row_str)
