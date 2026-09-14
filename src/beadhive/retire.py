"""retire.py — guarded teardown for the retire and reclaim flows.

Before a hive's clone is removed, every managed worktree must be torn down cleanly.
Dirty worktrees (uncommitted changes) are never force-removed — they are surfaced in the
result so the caller (``bh hive retire`` / ``bh hive reclaim``) can gate on them or request
explicit consent.

``managed_repos`` is FLEET-scoped truth (``config_partition.py`` — every host sees the same
registry), so unregistering a hive is a fleet-wide act: every other host loses it too. The
two public orchestrators below share one teardown core (``_teardown_and_dispose``) and differ
in exactly ONE step — whether the registry is touched:

- ``retire_hive`` — FLEET-WIDE: assess → (backup|consent) → worktree teardown → **unregister**
  → soft-archive (or hard-purge) the clone. Use when no host needs this hive anymore.
- ``reclaim_hive`` — HOST-LOCAL: the identical assess → (backup|consent) → worktree teardown →
  soft-archive (or hard-purge) the clone, but the registry step is skipped entirely — the
  hive stays registered for the fleet, and every OTHER host's clone/worktrees are untouched.
  Use when this host no longer wants a local copy but other hosts (or the fleet itself) still
  do. The data-loss risk of losing this host's own clone is identical to retire's, so it
  reuses ``safety.assess_retire`` unchanged.

Exported API
------------
- ``TeardownResult`` — structured result: removed, dirty, reclaimed_dirs
- ``teardown_worktrees(hive, *, dry_run=False)`` — enumerate + selectively tear down all
  managed worktrees for a hive; dirty worktrees are flagged and skipped, not force-removed.
- ``RetirePlan`` — structured outcome of ``retire_hive``/``reclaim_hive`` (what happened / would
  happen).
- ``retire_hive(hive, *, dry_run, backup, confirm, purge)`` — fleet-wide guarded teardown:
  unregisters the hive.
- ``reclaim_hive(hive, *, dry_run, backup, confirm, purge)`` — host-local guarded teardown:
  never unregisters; ``managed_repos`` is left byte-identical.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

import typer

from . import config, plugins, registry, safety, worktree
from .identity import workspace_root
from .modules.hives import RetireEvent
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


def teardown_worktrees(hive: str, *, dry_run: bool = False) -> TeardownResult:
    """Enumerate and tear down all managed worktrees for ``hive`` before clone removal.

    Dirty worktrees are detected via ``worktree.is_clean`` and are never force-removed;
    they appear in ``TeardownResult.dirty`` so the caller can surface them and gate on
    them.  ``dry_run=True`` previews the plan (populates ``removed``) without touching
    anything.

    Reuses ``worktree.managed``, ``worktree.is_clean``, ``worktree.remove``, and
    ``worktree._rmdir_empty_parents`` — does not duplicate git plumbing.

    Runs inside ``worktree.store_probe_cache()`` (bh-ioub2). Every ``remove`` here runs the
    UNKNOWN preflight, which asks whether THIS hive's bead store can be read — one hive-level
    fact that was otherwise re-probed once per worktree. Retiring the 28-worktree
    agentguides/runtime hive paid 28 of them, against a store that is by that bead's own premise
    slow or refusing. The cache is scoped to this call, so the answer is fresh per command and
    two teardowns never share one.
    """
    cfg = config.load()
    result = TeardownResult()

    all_rows = worktree.managed(cfg)
    rows = [r for r in all_rows if r[0] == hive]
    root = config.worktrees_root().resolve()

    with worktree.store_probe_cache():
        return _teardown_rows(rows, root, result, dry_run=dry_run)


def _teardown_rows(rows, root: Path, result: TeardownResult, *, dry_run: bool) -> TeardownResult:
    """The per-worktree loop, extracted only so `teardown_worktrees` can hold the store-probe
    cache around the whole pass without re-indenting the body."""
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
    """Structured outcome of ``retire_hive``/``reclaim_hive`` — what happened (or would happen
    on dry-run).

    Carries semantic events so callers/tests can assert without parsing stdout.
    ``unregistered`` is only ever set ``True`` by ``retire_hive``'s fleet-wide path;
    ``reclaim_hive`` never sets it (it never calls ``registry.unregister``).
    """

    hive: str
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
    events: list[RetireEvent] = field(default_factory=list)
    successful: bool = True


class _RetireRefused(Exception):
    def __init__(self, plan: RetirePlan) -> None:
        super().__init__("retire refused")
        self.plan = plan


def _note(plan: RetirePlan, code: str, *, error: bool = False, render: bool, **facts) -> None:
    event = RetireEvent(code, MappingProxyType(facts), error)
    plan.events.append(event)
    if render:
        typer.echo(_event_text(plan, event), err=error)


def _event_text(plan: RetirePlan, event: RetireEvent) -> str:
    """Render a semantic retirement event for the legacy direct-call adapter."""

    facts = event.facts
    action = "retire" if facts.get("fleet", False) else "reclaim"
    prefix = "DRY-RUN " if plan.dry_run else ""
    code = event.code
    if code == "operation":
        return f"{prefix}{action} {facts['identity']}"
    if code == "clone":
        return f"  clone: {facts['path']}"
    if code == "scope":
        return (
            "  scope: host-local — managed_repos is untouched; "
            f"{facts['identity']} stays registered for the fleet"
        )
    if code == "clone_missing":
        return f"✗ clone path does not exist: {facts['path']}"
    if code == "assessment":
        return f"  assess: {facts['verdict']}"
    if code in {"assessment_reason", "backup_reason", "dirty_worktree", "failed_worktree"}:
        return f"    - {facts['reason']}"
    if code == "worktree_removed":
        verb = "would remove" if plan.dry_run else "removed"
        return f"  worktree: {verb} {facts['path']}"
    if code == "purge":
        verb = "would rm -rf" if plan.dry_run else "rm -rf"
        return f"  purge: {verb} {facts['path']}"
    if code == "archive":
        verb = "would move" if plan.dry_run else "moved"
        return f"  archive: {verb} {facts['source']} → {facts['destination']}"
    if code == "archive_exists":
        return f"✗ archive destination already exists: {facts['path']}"
    if code == "unregister":
        return (
            f"  unregister: would drop {facts['identity']} from the registry "
            "(fleet-wide — every host loses this hive)"
        )
    if code == "registry_retained":
        return (
            f"  registry: left untouched — {facts['identity']} remains registered for the fleet "
            f"(`{config.BINARY_ALIAS} hive rm --confirm` unregisters it fleet-wide)"
        )
    if code == "plugin_preview":
        return f"  plugin {facts['plugin']}: would notify of retire (manual removal)"
    if code == "plugin_failed":
        return f"  plugin {facts['plugin']}: notify failed ({facts['reason']})"
    if code == "complete":
        return "✓ dry-run complete — nothing changed" if plan.dry_run else f"✓ {action} complete"
    if code == "backup_failed":
        return f"✗ backup failed: {facts['reason']}"
    if code == "nothing_deleted":
        return "  nothing was deleted — resolve the error and retry"
    if code == "backup_incomplete_accepted":
        return "  backup: incomplete — --confirm accepts the remaining loss"
    if code == "backup_unsafe":
        return "✗ refusing: backup did not make the repository safe:"
    if code == "confirm_hint":
        hints = {
            "remaining_loss": "  pass --confirm to accept the remaining loss",
            "blocked": "  pass --confirm to override and proceed anyway",
            "teardown": "  resolve the failure, or pass --confirm to proceed anyway",
        }
        return hints[facts["kind"]]
    if code == "backup_skipped":
        return "  backup: skipped — --confirm accepts the data loss"
    if code == "unbacked_work":
        return "✗ refusing: repository has unbacked work that would be lost"
    if code == "backup_hint":
        noun = facts["subject"]
        return (
            "  pass --backup to snapshot it durably, or --confirm to accept the loss"
            if noun == "repository"
            else "  pass --backup to snapshot them, or --confirm to accept the loss"
        )
    if code == "blocked_overridden":
        return "  assess: BLOCKED overridden by --confirm"
    if code == "assessment_blocked":
        return "✗ refusing: assessment is BLOCKED (see reasons above)"
    if code == "dirty_worktree_accepted":
        return f"  worktree: keeping dirty {facts['path']} — --confirm accepts the loss"
    if code == "dirty_worktrees":
        return "✗ refusing: dirty worktrees hold unbacked work:"
    if code == "teardown_failed_accepted":
        return f"  worktree: FAILED to remove {facts['path']} — --confirm proceeds anyway"
    if code == "teardown_failed":
        return "✗ refusing: worktree teardown failed (live worktrees remain):"
    if code == "backup":
        verb = "would back up" if plan.dry_run else "backed up"
        return f"  backup: {verb} {facts['label']} {facts['path']}"
    if code == "backup_action":
        return f"    · {facts['action']}"
    raise ValueError(f"unknown retirement event: {code}")


def _refuse(plan: RetirePlan) -> None:
    plan.successful = False
    raise _RetireRefused(plan)


def _archive_dir(cfg) -> Path:
    """Resolve the soft-archive root via the formal ``archive`` config section.

    Delegates to ``config.archive_dir`` which reads ``archive.dir`` with a graceful
    fallback to ``workspace_root()/.archived``.
    """
    return config.archive_dir(cfg)


def retire_hive(
    hive: str,
    *,
    dry_run: bool = False,
    backup: bool = False,
    confirm: bool = False,
    purge: bool = False,
) -> RetirePlan:
    """FLEET-WIDE guarded teardown of a hive: assess → (backup|consent) → teardown →
    **unregister** → archive. Unregistering drops ``managed_repos`` — every host loses this
    hive, not just this one. Use ``reclaim_hive`` instead when only THIS host should drop its
    local copy.

    The whole point is the guardrail contract: **a repo must NEVER lose data without operator
    consent.** The safety gate (``safety.assess_retire``) and the dirty-worktree check both
    refuse to proceed unless the operator either backs the work up (``--backup``) or explicitly
    accepts the loss (``--confirm``).

    Order
    -----
    1. Resolve the hive entry + its on-disk clone (``workspace_root()/provider/org/repo``).
    2. ``safety.assess_retire`` gate — SAFE proceeds; NEEDS_BACKUP needs ``--backup`` or
       ``--confirm``; BLOCKED needs ``--confirm``.
    3. ``teardown_worktrees`` — dirty worktrees are unbacked work: need ``--backup`` or
       ``--confirm``.
    4. Soft-archive the clone to the archive dir (``--purge`` hard-deletes instead) —
       skipped on dry-run.
    5. ``registry.unregister`` — FLEET-WIDE (skipped on dry-run).

    ``--dry-run`` prints the full plan and performs ZERO mutation (default-safe mindset).

    Returns a ``RetirePlan`` describing what happened (or would happen). Raises ``typer.Exit``
    on a refused gate or an unresolvable/absent clone.
    """
    try:
        return _teardown_and_dispose(
            hive,
            dry_run=dry_run,
            backup=backup,
            confirm=confirm,
            purge=purge,
            unregister=True,
            render=True,
        )
    except _RetireRefused as exc:
        raise typer.Exit(1) from exc


def execute_retire_hive(
    hive: str, *, dry_run: bool, backup: bool, confirm: bool, purge: bool
) -> RetirePlan:
    """Transport-neutral fleet retirement used by the hives application adapter."""

    try:
        return _teardown_and_dispose(
            hive,
            dry_run=dry_run,
            backup=backup,
            confirm=confirm,
            purge=purge,
            unregister=True,
            render=False,
        )
    except _RetireRefused as exc:
        return exc.plan


def reclaim_hive(
    hive: str,
    *,
    dry_run: bool = False,
    backup: bool = False,
    confirm: bool = False,
    purge: bool = False,
) -> RetirePlan:
    """HOST-LOCAL guarded teardown of a hive: assess → (backup|consent) → teardown → archive —
    the identical data-loss-safety contract as ``retire_hive``, but the registry step is
    skipped entirely: ``managed_repos`` (and therefore every other host's view of this hive)
    is left byte-identical. Use when this host no longer wants a local clone of a hive that
    stays registered for the fleet (or that other hosts still hold).

    Reuses ``safety.assess_retire`` UNCHANGED — losing this host's own unbacked/unpushed work
    is exactly as risky here as it is on the fleet-wide path, so the same SAFE / NEEDS_BACKUP /
    BLOCKED gate applies verbatim. Only the final registry step differs from ``retire_hive``
    (omitted here, not merely deferred) — see that function for the full order and the
    guardrail contract shared by both.

    Returns a ``RetirePlan`` describing what happened (or would happen); ``plan.unregistered``
    is always ``False`` — this path never calls ``registry.unregister``. Raises ``typer.Exit``
    on a refused gate or an unresolvable/absent clone.
    """
    try:
        return _teardown_and_dispose(
            hive,
            dry_run=dry_run,
            backup=backup,
            confirm=confirm,
            purge=purge,
            unregister=False,
            render=True,
        )
    except _RetireRefused as exc:
        raise typer.Exit(1) from exc


def execute_reclaim_hive(
    hive: str, *, dry_run: bool, backup: bool, confirm: bool, purge: bool
) -> RetirePlan:
    """Transport-neutral host reclaim used by the hives application adapter."""

    try:
        return _teardown_and_dispose(
            hive,
            dry_run=dry_run,
            backup=backup,
            confirm=confirm,
            purge=purge,
            unregister=False,
            render=False,
        )
    except _RetireRefused as exc:
        return exc.plan


def _teardown_and_dispose(
    hive: str,
    *,
    dry_run: bool,
    backup: bool,
    confirm: bool,
    purge: bool,
    unregister: bool,
    render: bool,
) -> RetirePlan:
    """Shared core behind ``retire_hive`` (``unregister=True``, fleet-wide) and
    ``reclaim_hive`` (``unregister=False``, host-local). Every step through the archive/purge
    is identical between the two scopes; only the registry step (last, and only reached once
    the clone is provably gone/moved) is conditional. See the two public wrappers' docstrings
    for the guardrail contract and full step order.
    """
    cfg = config.load()
    entry = registry.resolve_hive(cfg, hive)
    provider, org, repo = str(entry["provider"]), str(entry["org"]), str(entry["repo"])
    clone_path = Path(workspace_root()) / provider / org / repo

    plan = RetirePlan(
        hive=hive,
        clone_path=str(clone_path),
        verdict=RetireVerdict.SAFE,
        dry_run=dry_run,
    )
    _note(
        plan,
        "operation",
        identity=f"{provider}/{org}/{repo}",
        fleet=unregister,
        render=render,
    )
    _note(plan, "clone", path=str(clone_path), render=render)
    if not unregister:
        _note(
            plan,
            "scope",
            identity=f"{org}/{repo}",
            render=render,
        )

    # --- Step 1: clone must exist on disk ---
    if not clone_path.exists():
        _note(
            plan,
            "clone_missing",
            path=str(clone_path),
            error=True,
            render=render,
        )
        _refuse(plan)

    # --- Step 2: safety gate ---
    assessment = safety.assess_retire(clone_path)
    plan.verdict = assessment.verdict
    _note(plan, "assessment", verdict=str(assessment.verdict), render=render)
    for reason in assessment.reasons:
        _note(plan, "assessment_reason", reason=reason, render=render)

    _gate_backup(
        clone_path,
        assessment,
        plan,
        backup=backup,
        confirm=confirm,
        dry_run=dry_run,
        render=render,
    )

    # --- Step 3: worktree teardown ---
    # Gate-first: probe with dry_run=True to discover the dirty set WITHOUT mutating, so the
    # dirty gate fires before any clean worktree is removed. This preserves the keystone
    # "assess fully, then act" contract — a real run against a hive with both clean and dirty
    # worktrees must never remove the clean ones and *then* refuse on the dirty ones.
    _gate_dirty_worktrees(
        hive, plan, backup=backup, confirm=confirm, dry_run=dry_run, render=render
    )

    # Gate passed — only now do the REAL teardown (still zero-mutation under --dry-run).
    # The real run removes the clean worktrees and still skips any dirty ones, which by now
    # are either backed up or explicitly accepted via --confirm.
    teardown = teardown_worktrees(hive, dry_run=dry_run)
    plan.teardown = teardown
    for path in teardown.removed:
        _note(plan, "worktree_removed", path=path, render=render)

    # --- Gate: a clean worktree that FAILED to remove still points at the clone. ---
    # Do not move/delete a clone out from under a live worktree.
    _gate_failed_teardown(teardown, plan, confirm=confirm, render=render)

    # --- Step 4: the IRREVERSIBLE filesystem step FIRST (archive/purge). ---
    # Unregister (fleet-wide path only) happens only AFTER this succeeds, so a failed
    # move/purge can never leave the hive unregistered-but-on-disk (it would propagate before
    # the unregister below).
    if purge:
        _note(
            plan,
            "purge",
            path=str(clone_path),
            render=render,
        )
        if not dry_run:
            shutil.rmtree(clone_path)
        plan.purged = True
    else:
        dest = _archive_dir(cfg) / provider / org / repo
        _note(
            plan,
            "archive",
            source=str(clone_path),
            destination=str(dest),
            render=render,
        )
        if not dry_run:
            if dest.exists():
                _note(
                    plan,
                    "archive_exists",
                    path=str(dest),
                    error=True,
                    render=render,
                )
                _refuse(plan)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(clone_path), str(dest))
        plan.archived_to = str(dest)

    # --- Step 5: registry, LAST (only reached once the clone is provably gone/moved). ---
    # Fleet-wide (retire_hive) drops managed_repos here; host-local (reclaim_hive) never
    # touches it at all — that's the whole point, not merely a deferral.
    if unregister:
        if dry_run:
            _note(
                plan,
                "unregister",
                identity=f"{org}/{repo}",
                render=render,
            )
        else:
            registry.unregister(provider, org, repo)
            plan.unregistered = True
    else:
        _note(
            plan,
            "registry_retained",
            identity=f"{org}/{repo}",
            render=render,
        )

    # --- Generic plugin notify: WARN-ONLY. Plugins have no de-registration verb (see orca),
    # so this only reminds; it never mutates any plugin's state. Loops the registry generically
    # so no integration is hardcoded here. Dry-run previews but does NOT record (mutation
    # contract). Runs for BOTH scopes: THIS host's clone is disappearing either way.
    for observer in plugins.retire_observers(cfg, entry):
        if dry_run:
            _note(
                plan,
                "plugin_preview",
                plugin=observer.plugin_id,
                render=render,
            )
            continue
        report = observer.deliver(str(clone_path), cfg, entry)
        if not plugins.delivery_succeeded(report):
            error = report.deliveries[-1].attempts[-1].error
            _note(
                plan,
                "plugin_failed",
                plugin=observer.plugin_id,
                reason=error,
                error=True,
                render=render,
            )
            continue
        plan.plugins_notified.append(observer.plugin_id)

    if dry_run:
        _note(plan, "complete", fleet=unregister, render=render)
    else:
        _note(plan, "complete", fleet=unregister, render=render)
    return plan


def _gate_backup(clone_path, assessment, plan, *, backup, confirm, dry_run, render):
    """Consent gate for the safety assessment (data-loss critical). NEEDS_BACKUP proceeds only with
    --backup (verified to make the clone SAFE — else --confirm accepts the remainder) or --confirm
    (accept the loss); BLOCKED proceeds only with --confirm. Mutates ``plan.backed_up``; raises
    ``typer.Exit(1)`` on a refused gate. Prompts / refusals / backups preserved byte-for-byte."""
    if assessment.verdict == RetireVerdict.NEEDS_BACKUP:
        if backup:
            try:
                _backup_path(clone_path, plan, dry_run=dry_run, label="clone", render=render)
            except (RuntimeError, ValueError) as exc:
                # Backup raised (e.g. a push failed) BEFORE anything was torn down —
                # nothing is deleted; refuse so the operator can resolve and retry.
                _note(
                    plan,
                    "backup_failed",
                    reason=str(exc),
                    error=True,
                    render=render,
                )
                _note(
                    plan,
                    "nothing_deleted",
                    error=True,
                    render=render,
                )
                _refuse(plan)
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
                    _note(
                        plan,
                        "backup_incomplete_accepted",
                        render=render,
                    )
                    for reason in recheck.reasons:
                        _note(plan, "backup_reason", reason=reason, render=render)
                else:
                    _note(
                        plan,
                        "backup_unsafe",
                        error=True,
                        render=render,
                    )
                    for reason in recheck.reasons:
                        _note(
                            plan,
                            "backup_reason",
                            reason=reason,
                            error=True,
                            render=render,
                        )
                    _note(
                        plan,
                        "confirm_hint",
                        kind="remaining_loss",
                        error=True,
                        render=render,
                    )
                    _refuse(plan)
        elif confirm:
            _note(
                plan,
                "backup_skipped",
                render=render,
            )
        else:
            _note(
                plan,
                "unbacked_work",
                error=True,
                render=render,
            )
            _note(
                plan,
                "backup_hint",
                subject="repository",
                error=True,
                render=render,
            )
            _refuse(plan)
    elif assessment.verdict == RetireVerdict.BLOCKED:
        if confirm:
            _note(
                plan,
                "blocked_overridden",
                render=render,
            )
        else:
            _note(
                plan,
                "assessment_blocked",
                error=True,
                render=render,
            )
            _note(
                plan,
                "confirm_hint",
                kind="blocked",
                error=True,
                render=render,
            )
            _refuse(plan)


def _gate_dirty_worktrees(hive, plan, *, backup, confirm, dry_run, render):
    """Consent gate for dirty worktrees (unbacked work). Probe the dirty set WITHOUT mutating, then
    require --backup (snapshot each) or --confirm (accept the loss) before any real teardown — so
    the gate fires before any clean worktree is removed. Mutates ``plan.backed_up``; raises
    ``typer.Exit(1)`` on refusal. Semantics preserved byte-for-byte."""
    probe = teardown_worktrees(hive, dry_run=True)
    if probe.dirty:
        if backup:
            for path in probe.dirty:
                _backup_path(Path(path), plan, dry_run=dry_run, label="worktree", render=render)
                plan.backed_up = True
        elif confirm:
            for path in probe.dirty:
                _note(
                    plan,
                    "dirty_worktree_accepted",
                    path=path,
                    render=render,
                )
        else:
            _note(
                plan,
                "dirty_worktrees",
                error=True,
                render=render,
            )
            for path in probe.dirty:
                _note(
                    plan,
                    "dirty_worktree",
                    reason=path,
                    error=True,
                    render=render,
                )
            _note(
                plan,
                "backup_hint",
                subject="worktrees",
                error=True,
                render=render,
            )
            _refuse(plan)


def _gate_failed_teardown(teardown, plan, *, confirm, render):
    """Consent gate for a clean worktree that FAILED to remove (a live worktree still points at the
    clone): refuse to move/delete the clone out from under it unless --confirm proceeds anyway.
    Raises ``typer.Exit(1)`` on refusal. Semantics preserved byte-for-byte."""
    if teardown.failed:
        if confirm:
            for path in teardown.failed:
                _note(
                    plan,
                    "teardown_failed_accepted",
                    path=path,
                    render=render,
                )
        else:
            _note(
                plan,
                "teardown_failed",
                error=True,
                render=render,
            )
            for path in teardown.failed:
                _note(
                    plan,
                    "failed_worktree",
                    reason=path,
                    error=True,
                    render=render,
                )
            _note(
                plan,
                "confirm_hint",
                kind="teardown",
                error=True,
                render=render,
            )
            _refuse(plan)


def _backup_path(
    path: Path, plan: RetirePlan, *, dry_run: bool, label: str, render: bool
) -> safety.BackupResult:
    """Back up unpushed work at ``path`` via ``backup_unpushed`` and record it on the plan.

    Does NOT set ``plan.backed_up`` — the caller owns that, setting it only once the work is
    provably safe (the clone case re-asserts ``assess_retire`` is SAFE before trusting it).
    """
    result = safety.backup_unpushed(path, dry_run=dry_run)
    plan.backup_actions.extend(result.actions)
    _note(plan, "backup", label=label, path=str(path), render=render)
    for action in result.actions:
        _note(plan, "backup_action", action=action, render=render)
    return result
