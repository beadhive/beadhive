"""Work-group (batch) mechanics for `ws work` — claim + merge a set of beads as ONE unit.

A *batch* is several beads sharing a `batch:<group>` label (the data model — label/spec field +
cohesion/size validation — already landed in 8v8.1), handled by ONE agent in ONE shared
`wt/batch/<group>` worktree, then validated and merged ONCE as a single `--no-ff` bubble with the
per-bead commits preserved inside (lossless / bisectable). Split out of `work.py` so the
single-bead lifecycle verbs stay the default path and a different file carries the opt-in batch
logic. The bd seam is `bd.run` / `bd.show` / `bd.state`; the guards (`_guard_open` /
`_history_ok`) live in `work.py` and are reached through the `work` module at call time, so this
module never imports `work` at load —
the cycle stays one-directional, exactly like `work_show`.
"""

from __future__ import annotations

import time
from contextlib import contextmanager

import typer

from . import config, identity, otel, worktree

BATCH_PREFIX = "batch/"  # a work-group's shared worktree branch is wt/batch/<group>


@contextmanager
def merge_slot(main, slot_attrs=None):
    """Hold the rig's exclusive merge slot for the block: create (idempotent) -> acquire -> yield
    -> release (best-effort, on every exit path). The one merge-slot skeleton shared by the bead /
    molecule / batch land sites. When `slot_attrs` is given, also emit the slot wait/hold otel
    timings (the bead & molecule sites pass them; the batch site does not, so its otel is
    unchanged)."""
    from . import bd  # lazy: avoids a load-time import cycle

    bd.run(["merge-slot", "create"], main)  # idempotent: no-op once the rig's slot bead exists
    slot_mark = time.perf_counter()
    if bd.run(["merge-slot", "acquire"], main).returncode != 0:
        typer.echo("✗ could not acquire merge slot — another merge holds it", err=True)
        raise typer.Exit(1)
    slot_acquired = time.perf_counter()
    if slot_attrs is not None:
        otel.record_merge_slot_wait(slot_acquired - slot_mark, slot_attrs)
    try:
        yield
    finally:
        if slot_attrs is not None:
            otel.record_merge_slot_hold(time.perf_counter() - slot_acquired, slot_attrs)
        bd.run(["merge-slot", "release"], main)


def members_of(group_arg: str) -> list[str]:
    """Parse `--group` into member bead ids (comma/whitespace separated, de-duped, order kept)."""
    seen: dict[str, None] = {}
    for tok in group_arg.replace(",", " ").split():
        if tok:
            seen.setdefault(tok, None)
    return list(seen)


def batch_label(data) -> str:
    """The `<group>` of a bead's `batch:<group>` label ('' if it carries none)."""
    for lbl in (data or {}).get("labels", []) or []:
        s = str(lbl)
        if s.startswith("batch:"):
            return s[len("batch:") :]
    return ""


def resolve_group(members, datas) -> str:
    """The single batch group name shared by every member (read from their existing
    `batch:<group>` labels). Refuse a member with no batch label, or a mix of groups — those
    aren't a runnable unit (8v8.1 validates this at plan time; we re-guard at the verb)."""
    names = {batch_label(datas[m]) for m in members}
    if "" in names:
        bare = [m for m in members if not batch_label(datas[m])]
        typer.echo(f"✗ not batch members (no batch:<group> label): {', '.join(bare)}", err=True)
        raise typer.Exit(1)
    if len(names) != 1:
        typer.echo(f"✗ members span multiple batch groups: {', '.join(sorted(names))}", err=True)
        raise typer.Exit(1)
    return next(iter(names))


def _members(group_arg):
    members = members_of(group_arg)
    if not members:
        typer.echo("✗ --group needs at least one member id", err=True)
        raise typer.Exit(1)
    return members


def ready_children(epic, main) -> list[str]:
    """Ids of an epic's ready (un-closed) child beads, in listing order — the candidate members of
    a collapsed run. Returns `[]` when the epic has no listable children so callers refuse a
    runnable-empty collapse rather than crashing."""
    from . import bd  # lazy: avoids a circular import at module level

    kids = bd.json(["list", "--parent", epic], main)
    if not isinstance(kids, list):
        return []
    return [str(k["id"]) for k in kids if k.get("id") and str(k.get("status", "")) != "closed"]


def synthesize_batch_labels(members, epic, datas, main, actor) -> None:
    """Collapsed-claim pre-step: stamp a synthetic `batch:<epic>` label onto every member that
    carries no `batch:` label yet, so `resolve_group`'s existing precondition holds for an
    un-batched epic (it is NOT modified — this only makes its precondition true). Additive and
    idempotent: a member already carrying a batch label (planner-authored or a prior stamp) is left
    untouched, and no other label is ever removed."""
    from . import bd  # lazy: avoids a circular import at module level

    label = f"batch:{epic}"
    for m in members:
        if batch_label(datas[m]):
            continue  # read-only w.r.t. existing (planner) batch labels — never overwrite
        if bd.run(["label", "add", m, label], main, actor=actor).returncode != 0:
            typer.echo(f"✗ could not stamp {label} on {m}", err=True)
            raise typer.Exit(1)


def claim_collapsed(cfg, rig, epic, as_):
    """Collapsed claim: run an epic's ready children as ONE grouped session even when the planner
    authored no `batch:` labels. Synthesizes a `batch:<epic>` label on each un-batched ready child
    as a PRE-STEP (making `resolve_group`'s precondition true), then delegates to the existing
    `claim_group` — the same one-shared-`wt/batch/<group>`-worktree path the planner-batch flow
    uses. The stamping is additive/idempotent, so re-running a collapse is safe."""
    from . import bd  # lazy: avoids a circular import at module level

    entry, main, _target, _branch = worktree.locate(cfg, rig, epic)
    members = ready_children(epic, main)
    if not members:
        typer.echo(f"✗ no ready children under {epic} to collapse", err=True)
        raise typer.Exit(1)
    actor = identity.resolve_actor(as_, config.work_identity(cfg, entry)["name"] or "")
    datas = {m: bd.show(m, main) for m in members}
    synthesize_batch_labels(members, epic, datas, main, actor)
    claim_group(cfg, rig, ",".join(members), as_)


def claim_group(cfg, rig, group_arg, as_):
    """Group-aware claim: one shared `wt/batch/<group>` worktree + identity for every member.
    Guards each member first (open, and not another actor's), resolves the shared group name from
    the existing `batch:<group>` labels, provisions/stamps the one batch worktree (forked off the
    members' molecule), then `bd update --claim`s each member as the single actor."""
    from . import bd, work  # lazy: guards/_stamp live in work.py; avoids the load-time cycle

    members = _members(group_arg)
    entry, main, _target, _branch = worktree.locate(cfg, rig, members[0])
    actor = identity.resolve_actor(as_, config.work_identity(cfg, entry)["name"] or "")
    datas = {}
    for m in members:
        data = bd.show(m, main)
        work._guard_open(data, m)
        work._guard_not_other(data, actor, m)
        datas[m] = data
    group = resolve_group(members, datas)
    entry, target, branch = worktree.ensure(
        cfg, rig, branch=f"{BATCH_PREFIX}{group}", base_bead=members[0]
    )
    # Guard: the batch worktree MUST be checked out on its own wt/batch/<group>
    # branch. If it resolved onto another seat's dir (e.g. the coordinator's wt/bead/epic/<epic>,
    # which shares the leaf in collapsed mode), commits would silently land on the wrong branch and
    # `merge --group` would find no delta. Hard-fail rather than stamp+claim into the wrong tree.
    on = worktree.current_branch(target)
    if on != branch:
        typer.echo(
            f"✗ refusing to claim batch {group}: worktree {target} is on '{on}', not '{branch}'.\n"
            f"  A batch/collapsed session needs its OWN wt/batch/<group> worktree; this dir\n"
            f"  belongs to another seat. Committing here would land on the wrong branch.",
            err=True,
        )
        raise typer.Exit(1)
    work._stamp(cfg, entry, target, actor)
    for m in members:
        if bd.run(["update", m, "--claim"], main, actor=actor).returncode != 0:
            raise typer.Exit(1)
        otel.count_bead_transition("claimed", {"bh.bead": m, "bh.batch": group})
    typer.echo(
        f"✓ claimed batch {group} ({len(members)} beads: {', '.join(members)}) as {actor}; "
        f"worktree {target}"
    )
    if not worktree.in_bead_worktree(target):
        typer.echo(
            f"\nWARNING: cwd is not the batch worktree — edits here target the wrong tree.\n"
            f'  → cd "{target}"  # the whole group is implemented in this one worktree',
            err=True,
        )


def merge_group(cfg, group_arg, rig, rm):
    """Land a work-group as ONE `--no-ff` bubble. Mirrors the single-bead merge guards per member
    (no changes-requested, no open gate), then — under the rig merge slot, held once — validates
    the shared `wt/batch/<group>` branch from a clean checkout, merges it `--no-ff` into the
    members' molecule base (per-bead commits preserved inside the one bubble), and closes every
    member. The history budget is RELAXED to ~per-bead-commits × members (a cohesive batch is
    several beads' worth of commits on one branch). The slot is released on every path; on conflict
    the merge aborts and nothing is closed — work is never dropped."""
    from . import bd, work  # lazy: guards/_open_gate/_history_ok live in work.py; avoids the cycle

    members = _members(group_arg)
    entry, main, _target, _branch = worktree.locate(cfg, rig, members[0])
    datas = {}
    for m in members:
        data = bd.show(m, main)
        work._guard_open(data, m)
        datas[m] = data
    group = resolve_group(members, datas)

    for m in members:
        if bd.state(m, "review", main) == "changes-requested":
            typer.echo(f"✗ {m} has changes-requested — resume & resubmit, don't merge", err=True)
            raise typer.Exit(1)
        if work._open_gate(m, main):
            typer.echo(f"✗ {m} review gate still open — batch not approved yet", err=True)
            raise typer.Exit(1)

    _entry, _main, target, branch = worktree.locate(cfg, rig, branch=f"{BATCH_PREFIX}{group}")
    if not worktree._branch_exists(main, branch):
        typer.echo(f"✗ no batch branch {branch} — was the group claimed?", err=True)
        raise typer.Exit(1)

    base = worktree.integration_base(entry, members[0], config.integration_branch(cfg, entry))
    count, subjects = worktree.history(entry, branch, base)
    if count == 0:
        # Distinguish 'work landed on the wrong branch' from a genuinely empty group so the
        # operator gets an actionable path instead of the generic submit message (ev1l).
        typer.echo(
            f"✗ batch {branch} has no commits over {base} — nothing to merge.\n"
            f"  If the group WAS implemented, its commits likely landed on the wrong branch\n"
            f"  (e.g. a coordinator seat wt/bead/epic/<epic>), not {branch}. Find them with:\n"
            f"      git -C {main} log --oneline {base}..wt/bead/epic/<epic>\n"
            f"  then move them onto {branch} (cherry-pick) and re-run; else abandon the group.",
            err=True,
        )
        raise typer.Exit(1)
    limit = config.max_commits(cfg, entry) * len(members)  # relaxed: per-bead-commits × members
    ok, msg = work._history_ok(count, subjects, limit)
    if not ok:
        typer.echo(f"✗ {msg} — bounce back for self-refine", err=True)
        raise typer.Exit(1)

    started = time.perf_counter()
    with merge_slot(main):
        rc = worktree.clean_checkout(entry, branch, config.validate_cmd(cfg, entry))
        otel.count_validation(rc == 0, {"bh.batch": group, "bh.work.phase": "batch"})
        if rc != 0:
            typer.echo(f"✗ batch validation failed (exit {rc}) — nothing landed", err=True)
            raise typer.Exit(rc)

        prof = config.work_identity(cfg, entry)
        agent = prof["mode"] == "agent"
        mrc, out = worktree.merge_no_ff(
            entry,
            branch,
            base,
            name=(prof["name"] or "") if agent else "",
            email=(prof["email"] or "") if agent else "",
            signing_key=(prof["signing_key"] or "") if agent else "",
            sign=prof["sign"] if agent else False,
            message=f"merge batch {group}",
        )
        if mrc != 0:
            typer.echo(f"✗ batch merge failed — aborted, nothing landed:\n{out}", err=True)
            raise typer.Exit(mrc)
        for m in members:
            if bd.run(["close", m, "--reason", f"merged in batch {group}"], main).returncode != 0:
                typer.echo(f"⚠ merged but failed to close {m} — close it manually", err=True)

    otel.record_merge_duration(
        time.perf_counter() - started, {"bh.merge.kind": "batch", "bh.batch": group}
    )
    for m in members:
        otel.count_bead_transition("merged", {"bh.bead": m, "bh.batch": group})
    typer.echo(
        f"✓ merged batch {group} ({len(members)} beads: {', '.join(members)}) "
        f"({branch} --no-ff → {base}) and closed all members"
    )
    if rm:
        worktree.remove(rig, branch, force=True)
