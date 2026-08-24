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
    backup = api._apply_refine_rebase(entry, target, branch, base, autosquash, rows, groups)
    return api.RefineResult(
        base=base,
        backup=backup,
        branch=branch,
        log=api.worktree.log_range(entry, base, branch),
        target=target,
    )


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
        return (base, api.worktree.commit_rows(entry, base, branch), [])
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
    if not api.worktree.is_clean(target):
        raise api.WorkError(["✗ working tree not clean — commit or discard changes first"])
    cur = api.worktree.current_branch(target)
    if cur != branch:
        raise api.WorkError([f"✗ on branch {cur or '(detached)'}, expected {branch}"])
    ts = api.datetime.datetime.now(api.datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = api.worktree.backup_branch(entry, branch, ts)
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
    api.typer.echo(f"backup branch: {result.backup}")
    api.typer.echo(f"✓ refined {bead} ({result.branch}) — backup left at {result.backup}:")
    api.typer.echo(result.log)
    api.typer.echo(f"restore with: git -C {result.target} reset --hard {result.backup}")
