"""sync_remote.py — guarded fleet-wide push+verify before switching physical hosts.

`bh hive sync-remote --all` is the "one command an operator runs before walking away from a
physical host" so another host can pick up bd/bh state cleanly via `bh -a bd dolt pull` +
`git pull`, with no silent gaps. It runs the dolt-ref-aware safety scan (``safety.scan``)
across every registered hive, classifies each as clean / dirty / unpushed-git / unpushed-dolt
/ blocked, and — outside ``--dry-run`` — pushes what's safe to push. Follows the guarded
dry-run/gate pattern established by ``retire.retire_hive`` (a repo/hive must never be pushed
over silently when its working tree is dirty).

HQ (``kind=hq``) is local-only by design (no origin) and is skipped entirely — same filter
``hub.sync`` applies — instead of misclassifying it BLOCKED and failing a clean fleet.

The fleet assessment is deliberately local/read-only.  A Dolt remote and a federation peer are
different configuration surfaces: ``sync remotes`` must not try to verify a remote named
``origin`` through ``bd federation status``.  The embedded engine therefore reports its normal
honest ``unknown`` state during assessment and the subsequent ``bd dolt pull``/``push`` talks to
the configured Dolt remote.

Exported API
------------
- ``SyncStatus``       — clean | dirty | unpushed-git | unpushed-dolt | remote-only | blocked
- ``HiveSyncRecord``    — one hive's classification + reasons (read-only assessment)
- ``SyncPlan``          — structured outcome of ``sync_remote`` (what happened / would happen)
- ``assess_hive(hive_id, clone_path, *, fetch=False)`` — pure, read-only classification
- ``sync_remote(*, dry_run=False)`` — the guarded fleet-wide sync orchestrator
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

import typer

from . import bd, config, engine, fleet, registry
from .run import run
from .safety import Category, DoltRefInfo, scan

# Worst-first: BLOCKED and DIRTY both refuse to sync this hive (offending); UNPUSHED_GIT /
# UNPUSHED_DOLT are safe-to-push states (would be pushed in a live run); CLEAN needs nothing.
_RANK: dict[str, int] = {
    "clean": 0,
    "remote-only": 0,
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
    REMOTE_ONLY = "remote-only"
    BLOCKED = "blocked"


# Statuses this hive can never be safely synced under (refused, not merely "has work to push").
_OFFENDING = frozenset({SyncStatus.DIRTY, SyncStatus.BLOCKED})

# dolt_status values that mean "has something to push" — shared by the dry-run preview and the
# live-run push gate so the two can never drift apart again (bh-jhu0: dry-run used to fire on
# `absent`/never-bootstrapped hives too, since it only checked `!= "clean"`).
# "unknown" is bd's embedded/local engine's default state (bh-fl26): a Dolt remote is
# configured but bd has no read-only ahead/behind primitive — treated the same as "ahead"
# (attempt the idempotent `bd dolt push` and trust its own success/failure).
# "diverged" IS IN HERE ON PURPOSE AND IS STILL WRONG (bh-efcu1): bh will attempt a push against
# a ref that moved underneath it instead of reconciling first. git rejects the non-fast-forward,
# so nothing corrupts — the operator just gets a push failure where they should have got a
# reconcile. The real fix is replacing the hand-rolled push leg with `bd sync`, which knows how;
# bh-89wxf.2 narrowed the blast radius (HQ no longer MANUFACTURES divergence on every host's
# aggregation refresh) without removing it. Tracked, not closed by implication.
_DOLT_PUSHABLE = frozenset({"ahead", "diverged", "no-remote", "unknown"})

# bh-5rn7: bd has no read-only remote-diff primitive for the embedded engine (no `bd dolt
# fetch`), so an "unknown" dolt_status can't be resolved to an exact unpushed-bead set. As a
# bounded, honest approximation, `--verbose` instead surfaces recently-*touched* beads — a
# fixed 24h lookback (simpler than tracking "since last successful push", which bd also has no
# reliable read-only way to determine) capped to a handful of entries. This is content context,
# not a diff: some listed beads may already be pushed.
_RECENT_LOOKBACK = timedelta(hours=24)
_RECENT_LIMIT = 8

# Bounded fan-out for the read-only assessment pass; the pushes themselves stay strictly serial.
_ASSESS_WORKERS = 4


def _dolt_reason(dolt: DoltRefInfo) -> str:
    """Human-readable reason line for a pushable dolt state — "unknown" (couldn't be verified:
    unreachable peer, timeout, or — on the no-network path — bd's embedded engine, bh-fl26)
    gets its own honest wording instead of the git-ref-flavored message. Verified ahead/behind
    counts (a ``fetch=True`` federation check) and ``DoltRefInfo.reason`` are appended when
    present."""
    if dolt.status == "unknown":
        detail = dolt.reason or "embedded engine — no read-only check ran"
        return f"dolt state could not be verified ({detail}); would attempt idempotent bd dolt push"
    if dolt.status == "behind":
        detail = f"{dolt.behind} behind" if dolt.behind else "behind"
        return f"refs/dolt/data is {detail} origin — pull first (bd dolt pull)"
    if dolt.status == "diverged" and not dolt.ahead and not dolt.behind:
        # git-transport dolt remote (refs/dolt/data exists locally) whose remote tip isn't
        # locally resolvable without a fetch — ls-remote proved the shas differ, but real
        # ahead/behind counts would require a transfer (bh-ummb9.3: no-transfer constraint).
        # Known-not-in-sync, direction unproven: name the same safe remedy as a confirmed
        # "behind" rather than silently defaulting to an idempotent push attempt.
        return (
            "refs/dolt/data has diverged from origin (ahead/behind not resolvable without a "
            "fetch) — could be behind; pull first (bd dolt pull)"
        )
    msg = f"refs/dolt/data: {dolt.status}"
    counts = [f"{n} {label}" for n, label in ((dolt.ahead, "ahead"), (dolt.behind, "behind")) if n]
    if counts:
        msg += f" ({', '.join(counts)})"
    if dolt.reason:
        msg += f" — {dolt.reason}"
    return msg


@dataclass
class HiveSyncRecord:
    """One hive's read-only sync assessment (mirrors ``safety.RetireResult``'s shape)."""

    hive: str
    clone_path: str
    status: SyncStatus
    reasons: list[str] = field(default_factory=list)
    unpushed_branches: list[str] = field(default_factory=list)
    dolt_status: str = "absent"


def assess_hive(
    hive_id: str, clone_path: Path, *, fetch: bool = False, remote_only: bool = False
) -> HiveSyncRecord:
    """Pure, read-only classification of one hive — never mutates anything.

    Reuses ``safety.scan`` (the dolt-ref-aware safety scan from bh-59q1.1) rather than
    re-deriving git/dolt state. Precedence: a missing clone / non-repo / no-origin hive is
    ``BLOCKED`` (cannot be safely assessed at all); any dirty branch makes the whole hive
    ``DIRTY`` (refused, even if it also has unpushed commits); otherwise unpushed git commits
    or an unpushed/diverged ``refs/dolt/data`` make it ``UNPUSHED_GIT`` / ``UNPUSHED_DOLT``;
    else ``CLEAN``.

    When ``remote_only=True``, a missing clone is an intentional all-fleet omission: its
    bead state is hydrated separately by ``bh sync`` into a minimal cache, so this git-working-
    tree sync reports it and moves on. A named hive remains strict (the default): a missing
    path is ``BLOCKED`` because that caller explicitly requested its local checkout.

    ``fetch=True`` (still read-only, but pays a real network fetch) consults ``bd federation
    status`` for the dolt state, yielding verified ahead/behind counts instead of the
    no-network path's ``unknown``. The default ``False`` keeps today's no-network behavior.
    """
    if not clone_path.exists():
        return HiveSyncRecord(
            hive=hive_id,
            clone_path=str(clone_path),
            status=SyncStatus.REMOTE_ONLY if remote_only else SyncStatus.BLOCKED,
            reasons=[
                "no local checkout — remote-only hive (hydrate with `bh sync`)"
                if remote_only
                else "clone path does not exist"
            ],
        )

    record = scan(clone_path, fetch=fetch)

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
            reasons.append(_dolt_reason(dolt))
        elif dolt.status == "behind":
            reasons.append(_dolt_reason(dolt))
    elif dolt_unpushed:
        status = SyncStatus.UNPUSHED_DOLT
        reasons.append(_dolt_reason(dolt))
    else:
        # "behind" is deliberately NOT in _DOLT_PUSHABLE (there is nothing local to push) —
        # but it's still a real, actionable state (bh-ummb9.3): surface it as a labelled
        # reason on an otherwise-CLEAN hive rather than staying silent, so "pull first" is
        # visible in both --dry-run and a live run.
        status = SyncStatus.CLEAN
        if dolt.status == "behind":
            reasons.append(_dolt_reason(dolt))

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
    auto_merges: dict[str, list[str]] = field(default_factory=dict)


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


def _recently_touched(clone_path: Path) -> list[dict]:
    """Bounded, best-effort ``bd list`` of beads updated within ``_RECENT_LOOKBACK``, scoped to
    this hive's clone (``bd -C <clone_path> …``, same convention as every other ``bd`` call in
    this module). NOT a precise unpushed-bead diff — see the ``_RECENT_LOOKBACK`` docstring
    above — just content context for a hive bd can't read-only-diff against its remote. Returns
    ``[]`` on any failure (never raises; this is best-effort context, not a gate)."""
    cutoff = (datetime.now(UTC) - _RECENT_LOOKBACK).strftime("%Y-%m-%dT%H:%M:%SZ")
    data = bd.json(
        [
            "list",
            "--all",
            "--updated-after",
            cutoff,
            "--sort",
            "updated",
            "--reverse",
            "-n",
            str(_RECENT_LIMIT),
        ],
        clone_path,
    )
    return data if isinstance(data, list) else []


def _echo_reason(reason: str) -> None:
    """Print a (possibly multi-line) failure reason as indented lines, matching the existing
    ``    - <reason>`` per-hive convention — one bullet per line rather than collapsing a
    multi-line dolt stderr block onto one."""
    for line in reason.splitlines():
        typer.echo(f"    - {line}", err=True)


def _dolt_stderr(result) -> str:
    """The full captured stderr from a ``bd dolt push``/``pull``, verbatim — NOT the last line.

    Unlike raw ``git push`` (see ``_last_stderr_line``), ``bd dolt push``/``pull`` WRAP the
    underlying error and APPEND their own hint text after it (e.g. a non-fast-forward's real
    complaint sits mid-output, followed by a generic "hint: ... before pushing again." tail; a
    missing git-remote-cache clone is followed by unrelated "ensure non-interactive auth"
    advice). Taking the last line there silently swaps the real cause for boilerplate — worse
    than no reason at all, since it looks specific while being wrong. Print everything bd said
    and let the operator read it, per the brief: surface verbatim, do not classify."""
    return (result.stderr or "").strip()


def _push_dolt_state(
    cfg, clone_path: Path, *, remote: str = "", force: bool = False
) -> tuple[bool, str]:
    """Push this hive's ``refs/dolt/data`` via the configured ``Engine`` (bh-dw3e.6 wiring).
    Returns ``(ok, reason)`` — ``reason`` is bd's full captured stderr on failure (see
    ``_dolt_stderr``), empty on success."""
    result = engine.get_engine(cfg).push_state(
        clone_path, message=f"sync-remote {clone_path}", remote=remote, force=force
    )
    ok = result.returncode == 0
    return ok, ("" if ok else _dolt_stderr(result))


def _auto_merge_notices(result) -> list[str]:
    """Lines from a ``bd dolt pull`` result that report an auto-merge (measured wording:
    ``Notice: auto-merged issue [...]; updated_at settled last-write-wins (the older side's
    edit was superseded)``) — a pull is not inert, and a silent last-write-wins resolution
    must never be swallowed. Matched on the substring ``auto-merged`` rather than the full
    ``Notice:`` prefix, since that's the one part of the wording this is actually about."""
    lines = ((result.stdout or "") + (result.stderr or "")).splitlines()
    return [line.strip() for line in lines if "auto-merged" in line.lower()]


def _pull_dolt_state(cfg, clone_path: Path, *, remote: str = "") -> tuple[bool, str, list[str]]:
    """Pull this hive's ``refs/dolt/data`` via the configured ``Engine``. Returns
    ``(ok, reason, auto_merge_notices)``: ``reason`` is bd's full captured stderr on failure
    (see ``_dolt_stderr``), empty on success; ``notices`` are extracted regardless of
    success/failure, since an auto-merge can happen on a pull that otherwise reports success.
    Mutating — never called under ``--dry-run``."""
    result = engine.get_engine(cfg).pull_state(clone_path, remote=remote)
    ok = result.returncode == 0
    return ok, ("" if ok else _dolt_stderr(result)), _auto_merge_notices(result)


def _resolve_targets(cfg, hive_ids: list[str] | None) -> list[tuple[str, Path]]:
    """The ``(hive_id, clone_path)`` pairs this run addresses: every registered hive (HQ
    skipped, as today) when ``hive_ids`` is empty/None, else exactly the resolved hives named —
    each resolved via ``registry.resolve_hive`` (prefix/triplet/flexible match), refusing HQ the
    same way ``hive_sync._targets`` does."""
    real_hives = registry.hives(cfg)
    if not hive_ids:
        targets: list[tuple[str, Path]] = []
        for entry in cfg.get("managed_repos", []) or []:
            if entry not in real_hives:
                typer.echo("  (skipping HQ — local-only by design)")
                continue
            provider, org, repo = str(entry["provider"]), str(entry["org"]), str(entry["repo"])
            targets.append((f"{provider}/{org}/{repo}", registry.hive_dir(entry)))
        return targets

    targets = []
    for hive_id in hive_ids:
        entry = registry.resolve_hive(cfg, hive_id)
        if entry not in real_hives:
            typer.echo("✗ HQ is local-only by design — it has no remote to sync", err=True)
            raise typer.Exit(1)
        provider, org, repo = str(entry["provider"]), str(entry["org"]), str(entry["repo"])
        targets.append((f"{provider}/{org}/{repo}", registry.hive_dir(entry)))
    return targets


def sync_remote(
    *,
    dry_run: bool = False,
    verbose: bool = False,
    hive_ids: list[str] | None = None,
    pull: bool = False,
    push: bool = True,
    remote: str = "",
    force: bool = False,
) -> SyncPlan:
    """Guarded fleet-wide sync: assess every registered hive, then (outside ``--dry-run``) push
    what's safe to push.

    Every hive is assessed with ``assess_hive`` (read-only). A ``DIRTY`` or ``BLOCKED`` hive is
    refused — never pushed — and lands in ``plan.offending``. Every other hive that isn't
    already ``CLEAN`` gets its unpushed git branches pushed (``git push origin <branch>``) and
    its dolt state pushed (``Engine.push_state``); a push failure also lands the hive in
    ``plan.offending`` (no silent partial success). ``--dry-run`` performs zero mutation.

    HQ (``kind=hq``) is skipped up front — local-only by design, matching ``hub.sync``'s
    filter — so a clean fleet can't exit non-zero just because HQ has no origin.

    Assessment runs first, in parallel (``_ASSESS_WORKERS`` threads), using only local and
    git-remote-aware checks.  It deliberately does not call ``bd federation status``: that
    probes federation peers, while this command synchronizes configured ``bd dolt`` remotes.
    The loop below then consumes the precomputed records in deterministic config order, and all
    pushes stay strictly serial.

    ``verbose=True`` (bh-5rn7) is purely additive to the default output: for a hive classified
    ``UNPUSHED_DOLT`` with ``dolt_status == "unknown"`` (the dolt state couldn't be verified —
    offline peer, timeout), it also runs one extra bounded ``bd list`` query (see
    ``_recently_touched``) and prints its result as labeled approximation context. Every
    other hive — and every hive when ``verbose`` is false — makes no extra query at all.

    Prints a per-hive summary line plus a final count, mirroring ``retire_hive``'s reporting
    style. Never raises — callers (the CLI) decide whether ``plan.offending`` should exit
    non-zero.
    """
    cfg = config.load()
    plan = SyncPlan(dry_run=dry_run)

    tag = "DRY-RUN " if dry_run else ""
    typer.echo(f"{tag}sync-remote --all" if not hive_ids else f"{tag}sync-remote {hive_ids}")

    targets = _resolve_targets(cfg, hive_ids)

    # Parallel read-only pre-assessment.  Do not set ``fetch=True`` here: that is a
    # federation-peer probe, whereas this command operates on ``bd dolt`` remotes. SHAPE B
    # (`fleet.fanout`) preserves input order, so output stays in deterministic config order.
    # ``--all`` deliberately includes registry-only hives. They have no checkout to assess or
    # push here; `bh sync` owns hydrating their minimal bead cache. A named hive is different:
    # specifying it promises that its local checkout is the target, so retain the BLOCKED error
    # for a missing/invalid path.
    remote_only_ok = not hive_ids
    records = fleet.fanout(
        lambda t: assess_hive(t[0], t[1], remote_only=remote_only_ok),
        targets,
        workers=_ASSESS_WORKERS,
    )

    # Pull-then-push (bh-ummb9.1 wiring; the pull leg's own behaviour — auto-merge reporting,
    # per-remote nuance, dry-run ahead/behind — is bh-ummb9.2/.3's job, not this one's). Never
    # runs under --dry-run (mutates), and only for a hive with an actual dolt remote —
    # "absent"/"no-remote" mirrors the push leg's own `_DOLT_PUSHABLE` gate, so a plain git
    # clone (or a bd store with no configured remote) is never handed to `bd dolt pull`.
    pull_failed: set[str] = set()
    if pull and not dry_run:
        for (hive_id, clone_path), record in zip(targets, records, strict=True):
            if record.status == SyncStatus.REMOTE_ONLY:
                continue
            if record.dolt_status in ("absent", "no-remote", None):
                continue
            ok, reason, auto_merges = _pull_dolt_state(cfg, clone_path, remote=remote)
            if ok:
                typer.echo(f"  {hive_id}: pulled")
            else:
                typer.echo(f"  {hive_id}: ✗ failed to pull dolt: refs/dolt/data", err=True)
                if reason:
                    _echo_reason(reason)
                pull_failed.add(hive_id)
                plan.offending.append(hive_id)
            for notice in auto_merges:
                # Surfaced regardless of pull success/failure — an auto-merge is a real,
                # already-committed last-write-wins resolution the operator must see, not
                # something the pull's own pass/fail verdict should swallow.
                typer.echo(f"  {hive_id}: ⚠ {notice}")
                plan.auto_merges.setdefault(hive_id, []).append(notice)

    for (hive_id, clone_path), record in zip(targets, records, strict=True):
        plan.records.append(record)

        typer.echo(f"  {hive_id}: {record.status}")
        for reason in record.reasons:
            typer.echo(f"    - {reason}")

        # bh-ummb9.1 review (changes-requested): --dry-run must NAME the pull leg it will run,
        # not just the push leg — a pull is not inert (it can auto-merge, LWW). Mirrors the
        # live pull loop's own eligibility exactly (same dolt_status gate, independent of this
        # hive's push-worthiness/offending status — the live loop pulls every eligible hive
        # regardless), so a hive that won't really be pulled is never described as if it would.
        if dry_run and pull and record.dolt_status not in ("absent", "no-remote", None):
            typer.echo(f"    would pull{', then push' if push else ''}: refs/dolt/data")

        is_unknown_dolt = (
            record.status == SyncStatus.UNPUSHED_DOLT and record.dolt_status == "unknown"
        )
        if verbose and is_unknown_dolt:
            recent = _recently_touched(clone_path)
            if recent:
                typer.echo(
                    "    recently touched (not a precise diff — bd has no read-only "
                    "remote-diff primitive for this engine):"
                )
                for item in recent:
                    typer.echo(f"      - {item.get('id', '?')}: {item.get('title', '')}")

        if record.status in _OFFENDING:
            plan.offending.append(hive_id)
            continue

        if record.status in (SyncStatus.CLEAN, SyncStatus.REMOTE_ONLY):
            continue

        if not push:
            typer.echo("    (--pull only: push skipped)")
            continue

        if hive_id in pull_failed:
            # Already reported + offending above; don't compound with a push against
            # possibly-stale local state.
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
            elif record.dolt_status == "diverged":
                # The reason line above already names this (see _dolt_reason) — an
                # idempotent push would still be attempted, but call out the real risk of
                # a non-fast-forward rejection instead of the plain "would push dolt" line.
                typer.echo(
                    "    would attempt: bd dolt push — refs/dolt/data diverged from "
                    "origin; may be rejected as non-fast-forward (pull first)"
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
            ok, reason = _push_dolt_state(cfg, clone_path, remote=remote, force=force)
            if ok:
                plan.dolt_pushed.append(hive_id)
                typer.echo("    pushed dolt: refs/dolt/data")
            else:
                failed_here = True
                typer.echo("    ✗ failed to push dolt: refs/dolt/data", err=True)
                if reason:
                    _echo_reason(reason)

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
