"""Assignment and race-safe claim lifecycle orchestration.

Mutable collaborators are supplied explicitly by the stable :mod:`beadhive.work` facade on
each call.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ClaimResult:
    """Structured result of the single-bead claim lifecycle.

    The CLI deliberately renders this result separately so composite callers can consume the
    complete provisioning record without scraping human progress output.
    """

    entry: dict
    main: Path
    bead: dict
    actor: str
    disposition: str
    worktree: Path
    identity: dict
    branch: str = ""


def impl_assign(api, bead, to, as_, hive, preview, as_json):
    api.otel.set_bead(bead)
    cfg = api.config.load()
    if preview:
        api._print_work_preview(cfg, hive, bead, to, op="assign", as_json=as_json)
        return
    api.guard.guard_primary(hive, cfg=cfg, verb="work assign")
    entry, main, _target, _branch = api.worktree.locate(cfg, hive, bead)
    actor = api.identity.resolve_actor(as_, api.config.work_identity(cfg, entry)["name"] or "")
    api._guard_orchestrator(actor, bead)
    data = api.bd.show(bead, main)
    api._guard_open(data, bead)
    api._guard_not_other(data, to, bead)
    api._guard_seat(data, to, bead, verb="assigned to")
    api._guard_conventions(cfg, data, bead, main, action="dispatch")
    brief_text = api._first(data, "description")
    with api.otel.record_agent_dispatch(
        agent=to,
        model=api.config.otel_genai_model(cfg),
        system=api.config.otel_genai_system(cfg, entry),
        brief=brief_text,
        attributes={"bh.bead": bead},
    ):
        res = api.bd.run(["assign", bead, to], main)
        if res.returncode != 0:
            raise api.typer.Exit(res.returncode)
        api._push_state(cfg, main, actor, f"assign {bead} -> {to}")
        api._maybe_open_molecule(cfg, hive, bead, main)
        entry, target, _branch = api.worktree.ensure(cfg, hive, bead, kind=api._kind_of(data))
        api._stamp(cfg, entry, target, to)
    api.otel.count_bead_transition("assigned")
    api.typer.echo(f"✓ assigned {bead} → {to}; worktree {target}")


def impl__claim_fence(api, cfg, hive):
    try:
        this_host = api.host.host_id()
    except FileNotFoundError:
        this_host = ""
    return (this_host, api.guard.live_epoch(hive, cfg=cfg))


def impl__issue_claim(api, cfg, entry, bead, actor, target, hive):
    authority = api.claim_authority.get_authority(api.config.claim_authority(cfg, entry))
    this_host, epoch = api._claim_fence(cfg, hive)
    authority.issue(bead, actor, target, host_id=this_host, epoch=epoch)


def impl_claim(api, bead, as_, group, collapse, hive, preview, as_json):
    cfg = api.config.load()
    group = api.work_logic.opt_str(group)
    collapse = api.work_logic.opt_str(collapse)
    if preview:
        if collapse or group:
            api.typer.echo(
                "✗ --preview supports a single <id> only (no --group/--collapse)", err=True
            )
            raise api.typer.Exit(1)
        if not bead:
            api.typer.echo("✗ pass a bead <id>", err=True)
            raise api.typer.Exit(1)
        entry, _main, _target, _branch = api.worktree.locate(cfg, hive, bead)
        actor = api.identity.resolve_actor(as_, api.config.work_identity(cfg, entry)["name"] or "")
        api._print_work_preview(cfg, hive, bead, actor, op="claim", as_json=as_json)
        return
    api.guard.guard_primary(hive, cfg=cfg, verb="work claim")
    if collapse:
        if bead or group:
            api.typer.echo(
                "✗ pass either <id>, --group, or --collapse — not more than one", err=True
            )
            raise api.typer.Exit(1)
        api.work_group.claim_collapsed(cfg, hive, collapse, as_)
        return
    if group:
        if bead:
            api.typer.echo("✗ pass either <id> or --group, not both", err=True)
            raise api.typer.Exit(1)
        api.work_group.claim_group(cfg, hive, group, as_)
        return
    if not bead:
        api.typer.echo("✗ pass a bead <id> (or --group <ids> for a batch)", err=True)
        raise api.typer.Exit(1)
    result = api._claim_single_bead(cfg, hive, bead, as_)
    api.typer.echo(f"✓ claimed {bead} as {result.actor}; worktree {result.worktree}")
    api._print_brief(cfg, result.entry, bead, result.bead)
    if not api.worktree.in_bead_worktree(result.worktree):
        api.typer.echo(
            "\nWARNING: cwd is not the bead worktree — edits here target the wrong tree.\n"
            f'  → cd "{result.worktree}"  # work happens in the worktree, NOT the main clone',
            err=True,
        )


def impl__claim_single_bead(api, cfg, hive, bead, as_):
    """Claim one bead and return its structured provisioning envelope (no success output)."""
    api.otel.set_bead(bead)
    entry, main, _target, _branch = api.worktree.locate(cfg, hive, bead)
    api._pull_state(cfg, main)
    actor = api.identity.resolve_actor(as_, api.config.work_identity(cfg, entry)["name"] or "")
    data = api.bd.show(bead, main)
    api._guard_open(data, bead)
    api._guard_not_other(data, actor, bead)
    api._guard_seat(data, actor, bead, verb="claimed by")
    api._guard_conventions(cfg, data, bead, main, action="dispatch")
    api._maybe_open_molecule(cfg, hive, bead, main)
    already_held = api.work_next.claim_won(data, actor)
    if not already_held:
        res = api.bd.run(["update", bead, "--claim"], main, actor=actor)
        if res.returncode != 0:
            raise api.typer.Exit(res.returncode)
        # bd's claim write is not a compare-and-swap.  Only proceed when the reread still proves
        # that this actor owns the bead; a competing writer must never look like a win.
        data = api.bd.show(bead, main)
        if not api.work_next.claim_won(data, actor):
            raise api.typer.Exit(1)
    try:
        # Provisioning is idempotent and may reattach an existing checkout for a same-actor retry.
        entry, target, branch = api.worktree.ensure(cfg, hive, bead, kind=api._kind_of(data))
        api._stamp(cfg, entry, target, actor)
        api._issue_claim(cfg, entry, bead, actor, target, hive)
    except Exception as exc:
        if not already_held:
            api._release_claim(main, bead, actor, detail=str(exc))
        raise
    # Provisioning can race with a reassignment too. Re-read after BOTH fresh and idempotent paths
    # so the returned envelope contains authoritative bead ownership.
    data = api.bd.show(bead, main)
    if not api.work_next.claim_won(data, actor):
        if not already_held:
            api._release_claim(main, bead, actor, detail="claim lost during provisioning")
        raise api.typer.Exit(1)
    api.otel.count_bead_transition("claimed")
    prof = api.config.work_identity(cfg, entry, actor)
    ident = {
        "mode": prof["mode"],
        "name": actor or prof["name"] or "",
        "email": prof["email"] or "",
        "signing_key": prof["signing_key"] or "",
        "sign": prof["sign"],
    }
    return ClaimResult(
        entry=entry,
        main=Path(main),
        bead=data,
        actor=actor,
        disposition="reattached" if already_held else "claimed",
        worktree=Path(target),
        identity=ident,
        branch=str(branch),
    )


def impl__batch_member_procedure_msg(api, bead, grp):
    alias = api.config.BINARY_ALIAS
    return (
        f"✗ {bead} is a batch member (batch:{grp}) — it has no per-bead worktree.\n"
        f"  Batch work happens in the ONE shared worktree wt/batch/{grp} and completes as a "
        f"UNIT:\n      {alias} work submit --group <ids>   # one review gate for the whole "
        f"batch\n      {alias} work merge --group <ids>    # after approval"
    )


def impl__batch_worktree(api, cfg, hive, bead, main):
    grp = api.work_group.batch_label(api.bd.show(bead, main))
    if not grp:
        return ("", None)
    target = api.worktree.locate(cfg, hive, branch=f"{api.work_group.BATCH_PREFIX}{grp}")[2]
    return (grp, target if target.exists() else None)


def impl__try_claim(api, bead, actor, main):
    res = api.bd.run(["update", bead, "--claim"], main, actor=actor)
    if res.returncode != 0:
        return False
    return api.work_next.claim_won(api.bd.show(bead, main), actor)


def impl__release_claim(api, main, bead, actor, detail):
    api.record_dispatch_failure(
        bead, "provisioning_failed", detail or "provisioning_failed", main, actor=actor
    )
    api.bd.run(["update", bead, "--status", "open", "--assignee", ""], main, actor=actor)


def impl__provision_claim(api, cfg, hive, main, bead, actor):
    try:
        data = api.bd.show(bead, main)
        entry, target, _br = api.worktree.ensure(cfg, hive, bead, kind=api._kind_of(data))
        api._stamp(cfg, entry, target, actor)
        api._issue_claim(cfg, entry, bead, actor, target, hive)
    except Exception as exc:
        api._release_claim(main, bead, actor, detail=str(exc))
        raise
    prof = api.config.work_identity(cfg, entry, actor)
    ident = {
        "mode": prof["mode"],
        "name": actor or prof["name"] or "",
        "email": prof["email"] or "",
        "signing_key": prof["signing_key"] or "",
        "sign": prof["sign"],
    }
    return (str(target), ident)
