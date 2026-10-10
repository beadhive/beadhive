"""Removal and SAFE-prune effect coordination for managed worktrees.

This module executes effects only after the facade's inventory and :mod:`beadhive.wt_status`
policy have classified them. Terminal/reaping policy remains owned by those existing layers.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import typer

from beadhive_worktrees import (
    execute_removal,
    reclaim_empty_parents,
    split_safe_skipped,
    withhold_untrustworthy,
)

from . import registry, worktree_state_adapters, wt_status
from .config_consumer_ports import work_settings as config


def _facade():
    from . import worktree

    return worktree


def _call_facade(name, *args, **kwargs):
    return getattr(_facade(), name)(*args, **kwargs)


def _rmdir_empty_parents(*args, **kwargs):
    return _call_facade("_rmdir_empty_parents", *args, **kwargs)


def _refuse_unknown_removal(*args, **kwargs):
    return _call_facade("_refuse_unknown_removal", *args, **kwargs)


def remove(*args, **kwargs):
    return _call_facade("remove", *args, **kwargs)


def _prune_load_entries(*args, **kwargs):
    return _call_facade("_prune_load_entries", *args, **kwargs)


def _prune_sweep_orphans(*args, **kwargs):
    return _call_facade("_prune_sweep_orphans", *args, **kwargs)


def _prune_classify(*args, **kwargs):
    return _call_facade("_prune_classify", *args, **kwargs)


def _prune_withhold_untrustworthy(*args, **kwargs):
    return _call_facade("_prune_withhold_untrustworthy", *args, **kwargs)


def _prune_report_skipped(*args, **kwargs):
    return _call_facade("_prune_report_skipped", *args, **kwargs)


def _prune_remove_one(*args, **kwargs):
    return _call_facade("_prune_remove_one", *args, **kwargs)


def _prune_remove_all(*args, **kwargs):
    return _call_facade("_prune_remove_all", *args, **kwargs)


def prune(*args, **kwargs):
    return _call_facade("prune", *args, **kwargs)


def _worktree_lifecycle_service(*args, **kwargs):
    return _call_facade("_worktree_lifecycle_service", *args, **kwargs)


def _remove_worktree(*args, **kwargs):
    return _call_facade("_remove_worktree", *args, **kwargs)


def _leaf(*args, **kwargs):
    return _call_facade("_leaf", *args, **kwargs)


def _record_wt_event(*args, **kwargs):
    return _call_facade("_record_wt_event", *args, **kwargs)


def _record_wt_op_duration(*args, **kwargs):
    return _call_facade("_record_wt_op_duration", *args, **kwargs)


def _resolve_entry(*args, **kwargs):
    return _call_facade("_resolve_entry", *args, **kwargs)


def _run_git(*args, **kwargs):
    return _call_facade("_run_git", *args, **kwargs)


def wt_dir(*args, **kwargs):
    return _call_facade("wt_dir", *args, **kwargs)


def managed(*args, **kwargs):
    return _call_facade("managed", *args, **kwargs)


def _classify_entry(*args, **kwargs):
    return _call_facade("_classify_entry", *args, **kwargs)


def _classify_entries(*args, **kwargs):
    return _call_facade("_classify_entries", *args, **kwargs)


def sweep_verify_dirs(*args, **kwargs):
    return _call_facade("sweep_verify_dirs", *args, **kwargs)


def _warn_untrustworthy(*args, **kwargs):
    return _call_facade("_warn_untrustworthy", *args, **kwargs)


def impl__rmdir_empty_parents(leaf_path, cfg):
    """Climb from a removed worktree's parent toward the shadow root, removing now-empty
    triplet dirs — ``beadhive_worktrees.reclaim_empty_parents`` (bh-qdezo.7, moved verbatim).
    Disabled by `worktrees.rmdir_empty: false` (absent ⇒ enabled); that config read, like the
    shadow root itself, stays root's job."""
    reclaim_empty_parents(
        leaf_path,
        config.worktrees_root(),
        enabled=config.worktrees_cfg(cfg).get("rmdir_empty", True),
    )


def impl__refuse_unknown_removal(cfg, entry, target: Path, *, force: bool) -> None:
    """Refuse to remove a worktree whose bead could not be RESOLVED (bh-167s0), unless forced.

    ``rm`` names one target, so "unattended" does not bite here the way it does for ``prune`` —
    but the operator's premise does.  Whoever types ``rm`` decided this seat was disposable, and
    on the hive that produced this bead that decision would have been made against rows reading
    ACTIVE that were neither active nor readable.  An UNKNOWN row means bh cannot say what is on
    that branch; refusing is the only answer that is not a guess.

    ``--force`` still removes it, deliberately: the operator may know exactly what this is (they
    just retired the prefix, say), and a preflight that cannot be overridden becomes a preflight
    people route around.  The refusal names ``--force`` so the escape is not a secret.

    Best-effort by construction — anything that goes wrong deciding this (an unregistered path,
    a git failure) leaves the removal alone rather than blocking it.  A safety check that turns
    an ordinary ``rm`` into a crash is a check that gets deleted.
    """
    if force:
        return
    try:
        rows = [r for r in managed(cfg) if Path(r[1]) == target]
        if not rows:
            return
        statuses = _classify_entry(entry, rows, cfg)
    except Exception:  # noqa: BLE001 — never let the preflight itself fail the verb
        return
    unknown = wt_status.untrustworthy(statuses)
    if not unknown:
        return
    st = unknown[0]
    typer.echo(
        f"✗ refusing to remove {target}: its bead ({st.bead_id}) could NOT BE RESOLVED, so bh "
        f"cannot tell you whether this worktree holds unmerged work.",
        err=True,
    )
    if st.unknown_reason:
        typer.echo(f"    {st.unknown_reason}", err=True)
    typer.echo(
        "  Resolve the hive's bead store (or the branch name) and re-check with "
        f"`{config.BINARY_ALIAS} worktree status`; `--force` removes it anyway.",
        err=True,
    )
    raise typer.Exit(1)


def impl_remove(hive, ref, force=False, as_json=False):
    """Remove one managed worktree through the selected ``worktrees.manager``. The branch is the
    durable artifact here (a bead's history lives on it) and a manager's ``remove`` never
    deletes it — never call this for a disposable prune removal (see `prune`). `as_json`
    (bh-73rz.4) emits the same `{op, hive, path, removed}` machine-readable shape an external
    orchestrator's preview→create→…→remove flow parses, mirroring `add --json`.

    The claim-record resolve/retire sequencing around the ONE removal effect
    (`_remove_worktree`, which releases any recorded presentation binding before the native
    remove per E30/bh-cb4jo.2) is `beadhive_worktrees.execute_removal` (bh-qdezo.7, moved
    verbatim); telemetry, the CLI exit-code contract, and metadata invalidation stay here."""
    cfg = config.load()
    entry = _resolve_entry(cfg, hive)
    main = registry.hive_dir(entry)
    target = wt_dir(entry, _leaf(ref))

    hive_key = registry.hive_key(entry)
    hive = str(entry.get("prefix", ""))
    _refuse_unknown_removal(cfg, entry, target, force=force)
    started = time.monotonic()
    outcome = execute_removal(
        lambda: _remove_worktree(cfg, entry, main, target, force=force),
        claim_records=worktree_state_adapters.CLAIM_RECORDS,
        target=target,
    )
    elapsed = time.monotonic() - started
    if not outcome.ok:
        _record_wt_event("remove", "error", hive=hive, leaf=target.name)
        _record_wt_op_duration("remove", elapsed, "error", hive=hive, leaf=target.name)
        raise typer.Exit(outcome.returncode)
    _rmdir_empty_parents(target, cfg)
    _record_wt_op_duration("remove", elapsed, "ok", hive=hive, leaf=target.name)
    _record_wt_event("remove", hive=hive, leaf=target.name)
    from . import metadata

    metadata.invalidate(cfg, registry.hive_key(entry))  # branch/worktree churn on this hive
    if as_json:
        typer.echo(
            json.dumps(
                {"op": "rm", "hive": hive_key, "path": str(target), "removed": True}, indent=2
            )
        )
        return
    typer.echo(f"✓ removed {target}")


def impl__prune_load_entries(cfg) -> tuple:
    """`(mains, keys, entries_by_prefix)` lookup tables, keyed by hive prefix, for every
    managed_repos entry — the per-hive tables `prune` needs regardless of `--hive` scoping."""
    mains: dict[str, Path] = {}
    keys: dict[str, str] = {}
    entries_by_prefix: dict[str, dict] = {}
    for e in cfg.get("managed_repos", []) or []:
        p = str(e["prefix"])
        mains[p] = registry.hive_dir(e)
        keys[p] = registry.hive_key(e)
        entries_by_prefix[p] = e
    return mains, keys, entries_by_prefix


def impl__prune_sweep_orphans(entries_by_prefix: dict, want: str | None) -> int:
    """Reap orphaned ephemeral verify-* clean-checkout dirs across in-scope hives (bh-nikb) before
    classification: they are not classifier rows to prune (detached, no bead) — the marker-based
    liveness sweep shared with `clean_checkout` is what reclaims them. Live ones are always spared
    and simply show up as DETACHED skips. Returns the number reaped."""
    return sum(
        sweep_verify_dirs(e) for p, e in entries_by_prefix.items() if want is None or p == want
    )


def impl__prune_classify(cfg, entries_by_prefix: dict, rows: list) -> tuple:
    """Classify every candidate row concurrently. Returns `(safe_set, skipped)` — the
    SAFE-to-remove and NOT-SAFE ``WtStatus`` lists.

    The concurrent classification stays root-adjacent orchestration (it drives git/bd I/O
    through the classifier); the post-classification split itself is
    ``beadhive_worktrees.split_safe_skipped`` (bh-qdezo.7, moved verbatim)."""
    prefixes = list(dict.fromkeys(r[0] for r in rows))
    entries = [entries_by_prefix[prefix] for prefix in prefixes if prefix in entries_by_prefix]
    rows_by_prefix: dict[str, list] = {}
    for row in rows:
        rows_by_prefix.setdefault(row[0], []).append(row)
    statuses_by_prefix = _classify_entries(cfg, entries, rows_by_prefix)

    all_statuses = [status for prefix in prefixes for status in statuses_by_prefix.get(prefix, [])]
    return split_safe_skipped(all_statuses)


def impl__prune_withhold_untrustworthy(
    safe_set: list, skipped: list
) -> tuple[list, list, set[str]]:
    """Drop every hive carrying an UNKNOWN row out of the removal set (bh-167s0) —
    ``beadhive_worktrees.withhold_untrustworthy`` (bh-qdezo.7, moved verbatim; see that
    function's docstring for the bh-167s0 rationale)."""
    return withhold_untrustworthy(safe_set, skipped)


def impl__prune_report_skipped(skipped: list) -> None:
    """Echo the '<n> skipped (not SAFE)' block `prune` renders both on the early "nothing to
    prune" return and after a real removal pass."""
    if skipped:
        typer.echo(f"  {len(skipped)} skipped (not SAFE):")
        for s in skipped:
            if getattr(s, "precious", ()):
                paths = ", ".join(f"{item.path} ({item.bytes} bytes)" for item in s.precious)
                base = s.underlying or s.classification
                typer.echo(f"    {s.leaf}  HELD (base: {base}; precious: {paths})")
            else:
                typer.echo(f"    {s.leaf}  {s.classification}")


def impl__prune_remove_one(cfg, entries_by_prefix: dict, main: Path, st) -> bool:
    """Remove one SAFE worktree through the selected ``worktrees.manager``, recording telemetry
    and deleting the now-merged branch afterwards. Returns True iff removal succeeded
    (outcome == "ok").

    The claim-record resolve/retire sequencing around the ONE removal effect is
    ``beadhive_worktrees.execute_removal`` (bh-qdezo.7, moved verbatim, same as `impl_remove`);
    telemetry, CLI echo, and the post-removal branch delete stay here."""
    prefix = st.hive
    entry = entries_by_prefix.get(prefix)

    started = time.monotonic()
    # SAFE (closed + merged + clean) → the branch is disposable; the manager removes only the
    # linked worktree and the `git branch -D` step below retires the branch. A row whose hive
    # left managed_repos still goes through the same release-then-remove effect (E30).
    result = execute_removal(
        lambda: _remove_worktree(cfg, entry, main, Path(st.path), st.branch, force=True),
        claim_records=worktree_state_adapters.CLAIM_RECORDS,
        target=Path(st.path),
    )
    outcome = "ok" if result.ok else "error"
    elapsed = time.monotonic() - started
    if outcome == "ok":
        typer.echo(f"  removed {st.path}  [{st.branch}]")
    else:
        # A failure retains its captured stderr when the facade runner supplied it; this line
        # also prevents a misleading "removed" claim.
        typer.echo(
            f"  failed to remove {st.path}  [{st.branch}]: "
            f"{result.error or 'git worktree remove failed'}"
        )
    _record_wt_event("prune", outcome, hive=prefix, leaf=st.leaf)
    _record_wt_op_duration("prune", elapsed, outcome, hive=prefix, leaf=st.leaf)
    if outcome != "ok":
        return False
    _rmdir_empty_parents(st.path, cfg)
    # A SAFE tree is already merged, so once its worktree is gone the branch is dead weight.
    # Best-effort: a stray branch never blocks the prune loop.
    _run_git(["git", "-C", str(main), "branch", "-D", st.branch], check=False)
    return True


def impl__prune_remove_all(
    cfg, mains: dict, keys: dict, entries_by_prefix: dict, safe_set: list
) -> int:
    """Remove every SAFE worktree, `git worktree prune` each touched main clone, and invalidate
    cached metadata for every hive touched. Returns the removed count."""
    from . import metadata

    removed_main_sets: set[str] = set()
    removed_count = 0
    for st in safe_set:
        main = mains.get(st.hive)
        if main is None:
            continue
        if _prune_remove_one(cfg, entries_by_prefix, main, st):
            removed_count += 1
        removed_main_sets.add(str(main))

    for main_str in removed_main_sets:
        _run_git(["git", "-C", main_str, "worktree", "prune"], check=False)

    for prefix in {s.hive for s in safe_set}:
        if prefix in keys:
            metadata.invalidate(cfg, keys[prefix])

    return removed_count


def impl_prune(hive=""):
    """Remove ONLY managed worktrees classified SAFE (closed + merged + clean).

    Uses the classifier to determine which worktrees are safe to remove on each run — no
    confirmation prompt and no --force flag are exposed: ``ws worktree status`` is the
    operator's pre-flight view.

    Scoping: ``--hive <id>`` limits to one hive; omit to prune all managed hives.

    Mode 1 (shared per-hive observaloop profile): this function deliberately does NOT tear down
    the hive's observaloop profile.  The profile is shared across all of the hive's worktrees and
    must remain up until the hive itself is retired — use ``ws observaloop down`` for that.
    Do NOT add per-worktree or per-prune observaloop teardown here; doing so would break the
    shared-profile contract and stop telemetry routing for any remaining worktrees or processes.

    Removal runs through the selected ``worktrees.manager`` (native git) and then deletes the
    now-merged branch (`git branch -D`) — SAFE means merged, so the branch is disposable.
    """
    cfg = config.load()
    want = str(registry.resolve_hive(cfg, hive)["prefix"]) if hive else None

    mains, keys, entries_by_prefix = _prune_load_entries(cfg)

    swept = _prune_sweep_orphans(entries_by_prefix, want)
    if swept:
        typer.echo(f"  reaped {swept} orphaned verify-* checkout(s)")

    all_rows = managed(cfg)
    rows = [r for r in all_rows if want is None or r[0] == want]

    # Reap dangling `.git/worktrees/<leaf>` admin entries for every hive in scope BEFORE
    # classifying (bh-exe8): a prior partial-failure prune run can leave a hive with a stale
    # worktree registration whose branch no longer resolves, which fires git fatals both
    # during classification (is_merged) and on removal. `_prune_remove_all`'s own trailing
    # `git worktree prune` call only runs for hives with at least one row in `safe_set` this
    # run — a hive where every row classifies NOT SAFE would never get reaped and would
    # repeat the same fatals on every future run. Prune every touched hive unconditionally so
    # it self-heals regardless of this run's classification outcome.
    for prefix in {r[0] for r in rows}:
        main = mains.get(prefix)
        if main is not None:
            _run_git(["git", "-C", str(main), "worktree", "prune"], check=False)

    safe_set, skipped = _prune_classify(cfg, entries_by_prefix, rows)
    safe_set, skipped, tainted = _prune_withhold_untrustworthy(safe_set, skipped)

    if not safe_set:
        typer.echo("no SAFE worktrees to prune")
        _prune_report_skipped(skipped)
        if tainted:
            _warn_untrustworthy(skipped)
            raise typer.Exit(1)
        return

    removed_count = _prune_remove_all(cfg, mains, keys, entries_by_prefix, safe_set)

    typer.echo(f"✓ pruned {removed_count} SAFE worktree(s)")
    _prune_report_skipped(skipped)
    if tainted:
        # Non-zero even though something WAS pruned: an unattended caller that only reads the
        # exit code must not come away believing the run was complete when a whole hive was
        # withheld. A partial prune reported as success is the same class of lie this bead is
        # about — a failure rendered as a normal result.
        _warn_untrustworthy(skipped)
        raise typer.Exit(1)


# ---- automatic reclaim + live-count cap (bh-qbu9t) --------------------------------------------


def impl_reclaim_merged(hive, ref) -> bool:
    """Remove a MERGED bead's worktree after a clean ``work merge`` / ``work finish`` (the branch
    stays the durable artifact). Never loses work: a tree with uncommitted or untracked changes
    (``git status --porcelain``; ignored caches like ``.venv`` do not count) or an unreadable
    status is kept with a warning. Returns True iff the worktree was removed or already absent."""
    cfg = config.load()
    entry = _resolve_entry(cfg, hive)
    target = wt_dir(entry, _leaf(ref))
    if not target.exists():
        return True
    res = _run_git(["git", "-C", str(target), "status", "--porcelain"], check=False, capture=True)
    if res.returncode != 0 or (getattr(res, "stdout", "") or "").strip():
        typer.echo(
            f"⚠ kept {target}: uncommitted changes (or unreadable status); the branch is merged "
            f"— inspect, then `{config.BINARY_ALIAS} worktree rm` it",
            err=True,
        )
        return False
    try:
        remove(hive, ref, True)
    except Exception:  # noqa: BLE001 — the merge already succeeded; reclaim is best-effort
        typer.echo(f"⚠ merged, but could not remove {target}", err=True)
        return False
    return True


def impl_live_worktree_dirs(root: Path) -> list[Path]:
    """Linked-worktree directories under the shadow ``root`` (``<provider>/<org>/<repo>/<leaf>``
    with a ``.git`` entry) — bead trees and verify-* checkouts alike, registered or not. A
    cheap filesystem scan: no git or tracker calls, safe on every provision."""
    if not root.is_dir():
        return []
    return [leaf for leaf in sorted(root.glob("*/*/*/*")) if (leaf / ".git").exists()]


def impl_enforce_live_cap(cfg) -> None:
    """Refuse to provision a NEW worktree once ``worktrees.max_live`` trees already exist.

    At the cap it first reclaims what is provably disposable — orphaned verify-* checkouts and
    SAFE (closed + merged + clean) bead trees, exactly ``prune``'s classifier, so nothing with
    uncommitted or unmerged work is touched — and re-counts. Only if the host is still full does
    it refuse, with the count, the cap and the ways out. ``0`` disables the cap."""
    cap = config.worktrees_max_live(cfg)
    if cap <= 0:
        return
    root = config.worktrees_root(cfg)
    live = impl_live_worktree_dirs(root)
    if len(live) < cap:
        return
    typer.echo(
        f"• {len(live)} live worktrees under {root} (worktrees.max_live={cap}) — "
        "reclaiming clean, merged trees first",
        err=True,
    )
    try:
        prune()
    except Exception:  # noqa: BLE001 — a failed reclaim falls through to the refusal
        pass
    remaining = len(impl_live_worktree_dirs(root))
    if remaining < cap:
        return
    typer.echo(
        f"✗ refusing to provision another worktree: {remaining} live under {root} "
        f"(cap worktrees.max_live={cap}) and none are clean + merged to reclaim.\n"
        f"  Fix one: finish or `{config.BINARY_ALIAS} work abandon <id> --rm` stale beads "
        f"(see `{config.BINARY_ALIAS} worktree status`) · raise the cap with "
        f"`{config.BINARY_ALIAS} config set worktrees.max_live <n> --scope host` "
        "(0 disables it; or BH_WORKTREES_MAX_LIVE)",
        err=True,
    )
    raise typer.Exit(1)
