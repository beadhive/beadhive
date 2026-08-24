"""Molecule, bead, and PR landing orchestration.

The stable :mod:`beadhive.work` facade supplies mutable collaborators explicitly on every
call, while merge-slot serialization and integration primitives remain owned by their existing
policy modules.
"""

from __future__ import annotations


def impl__delete_branch(api, main, branch):
    """Best-effort delete of a landed molecule branch. The molecule already landed, so a failure
    here only warns (leaving a stale ref the coordinator can drop). GIT_* dir-pointing env is
    scrubbed so our explicit `-C <main>` always wins."""
    env = {k: v for k, v in api.os.environ.items() if not k.startswith("GIT_")}
    res = api.run(
        ["git", "-C", str(main), "branch", "-d", branch], check=False, capture=True, env=env
    )
    if res.returncode != 0:
        api.typer.echo(f"⚠ landed but failed to delete {branch} — delete it manually", err=True)


def impl__teardown_coordinator_seat(api, cfg, hive, epic):
    """Best-effort removal of a coordinator seat worktree after its molecule lands (mirrors
    `merge --rm`). Runs BEFORE `_delete_branch` so the container branch isn't checked out (a
    `git branch -d` on a still-attached branch fails). No-op when the seat was never provisioned
    (a Phase-A / separate-merger land drove from the main clone) — a removal failure only warns,
    never blocks the completed land."""
    _entry, _main, target, _branch = api.worktree.locate(cfg, hive, epic, kind="epic")
    if not target.exists():
        return
    try:
        api.worktree.remove(hive, epic, force=True)
    except api.typer.Exit:
        api.typer.echo(
            f"⚠ landed but failed to remove coordinator seat {target} — remove it manually",
            err=True,
        )


def impl__rollback_or_keep(api, entry, main, base, pre, slot_attrs):
    """Handle a RED post-merge re-validation while still holding the slot: roll `base` back to its
    pre-merge sha `pre` IFF the branch is safe to rewrite (local/unpushed), else leave the merge
    bubble standing (a shared/pushed branch is fixed FORWARD, never reset). Emits the
    rolled_back/red_kept merge-outcome metric. Returns True iff the tip was rolled back — the caller
    renders the (site-specific) message and any bead bounce."""
    base_clone = api.worktree.clone_for_branch(entry, base)
    rolled = (
        api.worktree.safe_to_rewrite(main, base) and api.worktree.reset_hard(base_clone, pre) == 0
    )
    how = "rolled_back" if rolled else "red_kept"
    api.otel.count_merge_outcome({**slot_attrs, "bh.merge.how": how})
    return rolled


def impl__pr_ref(api, pr):
    """The human/bd-facing 'PR #<n> <url>' handle for a gh PR row."""
    num = str((pr or {}).get("number") or "").strip()
    url = str((pr or {}).get("url") or "").strip()
    return " ".join(x for x in (f"PR #{num}" if num else "PR", url) if x)


def impl__close_swarm_bead(api, epic, main):
    """Close the swarm orchestration bead(s) created over `epic` at kickoff (bh-7tno): without
    this every landed molecule leaves one permanent open type:molecule bead behind, silting up
    `work list` until a manual groom sweep. Best-effort — a failure warns, never unwinds a
    completed land. Batched into ONE `bd close` for every still-open match (`bd close` accepts
    multiple ids) instead of a subprocess-per-swarm loop."""
    data = api.bd.json(["swarm", "list"], main)
    swarms = data.get("swarms") if isinstance(data, dict) else None
    ids = [
        str(sw.get("id") or "")
        for sw in swarms or []
        if str(sw.get("epic_id")) == epic and str(sw.get("status", "")) != "closed" and sw.get("id")
    ]
    if not ids:
        return
    if api.bd.run(["close", *ids, "--reason", f"molecule {epic} landed"], main).returncode != 0:
        api.typer.echo(
            f"⚠ landed but failed to close swarm bead(s) {', '.join(ids)} — close manually",
            err=True,
        )


def impl__pr_merge_gates(api, bead, main):
    """The OPEN `pr-merge` gates blocking `bead` — the landing-PR analog of `review_gates`
    (same description-marker selector convention, bh-c3il)."""
    return [
        g
        for g in api.work_logic._bead_gates(bead, main)
        if str(g.get("status")) == "open" and "pr-merge" in str(g.get("description") or "").lower()
    ]


def impl__ensure_pr_gate(api, main, bead, ref):
    """Idempotently open the bd `gh:pr` gate that blocks `bead` until its landing PR merges —
    bd's own gate check/discover watcher machinery can resolve it, and `work land` resolves any
    survivor at close. Reuses an already-open pr-merge gate on re-runs (submit's reuse rule)."""
    gates = api._pr_merge_gates(bead, main)
    if gates:
        api.typer.echo(f"• gh:pr gate {gates[0].get('id')} already open for {bead} — reusing it")
        return
    g = api.bd.run(
        ["gate", "create", "--blocks", bead, "--type", "gh:pr", "--reason", f"pr-merge {ref}"], main
    )
    if g.returncode != 0:
        opened = [
            gg
            for gg in api._pr_merge_gates(bead, main)
            if f"pr-merge {ref}" in str(gg.get("description") or "")
        ]
        if not opened:
            api.typer.echo(
                "✗ PR opened but failed to open the gh:pr gate — re-run the merge to retry",
                err=True,
            )
            raise api.typer.Exit(1)
        api.typer.echo(
            "· gh:pr gate opened without a blocking dep (bd refuses blocks edges onto epics)"
        )


def impl__open_landing_pr(api, cfg, entry, main, bead, data, branch, base):
    """The `work.landing: pr` boundary — landing onto the SHARED integration branch of a
    PR-only-main repo. Instead of a local --no-ff merge: push the branch (work.push_remote) and
    open a GitHub PR against `base` (title from the bead digest, body carries id + acceptance),
    record the PR on the bead, and leave the bead/epic OPEN in a `landing=pr-pending` condition
    behind a `gh:pr` gate. CI on the PR takes over the postland-validation role; the close (with
    the squash-proof close_reason) fires from `work land` once GitHub reports the PR merged.
    Idempotent: a re-run reuses the open PR and its gate."""
    if not api.ghpr.available():
        api.typer.echo(
            "✗ work.landing is 'pr' but `gh` is not on PATH — install gh or set landing: local",
            err=True,
        )
        raise api.typer.Exit(1)
    remote = api.config.push_remote(cfg, entry)
    api._guard_fork_remote(entry, remote)
    if api.worktree.push_branch(entry, branch, remote) != 0:
        api.typer.echo(f"✗ failed to push {branch} to {remote} — nothing landed", err=True)
        raise api.typer.Exit(1)
    pr = api.ghpr.open_pr_for(entry, branch)
    if pr:
        api.typer.echo(f"• {api._pr_ref(pr)} already open for {branch} — reusing it")
    else:
        title = str(api._first(data, "title") or bead)
        acceptance = api._first(data, "acceptance_criteria", "acceptance") or ""
        body = f"Lands {bead} ({branch} → {base}) via `work.landing: pr`."
        if acceptance:
            body += f"\n\n## Acceptance\n{acceptance}"
        rc, out = api.ghpr.create_pr(entry, base, branch, title, body)
        if rc != 0:
            api.typer.echo(f"✗ `gh pr create` failed — nothing landed:\n{out}", err=True)
            raise api.typer.Exit(1)
        pr = api.ghpr.pr_from_url(out)
    ref = api._pr_ref(pr)
    api._ensure_pr_gate(main, bead, ref)
    if api.bd.run(["set-state", bead, "landing=pr-pending", "--reason", ref], main).returncode != 0:
        api.typer.echo(
            "⚠ PR opened but failed to record landing=pr-pending — set it by hand", err=True
        )
    api.otel.count_bead_transition("pr_pending")
    api.typer.echo(
        f"✓ opened {ref} for {bead} ({branch} → {base}); bead stays OPEN (pr-pending) — "
        f"`{api.config.BINARY_ALIAS} work land {bead}` once the PR merges"
    )


def impl__guard_molecule_children(api, epic, main):
    """Guard the molecule is complete — every child closed, except an adopted origin report,
    linked child-of the epic as PROVENANCE, not molecule work — it carries no acceptance and never
    gets worked/closed on its own, so it must never gate the land. Returns the origin-report
    children (the intended jf5k/jey0 behavior: the report rides the epic to completion) for the
    caller to auto-close once the epic lands. Children come from `bd.children`, which trusts the
    parent EDGE — bd's own `--parent` matches by dotted-id PREFIX, so a bead detached from this
    epic used to gate the land forever on the strength of its id alone (bh-89mrf)."""
    children = api.bd.children(epic, main)
    if not isinstance(children, list):
        api.typer.echo(f"✗ cannot list children of {epic} — refusing to land", err=True)
        raise api.typer.Exit(1)
    origin_reports = [c for c in children if api.adopt.is_origin_report(c.get("labels"))]
    open_kids = [
        str(c.get("id"))
        for c in children
        if str(c.get("status", "")) != "closed"
        and (not api.adopt.is_origin_report(c.get("labels")))
    ]
    if open_kids:
        api.typer.echo(
            f"✗ molecule {epic} incomplete — open child issue(s): {', '.join(open_kids)}", err=True
        )
        raise api.typer.Exit(1)
    return origin_reports


def impl__guard_molecule_land_base(api, entry, epic, integration):
    """Recursive land (xn3o.7): resolve the land target one tier up via the integration_base climb,
    so `finish <container>` lands wt/bead/epic/<container> onto its nearest container ancestor —
    a top-level epic onto main (byte-identical to the old hardcoded target), a nested epic
    <ws>.<epic> onto its workstream container. Guards a container parent-link ambiguity and a
    closed-epic land target before resolving."""
    conflict = api.worktree.container_conflict(entry, epic, integration)
    if conflict:
        id_base, link_base = conflict
        api.typer.echo(
            f"✗ {epic}: container ambiguity — the dotted id resolves to {id_base} but the "
            f"parent-child link resolves to {link_base}. A re-parent/split left both containers "
            "live; refusing to land onto a guessed container. Reconcile the parent link, retry.",
            err=True,
        )
        raise api.typer.Exit(1)
    base = api.worktree.integration_base(entry, epic, integration)
    if api.worktree.container_epic_closed(entry, base):
        api.typer.echo(
            f"✗ {epic}: land target {base} belongs to a CLOSED epic — refusing to resurrect "
            f"a landed container. Re-parent {epic} onto a live container and retry.",
            err=True,
        )
        raise api.typer.Exit(1)
    return base


def impl__open_molecule_pr(api, cfg, entry, main, epic, epic_data, mol_branch, base, mode):
    """PR-only-main landing (work.landing: pr): a molecule landing onto the SHARED integration
    branch publishes as a PR instead of local-merging. The assembled molecule is still validated
    from a clean checkout first (a red molecule never reaches the PR either); the
    postland/combined validation role passes to CI on the PR. Reuses an exact-tree verdict on the
    same terms as the local-land path (`_validate_molecule_checkout`)."""
    if mode != "loose":
        rc = api.worktree.clean_checkout(
            entry, mol_branch, api.config.validate_cmd(cfg, entry, "molecule"), reuse=True
        )
        api.otel.count_validation(rc == 0, {"bh.work.phase": "molecule"})
        if rc != 0:
            api.typer.echo(f"✗ molecule validation failed (exit {rc}) — no PR opened", err=True)
            raise api.typer.Exit(rc)
    api._open_landing_pr(cfg, entry, main, epic, epic_data, mol_branch, base)


def impl__validate_molecule_checkout(api, entry, mol_branch, cfg, mode):
    """Validate the ASSEMBLED molecule from a clean checkout before landing — the land must not
    depend on dirty local state, and a red molecule never reaches the integration line. `loose`
    trusts the per-bead submits and skips even this. Raises on a red result.

    `reuse=True` — LANDING-BOUNDARY REUSE, ADR Decision 4 (bh-ku9n9.17). The ledger is keyed on
    (TREE, cmd_hash) since bh-ku9n9.3, so a hit IS the exact-tree-match test and nothing else can
    hit: same patch on a different base, a subtree, a moved base, a changed command, a stale entry
    and a red verdict all miss and run the gate for real. That is the whole condition Decision 4
    relaxes to, so there is deliberately no second tree comparison layered on top of the key —
    one source of truth for "same bytes", and it is the lookup. The last bead to land onto
    mol/<epic> already validated this exact tree; re-running it here proves nothing new."""
    if mode == "loose":
        return
    v_start = api.time.perf_counter()
    rc = api.worktree.clean_checkout(
        entry, mol_branch, api.config.validate_cmd(cfg, entry, "molecule"), reuse=True
    )
    api.otel.record_validation_duration(
        api.time.perf_counter() - v_start,
        {
            "bh.work.phase": "molecule",
            "bh.validation.result": api._vres(rc),
            "bh.hive": api._hive(entry),
        },
    )
    api.otel.count_validation(rc == 0, {"bh.work.phase": "molecule"})
    if rc != 0:
        api.typer.echo(f"✗ molecule validation failed (exit {rc}) — nothing landed", err=True)
        raise api.typer.Exit(rc)


def impl__postland_revalidate_molecule(
    api, cfg, entry, main, base, pre, mode, stale, epic, mol_branch, slot_attrs
):
    """Post-land re-validation of the integration tip. Runs under `conservative` always, and as a
    correctness backstop under `relaxed` when main moved (stale). Still holding the slot, so a red
    tip is reset to its pre-land sha before release — no one ever sees a broken main. Raises on an
    unrecoverable red result. Reuses an exact-tree verdict (Decision 4, see
    `_validate_molecule_checkout`): a land onto an UNMOVED base produces a merge commit whose tree
    is byte-identical to the molecule's, which the pre-land run just proved — and the `stale` arm
    above is exactly the case where the base MOVED, so that tree is new and the lookup misses."""
    if mode == "conservative" or (mode != "loose" and stale):
        vrc = api.worktree.clean_checkout(
            entry, base, api.config.validate_cmd(cfg, entry, "postland"), reuse=True
        )
        api.otel.count_validation(vrc == 0, {"bh.work.phase": "postland"})
        if vrc != 0:
            if api._rollback_or_keep(entry, main, base, pre, slot_attrs):
                api.typer.echo(
                    f"✗ post-land validation failed (exit {vrc}) — the integration tip is RED "
                    f"after landing {epic} (main moved underneath it). Rolled {base} back to "
                    f"{pre[:7]}; {mol_branch} preserved, epic still open. Rebase the molecule "
                    f"on {base} and re-run the wrap-up.",
                    err=True,
                )
            else:
                api.typer.echo(
                    f"✗✗ post-land validation failed (exit {vrc}) — {base} is RED after "
                    f"landing {epic} (main moved underneath it), and {base} is shared "
                    "(pushed) so it is NOT rewritten. The merge bubble stands; epic left "
                    "open. Fix forward: revert the bubble or land a follow-up fix.",
                    err=True,
                )
            raise api.typer.Exit(vrc)
    elif mode == "loose" and stale:
        api.typer.echo(
            f"⚠ main advanced under {epic}; skipping post-land revalidation per loose mode — "
            f"{base} may be red",
            err=True,
        )


def impl__close_molecule_origin_reports(api, origin_reports, epic, main):
    """Auto-close any adopted origin report now that its epic has landed: the report is
    provenance that rides the epic to completion, so it closes WITH the molecule rather than
    lingering open forever. Best-effort — a close failure only warns, never unwinds a completed
    land. Batched into ONE `bd close` for every still-open report (`bd close` accepts multiple
    ids) instead of a subprocess-per-report loop."""
    ids = [str(r.get("id")) for r in origin_reports if str(r.get("status", "")) != "closed"]
    if not ids:
        return
    if api.bd.run(["close", *ids, "--reason", f"adopted epic {epic} landed"], main).returncode != 0:
        api.typer.echo(
            f"⚠ landed but failed to close origin report(s) {', '.join(ids)} — close manually",
            err=True,
        )


def impl__reconcile_landed_molecule(api, cfg, entry, main, epic, epic_data, mol_branch, base, hive):
    """Finish the bookkeeping half of a molecule land whose CODE already landed (bh-lvqs).

    The molecule twin of `_reconcile_landed_bead`, and the one with the least forgiving failure
    mode: `merge_no_ff` over an already-merged container succeeds with "Already up to date", so
    the old path could re-run forever, reporting nothing wrong while the epic stayed open and its
    seat worktree and container branch stayed alive. Reconcile does the tail the first run missed —
    close the epic, ride the origin reports and swarm bead down with it, tear the seat down, delete
    the container — and exits 0."""
    origin_reports = api._guard_molecule_children(epic, main)
    with api.work_group.merge_slot(
        main, {"bh.merge.kind": "molecule", "bh.hive": api._hive(entry)}
    ):
        closed = api.work_logic.close_merged(epic, main, "molecule landed", data=epic_data)
        api._close_molecule_origin_reports(origin_reports, epic, main)
        api._close_swarm_bead(epic, main)
        api._teardown_coordinator_seat(cfg, hive, epic)
        api._delete_branch(api.worktree.clone_for_branch(entry, base), mol_branch)
    if not closed:
        assignee = str(epic_data.get("assignee") or "").strip()
        api.typer.echo(
            f"✗ molecule {epic} is ALREADY LANDED ({mol_branch} → {base}) but {epic} "
            f"could not be closed{f' (assignee {assignee!r})' if assignee else ''} — close it "
            f"manually; the molecule is on {base}, do NOT re-land it",
            err=True,
        )
        raise api.typer.Exit(1)
    api.typer.echo(
        f"✓ molecule {epic} was already landed ({mol_branch} → {base}) — reconciled "
        f"bookkeeping (closed {epic}, tore down the seat, deleted the container; no re-merge)"
    )


def impl__merge_molecule(api, cfg, epic, hive):
    """The molecule wrap-up / land: collapse a whole assembled `mol/<epic>` onto the hive
    integration branch as ONE `--no-ff` bubble (the bead merges live inside it). Guards the
    molecule is complete (every child closed) + clean, holds the hive merge slot, validates the
    assembled branch from a clean checkout, lands it, closes the epic, and deletes the branch.
    On conflict / validation failure it aborts and releases the slot — never drops work."""
    entry, main, _target, _branch = api.worktree.locate(cfg, hive, epic)
    epic_data = api.bd.show(epic, main)
    api._guard_open(epic_data, epic)
    mol_branch = f"{api.worktree._BEAD_PREFIX}epic/{epic}"
    if not api.worktree._branch_exists(main, mol_branch):
        api.typer.echo(f"✗ no container branch {mol_branch} — was {epic} kicked off?", err=True)
        raise api.typer.Exit(1)
    origin_reports = api._guard_molecule_children(epic, main)
    if not api.worktree.is_clean(main):
        api.typer.echo(f"✗ main clone {main} not clean — cannot land molecule", err=True)
        raise api.typer.Exit(1)
    integration = api.config.integration_branch(cfg, entry)
    base = api._guard_molecule_land_base(entry, epic, integration)
    if api.already_landed(entry, mol_branch, base):
        api._reconcile_landed_molecule(cfg, entry, main, epic, epic_data, mol_branch, base, hive)
        return
    api._guard_signed_history(entry, mol_branch, base, cfg)
    mode = api.config.validation_mode(cfg, entry)
    if base == integration and api.config.work_landing(cfg, entry) == "pr":
        api._open_molecule_pr(cfg, entry, main, epic, epic_data, mol_branch, base, mode)
        return
    slot_attrs = {"bh.merge.kind": "molecule", "bh.hive": api._hive(entry)}
    started = api.time.perf_counter()
    with api.work_group.merge_slot(main, slot_attrs):
        api._validate_molecule_checkout(entry, mol_branch, cfg, mode)
        pre = api.worktree._ref_sha(main, base)
        stale = api.worktree.base_of(entry, mol_branch, base) != pre
        prof = api.config.work_identity(cfg, entry)
        agent = prof["mode"] == "agent"
        mrc, out = api.worktree.merge_no_ff(
            entry,
            mol_branch,
            base,
            name=prof["name"] or "" if agent else "",
            email=prof["email"] or "" if agent else "",
            signing_key=prof["signing_key"] or "" if agent else "",
            sign=prof["sign"] if agent else False,
            message=f"chore(merge): molecule {epic}",
        )
        if mrc != 0:
            api.otel.count_merge_outcome({**slot_attrs, "bh.merge.how": "conflict"})
            where = api.work_logic.record_merge_conflict(
                entry, mol_branch, base, main, [epic], "molecule land"
            )
            api.typer.echo(
                f"✗ molecule merge failed — aborted, nothing landed; bounced {epic} to "
                f"review=changes-requested (conflict in: {where}) — resolve in the {mol_branch} "
                f"seat, then re-run `{api.config.BINARY_ALIAS} work finish {epic}`:\n{out}",
                err=True,
            )
            raise api.typer.Exit(mrc)
        api._postland_revalidate_molecule(
            cfg, entry, main, base, pre, mode, stale, epic, mol_branch, slot_attrs
        )
        api._record_merge_commit(epic, main, base)
        api.otel.count_merge_outcome({**slot_attrs, "bh.merge.how": "no_ff"})
        closed = api.work_logic.close_merged(epic, main, "molecule landed", data=epic_data)
        api._close_molecule_origin_reports(origin_reports, epic, main)
        api._close_swarm_bead(epic, main)
        api._teardown_coordinator_seat(cfg, hive, epic)
        api._delete_branch(api.worktree.clone_for_branch(entry, base), mol_branch)
    api.otel.record_merge_duration(api.time.perf_counter() - started, {"bh.merge.kind": "molecule"})
    try:
        api._emit_cycle(epic_data, {"bh.merge.kind": "molecule", "bh.hive": api._hive(entry)})
    except Exception:
        pass
    api.otel.count_bead_transition("molecule_landed")
    if not closed:
        assignee = str(epic_data.get("assignee") or "").strip()
        api.typer.echo(
            f"✗ landed molecule {epic} ({mol_branch} --no-ff → {base}) but FAILED to "
            f"close {epic}{f' (assignee {assignee!r})' if assignee else ''} — close it manually",
            err=True,
        )
        raise api.typer.Exit(1)
    api.typer.echo(f"✓ landed molecule {epic} ({mol_branch} --no-ff → {base}); closed {epic}")


def impl_finish(api, epic, hive):
    """Coordinator/merger wrap-up: land a whole assembled molecule. Epic-only alias of
    `merge --molecule` — guards the bead is an epic, then validates the assembled `mol/<epic>`,
    lands it onto the integration branch as ONE `--no-ff` bubble, closes the epic, and deletes the
    branch. `merge --molecule <epic>` remains the equivalent."""
    api.otel.set_bead(epic)
    cfg = api.config.load()
    _entry, main, _target, _branch = api.worktree.locate(cfg, hive, epic)
    data = api.bd.show(epic, main)
    api._guard_open(data, epic)
    if not api._is_epic(data):
        api.typer.echo(f"✗ {epic} is not an epic — nothing to finish", err=True)
        raise api.typer.Exit(1)
    api._merge_molecule(cfg, epic, hive)


def impl_land(api, bead, hive):
    """Complete a `work.landing: pr` landing after GitHub merges the PR: confirm a MERGED PR
    with head `wt/bead/<type>/<id>` (`gh pr list --state merged --head …`), resolve the gh:pr
    gate, and close the bead with the squash-proof close_reason (`merged`; `molecule landed`
    for an epic) that `worktree prune`'s landed detection honors. Refuses while the PR is
    unmerged — completion is driven by PR STATE, never asserted (the operator escape hatch for
    an out-of-band landing is `worktree mark-landed`). For an epic it also closes adopted
    origin reports and tears down the coordinator seat, mirroring the local land; the pushed
    branch itself is left for `worktree prune` to reap."""
    api.otel.set_bead(bead)
    cfg = api.config.load()
    entry, main, _target, branch = api.worktree.locate(cfg, hive, bead)
    data = api.bd.show(bead, main)
    api._guard_open(data, bead)
    api._guard_land_pr_pending(bead, main)
    pr = api._resolve_merged_land_pr(entry, branch)
    ref = api._pr_ref(pr)
    api._resolve_land_pr_merge_gates(bead, main, ref)
    reason = "molecule landed" if api._is_epic(data) else "merged"
    if api.bd.run(["close", bead, "--reason", reason], main).returncode != 0:
        api.typer.echo(f"✗ PR merged but failed to close {bead} — close it manually", err=True)
        raise api.typer.Exit(1)
    api._clear_review_label(bead, data, main)
    if api._is_epic(data):
        api._close_land_origin_reports(bead, main)
        api._close_swarm_bead(bead, main)
        api._teardown_coordinator_seat(cfg, hive, bead)
    api._prune_landed_hive(entry)
    api.otel.count_bead_transition("pr_landed")
    api.typer.echo(
        f"✓ {ref} merged — closed {bead} (close_reason: {reason}); reaped any SAFE worktree(s)"
    )


def impl__prune_landed_hive(api, entry):
    """Best-effort cleanup after a PR-confirmed land.

    ``worktree.prune`` applies the SAFE classifier, so this can only reclaim work that is
    already closed, landed, and clean.  The close has already completed, however, so a metadata
    or filesystem failure here must never turn a successful land into a failed command.
    """
    hive = api._hive(entry)
    try:
        api.worktree.prune(hive=hive)
    except Exception as exc:
        api.typer.echo(f"⚠ landed but automatic worktree prune for {hive} failed: {exc}", err=True)


def impl__guard_land_pr_pending(api, bead, main):
    """Refuse `land` on a bead that isn't `pr-pending` — it only completes a `work.landing: pr`
    landing opened by `merge`/`finish`."""
    if api.bd.state(bead, "landing", main) != "pr-pending":
        api.typer.echo(
            f"✗ {bead} is not pr-pending — `land` completes a `work.landing: pr` landing "
            "opened by merge/finish",
            err=True,
        )
        raise api.typer.Exit(1)


def impl__resolve_merged_land_pr(api, entry, branch):
    """The MERGED PR for `branch`, or refuse — completion is driven by PR STATE, never asserted
    (the operator escape hatch for an out-of-band landing is `worktree mark-landed`)."""
    pr = api.ghpr.merged_pr_for(entry, branch)
    if pr:
        return pr
    cur = api.ghpr.pr_for_branch(entry, branch)
    state = str((cur or {}).get("state") or "not found")
    api.typer.echo(f"✗ PR for {branch} is {state}, not MERGED — nothing landed", err=True)
    raise api.typer.Exit(1)


def impl__resolve_land_pr_merge_gates(api, bead, main, ref):
    """Resolve any still-open pr-merge gate — bd's own gh:pr gate watcher may already have (both
    orders are fine); a resolve failure only warns, the merge already happened on GitHub. `bd
    gate resolve` only ever takes ONE gate id, so this stays a per-gate spawn (not batchable)."""
    for g in api._pr_merge_gates(bead, main):
        gid = str(g.get("id") or "")
        if api.bd.run(["gate", "resolve", gid, "--reason", f"{ref} merged"], main).returncode != 0:
            api.typer.echo(f"⚠ failed to resolve gh:pr gate {gid} — resolve it manually", err=True)


def impl__close_land_origin_reports(api, bead, main):
    """Epic parity with the local land: adopted origin reports ride the epic to completion.
    Best-effort — never unwinds a completed land. Batched into ONE `bd close` for every
    still-open report (`bd close` accepts multiple ids) instead of a subprocess-per-report loop.

    Uses `bd.children` (the parent EDGE), matching `_guard_molecule_children`. These are the READ
    and WRITE halves of one feature, and a first pass at bh-89mrf fixed only the read — leaving a
    detached bead invisible to the guard yet still CLOSED by the land, which is worse than fixing
    neither."""
    children = api.bd.children(bead, main)
    ids = [
        str(r.get("id"))
        for r in (children if isinstance(children, list) else [])
        if api.adopt.is_origin_report(r.get("labels")) and str(r.get("status", "")) != "closed"
    ]
    if not ids:
        return
    if api.bd.run(["close", *ids, "--reason", f"adopted epic {bead} landed"], main).returncode != 0:
        api.typer.echo(f"⚠ landed but failed to close origin report(s) {', '.join(ids)}", err=True)


def impl_merge(api, bead, hive, rm, molecule, group):
    """Merger-only: serialize integration of an *approved* bead onto the integration branch.
    Holds the hive merge slot, re-verifies a small clean conventional history, merges `--no-ff`
    (history preserved, never squashed at the boundary), closes the bead, releases the slot.
    Refuses unless the review gate is resolved; on conflict it aborts and releases — never drops
    work. (No worker-side ack: this is the merge owner, not the developer.)

    With `--molecule`, the positional arg is an *epic* and this lands the assembled `mol/<epic>`
    onto the integration branch as ONE `--no-ff` bubble (the wrap-up verb): guard the molecule is
    complete + clean, validate it, land it, close the epic, delete the branch.

    With `--group <ids>`, lands a whole work-group: validate the shared `wt/batch/<group>` branch
    once, merge it `--no-ff` into the members' molecule as ONE bubble (per-bead commits preserved
    inside, so it stays bisectable), then close every member — release the slot either way."""
    cfg = api.config.load()
    api.guard.guard_primary(hive, cfg=cfg, verb="work merge")
    group = api.work_logic.opt_str(group)
    if group:
        if bead:
            api.typer.echo(
                f"✗ pass either <id> or --group, not both (got <id>={bead}, "
                f"--group={group}).\n"
                "  --group takes ONE comma-separated value — ids must not be "
                "space-separated:\n"
                f"      {api.config.BINARY_ALIAS} work merge --group {group},{bead}   "
                "# correct\n"
                f"      {api.config.BINARY_ALIAS} work merge --group {group} {bead}   "
                f"# drops {bead}",
                err=True,
            )
            raise api.typer.Exit(1)
        api.work_group.merge_group(cfg, group, hive, rm)
        return
    if not bead:
        api.typer.echo("✗ pass a bead <id> (or --group <ids> / --molecule <epic>)", err=True)
        raise api.typer.Exit(1)
    api.otel.set_bead(bead)
    if molecule:
        api._merge_molecule(cfg, bead, hive)
        return
    api._merge_bead(cfg, bead, hive, rm)


def impl__guard_bead_merge_gates(api, bead, main, landing_pr):
    """Guard `bead` is mergeable: not changes-requested, and no open gate blocks it (broad on
    purpose — the warden's security:* gate blocks in parallel with review); the refusal
    enumerates each open gate by kind so the merger knows who clears what (bh-c3il). Under
    `landing: pr` the ONE exception is the landing path's own `pr-merge` gate — it must not block
    an idempotent re-run of that same path (which reuses the open PR + gate rather than opening
    duplicates)."""
    if api.bd.state(bead, "review", main) == "changes-requested":
        api.typer.echo(f"✗ {bead} has changes-requested — resume & resubmit, don't merge", err=True)
        raise api.typer.Exit(1)
    gate_lines = api.work_logic.open_gate_lines(
        bead, main, skip_marker="pr-merge" if landing_pr else ""
    )
    if gate_lines:
        api.typer.echo(
            f"✗ {bead}: open gate(s) block the merge:\n" + "\n".join(gate_lines), err=True
        )
        raise api.typer.Exit(1)


def impl__guard_bead_land_base(api, entry, bead, integration):
    """Recursive land (xn3o.7): guard a container parent-link ambiguity and a closed-epic land
    target, then resolve `bead`'s land base one tier up via the integration_base climb."""
    conflict = api.worktree.container_conflict(entry, bead, integration)
    if conflict:
        id_base, link_base = conflict
        api.typer.echo(
            f"✗ {bead}: container ambiguity — the dotted id resolves to {id_base} but the "
            f"parent-child link resolves to {link_base}. A re-parent/split left both containers "
            "live; refusing to guess. Reconcile the parent link (or retire the stale container) "
            "and retry.",
            err=True,
        )
        raise api.typer.Exit(1)
    base = api.worktree.integration_base(entry, bead, integration)
    if api.worktree.container_epic_closed(entry, base):
        api.typer.echo(
            f"✗ {bead}: {base} belongs to a CLOSED epic — refusing to land on (or "
            f"resurrect) a landed container. Re-parent {bead} onto a live epic and retry.",
            err=True,
        )
        raise api.typer.Exit(1)
    return base


def impl_already_landed(api, entry, branch, base):
    """Merge-verb alias for :func:`worktree.landed_via_merge` — the branch's commits are on `base`
    because they were merged there, not because the branch was never implemented (bh-lvqs)."""
    return api.worktree.landed_via_merge(entry, branch, base)


def impl__guard_bead_clean_history(api, entry, branch, base, cfg):
    """Guard the branch is a small clean conventional history before it's allowed to merge —
    reuses submit's `_history_ok` check as a merge-time backstop.

    Returns True when the branch is ALREADY LANDED, so the caller reconciles bookkeeping instead
    of merging (bh-lvqs); False on the ordinary path. A genuinely empty branch — no commits over
    base and NOT an ancestor of it — still takes the self-refine bounce unchanged."""
    count, subjects = api.worktree.history(entry, branch, base)
    if count == 0 and api.already_landed(entry, branch, base):
        return True
    ok, msg = api._history_ok(count, subjects, api.config.max_commits(cfg, entry))
    if not ok:
        api.typer.echo(f"✗ {msg} — bounce back for self-refine", err=True)
        raise api.typer.Exit(1)
    return False


def impl__reconcile_landed_bead(api, cfg, entry, main, bead, bead_data, branch, base, hive, rm):
    """Finish the bookkeeping half of a merge whose CODE already landed (bh-lvqs).

    Reached when the branch is an ancestor of the base — the merge happened, but the run that did
    it died before closing the bead, so the tracker still says in-progress while main carries the
    work. Re-running merge used to report that as "nothing to submit". This makes the verb
    IDEMPOTENT instead: do exactly the steps the first run missed and exit 0.

    Deliberately does NOT re-merge, re-validate, or re-emit merge-outcome metrics — the merge is
    not happening now, it happened then, and counting it twice would corrupt the very telemetry an
    operator would use to spot this failure. The merge slot is still held around the close so a
    concurrent merger cannot interleave with the reconcile."""
    slot_attrs = {"bh.merge.kind": "bead", "bh.hive": api._hive(entry)}
    with api.work_group.merge_slot(main, slot_attrs):
        closed = api.work_logic.close_merged(bead, main, "merged", data=bead_data)
        api._clear_review_label(bead, bead_data, main)
    if rm:
        try:
            api.worktree.remove(hive, bead, force=True)
        except Exception:
            pass
    if not closed:
        assignee = str(bead_data.get("assignee") or "").strip()
        api.typer.echo(
            f"✗ {bead} is ALREADY MERGED ({branch} → {base}) but could not be closed"
            f"{f' (assignee {assignee!r})' if assignee else ''} — close it manually; "
            "the code is on the integration branch, do NOT re-implement it",
            err=True,
        )
        raise api.typer.Exit(1)
    api.typer.echo(
        f"✓ {bead} was already merged ({branch} → {base}) — reconciled bookkeeping "
        "(closed the bead; no re-merge)"
    )


def impl__guard_signed_history(api, entry, branch, base, cfg):
    """The enforce-signing gate (bh-ijd4), beside the clean-history check because that is
    already the pre-merge guard: with `work.enforce_signing` on, EVERY commit in the merge
    range must verify as trusted, not just the tip. A no-op when the flag is off, so default
    behaviour is byte-identical to before."""
    if not api.config.enforce_signing(cfg, entry):
        return
    ok, msg = api.work_logic._signing_ok(
        api.worktree.signature_status(entry, branch, base), branch, base
    )
    if not ok:
        api.typer.echo(f"✗ {msg}", err=True)
        raise api.typer.Exit(1)


def impl__merge_bead_no_ff(api, entry, branch, base, target, cfg, bead, main, slot_attrs):
    """rebase-then-retry the merge: a replay-resolvable conflict (a coupled sibling's change
    already landed on the base — e.g. both beads added the same boilerplate line) is recovered by
    rebasing this bead onto the newer base; a genuinely divergent conflict still fails cleanly
    with the bead branch restored, so the merger bounces it for rework. Returns `how`
    ('merged'/'rebased'/'union') on success; raises Exit on a real conflict.

    On a real conflict the merger has no write authority to hand-resolve it (bh-2p6w — the
    merger seat is 'not implement' per `docs/design/roles-rbac-matrix.md`), so the bounce is
    made RECORDED + ROUTABLE state (`work_logic.record_merge_conflict`: a note + bounce to
    `review=changes-requested` naming the conflicted paths), not just this stderr transcript."""
    prof = api.config.work_identity(cfg, entry)
    agent = prof["mode"] == "agent"
    rc, out, how = api.worktree.try_merge_rebase(
        entry,
        branch,
        base,
        target,
        name=prof["name"] or "" if agent else "",
        email=prof["email"] or "" if agent else "",
        signing_key=prof["signing_key"] or "" if agent else "",
        sign=prof["sign"] if agent else False,
        message=f"chore(merge): bead {bead}",
        union_globs=tuple(api.config.union_globs(cfg, entry)),
        validate_cmd=api.config.validate_cmd(cfg, entry, "union"),
    )
    if rc != 0:
        api.otel.count_merge_outcome({**slot_attrs, "bh.merge.how": "conflict"})
        where = api.work_logic.record_merge_conflict(entry, branch, base, main, [bead], "merge")
        api.typer.echo(
            f"✗ real conflict merging {bead} — rebase retry failed, bead branch restored; "
            f"bounced {bead} to review=changes-requested (conflict in: {where}) — "
            f"`{api.config.BINARY_ALIAS} work resume {bead}`, rebase onto {base}, resolve, "
            f"resubmit:\n{out}",
            err=True,
        )
        raise api.typer.Exit(rc)
    return how


def impl__postland_revalidate_bead(api, cfg, entry, main, base, pre, bead, slot_attrs, on_main):
    """Re-test the integration tip after a clean bead merge — green in isolation at submit, but
    the COMBINATION with what's already on the tip may be red. Still holding the slot, so on red
    we reset a safe-to-rewrite tip (the private mol/<epic>, or an unpushed main) to its pre-merge
    sha and bounce the bead to changes-requested; a shared (pushed) tip is left standing and
    fixed forward. Raises on an unrecoverable red result.

    "The COMBINATION may be red" is precisely a statement about the TREE: a merge onto a base that
    moved since the branch forked produces a tree neither parent has, the (tree, cmd_hash) lookup
    misses, and this runs in full. When the base did NOT move the merge tree is byte-identical to
    the branch tip submit already validated, so there is no combination to test — that is ADR
    Decision 4 (bh-ku9n9.17), and the ledger key is the entire test for it (see
    `_validate_molecule_checkout` for why no second tree comparison exists)."""
    vrc = api.worktree.clean_checkout(
        entry, base, api.config.validate_cmd(cfg, entry, "merge", main_gate=on_main), reuse=True
    )
    api.otel.count_validation(vrc == 0, {"bh.work.phase": "merge"})
    if vrc == 0:
        return
    rolled = api._rollback_or_keep(entry, main, base, pre, slot_attrs)
    api.bd.run(
        [
            "set-state",
            bead,
            "review=changes-requested",
            "--reason",
            "combined-state red after merge — may be an interaction with "
            "already-merged siblings; rebase on the current tip and fix",
        ],
        main,
    )
    if rolled:
        api.typer.echo(
            f"✗ {bead} merged clean but the {base} tip is RED in combination (exit "
            f"{vrc}) — rolled {base} back to {pre[:7]} and bounced the bead to "
            "changes-requested.",
            err=True,
        )
    else:
        api.typer.echo(
            f"✗✗ {bead} merged clean but {base} is RED in combination (exit {vrc}) and "
            f"{base} is shared (pushed) so it is NOT rewritten — the merge stands. "
            "Bounced the bead; fix forward.",
            err=True,
        )
    raise api.typer.Exit(vrc)


def impl__record_merge_commit(api, bead, main, base):
    """Append the just-landed merge commit's own sha onto `bead`'s `git.commits` linkage
    (bh-1b0rc.2, docs/design/bead-commit-linkage-contract.md). Read `base`'s tip AFTER the merge
    lands and (when this run re-validates) AFTER `_postland_revalidate_bead` has returned —
    that call either returns clean or raises `typer.Exit` on a red re-validation that ROLLS THE
    MERGE BACK, so calling this only once control reaches here means a rolled-back sha is never
    recorded. Non-fatal by construction: a metadata write must never fail a merge that already
    landed — a failure is surfaced as a warning, never swallowed silently and never raised."""
    try:
        merge_sha = api.worktree._ref_sha(main, base)
        if merge_sha:
            api.git_linkage.record_commits(bead, main, [merge_sha])
    except Exception as exc:
        api.typer.echo(f"⚠ failed to record commit linkage for {bead}: {exc}", err=True)


def impl__merge_bead(api, cfg, bead, hive, rm):
    """Serialize the land of a single approved bead onto its integration base: guard open + review
    resolved + a small clean conventional history, hold the merge slot, rebase-retry merge
    `--no-ff`, re-validate the combined tip on a main-gate, close the bead. The single-bead
    sibling of `_merge_molecule` / `merge_group`; `merge` is the thin 3-way dispatch over them."""
    started = api.time.perf_counter()
    entry, main, target, branch = api.worktree.locate(cfg, hive, bead)
    bead_data = api.bd.show(bead, main)
    api._guard_open(bead_data, bead)
    landing_pr = api.config.work_landing(cfg, entry) == "pr"
    api._guard_bead_merge_gates(bead, main, landing_pr)
    integration = api.config.integration_branch(cfg, entry)
    base = api._guard_bead_land_base(entry, bead, integration)
    if api._guard_bead_clean_history(entry, branch, base, cfg):
        api._reconcile_landed_bead(cfg, entry, main, bead, bead_data, branch, base, hive, rm)
        return
    api._guard_signed_history(entry, branch, base, cfg)
    if base == integration and landing_pr:
        api._open_landing_pr(cfg, entry, main, bead, bead_data, branch, base)
        return
    slot_attrs = {"bh.merge.kind": "bead", "bh.hive": api._hive(entry)}
    mode = api.config.validation_mode(cfg, entry)
    on_main = base == integration
    revalidate = mode == "conservative" or (on_main and mode != "loose")
    pre = api.worktree._ref_sha(main, base) if revalidate else ""
    with api.work_group.merge_slot(main, slot_attrs):
        how = api._merge_bead_no_ff(entry, branch, base, target, cfg, bead, main, slot_attrs)
        if revalidate:
            api._postland_revalidate_bead(cfg, entry, main, base, pre, bead, slot_attrs, on_main)
        api._record_merge_commit(bead, main, base)
        api.otel.count_merge_outcome({**slot_attrs, "bh.merge.how": how})
        try:
            closed = api.work_logic.close_merged(bead, main, "merged", data=bead_data)
            api._clear_review_label(bead, bead_data, main)
        except Exception:
            assignee = str(bead_data.get("assignee") or "").strip()
            api.typer.echo(
                f"✗ {bead} MERGED SUCCESSFULLY ({branch} --no-ff → {base}) but its "
                f"bookkeeping did not complete.\n"
                f"  THE CODE IS ON {base} — do not re-implement or re-submit it.\n"
                f"  Unreconciled: bead {bead}"
                f"{f', assignee {assignee!r}' if assignee else ''}, branch {branch}.\n"
                f"  Re-run `{api.config.BINARY_ALIAS} work merge {bead}` — it is idempotent "
                "over an already-landed branch and will finish the reconcile.",
                err=True,
            )
            raise
    api.otel.record_merge_duration(
        api.time.perf_counter() - started, {"bh.merge.kind": "bead", "bh.merge.how": how}
    )
    try:
        api._emit_bead_flow(
            bead, bead_data, main, {"bh.merge.kind": "bead", "bh.hive": api._hive(entry)}
        )
    except Exception:
        pass
    api.otel.count_bead_transition("merged")
    note = ""
    if how == "rebased":
        note = " (rebased onto a newer base first)"
    elif how == "union":
        note = " (landed via union conflict resolution)"
    if rm:
        api.worktree.remove(hive, bead, force=True)
    if not closed:
        assignee = str(bead_data.get("assignee") or "").strip()
        api.typer.echo(
            f"✗ merged {bead} ({branch} --no-ff → {base}){note} but FAILED to close it"
            f"{f' (assignee {assignee!r})' if assignee else ''} — close it manually",
            err=True,
        )
        raise api.typer.Exit(1)
    api.typer.echo(f"✓ merged {bead} ({branch} --no-ff → {base}){note} and closed it")
