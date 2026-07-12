"""retire.py — guarded teardown for the retire flow.

Before a rig's clone is removed, every managed worktree must be torn down cleanly.
Dirty worktrees (uncommitted changes) are never force-removed — they are surfaced in the
result so the caller (``ws rig retire``) can gate on them or request explicit consent.

The keystone orchestrator ``retire_rig`` composes the safety gate, backup, worktree
teardown, registry unregister, and soft-archive (or hard-purge) into a single operator
verb whose central invariant is: **a repo never loses data without operator consent**.

Exported API
------------
- ``TeardownResult`` — structured result: removed, dirty, reclaimed_dirs
- ``teardown_worktrees(rig, *, dry_run=False)`` — enumerate + selectively tear down all
  managed worktrees for a rig; dirty worktrees are flagged and skipped, not force-removed.
- ``RetirePlan`` — structured outcome of ``retire_rig`` (what happened / would happen).
- ``retire_rig(rig, *, dry_run, backup, confirm, purge)`` — the guarded teardown orchestrator.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

import typer

from . import config, plugins, registry, safety, worktree
from .identity import workspace_root
from .safety import RetireVerdict


@dataclass
class TeardownResult:
    """Outcome of ``teardown_worktrees``.

    ``removed`` is populated in both real and dry-run modes — in dry-run it holds the paths
    that *would* be removed.  ``reclaimed_dirs`` is only populated in real (non-dry-run) runs.
    """

    removed: list[str] = field(default_factory=list)
    """Paths of managed worktrees removed (or would-be-removed in dry_run)."""

    dirty: list[str] = field(default_factory=list)
    """Paths of managed worktrees skipped because they contain uncommitted changes."""

    reclaimed_dirs: list[str] = field(default_factory=list)
    """Empty triplet dirs (parent dirs under the shadow root) reclaimed after removal."""

    failed: list[str] = field(default_factory=list)
    """Paths of clean worktrees whose removal FAILED (git error) — surfaced, not swallowed,
    so the orchestrator can refuse before deleting a clone a live worktree still points at."""


def teardown_worktrees(rig: str, *, dry_run: bool = False) -> TeardownResult:
    """Enumerate and tear down all managed worktrees for ``rig`` before clone removal.

    Dirty worktrees are detected via ``worktree.is_clean`` and are never force-removed;
    they appear in ``TeardownResult.dirty`` so the caller can surface them and gate on
    them.  ``dry_run=True`` previews the plan (populates ``removed``) without touching
    anything.

    Reuses ``worktree.managed``, ``worktree.is_clean``, ``worktree.remove``, and
    ``worktree._rmdir_empty_parents`` — does not duplicate git plumbing.
    """
    cfg = config.load()
    result = TeardownResult()

    all_rows = worktree.managed(cfg)
    rows = [r for r in all_rows if r[0] == rig]
    root = config.worktrees_root().resolve()

    for prefix, path, _brref in rows:
        target = Path(path)

        if not worktree.is_clean(target):
            result.dirty.append(str(target))
            continue

        if dry_run:
            result.removed.append(str(target))
            continue

        # Collect candidate parent dirs before removal to detect what _rmdir_empty_parents
        # reclaims after the worktree dir is gone.
        candidates: list[Path] = []
        p = target.parent.resolve()
        while root in p.parents and p != root:
            candidates.append(p)
            p = p.parent

        # Remove the clean worktree; worktree.remove handles git + _rmdir_empty_parents.
        try:
            worktree.remove(prefix, target.name)
        except typer.Exit:
            # Removal failed (git error). Record it so the orchestrator can gate on it
            # instead of silently proceeding to delete a clone a live worktree references.
            result.failed.append(str(target))
            continue

        result.removed.append(str(target))

        for candidate in candidates:
            if not candidate.exists():
                result.reclaimed_dirs.append(str(candidate))

    return result


# ---------------------------------------------------------------------------
# Guarded teardown orchestrator
# ---------------------------------------------------------------------------


@dataclass
class RetirePlan:
    """Structured outcome of ``retire_rig`` — what happened (or would happen on dry-run).

    Mirrors the printed summary so callers/tests can assert without parsing stdout.
    """

    rig: str
    clone_path: str
    verdict: RetireVerdict
    dry_run: bool
    backed_up: bool = False
    backup_actions: list[str] = field(default_factory=list)
    teardown: TeardownResult | None = None
    unregistered: bool = False
    archived_to: str | None = None
    purged: bool = False
    plugins_notified: list[str] = field(default_factory=list)


def _archive_dir(cfg) -> Path:
    """Resolve the soft-archive root via the formal ``archive`` config section.

    Delegates to ``config.archive_dir`` which reads ``archive.dir`` with a graceful
    fallback to ``workspace_root()/.archived``.
    """
    return config.archive_dir(cfg)


def retire_rig(
    rig: str,
    *,
    dry_run: bool = False,
    backup: bool = False,
    confirm: bool = False,
    purge: bool = False,
) -> RetirePlan:
    """Guarded teardown of a rig: assess → (backup|consent) → teardown → unregister → archive.

    The whole point is the guardrail contract: **a repo must NEVER lose data without operator
    consent.** The safety gate (``safety.assess_retire``) and the dirty-worktree check both
    refuse to proceed unless the operator either backs the work up (``--backup``) or explicitly
    accepts the loss (``--confirm``).

    Order
    -----
    1. Resolve the rig entry + its on-disk clone (``workspace_root()/provider/org/repo``).
    2. ``safety.assess_retire`` gate — SAFE proceeds; NEEDS_BACKUP needs ``--backup`` or
       ``--confirm``; BLOCKED needs ``--confirm``.
    3. ``teardown_worktrees`` — dirty worktrees are unbacked work: need ``--backup`` or
       ``--confirm``.
    4. ``registry.unregister`` (skipped on dry-run).
    5. Soft-archive the clone to the archive dir (``--purge`` hard-deletes instead) —
       skipped on dry-run.

    ``--dry-run`` prints the full plan and performs ZERO mutation (default-safe mindset).

    Returns a ``RetirePlan`` describing what happened (or would happen). Raises ``typer.Exit``
    on a refused gate or an unresolvable/absent clone.
    """
    cfg = config.load()
    entry = registry.resolve_rig(cfg, rig)
    provider, org, repo = str(entry["provider"]), str(entry["org"]), str(entry["repo"])
    clone_path = Path(workspace_root()) / provider / org / repo

    tag = "DRY-RUN " if dry_run else ""
    typer.echo(f"{tag}retire {provider}/{org}/{repo}")
    typer.echo(f"  clone: {clone_path}")

    # --- Step 1: clone must exist on disk ---
    if not clone_path.exists():
        typer.echo(f"✗ clone path does not exist: {clone_path}", err=True)
        raise typer.Exit(1)

    plan = RetirePlan(
        rig=rig,
        clone_path=str(clone_path),
        verdict=RetireVerdict.SAFE,
        dry_run=dry_run,
    )

    # --- Step 2: safety gate ---
    assessment = safety.assess_retire(clone_path)
    plan.verdict = assessment.verdict
    typer.echo(f"  assess: {assessment.verdict}")
    for reason in assessment.reasons:
        typer.echo(f"    - {reason}")

    _gate_backup(clone_path, assessment, plan, backup=backup, confirm=confirm, dry_run=dry_run)

    # --- Step 3: worktree teardown ---
    # Gate-first: probe with dry_run=True to discover the dirty set WITHOUT mutating, so the
    # dirty gate fires before any clean worktree is removed. This preserves the keystone
    # "assess fully, then act" contract — a real run against a rig with both clean and dirty
    # worktrees must never remove the clean ones and *then* refuse on the dirty ones.
    _gate_dirty_worktrees(rig, plan, backup=backup, confirm=confirm, dry_run=dry_run)

    # Gate passed — only now do the REAL teardown (still zero-mutation under --dry-run).
    # The real run removes the clean worktrees and still skips any dirty ones, which by now
    # are either backed up or explicitly accepted via --confirm.
    teardown = teardown_worktrees(rig, dry_run=dry_run)
    plan.teardown = teardown
    verb = "would remove" if dry_run else "removed"
    for path in teardown.removed:
        typer.echo(f"  worktree: {verb} {path}")

    # --- Gate: a clean worktree that FAILED to remove still points at the clone. ---
    # Do not move/delete a clone out from under a live worktree.
    _gate_failed_teardown(teardown, confirm=confirm)

    # --- Step 4: the IRREVERSIBLE filesystem step FIRST (archive/purge). ---
    # Unregister happens only AFTER this succeeds, so a failed move/purge can never leave the
    # rig unregistered-but-on-disk (it would propagate before the unregister below).
    if purge:
        typer.echo(f"  purge: {'would rm -rf' if dry_run else 'rm -rf'} {clone_path}")
        if not dry_run:
            shutil.rmtree(clone_path)
        plan.purged = True
    else:
        dest = _archive_dir(cfg) / provider / org / repo
        typer.echo(f"  archive: {'would move' if dry_run else 'moved'} {clone_path} → {dest}")
        if not dry_run:
            if dest.exists():
                typer.echo(f"✗ archive destination already exists: {dest}", err=True)
                raise typer.Exit(1)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(clone_path), str(dest))
        plan.archived_to = str(dest)

    # --- Step 5: unregister LAST (only reached once the clone is provably gone/moved). ---
    if dry_run:
        typer.echo(f"  unregister: would drop {org}/{repo} from the registry")
    else:
        registry.unregister(provider, org, repo)
        plan.unregistered = True

    # --- Generic plugin notify: WARN-ONLY. Plugins have no de-registration verb (see orca),
    # so this only reminds; it never mutates any plugin's state. Loops the registry generically
    # so no integration is hardcoded here. Dry-run previews but does NOT record (mutation contract).
    for p in plugins.registry():
        if p.on_retire is None or not p.enabled(cfg, entry):
            continue
        if dry_run:
            typer.echo(f"  plugin {p.name}: would notify of retire (manual removal)")
            continue
        try:
            p.on_retire(str(clone_path), cfg, entry)
        except Exception as exc:  # noqa: BLE001 - defensive fence: a plugin never aborts retire
            typer.echo(f"  plugin {p.name}: notify failed ({exc})", err=True)
            continue
        plan.plugins_notified.append(p.name)

    typer.echo("✓ retire complete" if not dry_run else "✓ dry-run complete — nothing changed")
    return plan


def _gate_backup(clone_path, assessment, plan, *, backup, confirm, dry_run):
    """Consent gate for the safety assessment (data-loss critical). NEEDS_BACKUP proceeds only with
    --backup (verified to make the clone SAFE — else --confirm accepts the remainder) or --confirm
    (accept the loss); BLOCKED proceeds only with --confirm. Mutates ``plan.backed_up``; raises
    ``typer.Exit(1)`` on a refused gate. Prompts / refusals / backups preserved byte-for-byte."""
    if assessment.verdict == RetireVerdict.NEEDS_BACKUP:
        if backup:
            try:
                _backup_path(clone_path, plan, dry_run=dry_run, label="clone")
            except (RuntimeError, ValueError) as exc:
                # Backup raised (e.g. a push failed) BEFORE anything was torn down —
                # nothing is deleted; refuse so the operator can resolve and retry.
                typer.echo(f"✗ backup failed: {exc}", err=True)
                typer.echo("  nothing was deleted — resolve the error and retry", err=True)
                raise typer.Exit(1) from exc
            if dry_run:
                plan.backed_up = True
            else:
                # Verify the backup actually made the clone safe BEFORE any destructive
                # step. backup_unpushed self-verifies too; this is the orchestrator's
                # independent gate (and the only place --confirm can accept a remainder).
                recheck = safety.assess_retire(clone_path)
                if recheck.verdict == RetireVerdict.SAFE:
                    plan.backed_up = True
                elif confirm:
                    typer.echo(
                        "  backup: incomplete — --confirm accepts the remaining loss"
                    )
                    for reason in recheck.reasons:
                        typer.echo(f"    - {reason}")
                else:
                    typer.echo(
                        "✗ refusing: backup did not make the repository safe:", err=True
                    )
                    for reason in recheck.reasons:
                        typer.echo(f"    - {reason}", err=True)
                    typer.echo(
                        "  pass --confirm to accept the remaining loss", err=True
                    )
                    raise typer.Exit(1)
        elif confirm:
            typer.echo("  backup: skipped — --confirm accepts the data loss")
        else:
            typer.echo(
                "✗ refusing: repository has unbacked work that would be lost", err=True
            )
            typer.echo(
                "  pass --backup to snapshot it durably, or --confirm to accept the loss",
                err=True,
            )
            raise typer.Exit(1)
    elif assessment.verdict == RetireVerdict.BLOCKED:
        if confirm:
            typer.echo("  assess: BLOCKED overridden by --confirm")
        else:
            typer.echo("✗ refusing: assessment is BLOCKED (see reasons above)", err=True)
            typer.echo("  pass --confirm to override and proceed anyway", err=True)
            raise typer.Exit(1)


def _gate_dirty_worktrees(rig, plan, *, backup, confirm, dry_run):
    """Consent gate for dirty worktrees (unbacked work). Probe the dirty set WITHOUT mutating, then
    require --backup (snapshot each) or --confirm (accept the loss) before any real teardown — so
    the gate fires before any clean worktree is removed. Mutates ``plan.backed_up``; raises
    ``typer.Exit(1)`` on refusal. Semantics preserved byte-for-byte."""
    probe = teardown_worktrees(rig, dry_run=True)
    if probe.dirty:
        if backup:
            for path in probe.dirty:
                _backup_path(Path(path), plan, dry_run=dry_run, label="worktree")
                plan.backed_up = True
        elif confirm:
            for path in probe.dirty:
                typer.echo(f"  worktree: keeping dirty {path} — --confirm accepts the loss")
        else:
            typer.echo("✗ refusing: dirty worktrees hold unbacked work:", err=True)
            for path in probe.dirty:
                typer.echo(f"    - {path}", err=True)
            typer.echo(
                "  pass --backup to snapshot them, or --confirm to accept the loss", err=True
            )
            raise typer.Exit(1)


def _gate_failed_teardown(teardown, *, confirm):
    """Consent gate for a clean worktree that FAILED to remove (a live worktree still points at the
    clone): refuse to move/delete the clone out from under it unless --confirm proceeds anyway.
    Raises ``typer.Exit(1)`` on refusal. Semantics preserved byte-for-byte."""
    if teardown.failed:
        if confirm:
            for path in teardown.failed:
                typer.echo(
                    f"  worktree: FAILED to remove {path} — --confirm proceeds anyway"
                )
        else:
            typer.echo(
                "✗ refusing: worktree teardown failed (live worktrees remain):", err=True
            )
            for path in teardown.failed:
                typer.echo(f"    - {path}", err=True)
            typer.echo(
                "  resolve the failure, or pass --confirm to proceed anyway", err=True
            )
            raise typer.Exit(1)


def _backup_path(
    path: Path, plan: RetirePlan, *, dry_run: bool, label: str
) -> safety.BackupResult:
    """Back up unpushed work at ``path`` via ``backup_unpushed`` and record it on the plan.

    Does NOT set ``plan.backed_up`` — the caller owns that, setting it only once the work is
    provably safe (the clone case re-asserts ``assess_retire`` is SAFE before trusting it).
    """
    result = safety.backup_unpushed(path, dry_run=dry_run)
    plan.backup_actions.extend(result.actions)
    prefix = "would back up" if dry_run else "backed up"
    typer.echo(f"  backup: {prefix} {label} {path}")
    for action in result.actions:
        typer.echo(f"    · {action}")
    return result
