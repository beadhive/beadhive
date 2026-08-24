"""Next, local-loop, and schedule lifecycle orchestration.

Mutable collaborators are supplied explicitly by the stable :mod:`beadhive.work` facade on
each call.
"""

from __future__ import annotations


def impl__next_seat_actor(api, actor, data):
    want = "dispatcher" if api._is_epic(data) else "developer"
    declared = api._seat_of(actor)
    if declared == "":
        prefix = api._DISP_PREFIX if want == "dispatcher" else api._DEV_PREFIX
        return f"{prefix}{actor}"
    if declared == want:
        return actor
    return None


def impl__molecule_members(api, epic, main):
    rows = api.bd.json(["list", "--parent", epic, "--include-infra", "--all"], main) or []
    members = {str(r.get("id") or "") for r in rows if isinstance(r, dict)}
    members.add(epic)
    members.discard("")
    return members


def impl__next_payload(
    api,
    hive,
    actor,
    claimed,
    claim_actor,
    rows,
    tried,
    refused,
    status,
    reason,
    worktree_path,
    ident,
):
    return api.jsonout.envelope(
        "work next",
        api.NEXT_SCHEMA,
        {
            "status": status,
            "bead": claimed,
            "actor": claim_actor if claimed else actor,
            "seat": api._seat_of(claim_actor if claimed else actor),
            "hive": hive,
            "worktree": worktree_path or None,
            "identity": ident,
            "reason": reason,
            "tried": list(tried),
            "refused": list(refused),
        },
    )


def impl_next_(api, as_, hive, as_json, epic):
    cfg = api.config.load()
    api.guard.guard_primary(hive, cfg=cfg, verb="work next")
    main = api.registry.hive_dir_for(cfg, hive)
    entry = api.registry.entry_for_dir(cfg, main) or {}
    actor = api.identity.resolve_actor(as_, api.config.work_identity(cfg, entry)["name"] or "")
    api._pull_state(cfg, main)
    rows = [r for r in api.bd.json(["ready", "--limit", "0"], main) or [] if isinstance(r, dict)]
    if epic:
        members = api._molecule_members(epic, main)
        rows = [r for r in rows if str(r.get("id") or "") in members]
    rows_by_id = {str(r.get("id") or ""): r for r in rows}
    tried: list[str] = []
    refused: list[str] = []
    claimed = ""
    worktree_path = ""
    ident: dict | None = None
    claim_actor = actor
    for bead in api.work_next.eligible(rows, actor):
        seat_actor = api._next_seat_actor(actor, rows_by_id.get(bead, {}))
        if seat_actor is None:
            refused.append(bead)
            continue
        tried.append(bead)
        if api._try_claim(bead, seat_actor, main):
            claimed = bead
            claim_actor = seat_actor
            api.otel.set_bead(bead)
            api.otel.count_bead_transition("claimed")
            worktree_path, ident = api._provision_claim(cfg, hive, main, bead, actor)
            break
    if claimed:
        status, reason = ("claimed", "")
    elif refused and (not tried):
        status, reason = ("refused", "seat_mismatch")
    else:
        status, reason = ("declined", api.work_next.decline(rows, tried))
    if as_json:
        api.jsonout.emit(
            api._next_payload(
                api._hive(entry),
                actor,
                claimed,
                claim_actor,
                rows,
                tried,
                refused,
                status,
                reason,
                worktree_path,
                ident,
            )
        )
    elif claimed:
        api.typer.echo(f"✓ claimed {claimed} as {claim_actor}; worktree {worktree_path}")
    elif status == "refused":
        want = ", ".join(refused)
        api.typer.echo(
            f"✗ {actor} declares a seat that does not match {want} — "
            f"an epic may only be worked by {api._DISP_PREFIX}<name>, any other bead only by "
            f"{api._DEV_PREFIX}<name>",
            err=True,
        )
    else:
        api.typer.echo(f"— nothing to claim ({reason})", err=True)
    if status == "refused":
        raise api.typer.Exit(api.NEXT_REFUSE_EXIT)
    if not claimed:
        raise api.typer.Exit(api.NEXT_DECLINE_EXIT)


def impl_loop(api, epic, as_, hive, passes, as_json, dry_run, seat_binary):
    cfg = api.config.load()
    api.guard.guard_primary(hive, cfg=cfg, verb="work loop")
    main = api.registry.hive_dir_for(cfg, hive)
    entry = api.registry.entry_for_dir(cfg, main) or {}
    actor = api.identity.resolve_actor(as_, api.config.work_identity(cfg, entry)["name"] or "")
    api.otel.set_bead(epic)
    dispatch_sink = api.os.environ.get("BH_DISPATCH_LOG_SINK", "").strip()
    if dispatch_sink:
        api.log.add_file_sink(dispatch_sink)
    from . import localloop

    driver = localloop.LocalLoop(
        hive_dir=main,
        epic=epic,
        actor=actor,
        caps=localloop.Caps(
            max_concurrency=api.config.dispatch_max_concurrency(cfg, entry),
            max_run_seconds=api.config.dispatch_max_run_seconds(cfg, entry),
        ),
        seat_command=seat_binary or api.config.dispatch_seat_command(cfg, entry),
        seat_bundle="" if seat_binary else api.config.dispatch_seat_bundle(cfg, entry),
        poll_interval=api.config.dispatch_poll_interval(cfg, entry),
        envelope_grace=api.config.dispatch_envelope_grace(cfg, entry),
        terminate_grace=api.config.dispatch_terminate_grace(cfg, entry),
        max_action_retries=api.config.dispatch_max_action_retries(cfg, entry),
        lease=localloop.lease_keeper_for(hive, cfg=cfg, hive_dir=main),
        dry_run=dry_run,
    )

    def _emit(report) -> None:
        """Render ONE pass, the moment it ends.

        `--help` promises "one JSON pass report per line", and a caller tailing this — the whole
        reason `bh host dispatch run` spawns it with `--json` — needs it to be a stream. Emitting
        after `run()` returned made it a batch: with `--passes 0` (the supervised default) that
        is nothing at all until the molecule lands, hours later, while the accumulated list of
        reports grows unboundedly in memory for the entire run."""
        if as_json:
            api.jsonout.emit(report.as_dict())
            return
        decision = report.decision
        prefix = "[DRY RUN] " if report.dry_run else ""
        api.typer.echo(
            f"{prefix}pass {report.number}: {decision.row if decision else '—'} "
            f"→ {decision.action if decision else '—'} "
            f"(dispatched {len(report.dispatched)}, harvested {len(report.harvested)}, "
            f"reclaimed {len(report.reclaimed)})"
        )
        if report.dry_run and decision and (decision.action == "dispatch"):
            api.typer.echo(
                f"{prefix}  would claim: "
                f"{', '.join(report.claimable) if report.claimable else '(none — all blocked)'}"
            )
            api.typer.echo(
                f"{prefix}  budget bound (NOT a plan; may include blocked beads): "
                f"{', '.join(decision.beads)}"
            )

    if dry_run:
        api.typer.echo(
            "⚠ DRY RUN — decide-only: nothing will be claimed, provisioned, spawned, or "
            "written to any bead. This is a LOWER BOUND: reclaim is skipped (it writes), so a "
            "real pass may see additional beads freed from stale claims and dispatch more than "
            "shown here.",
            err=True,
        )
        run_passes = 1
    else:
        run_passes = passes or None
    api.asyncio.run(driver.run(max_passes=run_passes, on_pass=_emit))
    if driver.halted:
        raise api.typer.Exit(1)


def impl__merged_batch_groups(api, cfg, entry, main, beads):
    integration = api.config.integration_branch(cfg, entry)
    groups = {api.schedule_mod.batch_group(b) for b in beads}
    groups.discard("")
    merged: set[str] = set()
    for g in groups:
        branch = f"{api.worktree.WT_PREFIX}{api.worktree.BATCH_BRANCH_PREFIX}{g}"
        if api.worktree._branch_exists(main, branch) and api.worktree.is_merged(
            entry, branch, integration
        ):
            merged.add(g)
    return merged


def impl_schedule_payload(api, epic, cfg, entry, main):
    children = api.bd.json(["list", "--parent", epic], main)
    if not isinstance(children, list):
        raise ValueError(f"cannot list children of {epic} — is it an epic in this hive?")
    beads = [c for c in children if str(c.get("status", "")) != "closed"]
    by_id = {str(b.get("id")): b for b in beads if b.get("id")}
    from . import localloop

    mode = api.config.dispatch_mode(cfg, entry)
    max_size = api.config.batch_max_size(cfg, entry)
    collapse = mode == "collapsed" or (
        mode == "auto"
        and api.schedule_mod.auto_should_collapse(
            beads, budget=api.config.dispatch_auto_budget(cfg, entry)
        )
    )
    if collapse:
        sched = api.schedule_mod.plan_schedule(
            beads,
            max_size=max_size,
            force_single_group=True,
            max_beads_per_session=api.config.dispatch_max_beads_per_session(cfg, entry),
        )
    else:
        merged_groups = api._merged_batch_groups(cfg, entry, main, beads)
        sched = api.schedule_mod.plan_schedule(
            beads, max_size=max_size, merged_groups=merged_groups
        )
    routes = api.config.routing_tiers(cfg, entry)
    policy = api.config.routing_policy(cfg, entry)
    harness = api.config.harness_name(cfg, entry)
    gateway = api.model_routing.GatewayAvailabilityAdapter()
    defaults = api.model_routing.HarnessAvailabilityAdapter()
    dev_availability = api.model_routing.discover_availability(
        routes, role="developer", harness=harness, gateway=gateway, harness_defaults=defaults
    )
    coord_availability = api.model_routing.discover_availability(
        routes, role="dispatcher", harness=harness, gateway=gateway, harness_defaults=defaults
    )

    def _decision(ids, *, role, availability):
        members = [by_id[i] for i in ids if i in by_id]
        decision = api.schedule_mod.resolve_launch_decision(
            members,
            policy=policy,
            role=role,
            harness=harness,
            routes=routes,
            availability=availability,
        )
        result = decision.as_dict()
        result["model"] = result["selected_model"]
        result["mode"] = (
            "headless-safe" if localloop.headless_capable(role) else "attached-required"
        )
        return result

    max_depth = api.config.dispatch_max_depth(cfg, entry)
    coord_dispatch = "nested-coordinator Task" if max_depth >= 1 else "separate supervised session"
    groups = [
        {
            "kind": g.kind,
            "ids": list(g.ids),
            "reason": g.reason,
            **_decision(g.ids, role="developer", availability=dev_availability),
        }
        for g in sched.groups
    ]
    coordinators = [
        {
            "id": c,
            "dispatch": coord_dispatch,
            **_decision([c], role="dispatcher", availability=coord_availability),
        }
        for c in sched.coordinators
    ]
    singletons = [
        {"id": singleton, **_decision([singleton], role="developer", availability=dev_availability)}
        for singleton in sched.singletons
    ]
    payload = {
        "groups": groups,
        "singletons": singletons,
        "coordinators": coordinators,
        "max_depth": max_depth,
    }
    api._apply_start_gating(payload, beads, cfg, entry)
    return payload


def impl__apply_start_gating(api, payload, beads, cfg, entry):
    strategy = str(api.config.release_value(cfg, entry, "strategy", "") or "")
    if not strategy:
        return
    ordering = api.release_order.order_beads(
        beads, strategy=strategy, fix_churn_budget=api.config.release_fix_churn_budget(cfg, entry)
    )
    deferrals = api.schedule_mod.start_gate(
        beads, ordering.order, estimator=api.config.release_conflict_estimator(cfg, entry)
    )
    for _d in deferrals:
        api.otel.record_deferred_start({"bh.release.strategy": strategy})
    payload["release"] = {
        "strategy": strategy,
        "order": list(ordering.order),
        "deferred": [
            {"id": d.id, "likelihood": d.likelihood, "reason": d.reason} for d in deferrals
        ],
    }


def impl_schedule(api, epic, hive, as_json):
    cfg = api.config.load()
    entry, main, _target, _branch = api.worktree.locate(cfg, hive, epic)
    try:
        payload = api.schedule_payload(epic, cfg, entry, main)
    except ValueError as exc:
        api.typer.echo(f"✗ {exc}", err=True)
        raise api.typer.Exit(1) from None
    if as_json:
        api.typer.echo(api.json.dumps(payload, indent=2))
        return
    if not payload["groups"] and (not payload["singletons"]) and (not payload["coordinators"]):
        api.typer.echo("(no open children to schedule)")
        return

    def _routing_diagnostics(item):
        for warning in item.get("warnings", []):
            api.typer.echo(f"    ⚠ {warning}")
        if item.get("blocked"):
            api.typer.echo(f"    ✗ {item['selection_reason']}; {item['remediation']}")

    for c in payload["coordinators"]:
        model = c["selected_model"] or "BLOCKED"
        api.typer.echo(f"◆ coordinator {c['id']}  — child epic → {c['dispatch']} (model: {model})")
        _routing_diagnostics(c)
    for g in payload["groups"]:
        api.typer.echo(
            f"▸ group [{g['kind']}] {', '.join(g['ids'])}  — {g['reason']} "
            f"(model: {g['selected_model'] or 'BLOCKED'})"
        )
        if g["kind"] == "collapsed":
            api.typer.echo(
                f"    → {api.config.BINARY_ALIAS} work claim --group {','.join(g['ids'])}"
            )
        _routing_diagnostics(g)
    deferred = {d["id"]: d for d in payload.get("release", {}).get("deferred", [])}
    for singleton in payload["singletons"]:
        bead_id = singleton["id"]
        if bead_id in deferred:
            api.typer.echo(
                f"⏸ deferred {bead_id}  — {deferred[bead_id]['reason']} "
                "(start-gate: hold behind queue)"
            )
        else:
            model = singleton["selected_model"] or "BLOCKED"
            api.typer.echo(f"· single {bead_id} (model: {model})")
            _routing_diagnostics(singleton)
