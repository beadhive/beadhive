"""Safe branch-refinement orchestration behind the stable work facade.

The facade injects collaborators so existing patch seams remain executable; this module owns
plan resolution, backup/restore, rewrite, and byte-identical verification.
"""

from __future__ import annotations


def impl__load_plan(api, plan_arg):
    """Read a squash-plan from a file path or '-' (stdin). Raises on read/JSON errors."""
    text = api.sys.stdin.read() if plan_arg == "-" else api.Path(plan_arg).read_text()
    return api.json.loads(text)


def impl__restore(api, target, backup):
    """Abort any in-progress rebase and hard-reset the branch back to its pre-refine tip."""
    api.worktree.rebase_abort(target)
    api.worktree.reset_hard(target, backup)


def impl_refine_branch(api, cfg, *, hive, bead, plan, autosquash, since, dry_run):
    """Squash local checkpoint noise into conventional digests, behind a backup branch and a
    byte-identical gate (the net tree never changes). Typer-free core shared by the CLI and the
    future MCP entrypoint; returns a RefineResult and raises WorkError on any failure.

    Exactly one input mode (--plan | --autosquash | --since). On a real refine the backup
    branch is created before the rebase and surfaced via RefineResult.backup (success) or
    WorkError.backup (restore paths) so callers can report it identically."""
    entry, _main, target, branch = api.worktree.locate(cfg, hive, bead)
    api._guard_refine_mode(target, bead, plan, autosquash, since)
    base = api._resolve_refine_base(cfg, entry, bead, branch)
    base, rows, groups = api._build_refine_plan(entry, base, branch, plan, autosquash, since)
    if dry_run:
        subjects = (
            [r["subject"] for r in rows if not api._MARKER.match(r["subject"])]
            if autosquash
            else api._simulate(rows, groups)
        )
        return api.RefineResult(base=base, dry_run=True, subjects=subjects)
    _guard_refine_target(api, target, branch)
    if _refine_is_noop(api, entry, base, branch, autosquash, rows, groups):
        return api.RefineResult(base=base, branch=branch, target=target, noop=True)
    backup = api._apply_refine_rebase(entry, target, branch, base, autosquash, rows, groups)
    reaped, failed = api.worktree.delete_safety_refs(
        entry, branch, labels=("refine",), keep=(backup,)
    )
    return api.RefineResult(
        base=base,
        backup=backup,
        branch=branch,
        log=api.worktree.log_range(entry, base, branch),
        target=target,
        reaped=reaped,
        cleanup_failed=failed,
    )


def _guard_refine_target(api, target, branch):
    if not api.worktree.is_clean(target):
        raise api.WorkError(["✗ working tree not clean — commit or discard changes first"])
    cur = api.worktree.current_branch(target)
    if cur != branch:
        raise api.WorkError([f"✗ on branch {cur or '(detached)'}, expected {branch}"])


def _refine_is_noop(api, entry, base, branch, autosquash, rows, groups):
    """True when the requested refinement cannot change commits or their parentage."""
    if autosquash:
        changes = any(api._MARKER.match(row["subject"]) for row in rows)
    else:
        by_sha = {row["sha"]: row for row in rows}
        changes = False
        for group in groups:
            row = by_sha[group["keep"]]
            changes = changes or bool(group["fold"])
            changes = changes or bool(group.get("subject")) and group["subject"] != row["subject"]
            changes = changes or group.get("body") not in (None, "")
            changes = changes or group.get("date") not in (None, "", "keep")
    return not changes and api.worktree.is_merged(entry, base, branch)


def impl__guard_refine_mode(api, target, bead, plan, autosquash, since):
    """Guard exactly one input mode (--plan | --autosquash | --since) is given and the worktree
    exists."""
    if sum([bool(plan), autosquash, bool(since)]) != 1:
        raise api.WorkError(["✗ pass exactly one of --plan / --autosquash / --since"])
    if not target.exists():
        raise api.WorkError([f"✗ no worktree for {bead} — claim it first"])


def impl__resolve_refine_base(api, cfg, entry, bead, branch):
    """Resolve the refine base (the integration base climbed onto the branch's actual fork
    point), or raise when it can't be computed."""
    base = api.worktree.base_of(
        entry,
        branch,
        api.worktree.integration_base(entry, bead, api.config.integration_branch(cfg, entry)),
    )
    if not base:
        raise api.WorkError(["✗ cannot compute base (is the integration branch present locally?)"])
    return base


def impl__build_refine_plan(api, entry, base, branch, plan, autosquash, since):
    """Build the squash plan + resolve commit rows/groups (autosquash lets git build its own
    todo, so no plan). Returns (base — possibly overridden by an explicit plan `base`, commit
    rows, groups)."""
    if autosquash:
        rows = api.worktree.commit_rows(entry, base, branch)
        merges = [row for row in rows if len(row.get("parents") or []) > 1]
        if merges:
            details = "; ".join(
                f"{row.get('short') or str(row.get('sha') or '')[:8]} "
                f"{str(row.get('subject') or '')!r}"
                for row in merges[:4]
            )
            raise api.WorkError(
                [
                    "✗ autosquash range contains merge commit(s); refine cannot rewrite "
                    f"reviewed merge topology: {details}"
                ]
            )
        return (base, rows, [])
    if since:
        plan_dict = api.plan_from_since(api.worktree.commit_rows(entry, since, branch))
    else:
        try:
            plan_dict = api._load_plan(plan)
        except (OSError, api.json.JSONDecodeError) as e:
            raise api.WorkError([f"✗ cannot read plan: {e}"]) from None
    if isinstance(plan_dict, dict) and plan_dict.get("base"):
        base = plan_dict["base"]
    rows = api.worktree.commit_rows(entry, base, branch)
    ok, errors, groups = api.validate_plan(plan_dict, rows)
    if not ok:
        raise api.WorkError([f"✗ {e}" for e in errors])
    return (base, rows, groups)


def impl__apply_refine_rebase(api, entry, target, branch, base, autosquash, rows, groups):
    """Real refine: require a clean tree on the expected branch, snapshot a backup branch,
    rebase (autosquash or an explicit squash-plan todo), and gate on a byte-identical net tree —
    restoring from the backup on any rebase failure or tree drift. Returns the backup branch."""
    _guard_refine_target(api, target, branch)
    # A timestamp alone collides when two invocations start in the same second. The shared
    # session id keeps chronological readability and adds a random suffix, matching premerge.
    backup = api.worktree.backup_branch(entry, branch, api.worktree._session_id())
    if autosquash:
        rc, out = api.worktree.rebase_autosquash(target, base)
    else:
        rc, out = api.worktree.rebase_squash(target, base, api.build_todo(rows, groups))
    if rc != 0:
        api._restore(target, backup)
        messages = [f"✗ refine rebase failed (exit {rc}) — restored from {backup}"]
        if out.strip():
            messages.append(out.strip())
        messages.append(
            "  keep a keep's folds contiguous, or refine-as-you-go with `git commit --fixup`"
        )
        raise api.WorkError(messages, backup=backup)
    if not api.worktree.same_tree(entry, backup, branch):
        api.worktree.reset_hard(target, backup)
        raise api.WorkError([f"✗ refine changed the tree — restored from {backup}"], backup=backup)
    return backup


def impl_refine(api, bead, plan, autosquash, since, dry_run, hive):
    """Squash local checkpoint noise into conventional digests behind a backup branch and a
    byte-identical gate (the net tree never changes). Retains per-digest author dates. Exactly
    one input mode: --plan | --autosquash | --since."""
    cfg = api.config.load()
    try:
        result = api.refine_branch(
            cfg,
            hive=hive,
            bead=bead,
            plan=plan,
            autosquash=autosquash,
            since=since,
            dry_run=dry_run,
        )
    except api.WorkError as e:
        if e.backup:
            api.typer.echo(f"backup branch: {e.backup}")
        for line in e.messages:
            api.typer.echo(line, err=True)
        raise api.typer.Exit(1) from None
    if result.dry_run:
        api.typer.echo(f"would produce {len(result.subjects)} commit(s) over {result.base[:7]}:")
        for s in result.subjects:
            api.typer.echo(f"  {s}")
        return
    if result.noop:
        api.typer.echo(
            f"✓ {bead} is already refined ({result.branch}) — no history change and no backup "
            "ref created"
        )
        return
    api.typer.echo(f"backup branch: {result.backup}")
    api.typer.echo(
        f"✓ refined {bead} ({result.branch}) — latest backup retained until submit at "
        f"{result.backup}:"
    )
    api.typer.echo(result.log)
    api.typer.echo(f"restore with: git -C {result.target} reset --hard {result.backup}")
    if result.reaped:
        api.typer.echo(f"  reaped {len(result.reaped)} superseded refine backup ref(s)")
    if result.cleanup_failed:
        api.typer.echo(
            "⚠ refined successfully but could not reap superseded backup ref(s): "
            + ", ".join(result.cleanup_failed),
            err=True,
        )
