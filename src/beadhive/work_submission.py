"""Check, submit, and review-gate lifecycle orchestration.

Mutable collaborators are supplied explicitly by the stable :mod:`beadhive.work` facade on
each call, keeping ledger and gate state transitions in one boundary without importing it.
"""

from __future__ import annotations

import contextlib
from pathlib import Path


def impl_check(api, bead, hive):
    """Resolve the target, then hold admission across its entire validation lifecycle."""
    cfg = api.config.load()
    entry, _main, _target, _branch = api.worktree.locate(cfg, hive, bead)
    with api.validation_admission.host_slot(cfg, entry):
        return _impl_check_unadmitted(api, bead, hive)


def _impl_check_unadmitted(api, bead, hive):
    api.otel.set_bead(bead)
    cfg = api.config.load()
    entry, main, target, _branch = api.worktree.locate(cfg, hive, bead)
    grp, batch_target = api._batch_worktree(cfg, hive, bead, main)
    if grp:
        if batch_target is None:
            api.typer.echo(api._batch_member_procedure_msg(bead, grp), err=True)
            raise api.typer.Exit(1)
        target = batch_target
    elif not target.exists():
        api.typer.echo(f"✗ no worktree for {bead} — claim it first", err=True)
        raise api.typer.Exit(1)
    if not api.worktree.in_bead_worktree(target):
        api.typer.echo(
            "WARNING: cwd is not the bead worktree — uncommitted edits here are invisible.\n"
            f'  → cd "{target}"  # work happens in the worktree, NOT the main clone',
            err=True,
        )
    cmd = api.config.validate_cmd(cfg, entry)
    sha = api.worktree.head_full_sha(target)
    clean_sha = api._checked_sha(target)
    tree = api.validation_ledger.tree_of(entry, clean_sha) if clean_sha else ""
    artifact_root_config = api.config.work_value(cfg, entry, "validation_artifact_root", "")
    try:
        api.validation_records.artifact_root(main, artifact_root_config)
    except ValueError as exc:
        api.typer.echo(f"✗ {exc}", err=True)
        raise api.typer.Exit(2) from None
    run_record = api.validation_records.begin_run(
        main,
        bead=bead,
        phase="check",
        branch=_branch,
        worktree=target,
        sha=sha,
        tree=tree,
        command_hash=api.validation_ledger.cmd_hash(cmd),
        command=cmd,
        owner_start=api.worktree._pid_start(api.os.getpid()),
        artifact_root_config=artifact_root_config,
    )
    try:
        api.worktree.run_init(cfg, entry, target, verify_only=True)
    except BaseException:
        if run_record is not None:
            api.validation_records.finish_run(main, run_record["run_id"], reason="setup_failure")
            api.validation_records.record_use(
                main,
                run_id=run_record["run_id"],
                bead=bead,
                phase="check",
                branch=_branch,
                worktree=target,
                sha=sha,
                tree=tree,
                command_hash=api.validation_ledger.cmd_hash(cmd),
                reused=False,
            )
        raise
    v_start = api.time.perf_counter()
    artifacts = (run_record or {}).get("artifacts") or {}
    if artifacts:
        drop_context = api.test_report.drop_zone(Path(artifacts["reports"]))
        log_context = contextlib.nullcontext(Path(artifacts["gate_log"]))
    else:
        drop_context = api.test_report.drop_zone()
        log_context = api.triage_store.gate_log()
    with drop_context as drop, log_context as log:
        protocol_path = api.validation_records.protocol_path(
            drop, api.config.work_value(cfg, entry, "validation_protocol", "none")
        )
        child_env = api.test_report.export(api.otel.telemetry_neutral_env(), drop)
        if protocol_path is not None:
            child_env[api.validation_records.PROTOCOL_RESULT_ENV] = str(protocol_path)
        try:
            res = api.run(
                api.shlex.split(cmd),
                cwd=str(target),
                check=False,
                env=child_env,
                tee=log,
            )
        except BaseException:
            if run_record is not None:
                api.validation_records.finish_run(main, run_record["run_id"], reason="interrupted")
                api.validation_records.record_use(
                    main,
                    run_id=run_record["run_id"],
                    bead=bead,
                    phase="check",
                    branch=_branch,
                    worktree=target,
                    sha=sha,
                    tree=tree,
                    command_hash=api.validation_ledger.cmd_hash(cmd),
                    reused=False,
                )
            raise
        rc = res.returncode
        v_elapsed = api.time.perf_counter() - v_start
        report = api.test_report.ingest(drop, rc)
        if run_record is not None:
            api.validation_records.attach_summary(
                main,
                run_record["run_id"],
                {"counts": api.test_report.counts(report), "tree": tree},
            )
        missing = api.missing_binary(res)
        if run_record is not None:
            api.validation_records.finish_run(
                main,
                run_record["run_id"],
                exit_code=rc,
                signal_number=-rc if rc < 0 else None,
                reason=(
                    "missing_binary" if missing else "interrupted" if rc < 0 else "command_exit"
                ),
                protocol=api.validation_records.read_protocol(protocol_path),
            )
            if rc != 0 or not clean_sha:
                api.validation_records.record_use(
                    main,
                    run_id=run_record["run_id"],
                    bead=bead,
                    phase="check",
                    branch=_branch,
                    worktree=target,
                    sha=sha,
                    tree=tree,
                    command_hash=api.validation_ledger.cmd_hash(cmd),
                    reused=False,
                )
        api._record_check_verdict(
            entry,
            target,
            cmd,
            rc,
            report,
            drop,
            log,
            cfg=cfg,
            run_id=run_record["run_id"] if run_record else None,
            bead=bead,
            branch=_branch,
        )
    api.otel.record_validation_duration(
        v_elapsed,
        {
            "bh.work.phase": "check",
            "bh.validation.result": api._vres(rc),
            "bh.hive": api._hive(entry),
        },
    )
    api.otel.count_validation(rc == 0, {"bh.work.phase": "check"})
    api._mark_self_check(cfg, entry, target, rc)
    if rc != 0 and (not missing):
        api.converge.converge(entry, cfg, target, api._checked_sha(target), report)
    if missing:
        api.typer.echo(
            f"✗ validation could not RUN: `{missing}` is not on PATH "
            f"(validate_cmd is {cmd!r}). This is not a test failure — install it or fix "
            "PATH, then re-run.",
            err=True,
        )
    elif rc == api.RETRYABLE_VALIDATION_EXIT:
        api.typer.echo(
            f"⚠ validation could not complete (exit {rc}) — a network dependency was "
            "unreachable. This is NOT a test failure — retry once connectivity recovers.",
            err=True,
        )
    if rc != 0:
        raise api.typer.Exit(rc)


def impl__mark_self_check(api, cfg, entry, target, rc):
    if not api.otel.is_active():
        return
    sha = api._checked_sha(target)
    record = api.claim_authority.get_authority(api.config.claim_authority(cfg, entry)).read(target)
    api.otel.set_self_check(
        rc == 0,
        seat=record.seat if record else "",
        tree=api.validation_ledger.tree_of(entry, sha or api.worktree.head_full_sha(target)),
        dirty=not sha,
    )


def impl__record_check_verdict(
    api, entry, target, cmd, rc, report, drop, log, cfg, run_id=None, bead=None, branch=None
):
    sha = api._checked_sha(target)
    if not sha:
        return
    api.triage_store.store(entry, sha, cmd, rc, report, drop, log, run_id=run_id)
    if rc == 0:
        api.validation_ledger.record(
            entry,
            sha,
            cmd,
            rc,
            report=report,
            cfg=cfg,
            run_id=run_id,
            phase="check",
            bead=bead,
            branch=branch,
            worktree=target,
        )
        api.converge.warn_flakes(entry, sha, rc)


def impl__checked_sha(api, target):
    return api.worktree.head_full_sha(target) if api.worktree.is_clean(target) else ""


def impl__guard_fork_remote(api, entry, remote):
    if str((entry or {}).get("kind", "")) == "external" and remote == api.worktree.UPSTREAM_REMOTE:
        api.typer.echo(
            "✗ refusing to push an external hive's branch to 'upstream' — it's the fork "
            "(origin) or nothing; check work.push_remote",
            err=True,
        )
        raise api.typer.Exit(1)


def impl_submit(api, bead, as_, hive, group):
    cfg = api.config.load()
    api.guard.guard_primary(hive, cfg=cfg, verb="work submit")
    group = api.work_logic.opt_str(group)
    if group:
        if bead:
            api.typer.echo("✗ pass either <id> or --group, not both", err=True)
            raise api.typer.Exit(1)
        api.work_group.submit_group(cfg, hive, group, as_)
        return
    if not bead:
        api.typer.echo("✗ pass a bead <id> (or --group <ids> for a batch)", err=True)
        raise api.typer.Exit(1)
    api.otel.set_bead(bead)
    entry, main, target, branch = api.worktree.locate(cfg, hive, bead)
    api._guard_submit_worktree(bead, main, target)
    actor = api._resolve_submit_actor(cfg, entry, target, bead, main, as_)
    api._guard_claim_fence(cfg, entry, target, hive)
    base = api._guard_submit_ready(entry, target, branch, bead, cfg)
    api._warn_submit_release_hint(bead, main, entry, branch, base)
    api._validate_submit_checkout(entry, branch, cfg, bead=bead)
    sha = api.worktree.head_sha(target)
    api._record_submit_commits(bead, main, entry, branch, base)
    gate, reuse = api._open_submit_gate(cfg, entry, bead, branch, main, sha)
    api._push_state(cfg, main, actor, f"submit {bead} @ {sha}")
    api.otel.count_bead_transition("review_pending", {"bh.review.gate": gate})
    verb = "reused open" if reuse else "opened"
    api.typer.echo(f"✓ submitted {bead} @ {sha} — {verb} {gate} review gate (worktree left intact)")


def impl__record_submit_commits(api, bead, main, entry, branch, base):
    try:
        shas = api.worktree.commit_shas(entry, branch, base)
        if shas:
            api.git_linkage.record_commits(bead, main, shas)
    except Exception as exc:
        api.typer.echo(f"⚠ failed to record commit linkage for {bead}: {exc}", err=True)


def impl__guard_submit_worktree(api, bead, main, target):
    if target.exists():
        return
    grp = api.work_group.batch_label(api.bd.show(bead, main))
    if grp:
        api.typer.echo(api._batch_member_procedure_msg(bead, grp), err=True)
    else:
        api.typer.echo(f"✗ no worktree for {bead} — claim it first", err=True)
    raise api.typer.Exit(1)


def impl__resolve_submit_actor(api, cfg, entry, target, bead, main, as_):
    authority = api.claim_authority.get_authority(api.config.claim_authority(cfg, entry))
    record = authority.read(target)
    claim_holder = record.seat if authority.verify(record, "submit", "") else ""
    actor = api.identity.resolve_actor(
        api.work_logic.opt_str(as_),
        claim_holder or api.config.work_identity(cfg, entry)["name"] or "",
    )
    api._guard_holds_claim(api.bd.show(bead, main), actor, bead)
    if not api.worktree.in_bead_worktree(target):
        api.typer.echo(
            "WARNING: cwd is not the bead worktree — ensure all changes are committed.\n"
            f'  → cd "{target}"  # work happens in the worktree, NOT the main clone',
            err=True,
        )
    return actor


def impl__guard_claim_fence(api, cfg, entry, target, hive):
    authority = api.claim_authority.get_authority(api.config.claim_authority(cfg, entry))
    api.guard.guard_claim_epoch(authority.read(target), hive, cfg=cfg, verb="work submit")


def impl__guard_submit_ready(api, entry, target, branch, bead, cfg):
    if not api.worktree.is_clean(target):
        api.typer.echo("✗ working tree not clean — commit or discard changes first", err=True)
        raise api.typer.Exit(1)
    cur = api.worktree.current_branch(target)
    if cur != branch:
        api.typer.echo(f"✗ on branch {cur or '(detached)'}, expected {branch}", err=True)
        raise api.typer.Exit(1)
    base = api.worktree.integration_base(entry, bead, api.config.integration_branch(cfg, entry))
    count, subjects = api.worktree.history(entry, branch, base)
    ok, msg = api._history_ok(count, subjects, api.config.max_commits(cfg, entry))
    if not ok:
        api.typer.echo(f"✗ {msg}", err=True)
        raise api.typer.Exit(1)
    return base


def impl__warn_submit_release_hint(api, bead, main, entry, branch, base):
    warn = api.work_logic.reconcile_release_hint(
        api.work_logic.release_hint(api.bd.show(bead, main)),
        api.worktree.commit_messages(entry, branch, base),
    )
    if warn:
        api.typer.echo(f"⚠ {warn}", err=True)


def impl__validate_submit_checkout(api, entry, branch, cfg, bead=None):
    main = api.registry.hive_dir(entry)
    sha = api.worktree._branch_sha(entry, branch)
    tree = api.validation_ledger.tree_of(entry, sha)
    command = api.config.validate_cmd(cfg, entry, "submit")
    command_hash = api.validation_ledger.cmd_hash(command)
    for active in api.validation_records.running_runs(main, bead=bead, tree=tree):
        owner = active.get("owner") or {}
        pid = owner.get("pid")
        token = owner.get("start_token")
        owner_dead = False
        if owner.get("host") == api.host.host_id() and isinstance(pid, int):
            owner_dead = not api.worktree._pid_alive(pid)
            if not owner_dead and token:
                current_token = api.worktree._pid_start(pid)
                owner_dead = bool(current_token and current_token != token)
        if owner_dead:
            api.validation_records.abandon_run(main, active["run_id"], reason="owner_dead")
            continue
        details = (
            f"run {active['run_id']} owned by pid {pid} (start {token or 'unprobeable'}), "
            f"phase {active.get('phase') or 'validation'}"
        )
        api.typer.echo(f"  → validation already active: {details}; waiting for that run")
        if active.get("command_hash") != command_hash:
            api.typer.echo(
                f"⚠ submit command conflicts with active {details}; wait for it or confirm its "
                "owner is dead before retrying. Use `bh work check`, not raw `just check`, when "
                "you intend to seed a reusable pre-submit verdict.",
                err=True,
            )
            raise api.typer.Exit(api.RETRYABLE_VALIDATION_EXIT)
    v_start = api.time.perf_counter()
    rc = api.worktree.clean_checkout(
        entry,
        branch,
        command,
        reuse=True,
        bead=bead,
        phase="submit",
    )
    api.otel.record_validation_duration(
        api.time.perf_counter() - v_start,
        {
            "bh.work.phase": "submit",
            "bh.validation.result": api._vres(rc),
            "bh.hive": api._hive(entry),
        },
    )
    api.otel.count_validation(rc == 0, {"bh.work.phase": "submit"})
    if rc == api.RETRYABLE_VALIDATION_EXIT:
        api.typer.echo(
            f"⚠ validation could not complete (exit {rc}) — a network dependency was "
            "unreachable; nothing submitted. This is NOT a policy verdict — retry "
            "`bh work submit` once connectivity recovers.",
            err=True,
        )
        raise api.typer.Exit(rc)
    if rc != 0:
        api.typer.echo(
            f"✗ clean-checkout validation failed (exit {rc}) — nothing submitted", err=True
        )
        raise api.typer.Exit(1)


def impl__open_submit_gate(api, cfg, entry, bead, branch, main, sha):
    gate = api.config.review_gate(cfg, entry)
    if gate.startswith("gh:") or str(entry.get("kind", "")) == "external":
        remote = api.config.push_remote(cfg, entry)
        api._guard_fork_remote(entry, remote)
        if api.worktree.push_branch(entry, branch, remote) != 0:
            api.typer.echo("✗ failed to push branch for review — nothing submitted", err=True)
            raise api.typer.Exit(1)
    reuse = api.work_logic.ensure_review_gate(main, bead, sha, gate)
    sres = api.bd.run(["set-state", bead, "review=pending", "--reason", f"submitted {sha}"], main)
    if sres.returncode != 0:
        api.typer.echo("✗ failed to set review state — nothing submitted", err=True)
        raise api.typer.Exit(1)
    return (gate, reuse)


def impl__person_of(api, name):
    return name.split("/", 1)[1] if "/" in name else name


def impl__guard_self_review(api, cfg, entry, data, actor, bead):
    author = str((data or {}).get("assignee") or "").strip()
    if not author or not actor or api._person_of(actor) != api._person_of(author):
        return
    mode = api.config.dispatch_reviewer_cross_seat(cfg, entry)
    if mode != "advise":
        api.typer.echo(
            f"✗ {bead}: self-review blocked — {actor!r} authored this bead "
            f"(as {author!r}); the reviewer cross-seat policy is `hard` (default). A "
            "different seat/person must approve; set "
            "`work.dispatch.reviewer_cross_seat: advise` to opt back into a warning.",
            err=True,
        )
        raise api.typer.Exit(1)
    from . import log

    log.get_logger(api.__name__).warning(
        "reviewer_cross_seat_self_review",
        bead=bead,
        actor=actor,
        author=author,
        policy=mode,
        reason="approver authored the bead (rubber-stamp risk); advise warns, hard "
        "(default) blocks",
    )
    api.typer.echo(
        f"⚠ {bead}: self-review — {actor!r} authored this bead (as {author!r}). "
        "Advisory only (reviewer cross-seat policy explicitly set to `advise`); the "
        "default `hard` policy would block this.",
        err=True,
    )


def impl_approve(api, bead, as_, hive):
    api.otel.set_bead(bead)
    cfg = api.config.load()
    entry, main, _target, _branch = api.worktree.locate(cfg, hive, bead)
    actor = api.identity.resolve_actor(as_, api.config.work_identity(cfg, entry)["name"] or "")
    data = api.bd.show(bead, main)
    api._guard_open(data, bead)
    gates = api._open_gates(main)
    open_review, _resolved = api.work_logic.review_gates(bead, main)
    if api._approve_security_gate(gates, bead, main, actor, open_review):
        return
    if api._approve_release_hold_gate(gates, bead, main, actor, open_review):
        return
    if not open_review:
        api.typer.echo(f"✗ no open review gate for {bead} — nothing to approve", err=True)
        raise api.typer.Exit(1)
    api._guard_human_review_gate(open_review, bead)
    api._guard_self_review(cfg, entry, data, actor, bead)
    resolved_ids = api._resolve_review_gates(open_review, bead, main, actor)
    api._clear_stale_review_state(bead, data, main, actor)
    api.otel.count_bead_transition("approved", {"bh.review.gate": "human"})
    api.typer.echo(
        f"✓ approved {bead}: resolved review gate(s) {', '.join(resolved_ids)} as {actor}"
    )


def impl__approve_security_gate(api, gates, bead, main, actor, open_review):
    security = api._security_gate(gates, bead)
    if (
        security is None
        or str(security.get("status")) != "open"
        or (not (api.guard.is_warden(actor) or not open_review))
    ):
        return False
    api.guard.guard_security_gate_resolution(security, actor)
    sec_id = str(security.get("id") or "")
    sres = api.bd.run(
        ["gate", "resolve", sec_id, "--reason", f"security cleared by {actor}"], main, actor=actor
    )
    if sres.returncode != 0:
        api.typer.echo(f"✗ failed to resolve security gate {sec_id} for {bead}", err=True)
        raise api.typer.Exit(sres.returncode or 1)
    api.otel.count_bead_transition("security_cleared", {"bh.assurance.gate": "security"})
    api.typer.echo(f"✓ cleared {bead}: resolved security gate {sec_id} as {actor}")
    return True


def impl__approve_release_hold_gate(api, gates, bead, main, actor, open_review):
    hold = api._release_hold_gate(gates, bead)
    if (
        hold is None
        or str(hold.get("status")) != "open"
        or (not (api.guard.is_releaser(actor) or not open_review))
    ):
        return False
    api.guard.guard_release_hold_gate_resolution(hold, actor)
    hold_id = str(hold.get("id") or "")
    hres = api.bd.run(
        ["gate", "resolve", hold_id, "--reason", f"release-hold cleared by {actor}"],
        main,
        actor=actor,
    )
    if hres.returncode != 0:
        api.typer.echo(f"✗ failed to resolve release-hold gate {hold_id} for {bead}", err=True)
        raise api.typer.Exit(hres.returncode or 1)
    api.typer.echo(f"✓ cleared {bead}: resolved release-hold gate {hold_id} as {actor}")
    return True


def impl__guard_human_review_gate(api, open_review, bead):
    non_human = next(
        (g for g in open_review if str(g.get("await_type") or "human") != "human"), None
    )
    if non_human is not None:
        await_type = str(non_human.get("await_type"))
        api.typer.echo(
            f"✗ {bead}'s review gate is a {await_type} gate — resolve it through its own "
            f"channel (CI / PR merge), not `{api.config.BINARY_ALIAS} work approve`",
            err=True,
        )
        raise api.typer.Exit(1)


def impl__resolve_review_gates(api, open_review, bead, main, actor):
    resolved_ids = []
    for gate in open_review:
        gate_id = str(gate.get("id") or "")
        res = api.bd.run(
            ["gate", "resolve", gate_id, "--reason", f"approved by {actor}"], main, actor=actor
        )
        if res.returncode != 0:
            api.typer.echo(f"✗ failed to resolve review gate {gate_id} for {bead}", err=True)
            raise api.typer.Exit(res.returncode or 1)
        resolved_ids.append(gate_id)
    return resolved_ids


def impl__clear_stale_review_state(api, bead, data, main, actor):
    if api.bd.state(bead, "review", main) == "changes-requested":
        api.bd.run(
            [
                "set-state",
                bead,
                "review=approved",
                "--reason",
                f"approved by {actor} (clears stale changes-requested)",
            ],
            main,
            actor=actor,
        )
    else:
        api._clear_review_label(bead, data, main, actor)


def impl_bounce(api, bead, message, as_, hive):
    api.otel.set_bead(bead)
    cfg = api.config.load()
    entry, main, _target, _branch = api.worktree.locate(cfg, hive, bead)
    actor = api.identity.resolve_actor(as_, api.config.work_identity(cfg, entry)["name"] or "")
    data = api.bd.show(bead, main)
    api._guard_open(data, bead)
    reason = api.work_logic.opt_str(message).strip()
    open_review, _resolved = api.work_logic.review_gates(bead, main)
    if not open_review:
        api.typer.echo(
            f"⚠ {bead}: no open review gate to resolve — recording the bounce anyway", err=True
        )
    gate_reason = f"changes requested by {actor}" + (f": {reason}" if reason else "")
    for gate in open_review:
        gate_id = str(gate.get("id") or "")
        res = api.bd.run(["gate", "resolve", gate_id, "--reason", gate_reason], main, actor=actor)
        if res.returncode != 0:
            api.typer.echo(f"✗ failed to resolve review gate {gate_id} for {bead}", err=True)
            raise api.typer.Exit(res.returncode or 1)
    sres = api.bd.run(
        ["set-state", bead, "review=changes-requested", "--reason", gate_reason], main, actor=actor
    )
    if sres.returncode != 0:
        api.typer.echo(f"✗ failed to set review state on {bead}", err=True)
        raise api.typer.Exit(sres.returncode or 1)
    api.otel.count_bead_transition("changes_requested", {"bh.review.gate": "human"})
    api.typer.echo(
        f"✓ bounced {bead} (review=changes-requested) as {actor} — developer picks it "
        f"up with `{api.config.BINARY_ALIAS} work resume {bead}`"
    )
