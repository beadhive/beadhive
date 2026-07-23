"""sync_remote.py — guarded fleet-wide push+verify before switching physical hosts.

`bh hive sync-remote --all` is the "one command an operator runs before walking away from a
physical host" so another host can pick up bd/bh state cleanly via `bh -a bd dolt pull` +
`git pull`, with no silent gaps. It runs the dolt-ref-aware safety scan (``safety.scan``)
across every registered hive, classifies each as clean / dirty / unpushed-git / unpushed-dolt
/ blocked, and — outside ``--dry-run`` — pushes what's safe to push. Follows the guarded
dry-run/gate pattern established by ``retire.retire_hive`` (a repo/hive must never be pushed
over silently when its working tree is dirty).

Exported API
------------
- ``SyncStatus``       — clean | dirty | unpushed-git | unpushed-dolt | blocked
- ``HiveSyncRecord``    — one hive's classification + reasons (read-only assessment)
- ``SyncPlan``          — structured outcome of ``sync_remote`` (what happened / would happen)
- ``assess_hive(hive_id, clone_path)`` — pure, read-only classification of one hive
- ``sync_remote(*, dry_run=False)`` — the guarded fleet-wide sync orchestrator
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

import typer

from . import config, engine, registry
from .run import run
from .safety import Category, scan

# Worst-first: BLOCKED and DIRTY both refuse to sync this hive (offending); UNPUSHED_GIT /
# UNPUSHED_DOLT are safe-to-push states (would be pushed in a live run); CLEAN needs nothing.
_RANK: dict[str, int] = {
    "clean": 0,
    "unpushed-dolt": 1,
    "unpushed-git": 2,
    "dirty": 3,
    "blocked": 4,
}


class SyncStatus(StrEnum):
    """Per-hive sync classification (see module docstring)."""

    CLEAN = "clean"
    DIRTY = "dirty"
    UNPUSHED_GIT = "unpushed-git"
    UNPUSHED_DOLT = "unpushed-dolt"
    BLOCKED = "blocked"


# Statuses this hive can never be safely synced under (refused, not merely "has work to push").
_OFFENDING = frozenset({SyncStatus.DIRTY, SyncStatus.BLOCKED})

# dolt_status values that mean "has something to push" — shared by the dry-run preview and the
# live-run push gate so the two can never drift apart again (bh-jhu0: dry-run used to fire on
# `absent`/never-bootstrapped hives too, since it only checked `!= "clean"`).
# "unknown" is bd's embedded/local engine's default state (bh-fl26): a Dolt remote is
# configured but bd has no read-only ahead/behind primitive — treated the same as "ahead"
# (attempt the idempotent `bd dolt push` and trust its own success/failure).
_DOLT_PUSHABLE = frozenset({"ahead", "diverged", "no-remote", "unknown"})


def _dolt_reason(dolt_status: str) -> str:
    """Human-readable reason line for a pushable ``dolt_status`` — "unknown" (bd's embedded
    engine, bh-fl26) has no ``refs/dolt/data`` to reference, so it gets its own honest
    wording instead of the git-ref-flavored message."""
    if dolt_status == "unknown":
        return (
            "dolt state: embedded engine has a remote configured; push status can't be "
            "verified without mutating (would attempt idempotent bd dolt push)"
        )
    return f"refs/dolt/data: {dolt_status}"


@dataclass
class HiveSyncRecord:
    """One hive's read-only sync assessment (mirrors ``safety.RetireResult``'s shape)."""

    hive: str
    clone_path: str
    status: SyncStatus
    reasons: list[str] = field(default_factory=list)
    unpushed_branches: list[str] = field(default_factory=list)
    dolt_status: str = "absent"


def assess_hive(hive_id: str, clone_path: Path) -> HiveSyncRecord:
    """Pure, read-only classification of one hive — never mutates anything.

    Reuses ``safety.scan`` (the dolt-ref-aware safety scan from bh-59q1.1) rather than
    re-deriving git/dolt state. Precedence: a missing clone / non-repo / no-origin hive is
    ``BLOCKED`` (cannot be safely assessed at all); any dirty branch makes the whole hive
    ``DIRTY`` (refused, even if it also has unpushed commits); otherwise unpushed git commits
    or an unpushed/diverged ``refs/dolt/data`` make it ``UNPUSHED_GIT`` / ``UNPUSHED_DOLT``;
    else ``CLEAN``.
    """
    if not clone_path.exists():
        return HiveSyncRecord(
            hive=hive_id,
            clone_path=str(clone_path),
            status=SyncStatus.BLOCKED,
            reasons=["clone path does not exist"],
        )

    record = scan(clone_path)

    if record.category == Category.NOT_A_REPO:
        return HiveSyncRecord(
            hive=hive_id,
            clone_path=str(clone_path),
            status=SyncStatus.BLOCKED,
            reasons=["not a git repository"],
        )

    if not record.has_origin:
        return HiveSyncRecord(
            hive=hive_id,
            clone_path=str(clone_path),
            status=SyncStatus.BLOCKED,
            reasons=["no origin remote configured — nothing to push to"],
        )

    dirty_branches = [b.name for b in record.branches if b.dirty]
    unpushed_branches = [
        b.name for b in record.branches if b.has_upstream and b.ahead > 0 and not b.dirty
    ]
    dolt = record.dolt_ref
    dolt_unpushed = dolt.status in _DOLT_PUSHABLE

    reasons: list[str] = []
    if dirty_branches:
        status = SyncStatus.DIRTY
        reasons.append(f"dirty branch(es): {', '.join(dirty_branches)}")
    elif unpushed_branches:
        status = SyncStatus.UNPUSHED_GIT
        reasons.append(f"unpushed git branch(es): {', '.join(unpushed_branches)}")
        if dolt_unpushed:
            reasons.append(_dolt_reason(dolt.status))
    elif dolt_unpushed:
        status = SyncStatus.UNPUSHED_DOLT
        reasons.append(_dolt_reason(dolt.status))
    else:
        status = SyncStatus.CLEAN

    return HiveSyncRecord(
        hive=hive_id,
        clone_path=str(clone_path),
        status=status,
        reasons=reasons,
        unpushed_branches=unpushed_branches,
        dolt_status=dolt.status,
    )


@dataclass
class SyncPlan:
    """Structured outcome of ``sync_remote`` — what happened (or would happen on dry-run)."""

    dry_run: bool
    records: list[HiveSyncRecord] = field(default_factory=list)
    pushed_branches: dict[str, list[str]] = field(default_factory=dict)
    dolt_pushed: list[str] = field(default_factory=list)
    offending: list[str] = field(default_factory=list)


def _last_stderr_line(result) -> str:
    """Last non-empty stderr line — git's actual failure ('! [rejected] ... non-fast-forward',
    'fatal: ...') is typically its final line, trailing any 'To <remote>' / hint noise above it."""
    for line in reversed((result.stderr or "").splitlines()):
        if line.strip():
            return line.strip()
    return f"exit {result.returncode}"


def _push_git_branches(
    clone_path: Path, branches: list[str]
) -> tuple[list[str], list[tuple[str, str]]]:
    """Push each already-tracked branch to its configured upstream (``git push origin <branch>``,
    no checkout required). Returns ``(pushed, failed)`` where ``failed`` pairs each branch name
    with the captured git stderr's last line, so a stale non-fast-forward ref can be told apart
    from an auth failure or anything else."""
    pushed: list[str] = []
    failed: list[tuple[str, str]] = []
    for name in branches:
        result = run(
            ["git", "-C", str(clone_path), "push", "origin", name],
            check=False,
            capture=True,
        )
        if result.returncode == 0:
            pushed.append(name)
        else:
            failed.append((name, _last_stderr_line(result)))
    return pushed, failed


def _push_dolt_state(cfg, clone_path: Path) -> bool:
    """Push this hive's ``refs/dolt/data`` via the configured ``Engine`` (bh-dw3e.6 wiring).
    Returns True on success."""
    result = engine.get_engine(cfg).push_state(
        clone_path, message=f"sync-remote {clone_path}"
    )
    return result.returncode == 0


def sync_remote(*, dry_run: bool = False) -> SyncPlan:
    """Guarded fleet-wide sync: assess every registered hive, then (outside ``--dry-run``) push
    what's safe to push.

    Every hive is assessed with ``assess_hive`` (read-only). A ``DIRTY`` or ``BLOCKED`` hive is
    refused — never pushed — and lands in ``plan.offending``. Every other hive that isn't
    already ``CLEAN`` gets its unpushed git branches pushed (``git push origin <branch>``) and
    its dolt state pushed (``Engine.push_state``); a push failure also lands the hive in
    ``plan.offending`` (no silent partial success). ``--dry-run`` performs zero mutation.

    Prints a per-hive summary line plus a final count, mirroring ``retire_hive``'s reporting
    style. Never raises — callers (the CLI) decide whether ``plan.offending`` should exit
    non-zero.
    """
    cfg = config.load()
    plan = SyncPlan(dry_run=dry_run)

    tag = "DRY-RUN " if dry_run else ""
    typer.echo(f"{tag}sync-remote --all")

    for entry in cfg.get("managed_repos", []) or []:
        provider, org, repo = str(entry["provider"]), str(entry["org"]), str(entry["repo"])
        hive_id = f"{provider}/{org}/{repo}"
        clone_path = registry.hive_dir(entry)

        record = assess_hive(hive_id, clone_path)
        plan.records.append(record)

        typer.echo(f"  {hive_id}: {record.status}")
        for reason in record.reasons:
            typer.echo(f"    - {reason}")

        if record.status in _OFFENDING:
            plan.offending.append(hive_id)
            continue

        if record.status == SyncStatus.CLEAN:
            continue

        if dry_run:
            if record.unpushed_branches:
                typer.echo(f"    would push git: {', '.join(record.unpushed_branches)}")
            if record.dolt_status == "unknown":
                # Embedded engine (bh-fl26): no read-only ahead/behind primitive exists,
                # so report the honest plan (an idempotent attempt) instead of a
                # fabricated ahead-count. Zero mutation still holds — nothing is called.
                typer.echo(
                    "    would attempt: bd dolt push (idempotent — no read-only "
                    "remote-diff primitive exists for this engine to preview exactly)"
                )
            elif record.dolt_status in _DOLT_PUSHABLE:
                typer.echo("    would push dolt: refs/dolt/data")
            continue

        failed_here = False

        if record.unpushed_branches:
            pushed, failed = _push_git_branches(clone_path, record.unpushed_branches)
            if pushed:
                plan.pushed_branches[hive_id] = pushed
                typer.echo(f"    pushed git: {', '.join(pushed)}")
            if failed:
                failed_here = True
                for name, err in failed:
                    typer.echo(f"    ✗ failed to push git: {name}: {err}", err=True)

        if record.dolt_status in _DOLT_PUSHABLE:
            if _push_dolt_state(cfg, clone_path):
                plan.dolt_pushed.append(hive_id)
                typer.echo("    pushed dolt: refs/dolt/data")
            else:
                failed_here = True
                typer.echo("    ✗ failed to push dolt: refs/dolt/data", err=True)

        if failed_here:
            plan.offending.append(hive_id)

    typer.echo(f"\n# {len(plan.records)} hive(s) assessed")
    if plan.offending:
        typer.echo(f"✗ {len(plan.offending)} hive(s) could not be safely synced:", err=True)
        for hive_id in plan.offending:
            typer.echo(f"    - {hive_id}", err=True)
    else:
        typer.echo("✓ dry-run complete — nothing changed" if dry_run else "✓ sync-remote complete")

    return plan
