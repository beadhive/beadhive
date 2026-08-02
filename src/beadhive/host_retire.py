"""``bh host retire`` — guarded, host-local decommission of THIS host (bh-twc8.2).

Mirrors the hive-level safety model that already works (``bh hive retire`` /
``safety.assess_retire`` -> SAFE | NEEDS_BACKUP | BLOCKED) but folded across HOST scope: every
registered hive (via ``safety.assess_retire``, unchanged), every managed worktree (via
``worktree`` status classification), every lease this host holds, and Factory HQ's own
ahead/behind on BOTH halves (git ``main`` + ``refs/dolt/data`` — bh-z9hl).

SCOPE — host-local ONLY, never fleet-wide
------------------------------------------
``managed_repos`` is FLEET truth (lives in ``~/.beadhive/hq/fleet.yaml`` — every host sees the
SAME registry). ``bh hive retire`` / ``bh hive rm`` unregister a hive FLEET-WIDE: composing
either into a host decommission would delete the WHOLE FLEET's hive registry, not just this
host's copy — retiring one host would wipe another host's hives. This module NEVER calls
``retire.retire_hive`` or ``registry.unregister``; it composes ``retire.reclaim_hive`` (bh-twc8.3
— host-local teardown that leaves ``managed_repos`` byte-identical) for every hive instead.

Host retire touches only host-scoped state: its own leases, its own
``hosts/<host_id>.yaml`` manifest, and its LOCAL clones/worktrees.

Ordering — the whole point
---------------------------
Several steps become IMPOSSIBLE if run late (see the module's own guarded ``retire`` for the
enforced order): release leases before this host's identity might be lost (only the holding
``host_id`` can release); sync+push every hive's beads AND code before local clones are
reclaimed; reclaim local clones/worktrees before the manifest is dropped (idle busywork
otherwise, but keeps the "what's still local" picture honest while it runs); deregister the
manifest, THEN publish it — one ``bh hq push`` at the very end durably ships both the lease
releases and the manifest removal in a single push, rather than an intermediate push that would
need a second one anyway once the manifest write lands.

Design choice — bd's embedded Dolt engine (bh-fl26)
-----------------------------------------------------
Per-hive assessment reuses ``safety.assess_retire`` **UNCHANGED**, exactly as instructed — and
that function hardcodes ``scan(path)`` with ``fetch=False``, so there is no fetch-verification
knob to expose at this call site even if we wanted one: an embedded-Dolt hive with a configured
remote always reports ``dolt_ref.status == "unknown"`` -> ``NEEDS_BACKUP`` here, conservatively,
until the always-run "sync + push hives" step (``sync_remote.sync_remote``, which DOES pay for a
real ``fetch=True`` federation check in its own pre-existing assessment pass) resolves it by
attempting the idempotent ``bd dolt push``. No separate ``--verify``/``--fetch`` flag was added
to ``bh host retire`` for this reason — the conservative-by-default posture falls out of reusing
``assess_retire`` as directed, and the one opt-in verification path that DOES exist in this
pipeline (``sync_remote``'s ``fetch=True`` pre-pass) already runs unconditionally.

Exported API
------------
- ``HiveFold``          — one hive's read-only fold into the host verdict
- ``HostAssessment``    — the ONE host-level SAFE/NEEDS_BACKUP/BLOCKED verdict + its inputs
- ``assess(...)``       — pure, read-only: compute a ``HostAssessment``
- ``StepResult``        — one ordered-plan step's outcome (mirrors ``host_provision.StepResult``)
- ``retire(...)``       — the guarded, ordered pipeline: assess -> gate -> release leases ->
  sync+push hives -> reclaim local clones/worktrees -> deregister manifest -> push HQ
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import typer

from . import config, host_cli, host_lease, hosts, hq, registry, safety, sync_remote, worktree

# Aliased: this module's own public orchestrator is ALSO named `retire` (the CLI-facing name,
# `host_retire.retire(...)`) — importing the sibling hive-level module under its own name would
# get shadowed the moment `def retire(...)` below rebinds it in this module's namespace.
from . import retire as hive_retire
from .safety import RetireResult, RetireVerdict
from .wt_status import WtClassification

# The SAME escalation ranking `safety.assess_retire` uses internally — reused, not re-derived,
# so folding several verdicts together can never disagree with what each individual verdict means.
_RANK = safety._RETIRE_RANK

# Worktree classifications that signal real risk beyond what `assess_retire`'s git-level scan
# can see: DIRTY is uncommitted work; ACTIVE/UNMERGED are bead-lifecycle risk (an open bead, or
# a closed one whose content isn't confirmed merged) that a plain git ahead/dirty check misses
# entirely; DETACHED/ABANDONED are commits reachable from no branch bh cares about. SAFE,
# LANDED_REBASED (content confirmed merged), MERGED_ORPHAN, and REVIEW (merged + clean, just
# awaiting a human close) are NOT escalated — their content is already safe.
_WT_ESCALATE = frozenset(
    {
        WtClassification.DIRTY,
        WtClassification.ACTIVE,
        WtClassification.UNMERGED,
        WtClassification.DETACHED,
        WtClassification.ABANDONED,
    }
)


# ---------------------------------------------------------------------------
# assess: the ONE host-level SAFE/NEEDS_BACKUP/BLOCKED verdict (read-only)
# ---------------------------------------------------------------------------


@dataclass
class HiveFold:
    """One hive's fold into the host verdict. ``present=False`` means this host has no local
    clone of it — nothing to assess or reclaim here (it may well be another host's copy);
    such hives never contribute to the host verdict at all."""

    hive: str
    clone_path: str
    present: bool
    verdict: RetireVerdict = RetireVerdict.SAFE
    reasons: list[str] = field(default_factory=list)


@dataclass
class HostAssessment:
    """The full, structured host-level verdict — every field `retire`'s printed plan and gate
    are computed from, so callers/tests can assert without parsing stdout."""

    host_id: str
    hq_dir: str
    verdict: RetireVerdict
    hives: list[HiveFold] = field(default_factory=list)
    worktrees_at_risk: list = field(default_factory=list)
    leases_held: list = field(default_factory=list)
    leases_unreadable: list = field(default_factory=list)
    hq_verdict: RetireVerdict = RetireVerdict.SAFE
    hq_reasons: list[str] = field(default_factory=list)


def _all_worktree_statuses(cfg) -> list:
    """Every managed worktree's classification, across every hive on this host — the SAME "hub
    scope: all managed hives" fold `worktree.status_rows()` falls back to when it can't resolve
    a `hive`/cwd to one entry, but WITHOUT the cwd-sensing: a host-wide verb must never silently
    narrow to whichever hive the operator happens to be sitting in when they run it."""
    all_rows = worktree.managed(cfg)
    rows_by_prefix: dict[str, list] = {}
    for r in all_rows:
        rows_by_prefix.setdefault(r[0], []).append(r)
    statuses: list = []
    for e in cfg.get("managed_repos", []) or []:
        prefix = str(e.get("prefix", ""))
        rows = rows_by_prefix.get(prefix, [])
        if not rows:
            continue
        statuses.extend(worktree._classify_entry(e, rows, cfg))
    return statuses


def _hq_fold(hq_dir: Path) -> RetireResult:
    """HQ's own ahead/behind on BOTH halves, folded into the RetireResult vocabulary — reuses
    `hq.status()`'s scan + line-rendering helpers (bh-z9hl) instead of re-deriving the read, so
    the wording an operator sees here matches `bh hq status` verbatim. Always pays for the real
    `fetch=True` federation check, matching `hq.status()`/`hq.push()`'s own existing choice —
    HQ is one store, not O(n) hives, so the cost is bounded."""
    if not (hq_dir / ".beads").is_dir():
        return RetireResult(
            RetireVerdict.BLOCKED,
            ["Factory HQ has no local bead store — run `bh hq init` or `bh hq clone` first"],
        )
    result = safety.scan(hq_dir, fetch=True)
    if not result.has_origin:
        return RetireResult(
            RetireVerdict.NEEDS_BACKUP,
            ["HQ has no remote configured — fleet config/bead state has no remote backup"],
        )
    branch = hq._hq_main_branch(result)
    dolt = result.dolt_ref
    reasons: list[str] = []
    verdict = RetireVerdict.SAFE
    if branch is None or not branch.has_upstream or branch.ahead or branch.dirty:
        verdict = RetireVerdict.NEEDS_BACKUP
        reasons.append(f"HQ git: {hq._branch_status_line(branch)}")
    if dolt.status in hq._DOLT_PUSHABLE:
        verdict = RetireVerdict.NEEDS_BACKUP
        reasons.append(f"HQ dolt: {hq._dolt_status_line(dolt)}")
    return RetireResult(verdict, reasons)


def assess(
    *, cfg: dict | None = None, hq_dir: Path | None = None, host_id: str | None = None
) -> HostAssessment:
    """Pure, read-only: fold every hive (`safety.assess_retire`), every managed worktree
    (`worktree` classification), every held/unreadable lease (`host_cli._scan_leases`), and HQ's
    own ahead/behind (`_hq_fold`) into the ONE host-level SAFE/NEEDS_BACKUP/BLOCKED verdict.
    Performs no mutation whatsoever — safe to call under `--dry-run` or standalone.

    `cfg`/`hq_dir`/`host_id` are accepted (not always reloaded) so `retire()` computes them once
    and reuses them for both the assessment and the pipeline that follows it.
    """
    cfg = cfg if cfg is not None else config.load()
    hq_dir = hq_dir if hq_dir is not None else host_cli._require_hq_dir()
    host_id = host_id if host_id is not None else host_cli._require_host_id()

    verdict = RetireVerdict.SAFE

    def _escalate(to: RetireVerdict) -> None:
        nonlocal verdict
        if _RANK[to] > _RANK[verdict]:
            verdict = to

    hives: list[HiveFold] = []
    for entry in registry.hives(cfg):
        provider, org, repo = str(entry["provider"]), str(entry["org"]), str(entry["repo"])
        triplet = f"{provider}/{org}/{repo}"
        clone_path = registry.hive_dir(entry)
        if not clone_path.exists():
            # Registered fleet-wide, but this host never cloned it — not this host's problem;
            # never assessed/reclaimed here (see module docstring: host-local scope only).
            hives.append(HiveFold(hive=triplet, clone_path=str(clone_path), present=False))
            continue
        result = safety.assess_retire(clone_path)
        hives.append(
            HiveFold(
                hive=triplet,
                clone_path=str(clone_path),
                present=True,
                verdict=result.verdict,
                reasons=result.reasons,
            )
        )
        _escalate(result.verdict)

    worktrees_at_risk = [
        st for st in _all_worktree_statuses(cfg) if st.classification in _WT_ESCALATE
    ]
    if worktrees_at_risk:
        _escalate(RetireVerdict.NEEDS_BACKUP)

    held, unreadable = host_cli._scan_leases(hq_dir, cfg, host_id=host_id)
    if unreadable:
        # Can't even confirm whether this host holds a lease that would be stranded — a
        # structural unknown, not a mere data-loss risk, so it ranks BLOCKED like `assess_retire`
        # ranks an unassessable repo.
        _escalate(RetireVerdict.BLOCKED)

    hq_result = _hq_fold(hq_dir)
    _escalate(hq_result.verdict)

    return HostAssessment(
        host_id=host_id,
        hq_dir=str(hq_dir),
        verdict=verdict,
        hives=hives,
        worktrees_at_risk=worktrees_at_risk,
        leases_held=held,
        leases_unreadable=unreadable,
        hq_verdict=hq_result.verdict,
        hq_reasons=hq_result.reasons,
    )


def _print_assessment(a: HostAssessment) -> None:
    typer.echo(f"  assess: {a.verdict}")
    for hv in a.hives:
        if not hv.present:
            continue
        typer.echo(f"    hive {hv.hive}: {hv.verdict}")
        for reason in hv.reasons:
            typer.echo(f"      - {reason}")
    if a.worktrees_at_risk:
        typer.echo(f"    worktrees at risk: {len(a.worktrees_at_risk)}")
        for st in a.worktrees_at_risk:
            typer.echo(f"      - {st.hive}/{st.leaf} [{st.branch}] {st.classification}")
    if a.leases_held:
        held_names = ", ".join(prefix for prefix, _lease in a.leases_held)
        typer.echo(f"    leases held: {held_names} (released in step 1 below)")
    if a.leases_unreadable:
        typer.echo("    leases UNREADABLE (HQ unreachable):")
        for prefix, detail in a.leases_unreadable:
            typer.echo(f"      - {prefix}: {detail}")
    typer.echo(f"    HQ ({a.hq_dir}): {a.hq_verdict}")
    for reason in a.hq_reasons:
        typer.echo(f"      - {reason}")


def _gate(assessment: HostAssessment, *, backup: bool, confirm: bool) -> None:
    """Consent gate for the FOLDED host verdict — mirrors `retire._gate_backup`'s SAFE /
    NEEDS_BACKUP / BLOCKED shape at host scope: SAFE proceeds unconditionally; NEEDS_BACKUP
    needs --backup or --confirm; BLOCKED needs --confirm. Raises `typer.Exit(1)` on refusal —
    this fires BEFORE any step in the ordered pipeline runs, `--dry-run` included (mirrors
    `bh hive retire`'s own precedent: previewing PAST the gate still needs an explicit
    --backup/--confirm, so a `--dry-run` alone never silently implies "yes, I accept this")."""
    if assessment.verdict == RetireVerdict.NEEDS_BACKUP:
        if not (backup or confirm):
            typer.echo(
                "✗ refusing: this host has unbacked/unsynced work that would be lost", err=True
            )
            typer.echo(
                "  pass --backup to snapshot it durably, or --confirm to accept the loss",
                err=True,
            )
            raise typer.Exit(1)
    elif assessment.verdict == RetireVerdict.BLOCKED:
        if not confirm:
            typer.echo("✗ refusing: assessment is BLOCKED (see reasons above)", err=True)
            typer.echo("  pass --confirm to override and proceed anyway", err=True)
            raise typer.Exit(1)


# ---------------------------------------------------------------------------
# retire: the guarded, ordered pipeline
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StepResult:
    """One ordered-plan step's outcome — mirrors `host_provision.StepResult`'s shape (its
    sibling verb) for a consistent `--dry-run` "print the ordered plan" rendering."""

    name: str
    status: str  # done | skipped | would | failed
    detail: str = ""


GLYPH: dict[str, str] = {"done": "✓", "skipped": "•", "would": "→", "failed": "✗"}

PLAN: tuple[str, ...] = (
    "release leases",
    "sync + push hives",
    "reclaim local clones/worktrees",
    "deregister host manifest",
    "push HQ",
)


def _step_release_leases(hq_dir, host_id, held, *, dry_run: bool) -> StepResult:
    if not held:
        return StepResult("release leases", "skipped", "nothing held")
    prefixes = [prefix for prefix, _lease in held]
    if dry_run:
        return StepResult(
            "release leases", "would", f"would release {len(held)} hive(s): {', '.join(prefixes)}"
        )
    released: list[str] = []
    failed: list[str] = []
    for prefix, _lease in held:
        try:
            outcome = host_lease.release("origin", prefix, host_id=host_id, cwd=hq_dir)
        except host_lease.HostLeaseRejected as exc:
            failed.append(f"{prefix}: {exc}")
            continue
        host_lease.cache(prefix, outcome, cwd=hq_dir)
        released.append(prefix)
    if failed:
        return StepResult("release leases", "failed", "; ".join(failed))
    return StepResult(
        "release leases", "done", f"released {len(released)} hive(s): {', '.join(released)}"
    )


def _step_sync_hives(assessment: HostAssessment, *, dry_run: bool) -> StepResult:
    """Sync + push every hive's beads AND code. Reaching this step with an offending (dirty/
    blocked) hive necessarily means the top-level gate ALREADY required — and got —
    `--backup`/`--confirm` for it (every status `sync_remote` refuses on is a status
    `assess_retire` also escalates on), so an offending hive already present in the fold
    (`assessment.hives`) is a KNOWN, already-consented risk the reclaim step's own gate
    resolves next — reported `skipped`, not `failed`. An offending hive `assess()` called SAFE
    is a genuine surprise (e.g. a push attempt that failed for an unrelated reason, like a
    transient network error) and DOES count as a step failure."""
    plan = sync_remote.sync_remote(dry_run=dry_run)
    known_risk = {hv.hive for hv in assessment.hives if hv.verdict != RetireVerdict.SAFE}
    surprises = [h for h in plan.offending if h not in known_risk]
    if surprises:
        return StepResult(
            "sync + push hives",
            "failed",
            f"{len(surprises)} hive(s) unexpectedly could not be synced: {', '.join(surprises)}",
        )
    if dry_run:
        return StepResult("sync + push hives", "would", f"{len(plan.records)} hive(s) assessed")
    if plan.offending:
        return StepResult(
            "sync + push hives",
            "skipped",
            f"{len(plan.offending)} hive(s) left unsynced (already-known risk, resolved by "
            f"the reclaim step's own --backup/--confirm gate): {', '.join(plan.offending)}",
        )
    pushed_hives = len(plan.pushed_branches) + len(
        [h for h in plan.dolt_pushed if h not in plan.pushed_branches]
    )
    if not pushed_hives:
        return StepResult(
            "sync + push hives", "skipped", f"{len(plan.records)} hive(s) already clean"
        )
    return StepResult(
        "sync + push hives",
        "done",
        f"pushed git for {len(plan.pushed_branches)} hive(s), dolt for {len(plan.dolt_pushed)}",
    )


def _step_reclaim_hives(
    cfg, *, dry_run: bool, backup: bool, confirm: bool, purge: bool
) -> StepResult:
    present = [
        (f"{e['provider']}/{e['org']}/{e['repo']}", registry.hive_dir(e))
        for e in registry.hives(cfg)
    ]
    present = [(triplet, path) for triplet, path in present if path.exists()]
    if not present:
        return StepResult(
            "reclaim local clones/worktrees", "skipped", "no local hive clones present"
        )

    ok: list[str] = []
    failed: list[str] = []
    for triplet, _path in present:
        try:
            hive_retire.reclaim_hive(
                triplet, dry_run=dry_run, backup=backup, confirm=confirm, purge=purge
            )
        except typer.Exit:
            failed.append(triplet)
            continue
        ok.append(triplet)

    if failed:
        return StepResult(
            "reclaim local clones/worktrees",
            "failed",
            f"{len(failed)}/{len(present)} hive(s) refused: {', '.join(failed)}",
        )
    status = "would" if dry_run else "done"
    return StepResult(
        "reclaim local clones/worktrees", status, f"{len(ok)} hive(s): {', '.join(ok)}"
    )


def _step_deregister(hq_dir, host_id, label: str, held_after, *, dry_run: bool) -> StepResult:
    if dry_run:
        return StepResult("deregister host manifest", "would", f"would remove hosts/{host_id}.yaml")
    if held_after:
        prefixes = ", ".join(prefix for prefix, _lease in held_after)
        return StepResult(
            "deregister host manifest",
            "failed",
            f"still holds live lease(s), refusing to deregister: {prefixes}",
        )
    try:
        removed = hosts.remove(hq_dir, host_id)
    except FileNotFoundError:
        return StepResult(
            "deregister host manifest", "skipped", "no manifest registered for this host"
        )
    hq._commit_if_dirty(hq_dir, f"chore(host): retire {host_id} ({label})")
    return StepResult("deregister host manifest", "done", f"removed {removed}")


def _step_push_hq(*, dry_run: bool) -> StepResult:
    try:
        hq.push(dry_run=dry_run)
    except typer.Exit:
        return StepResult("push HQ", "failed", "hq push failed — see output above")
    return StepResult("push HQ", "would" if dry_run else "done", "")


def retire(
    *, dry_run: bool = False, backup: bool = False, confirm: bool = False, purge: bool = False
) -> list[StepResult]:
    """The guarded, ordered host decommission: assess -> gate -> release leases -> sync+push
    every hive -> reclaim local clones/worktrees (`retire.reclaim_hive`, host-local, NEVER
    `retire.retire_hive`/`registry.unregister`) -> deregister this host's own manifest -> push
    HQ (both halves), publishing the lease releases and manifest removal in one durable push.

    `--dry-run` prints the full ordered plan and performs ZERO mutation (default-safe mindset,
    same as `bh hive retire`) — but, matching that verb's own precedent, still needs
    `--backup`/`--confirm` to get PAST a NEEDS_BACKUP/BLOCKED gate; a bare `--dry-run` on an
    at-risk host stops at the assessment and refusal, same as a live run would.

    One step's failure — refused or an unexpected exception — never aborts the rest (mirrors
    `host_provision.provision`'s own resilience: a single hive's refusal must not also block
    releasing leases, or deregistering the manifest, or publishing HQ, for the OTHER, unrelated
    hives/steps that are fine). Callers (the CLI) decide the process exit code from the
    returned results.

    Raises `typer.Exit(1)` when this host has no local Factory HQ clone / no `host.yaml`
    identity yet (nothing to retire), or when the pre-pipeline gate refuses.
    """
    cfg = config.load()
    hq_dir = host_cli._require_hq_dir()
    host_id = host_cli._require_host_id()
    try:
        manifest = hosts.load(hq_dir, host_id)
        label = manifest.label
    except (FileNotFoundError, hosts.ManifestError):
        label = host_id

    tag = "DRY-RUN " if dry_run else ""
    typer.echo(f"{tag}host retire — {host_id} ({label})")

    assessment = assess(cfg=cfg, hq_dir=hq_dir, host_id=host_id)
    _print_assessment(assessment)

    _gate(assessment, backup=backup, confirm=confirm)

    held, _unreadable = host_cli._scan_leases(hq_dir, cfg, host_id=host_id)
    # Mirrors `PLAN`'s own order exactly (zipped below) — one canonical source of the ordering.
    step_fns = (
        lambda: _step_release_leases(hq_dir, host_id, held, dry_run=dry_run),
        lambda: _step_sync_hives(assessment, dry_run=dry_run),
        lambda: _step_reclaim_hives(
            cfg, dry_run=dry_run, backup=backup, confirm=confirm, purge=purge
        ),
        lambda: _step_deregister(
            hq_dir,
            host_id,
            label,
            [] if dry_run else host_cli._scan_leases(hq_dir, cfg, host_id=host_id)[0],
            dry_run=dry_run,
        ),
        lambda: _step_push_hq(dry_run=dry_run),
    )

    results: list[StepResult] = []
    for name, step in zip(PLAN, step_fns, strict=True):
        try:
            results.append(step())
        except Exception as exc:  # noqa: BLE001 - one step's crash must not abort the whole run
            results.append(StepResult(name, "failed", f"unexpected error: {exc}"))

    typer.echo(f"\n{tag}ordered plan:")
    for i, r in enumerate(results, start=1):
        tail = f" — {r.detail}" if r.detail else ""
        typer.echo(f"  {i}. {GLYPH[r.status]} {r.name}{tail}")

    if dry_run:
        typer.echo("\nDRY-RUN — no changes made.")
    elif any(r.status == "failed" for r in results):
        typer.echo("\n✗ host retire incomplete — see the failed step(s) above.", err=True)
    else:
        typer.echo(f"\n✓ host {host_id} retired — safe to decommission.")
    return results
