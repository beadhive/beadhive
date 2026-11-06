"""Hive-scoped intake command implementations behind the stable ``beadhive.work`` facade."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from . import identity, intake_services, otel, registry, triage
from .config_consumer_ports import work_settings as config


def render_disposition(code, error, message):
    if error:
        typer.echo(f"✗ {error}", err=True)
        raise typer.Exit(code)
    typer.echo(message)


def intake(hive: str = "", source: str = "", as_json: bool = False, no_dupes: bool = False):
    cfg = config.load()
    triage.print_intake(
        registry.hive_dir_for(cfg, hive), source=source, dupes=not no_dupes, as_json=as_json
    )


def accept(
    bead: str,
    issue_type: str = "",
    priority: str = "",
    as_: str = "",
    hive: str = "",
    work: str = "",
    new_work: bool = False,
):
    """Accept an intake report. With ``work`` / ``new_work`` it also makes the report track work
    (``accept_to_work``); without them it is the plain accept it always was."""
    otel.set_bead(bead)
    cfg = config.load()
    actor = identity.resolve_actor(as_)
    if work or new_work:
        return accept_to_work(cfg, bead, actor, work, new_work, issue_type, priority, hive)
    cwd = registry.hive_dir_for(cfg, hive)
    render_disposition(
        *triage.accept(cwd, bead, actor, issue_type=issue_type, priority=priority, cfg=cfg)
    )


def reject(bead: str, reason: str, as_: str = "", hive: str = ""):
    otel.set_bead(bead)
    cfg = config.load()
    cwd = registry.hive_dir_for(cfg, hive)
    actor = identity.resolve_actor(as_)
    render_disposition(*triage.reject(cwd, bead, actor, reason=reason, cfg=cfg))


def reroute(
    bead: str,
    to: str = "",
    super_: str = "",
    as_: str = "",
    hive: str = "",
):
    otel.set_bead(bead)
    cfg = config.load()
    cwd = registry.hive_dir_for(cfg, hive)
    actor = identity.resolve_actor(as_)
    render_disposition(
        *triage.reroute(cwd, bead, actor, to_hive=to, superintendent=super_, cfg=cfg)
    )


def promote(bead: str, as_: str = "", hive: str = ""):
    otel.set_bead(bead)
    cfg = config.load()
    cwd = registry.hive_dir_for(cfg, hive)
    actor = identity.resolve_actor(as_)
    render_disposition(*triage.promote(cwd, bead, actor, cfg=cfg))


def _import_entry(cfg, hive: str):
    entry = registry.resolve_hive(cfg, hive) if hive else registry.current_hive(cfg)
    if entry is None:
        typer.echo("✗ not inside a managed hive; pass --hive <hive>", err=True)
        raise typer.Exit(1)
    return entry


def render_import(result, url: str, dry_run: bool, as_json: bool) -> None:
    """Print one ``bh work import`` result (human or ``--json``) and exit non-zero on a refusal."""
    outcome = result.outcome
    if as_json:
        payload = {
            "ok": result.code == 0,
            "dry_run": dry_run,
            "reason": result.reason,
            "error": result.error,
            "report": result.report or (outcome.binding.report if outcome else ""),
            "url": outcome.binding.url if outcome else url,
            "kind": outcome.kind.value if outcome else "",
            "repaired": list(outcome.repaired) if outcome else [],
        }
        typer.echo(json.dumps(payload, sort_keys=True))
        if result.code:
            raise typer.Exit(result.code)
        return
    if result.code or outcome is None:
        code = f" ({result.reason})" if result.reason else ""
        typer.echo(f"✗ import refused{code}: {result.error}", err=True)
        raise typer.Exit(result.code or 1)
    report, repaired = outcome.binding.report, outcome.repaired
    extra = f" [completed: {', '.join(repaired)}]" if repaired else ""
    verbs = {
        "created": ("would create a held, untriaged report for", "imported"),
        "updated": (f"would update {report} from", f"updated {report} from"),
        "unchanged": (
            f"{report} is already up to date with",
            f"{report} is already up to date with",
        ),
    }[outcome.kind.value]
    typer.echo(f"✓ {verbs[0] if dry_run else verbs[1]} {outcome.binding.url}{extra}")
    if not dry_run and outcome.kind.value == "created":
        typer.echo(f"  → {report} (intake:untriaged, held: it is not work until triaged)")


def render_discover(result, as_json: bool) -> None:
    """Print one ``bh work discover`` result (human or ``--json``); non-zero exit on a refusal."""
    outcome = result.outcome
    if as_json:
        payload = {
            "ok": result.code == 0,
            "full": result.full,
            "reason": result.reason,
            "error": result.error,
            "report": result.report,
            "created": outcome.created if outcome else 0,
            "updated": outcome.updated if outcome else 0,
            "unchanged": outcome.unchanged if outcome else 0,
            "source_missing": outcome.source_missing if outcome else 0,
            "checkpoint_advanced": bool(outcome and outcome.checkpoint_advanced),
            "reconciled": bool(outcome and outcome.reconciled),
            "reports": list(outcome.reports) if outcome else [],
        }
        typer.echo(json.dumps(payload, sort_keys=True))
        if result.code:
            raise typer.Exit(result.code)
        return
    if result.code or outcome is None:
        code = f" ({result.reason})" if result.reason else ""
        typer.echo(f"✗ discover refused{code}: {result.error}", err=True)
        raise typer.Exit(result.code or 1)
    mode = "full pass" if result.full else "incremental"
    typer.echo(
        f"✓ discover ({mode}): {outcome.created} created, {outcome.updated} updated, "
        f"{outcome.unchanged} unchanged, {outcome.source_missing} newly source-missing"
    )
    for report in outcome.reports:
        typer.echo(f"  → {report}")


def render_link(result, report: str, as_json: bool) -> None:
    """Print one link, unlink or linking accept (human or ``--json``); non-zero exit on refusal."""
    outcome, disposition = result.outcome, result.disposition
    if as_json:
        payload = {
            "ok": result.code == 0,
            "reason": result.reason,
            "error": result.error,
            "report": report,
            "kind": outcome.kind.value if outcome else "",
            "work": (disposition.work if disposition else outcome.work if outcome else ""),
            "previous": outcome.previous if outcome else "",
            "delivery": outcome.delivery if outcome else "",
            "created_work": bool(disposition and disposition.created_work),
            "accepted": disposition is not None,
        }
        caught_up = _caught_up(result)
        if caught_up:  # only a catch-up adds the key, so every other payload is unchanged
            payload["milestones"] = [_milestone_row(item) for item in caught_up]
        typer.echo(json.dumps(payload, sort_keys=True))
        if result.code:
            raise typer.Exit(result.code)
        return
    if result.code:
        code = f" ({result.reason})" if result.reason else ""
        typer.echo(f"✗ link refused{code}: {result.error}", err=True)
        raise typer.Exit(result.code)
    caught_up = _caught_up(result)
    if disposition is not None:
        made = "created and tracks" if disposition.created_work else "tracks"
        typer.echo(f"✓ accepted {report}: it {made} {disposition.work} (delivery:pending)")
        typer.echo(
            f"  → {report} stays held (it is a report, not work); {disposition.work} is work"
        )
        _echo_caught_up(caught_up)
        return
    assert outcome is not None
    kind = outcome.kind.value
    if kind == "unlinked":
        if not outcome.work:
            typer.echo(f"{report} tracks no work; nothing to unlink")
            return
        typer.echo(f"✓ unlinked {report} from {outcome.work}")
        if outcome.delivery:
            typer.echo(
                f"  → delivery:{outcome.delivery} is kept (it cannot be cleared) and an `unlinked` "
                "fact is recorded beside it"
            )
        return
    verbs = {
        "linked": f"{report} now tracks {outcome.work}",
        "already_linked": f"{report} already tracks {outcome.work}",
        "relinked": f"{report} now tracks {outcome.work} (was {outcome.previous})",
    }
    typer.echo(f"✓ {verbs[kind]} (delivery:{outcome.delivery})")
    _echo_caught_up(caught_up)


def _caught_up(result) -> tuple:
    """The milestones a link (or linking accept) caught the report up on (bh-61xkc)."""
    found = result.disposition if result.disposition is not None else result.outcome
    return tuple(getattr(found, "milestones", ()) or ())


def _milestone_row(item) -> dict:
    return {
        "report": item.report,
        "event": item.event,
        "delivery": item.delivery,
        "drafted": item.drafted,
        "verdict": item.verdict.value if item.verdict else "",
        "closed": item.closed,
        "note": item.note,
        "error": item.error,
    }


def _echo_caught_up(items) -> None:
    if not items:
        return
    typer.echo("  the work had already merged; caught up:")
    for line in _milestone_item_lines(items):
        typer.echo(line, err=line.lstrip().startswith("⚠"))


def link(
    bead: str,
    to: str = "",
    remove: bool = False,
    replace: bool = False,
    as_: str = "",
    hive: str = "",
    as_json: bool = False,
):
    """Make a report track a work bead (``--to``), or stop (``--remove``); off unless
    ``intake.external.enabled``. Several reports may track one work bead."""
    otel.set_bead(bead)
    cfg = config.load()
    if remove == bool(to):
        typer.echo("✗ pass exactly one of --to <work> or --remove", err=True)
        raise typer.Exit(2)
    entry = _import_entry(cfg, hive)
    actor = identity.resolve_actor(as_)
    result = intake_services.link_report(
        cfg,
        entry,
        Path(registry.hive_dir(entry)),
        bead,
        to,
        actor,
        replace=replace,
        remove=remove,
        hive=hive,
    )
    render_link(result, bead, as_json)


def accept_to_work(
    cfg,
    bead: str,
    actor: str,
    work: str,
    new_work: bool,
    issue_type: str = "",
    priority: str = "",
    hive: str = "",
    as_json: bool = False,
):
    """``accept --work <id>`` / ``--new-work``: accept the report and make it track work."""
    entry = _import_entry(cfg, hive)
    result = intake_services.accept_to_work(
        cfg,
        entry,
        Path(registry.hive_dir(entry)),
        bead,
        actor,
        work=work,
        new_work=new_work,
        issue_type=issue_type,
        priority=priority,
        hive=hive,
    )
    render_link(result, bead, as_json)


# -- reproduction work (bh work investigate, bh-u3a53) -----------------------------------------


def _relations_row(relations) -> dict:
    return {
        "tracks": relations.tracks,
        "progress": relations.progress.value,
        "investigated_by": list(relations.investigated_by),
    }


def _relations_lines(relations) -> list[str]:
    tracks = f"{relations.tracks} ({relations.progress.value})" if relations.tracks else "none"
    return [
        f"  tracks: {tracks}",
        f"  investigated by: {', '.join(relations.investigated_by) or 'none'}",
    ]


def render_investigate(result, report: str, as_json: bool) -> None:
    """Print one ``bh work investigate`` result (human or ``--json``); non-zero exit on refusal."""
    outcome, relations = result.outcome, result.relations
    if as_json:
        payload = {
            "ok": result.code == 0,
            "reason": result.reason,
            "error": result.error,
            "report": report,
            "kind": outcome.kind.value if outcome else "",
            "work": outcome.work if outcome else "",
            "created_work": bool(outcome and outcome.created_work),
            "relations": _relations_row(relations) if relations is not None else None,
        }
        typer.echo(json.dumps(payload, sort_keys=True))
        if result.code:
            raise typer.Exit(result.code)
        return
    if result.code:
        code = f" ({result.reason})" if result.reason else ""
        typer.echo(f"✗ investigate refused{code}: {result.error}", err=True)
        raise typer.Exit(result.code)
    if outcome is None:
        typer.echo(f"{report} links:")
    else:
        kind, work = outcome.kind.value, outcome.work
        if kind == "unlinked" and not work:
            typer.echo(f"nothing to remove: that bead does not investigate {report}")
        elif kind == "unlinked":
            typer.echo(f"✓ {work} no longer investigates {report}")
        elif kind == "already_linked":
            typer.echo(f"✓ {work} already investigates {report}")
        else:
            made = "created " if outcome.created_work else ""
            typer.echo(f"✓ {made}{work} now investigates {report}")
            typer.echo(
                "  → it does not track the report: closing or merging it drafts no delivery "
                "milestone and never closes the report"
            )
    if relations is not None:
        for line in _relations_lines(relations):
            typer.echo(line)


def investigate(
    bead: str,
    work: str = "",
    new_work: bool = False,
    remove: bool = False,
    issue_type: str = "",
    priority: str = "",
    as_: str = "",
    hive: str = "",
    as_json: bool = False,
):
    """Link reproduction work to a report with the ``investigates`` relation (never ``tracks``),
    remove one such link, or with neither ``--work`` nor ``--new-work`` list the report's links.
    Off unless ``intake.external.enabled``; never touches a tracker."""
    otel.set_bead(bead)
    if work and new_work:
        typer.echo("✗ pass at most one of --work <id> or --new-work", err=True)
        raise typer.Exit(2)
    if remove and not work:
        typer.echo("✗ --remove needs --work <id>: the bead to stop investigating", err=True)
        raise typer.Exit(2)
    if (issue_type or priority) and not new_work:
        typer.echo("✗ --type and --priority apply to the bead --new-work creates", err=True)
        raise typer.Exit(2)
    cfg = config.load()
    entry = _import_entry(cfg, hive)
    actor = identity.resolve_actor(as_)
    result = intake_services.investigate_report(
        cfg,
        entry,
        Path(registry.hive_dir(entry)),
        bead,
        actor,
        work=work,
        new_work=new_work,
        remove=remove,
        issue_type=issue_type,
        priority=priority,
        hive=hive,
    )
    render_investigate(result, bead, as_json)


def import_issue(
    url: str, hive: str = "", dry_run: bool = False, as_json: bool = False, as_: str = ""
):
    """Import an external issue as an untriaged, held internal report bead (read-only toward the
    tracker; off unless ``intake.external.enabled``)."""
    cfg = config.load()
    entry = _import_entry(cfg, hive)
    directory = Path(registry.hive_dir(entry))
    actor = identity.resolve_actor(as_)
    result = intake_services.import_issue(
        cfg, entry, directory, url, actor, dry_run=dry_run, hive=hive
    )
    render_import(result, url, dry_run, as_json)


def discover(hive: str = "", full: bool = False, as_json: bool = False, as_: str = ""):
    """Discover the hive's external issues, pull-only, through the import upsert (off unless
    ``intake.external.enabled``; never a tracker or public-store write). ``full`` re-pulls every
    issue including closed ones and reconciles which ones are gone."""
    cfg = config.load()
    entry = _import_entry(cfg, hive)
    directory = Path(registry.hive_dir(entry))
    actor = identity.resolve_actor(as_)
    result = intake_services.discover(cfg, entry, directory, actor, full=full, hive=hive)
    render_discover(result, as_json)


# -- the merge and release milestones (I10): they draft, they never send ------------------------


def _milestone_lines(outcome) -> list[str]:
    """One human line per report a milestone looked at: what changed, is pending or failed."""
    return _milestone_item_lines(outcome.items)


def _milestone_item_lines(items) -> list[str]:
    lines: list[str] = []
    for item in items:
        parts = []
        if item.delivery:
            parts.append(f"delivery:{item.delivery}")
        if item.drafted:
            held = ", held by the leak check" if getattr(item, "held", False) else ""
            parts.append(f"drafted record {item.drafted}{held} (nothing is sent)")
        if item.closed:
            parts.append("complete: closed")
        if item.note:
            parts.append(item.note)
        if item.error:
            parts.append(f"FAILED: {item.error}")
        lines.append(f"  {'⚠' if item.error else '→'} {item.report}: {'; '.join(parts)}")
    return lines


def on_work_merged(
    cfg,
    entry,
    main,
    base: str,
    beads,
    hive: str = "",
    members=None,
) -> None:
    """The merge trigger, called by ``work.merge`` / ``finish`` after the bead closes.

    **Never fails the merge.** It is a no-op, with no bd call and no git call, unless
    ``intake.external.enabled``; otherwise it records ``delivery:merged`` on every report that
    tracks one of ``beads`` (and, for a molecule, ``members()``: its closed children), drafts a
    record when the hive turned that on, and applies the completion policy. Any failure, an
    unexpected exception included, becomes a warning on stderr and the merge carries on. It
    sends nothing and writes no tracker.
    """
    if not intake_services.external_intake_settings(cfg)[0]:
        return
    try:
        from . import worktree

        works = list(beads)
        if members is not None:
            try:  # a molecule's children; failing to list them still triggers the epic itself
                works = list(dict.fromkeys([*works, *members()]))
            except Exception as exc:  # noqa: BLE001 - never fails the merge
                typer.echo(
                    f"⚠ intake milestones could not list the molecule's children: {exc}", err=True
                )
        commit = worktree._ref_sha(main, base)
        if not commit:
            typer.echo(
                f"⚠ intake milestones skipped: could not read the tip of {base} (the merge is "
                "unaffected)",
                err=True,
            )
            return
        result = intake_services.work_merged(
            cfg,
            entry,
            Path(main),
            works,
            commit,
            identity.resolve_actor(""),
            hive=hive,
        )
    except Exception as exc:  # noqa: BLE001 - a milestone must never fail a merge
        typer.echo(f"⚠ intake milestones did not run (the merge is unaffected): {exc}", err=True)
        return
    if result.code:
        why = f" ({result.reason})" if result.reason else ""
        typer.echo(
            f"⚠ intake milestones did not complete{why}: {result.error} (the merge is unaffected)",
            err=True,
        )
        return
    outcome = result.outcome
    if outcome is None or not outcome.items:
        return
    for line in _milestone_lines(outcome):
        typer.echo(line, err=line.lstrip().startswith("⚠"))


def render_delivery_release(result, tag: str, as_json: bool) -> None:
    """Print one ``bh work delivery release`` (human or ``--json``); non-zero on any failure."""
    outcome = result.outcome
    items = outcome.items if outcome is not None else ()
    failed = bool(result.code) or any(item.error for item in items)
    if as_json:
        payload = {
            "ok": not failed,
            "reason": result.reason,
            "error": result.error,
            "tag": result.tag or tag,
            "commit": result.commit,
            "reports": [
                {
                    "report": item.report,
                    "delivery": item.delivery,
                    "drafted": item.drafted,
                    "verdict": item.verdict.value if item.verdict else "",
                    "closed": item.closed,
                    "note": item.note,
                    "error": item.error,
                }
                for item in items
            ],
        }
        typer.echo(json.dumps(payload, sort_keys=True))
        if failed:
            raise typer.Exit(result.code or 1)
        return
    if result.code:
        code = f" ({result.reason})" if result.reason else ""
        typer.echo(f"✗ release not recorded{code}: {result.error}", err=True)
        raise typer.Exit(result.code)
    typer.echo(f"✓ verified {result.tag} → {result.commit[:12]}")
    if not items:
        typer.echo("  no report is waiting on a merge to release")
    for line in _milestone_lines(outcome):
        typer.echo(line, err=line.lstrip().startswith("⚠"))
    if failed:
        raise typer.Exit(1)


def delivery_release(tag: str, as_: str = "", hive: str = "", as_json: bool = False):
    """``bh work delivery release <tag>``: verify the tag exists (read-only against git), move the
    reports whose merge it contains to ``delivery:released`` and draft a record when the hive
    turned that on. A merge the release cannot be shown to contain stays ``merged``. Nothing is
    sent; off unless ``intake.external.enabled``."""
    cfg = config.load()
    entry = _import_entry(cfg, hive)
    actor = identity.resolve_actor(as_)
    result = intake_services.delivery_release(
        cfg, entry, Path(registry.hive_dir(entry)), tag, actor, hive=hive
    )
    render_delivery_release(result, tag, as_json)


# -- the progress milestones (bh-dev42): they draft, they never send ---------------------------


def on_work_claimed(cfg, entry, main, beads, actor: str, hive: str = "") -> None:
    """The claim hook, called by ``work claim`` / ``start`` / ``claim --group`` / ``next`` after a
    FRESH claim of each of ``beads``: a ``started`` record for every report that tracks one and is
    still ``delivery:pending`` (once per work: the record is keyed on it, so a later claim drafts
    nothing).

    **Never fails the claim, and costs nothing unless opted in.** It returns after a pure config
    read, with no bd call, no git call and no lock, unless ``intake.external.enabled``, the outbox
    and ``intake.external.milestones.draft_on_start`` are all on. Any failure, an unexpected
    exception included, becomes a warning on stderr and the claim stands. Every line goes to
    stderr, so the claim's own output is unchanged. It sends nothing and writes no tracker.
    """
    try:
        if not intake_services.draft_on_start(cfg):
            return
    except Exception:  # noqa: BLE001 - a malformed section must never fail a claim
        return
    for work in beads:
        try:
            result = intake_services.work_started(
                cfg, entry, Path(main), work, actor or identity.resolve_actor(""), hive=hive
            )
        except Exception as exc:  # noqa: BLE001 - a milestone must never fail a claim
            typer.echo(
                f"⚠ intake started milestone did not run for {work} (the claim stands): {exc}",
                err=True,
            )
            continue
        if result.code:
            why = f" ({result.reason})" if result.reason else ""
            typer.echo(
                f"⚠ intake started milestone did not complete for {work}{why}: {result.error} "
                "(the claim stands)",
                err=True,
            )
            continue
        items = result.outcome.items if result.outcome is not None else ()
        if items:
            typer.echo(f"intake: the started milestone for {work}", err=True)
        for line in _milestone_item_lines(items):
            typer.echo(line, err=True)


def _notice_row(item) -> dict:
    return {
        "report": item.report,
        "drafted": item.drafted,
        "held": item.held,
        "note": item.note,
        "error": item.error,
    }


def render_delivery_notice(result, verb: str, work: str, as_json: bool) -> None:
    """Print one ``bh work delivery blocked|deprioritised`` (human or ``--json``); non-zero on a
    refusal or when any report failed (every other report was still handled)."""
    outcome = result.outcome
    items = outcome.items if outcome is not None else ()
    failed = bool(result.code) or any(item.error for item in items)
    if as_json:
        payload = {
            "ok": not failed,
            "verb": verb,
            "work": work,
            "reason": result.reason,
            "error": result.error,
            "reports": [_notice_row(item) for item in items],
        }
        typer.echo(json.dumps(payload, sort_keys=True))
        if failed:
            raise typer.Exit(result.code or 1)
        return
    if result.code:
        code = f" ({result.reason})" if result.reason else ""
        typer.echo(f"✗ {verb} notice not drafted{code}: {result.error}", err=True)
        raise typer.Exit(result.code)
    if not items:
        typer.echo(f"  no report tracks {work}; nothing drafted")
    for line in _milestone_item_lines(items):
        typer.echo(line, err=line.lstrip().startswith("⚠"))
    held = [item for item in items if item.held]
    for item in held:
        typer.echo(
            f"⚠ {item.drafted} is held by the leak check: fix it with `bh work outbox show "
            f"{item.drafted}` (edit, or publish a finding deliberately) before it can be approved",
            err=True,
        )
    if any(item.drafted for item in items):
        typer.echo(
            "  → nothing is sent: an operator renders, approves and sends with `bh work outbox`"
        )
    if failed:
        raise typer.Exit(1)


def delivery_notice(
    verb: str, work: str, reason: str, as_: str = "", hive: str = "", as_json: bool = False
):
    """``bh work delivery blocked|deprioritised <work> --reason <text>``: draft the public reason
    under the event's fixed wording for every report that tracks ``work`` (still
    ``delivery:pending``). Nothing is sent; off unless ``intake.external.enabled``, the outbox
    and ``intake.external.milestones.draft_on_<verb>``."""
    otel.set_bead(work)
    cfg = config.load()
    entry = _import_entry(cfg, hive)
    actor = identity.resolve_actor(as_)
    result = intake_services.delivery_notice(
        cfg, entry, Path(registry.hive_dir(entry)), work, verb, reason, actor, hive=hive
    )
    render_delivery_notice(result, verb, work, as_json)


# -- the supervised outbox (bh work outbox ...): operator seat, off by default ------------------


def _outbox_result_row(result) -> dict:
    rendered = result.rendered
    return {
        "report": result.report,
        "outbox_id": result.outbox_id,
        "kind": result.kind.value,
        "state": result.state.value if result.state else "",
        "rules": list(result.rules),
        "grant_id": result.grant_id,
        "comment_id": result.comment_id,
        "notified": result.notified,
        "complete": result.complete,
        "detail": result.detail,
        "digest": rendered.digest if rendered else "",
    }


def _rendered_row(rendered) -> dict:
    return {
        "kind": rendered.kind.value,
        "target_url": rendered.target_url,
        "digest": rendered.digest,
        "short_digest": rendered.short_digest,
        "payload": rendered.payload,
        "add": sorted(rendered.add),
        "remove": sorted(rendered.remove),
        "close": rendered.close.value if rendered.close is not None else None,
        "duplicate_of": rendered.duplicate_of.url if rendered.duplicate_of is not None else None,
        "values": [list(pair) for pair in rendered.values],
        "findings": [
            {
                "id": finding.id,
                "kind": finding.kind.value,
                "match": finding.match,
                "overridable": finding.overridable,
                "warning": finding.warning,
            }
            for finding in rendered.findings
        ],
    }


def _record_row(report: str, record) -> dict:
    return {
        "report": report,
        "outbox_id": record.outbox_id,
        "event": record.event.value,
        "group": record.group,
        "operations": [
            {"kind": op.kind.value, "state": op.state.value} for op in record.operations
        ],
    }


def _stored_operation_row(op) -> dict:
    row: dict = {"kind": op.kind.value, "state": op.state.value}
    if op.labels is not None:
        row["add"], row["remove"] = sorted(op.labels.add), sorted(op.labels.remove)
    if op.close is not None:
        row["close"] = op.close.value
    if op.duplicate_of is not None:
        row["duplicate_of"] = op.duplicate_of.url
    if op.values is not None:
        row["values"] = [list(pair) for pair in op.values.values]
    return row


def _outbox_payload(call, verb: str) -> dict:
    value = call.value
    payload: dict = {"ok": call.code == 0, "verb": verb, "reason": call.reason, "error": call.error}
    if value is None:
        return payload
    if isinstance(value, intake_services.StoredOutboxRecord):  # no tracker wired (bh-fehna)
        payload["record"] = _record_row(value.report, value.record)
        payload["stored"] = {
            "text": value.text,
            "note": value.note,
            "operations": [_stored_operation_row(op) for op in value.record.operations],
        }
    elif hasattr(value, "renders"):  # a RenderedRecord
        payload["record"] = _record_row(value.report, value.record)
        payload["renders"] = [_rendered_row(r) for r in value.renders]
    elif hasattr(value, "entries"):  # an OutboxListing
        payload["records"] = [_record_row(e.report, e.record) for e in value.entries]
    elif isinstance(value, tuple):
        payload["results"] = [_outbox_result_row(r) for r in value]
    else:
        payload["results"] = [_outbox_result_row(value)]
    return payload


def _outbox_result_lines(result) -> list[str]:
    state = result.state.value if result.state else "?"
    lines = [f"✓ {result.outbox_id} {result.kind.value}: {state}"]
    if result.rules:
        lines.append(f"  rules {', '.join(result.rules)}")
    if result.grant_id:
        lines.append(f"  grant {result.grant_id} (single use)")
    if result.comment_id:
        lines.append(f"  comment {result.comment_id}")
    if result.notified:
        lines.append(f"  notified:{result.notified}")
    if result.detail:
        lines.append(f"  → {result.detail}")
    return lines


def _rendered_lines(rendered) -> list[str]:
    lines = [f"{rendered.kind.value} → {rendered.target_url}", f"  digest {rendered.digest}"]
    lines += [f"  | {line}" for line in rendered.payload.splitlines() or [""]]
    for finding in rendered.findings:
        how = "warning" if finding.warning else ("publishable" if finding.overridable else "blocks")
        lines.append(f"  finding {finding.id} ({how}): {finding.match}")
    return lines


def _stored_lines(stored) -> list[str]:
    record = stored.record
    lines = [f"{record.outbox_id} on {stored.report} ({record.event.value}, stored draft)"]
    for op in record.operations:
        line = f"{op.kind.value}: {op.state.value}"
        if op.labels is not None:
            changes = [f"+{label}" for label in sorted(op.labels.add)]
            changes += [f"-{label}" for label in sorted(op.labels.remove)]
            line += f" ({' '.join(changes)})" if changes else ""
        if op.close is not None:
            line += f" (as {op.close.value})"
        if op.values is not None:
            line += f" ({_values_text(op.values.values)})"
        lines.append(line)
    lines += [f"  | {line}" for line in stored.text.splitlines() or [""]]
    lines.append(f"  → {stored.note}")
    return lines


def render_outbox(call, verb: str, as_json: bool) -> None:
    """Print one ``bh work outbox <verb>`` result (human or ``--json``); non-zero on a refusal."""
    if as_json:
        typer.echo(json.dumps(_outbox_payload(call, verb), sort_keys=True))
        if call.code:
            raise typer.Exit(call.code)
        return
    value = call.value
    done = value if isinstance(value, tuple) else ((value,) if value is not None else ())
    if isinstance(value, intake_services.StoredOutboxRecord):
        for line in _stored_lines(value):
            typer.echo(line)
    elif hasattr(value, "renders"):
        typer.echo(f"{value.record.outbox_id} on {value.report} ({value.record.event.value})")
        for rendered in value.renders:
            for line in _rendered_lines(rendered):
                typer.echo(line)
    elif hasattr(value, "entries"):
        if not value.entries:
            typer.echo("no outbox records")
        for entry in value.entries:
            states = ", ".join(
                f"{op.kind.value}:{op.state.value}" for op in entry.record.operations
            )
            typer.echo(
                f"{entry.record.outbox_id}  {entry.report}  {entry.record.event.value}  {states}"
            )
    else:
        if verb == "reconcile" and not done and not call.code:
            typer.echo("nothing to reconcile")
        for result in done:
            for line in _outbox_result_lines(result):
                typer.echo(line)
    if call.code:
        code = f" ({call.reason})" if call.reason else ""
        typer.echo(f"✗ outbox {verb} refused{code}: {call.error}", err=True)
        raise typer.Exit(call.code)


def _outbox(verb: str, hive: str, as_json: bool, run) -> None:
    cfg = config.load()
    entry = _import_entry(cfg, hive)
    call = run(cfg, entry, Path(registry.hive_dir(entry)))
    render_outbox(call, verb, as_json)


def outbox_draft(bead: str, text: str, as_: str = "", hive: str = "", as_json: bool = False):
    """Draft an operator reply to a bound report's issue (O1): nothing is sent."""
    otel.set_bead(bead)
    actor = identity.resolve_actor(as_)
    _outbox(
        "draft",
        hive,
        as_json,
        lambda cfg, entry, directory: intake_services.outbox_draft(
            cfg, entry, directory, bead, text, actor, hive=hive
        ),
    )


def outbox_show(
    outbox_id: str = "",
    bead: str = "",
    open_only: bool = False,
    hive: str = "",
    as_json: bool = False,
):
    """The dry-run render of one record (``<id>``), or the listing of the hive's records."""
    _outbox(
        "show",
        hive,
        as_json,
        lambda cfg, entry, directory: intake_services.outbox_show(
            cfg, entry, directory, outbox_id, bead, open_only, hive=hive
        ),
    )


def outbox_approve(
    outbox_id: str,
    op: list[str],
    publish: list[str],
    as_: str = "",
    hive: str = "",
    as_json: bool = False,
):
    """Approve the digests the operator reviewed; each ``--op`` mints its own single-use grant."""
    actor = identity.resolve_actor(as_)
    _outbox(
        "approve",
        hive,
        as_json,
        lambda cfg, entry, directory: intake_services.outbox_approve(
            cfg, entry, directory, outbox_id, op, publish, actor, hive=hive
        ),
    )


def outbox_send(
    outbox_id: str, kind: str = "", as_: str = "", hive: str = "", as_json: bool = False
):
    """Send one approved operation (write-ahead, receipt)."""
    actor = identity.resolve_actor(as_)
    _outbox(
        "send",
        hive,
        as_json,
        lambda cfg, entry, directory: intake_services.outbox_send(
            cfg, entry, directory, outbox_id, kind, actor, hive=hive
        ),
    )


def _backfill_row(item) -> dict:
    return {
        "report": item.report,
        "outbox_id": item.outbox_id,
        "reason": item.reason.value if item.reason is not None else "",
        "drafted": item.drafted,
        "detail": item.detail,
        "reply_text": item.reply_text,
        "duplicate_of": item.duplicate_of,
    }


def render_close_backfill(call, as_json: bool) -> None:
    """Print ``bh work outbox backfill-close`` (human or ``--json``); non-zero on a refusal."""
    outcome = call.value
    if as_json:
        payload: dict = {
            "ok": call.code == 0,
            "verb": "backfill-close",
            "reason": call.reason,
            "error": call.error,
        }
        if outcome is not None:
            payload["dry_run"] = outcome.dry_run
            payload["items"] = [_backfill_row(item) for item in outcome.items]
        typer.echo(json.dumps(payload, sort_keys=True))
        if call.code:
            raise typer.Exit(call.code)
        return
    if outcome is not None:
        if not outcome.items:
            typer.echo("no closed report has an open issue to close")
        verb = "would draft" if outcome.dry_run else "drafted"
        for item in outcome.items:
            reason = item.reason.value if item.reason is not None else "?"
            if item.skipped:
                typer.echo(f"- {item.report} skipped: {item.detail}")
            elif item.drafted or outcome.dry_run:
                where = item.outbox_id or "a new reply"
                typer.echo(f"✓ {item.report} {verb} issue.close as {reason} on {where}")
                if item.duplicate_of:
                    typer.echo(f"    as a duplicate of {item.duplicate_of}")
                if item.reply_text:
                    typer.echo(f"    with the reply it never got: {item.reply_text}")
        if outcome.drafted:
            typer.echo(
                "  → review each with `outbox show <id>`, approve with `--op close:<digest>`, "
                "send with `--kind close` after the reply and the labels"
            )
    if call.code:
        code = f" ({call.reason})" if call.reason else ""
        typer.echo(f"✗ outbox backfill-close refused{code}: {call.error}", err=True)
        raise typer.Exit(call.code)


def outbox_backfill_close(
    dry_run: bool = False, as_: str = "", hive: str = "", as_json: bool = False
):
    """Draft the missing issue closes of reports already closed (drafts only; never sends)."""
    actor = identity.resolve_actor(as_)
    cfg = config.load()
    entry = _import_entry(cfg, hive)
    call = intake_services.outbox_backfill_close(
        cfg,
        entry,
        Path(registry.hive_dir(entry)),
        actor,
        dry_run,
        branch=str(config.integration_branch(cfg, entry)),
        hive=hive,
    )
    render_close_backfill(call, as_json)


def outbox_reconcile(outbox_id: str = "", as_: str = "", hive: str = "", as_json: bool = False):
    """Settle unknown outcomes by reading the issue back (never sends)."""
    actor = identity.resolve_actor(as_)
    _outbox(
        "reconcile",
        hive,
        as_json,
        lambda cfg, entry, directory: intake_services.outbox_reconcile(
            cfg, entry, directory, outbox_id, actor, hive=hive
        ),
    )


def outbox_discard(
    outbox_id: str,
    reason: str,
    accept_unknown: bool = False,
    kind: str = "",
    as_: str = "",
    hive: str = "",
    as_json: bool = False,
):
    """Abandon an unsent operation (or, with ``--accept-unknown``, one whose outcome is unknown)."""
    actor = identity.resolve_actor(as_)
    _outbox(
        "discard",
        hive,
        as_json,
        lambda cfg, entry, directory: intake_services.outbox_discard(
            cfg, entry, directory, outbox_id, reason, accept_unknown, kind, actor, hive=hive
        ),
    )


def outbox_retry(
    outbox_id: str, kind: str = "", as_: str = "", hive: str = "", as_json: bool = False
):
    """Return a failed operation to drafted (a new render, digest and approval are needed)."""
    actor = identity.resolve_actor(as_)
    _outbox(
        "retry",
        hive,
        as_json,
        lambda cfg, entry, directory: intake_services.outbox_retry(
            cfg, entry, directory, outbox_id, kind, actor, hive=hive
        ),
    )


# -- the conversation (bh work thread ...): comment ingestion, off by default -------------------


def _actor_row(actor) -> dict | None:
    if actor is None:
        return None
    return {"id": actor.id, "kind": actor.kind, "display": actor.display}


def _comment_row(record, new: bool, flagged: bool) -> dict:
    return {
        "id": record.id,
        "role": record.role.value,
        "evidence": record.evidence.value,
        "state": record.state.value,
        "author": _actor_row(record.author),
        "created_at": record.created_at.isoformat(),
        "rev": record.rev,
        "considered_rev": record.considered_rev,
        "outbox_id": record.outbox_id,
        "flags": sorted(record.flags),
        "body": record.body,
        "new_evidence": new,
        "flagged": flagged,
    }


def _thread_row(view) -> dict:
    thread = view.thread
    new, flagged = set(view.new_evidence), set(view.flagged)
    return {
        "report": view.report,
        "seq": thread.seq,
        "state": thread.state.value,
        "considered_seq": thread.considered_seq,
        "tracker": thread.tracker,
        "project": thread.project,
        "issue_url": thread.issue.url,
        "new_evidence": list(view.new_evidence),
        "flagged": list(view.flagged),
        "comments": [
            _comment_row(record, record.id in new, record.id in flagged)
            for record in thread.records.values()
        ],
    }


def _outcome_row(outcome, step: str = "") -> dict:
    row = {
        "report": outcome.report,
        "seq": outcome.seq,
        "written": outcome.written,
        "complete": outcome.complete,
        "checkpoint_advanced": outcome.checkpoint_advanced,
        "events": [
            {"kind": e.kind.value, "comment": e.comment, "seq": e.seq, "outbox_id": e.outbox_id}
            for e in outcome.events
        ],
        "detail": outcome.detail,
    }
    if step:
        row["step"] = step
    return row


def _incomplete(call) -> tuple:
    value = call.value if isinstance(call.value, tuple) else (call.value,)
    return tuple(o for o in value if o is not None and not getattr(o, "complete", True))


def _thread_payload(call, verb: str, report: str) -> dict:
    payload: dict = {
        "ok": call.code == 0 and not _incomplete(call),
        "verb": verb,
        "report": report,
        "reason": call.reason,
        "error": call.error,
    }
    if call.code:
        return payload
    value = call.value
    if verb == "show":
        payload["thread"] = None if value is None else _thread_row(value)
        links = getattr(call, "links", None)
        payload["links"] = None if links is None else _relations_row(links)
    elif verb == "ingest":
        payload["outcomes"] = [
            _outcome_row(outcome, step)
            for step, outcome in zip(("ingest", "reconcile"), value, strict=True)
        ]
    else:
        payload["outcome"] = _outcome_row(value)
    return payload


def _event_summary(outcome) -> str:
    counts: dict[str, int] = {}
    for event in outcome.events:
        counts[event.kind.value] = counts.get(event.kind.value, 0) + 1
    if not counts:
        return "no change"
    return ", ".join(f"{kind} {n}" for kind, n in sorted(counts.items()))


def _thread_lines(view) -> list[str]:
    thread = view.thread
    lines = [f"{view.report} thread seq {thread.seq} ({thread.state.value}) → {thread.issue.url}"]
    lines.append(f"  new evidence: {', '.join(view.new_evidence) or 'none'}")
    if view.flagged:
        lines.append(f"  flagged: {', '.join(view.flagged)}")
    new, flagged = set(view.new_evidence), set(view.flagged)
    for record in thread.records.values():
        who = (record.author.display or record.author.id) if record.author else "?"
        marks = [
            m for m, on in (("new", record.id in new), ("flagged", record.id in flagged)) if on
        ]
        state = "" if record.state.value == "live" else f" {record.state.value}"
        tail = f"  [{', '.join(marks)}]" if marks else ""
        lines.append(f"{record.id}  {record.role.value}  {who}  rev {record.rev}{state}{tail}")
        body = record.body if record.body is not None else "(body purged)"
        lines += [f"  | {line}" for line in body.splitlines() or [""]]
    return lines


def render_thread(call, verb: str, report: str, as_json: bool) -> None:
    """Print one ``bh work thread <verb>`` result (human or ``--json``); non-zero on a refusal or
    on an ingest whose tracker read failed part-way (what was read before it is kept)."""
    incomplete = () if call.code else _incomplete(call)
    if as_json:
        typer.echo(json.dumps(_thread_payload(call, verb, report), sort_keys=True))
        if call.code or incomplete:
            raise typer.Exit(call.code or 1)
        return
    if call.code:
        code = f" ({call.reason})" if call.reason else ""
        typer.echo(f"✗ thread {verb} refused{code}: {call.error}", err=True)
        raise typer.Exit(call.code)
    value = call.value
    if verb == "show":
        if value is None:
            typer.echo(f"no thread recorded for {report} (run `bh work thread ingest {report}`)")
        else:
            for line in _thread_lines(value):
                typer.echo(line)
        links = getattr(call, "links", None)
        if links is not None:
            typer.echo("links:")
            for line in _relations_lines(links):
                typer.echo(line)
    elif verb == "ingest":
        for step, outcome in zip(("ingested", "reconciled"), value, strict=True):
            typer.echo(f"✓ {step} {outcome.report}: seq {outcome.seq}, {_event_summary(outcome)}")
    else:
        typer.echo(f"✓ considered {value.report} through seq {value.seq}: {_event_summary(value)}")
    for outcome in incomplete:
        typer.echo(f"⚠ {outcome.report}: incomplete read: {outcome.detail}", err=True)
    if incomplete:
        raise typer.Exit(1)


def _thread(verb: str, report: str, hive: str, as_json: bool, run) -> None:
    otel.set_bead(report)
    cfg = config.load()
    entry = _import_entry(cfg, hive)
    call = run(cfg, entry, Path(registry.hive_dir(entry)))
    render_thread(call, verb, report, as_json)


def thread_show(bead: str, hive: str = "", as_json: bool = False):
    """The stored thread of a bound report (no tracker request)."""
    _thread(
        "show",
        bead,
        hive,
        as_json,
        lambda cfg, entry, directory: intake_services.thread_show(
            cfg, entry, directory, bead, hive=hive
        ),
    )


def thread_ingest(bead: str, as_: str = "", hive: str = "", as_json: bool = False):
    """Ingest a bound report's comments, then reconcile deletions (reads only)."""
    actor = identity.resolve_actor(as_)
    _thread(
        "ingest",
        bead,
        hive,
        as_json,
        lambda cfg, entry, directory: intake_services.thread_ingest(
            cfg, entry, directory, bead, actor, hive=hive
        ),
    )


def thread_considered(
    bead: str, through: int, as_: str = "", hive: str = "", as_json: bool = False
):
    """Mark the thread considered through ``through`` (no tracker request)."""
    actor = identity.resolve_actor(as_)
    _thread(
        "considered",
        bead,
        hive,
        as_json,
        lambda cfg, entry, directory: intake_services.thread_considered(
            cfg, entry, directory, bead, through, actor, hive=hive
        ),
    )


# -- waiting for the reporter (bh-61xkc): needs-info and wake-on-reply, off by default -----------


def render_needs_info(call, report: str, as_json: bool) -> None:
    """Print one ``bh work needs-info`` (human or ``--json``); non-zero on a refusal."""
    outcome = call.value if not call.code else None
    if as_json:
        payload = {
            "ok": call.code == 0,
            "verb": "needs-info",
            "report": report,
            "reason": call.reason,
            "error": call.error,
        }
        if outcome is not None:
            payload.update(
                {
                    "state": outcome.state,
                    "state_written": outcome.state_written,
                    "outbox_id": outcome.outbox_id,
                    "held": outcome.held,
                    "detail": outcome.detail,
                }
            )
        typer.echo(json.dumps(payload, sort_keys=True))
        if call.code:
            raise typer.Exit(call.code)
        return
    if call.code:
        code = f" ({call.reason})" if call.reason else ""
        typer.echo(f"✗ needs-info refused{code}: {call.error}", err=True)
        raise typer.Exit(call.code)
    already = "" if outcome.state_written else " (it was already waiting; another question)"
    typer.echo(f"✓ {report} is waiting for its reporter (intake:needs-info){already}")
    typer.echo(f"  → question drafted as {outcome.outbox_id}; nothing is sent")
    if outcome.held:
        typer.echo(f"⚠ the draft is held: {outcome.detail}", err=True)
    typer.echo(
        f"  → review it with `bh work outbox show {outcome.outbox_id}`; an operator approves and "
        "sends it. A reply wakes the report with `bh work wake` after `bh work thread ingest`"
    )


def _wake_row(item) -> dict:
    return {
        "report": item.report,
        "woken": item.woken,
        "evidence": list(item.evidence),
        "detail": item.detail,
        "error": item.error,
    }


def _wake_line(item) -> str:
    if item.error:
        return f"⚠ {item.report}: FAILED: {item.error}"
    if item.woken:
        return (
            f"✓ woke {item.report}: the reporter replied ({', '.join(item.evidence)}) → "
            "intake:untriaged (still held, never work; read it with `bh work thread show`)"
        )
    return f"  {item.report} stays on intake:needs-info: {item.detail}"


def render_wake(call, report: str, all_: bool, as_json: bool) -> None:
    """Print one ``bh work wake`` (human or ``--json``); non-zero on a refusal, or when the
    sweep failed for any report (every other report was still handled)."""
    value = call.value if not call.code else None
    items = () if value is None else (value.items if all_ else (value,))
    failed = bool(call.code) or any(item.error for item in items)
    if as_json:
        payload: dict = {
            "ok": not failed,
            "verb": "wake",
            "all": all_,
            "report": report,
            "reason": call.reason,
            "error": call.error,
        }
        if value is not None and all_:
            payload["items"] = [_wake_row(item) for item in items]
            payload["woken"] = [item.report for item in items if item.woken]
        elif value is not None:
            payload.update(_wake_row(value))
        typer.echo(json.dumps(payload, sort_keys=True))
        if failed:
            raise typer.Exit(call.code or 1)
        return
    if call.code:
        code = f" ({call.reason})" if call.reason else ""
        typer.echo(f"✗ wake refused{code}: {call.error}", err=True)
        raise typer.Exit(call.code)
    if all_ and not items:
        typer.echo("no report is waiting for its reporter")
    for item in items:
        line = _wake_line(item)
        typer.echo(line, err=bool(item.error))
    if failed:
        raise typer.Exit(1)


def needs_info(bead: str, ask: str, as_: str = "", hive: str = "", as_json: bool = False):
    """Put a bound report on ``intake:needs-info`` and draft the question as a reply (never
    sent without an operator); off unless ``intake.external.needs_info.enabled``."""
    otel.set_bead(bead)
    cfg = config.load()
    entry = _import_entry(cfg, hive)
    actor = identity.resolve_actor(as_)
    call = intake_services.needs_info(
        cfg, entry, Path(registry.hive_dir(entry)), bead, ask, actor, hive=hive
    )
    render_needs_info(call, bead, as_json)


def wake(bead: str = "", all_: bool = False, as_: str = "", hive: str = "", as_json: bool = False):
    """Wake a waiting report its reporter answered (or ``--all`` of them, opt-in); off unless
    ``intake.external.needs_info.enabled``."""
    if bool(bead) == all_:
        typer.echo("✗ pass exactly one of <report> or --all", err=True)
        raise typer.Exit(2)
    if bead:
        otel.set_bead(bead)
    cfg = config.load()
    entry = _import_entry(cfg, hive)
    actor = identity.resolve_actor(as_)
    call = intake_services.wake(
        cfg, entry, Path(registry.hive_dir(entry)), bead, actor, all_=all_, hive=hive
    )
    render_wake(call, bead, all_, as_json)


# -- the label projection (bh work labels ...) and review-gated triage (bh work triage ...) ------
#
# The JSON serializers below are the published shape of each verb's ``--json`` document (and of
# the ``triage review --out`` files): plain dicts, sorted keys, every record flattened to its
# public fields. Proposals use the stored ``intake.proposal`` document shape.


def _sorted(values) -> list[str]:
    return sorted(values or ())


def _envelope(call, verb: str) -> dict:
    return {"ok": call.code == 0, "verb": verb, "reason": call.reason, "error": call.error}


def _refused(call, verb: str) -> None:
    code = f" ({call.reason})" if call.reason else ""
    typer.echo(f"✗ {verb} refused{code}: {call.error}", err=True)
    raise typer.Exit(call.code or 1)


def _emit(payload: dict, code: int = 0) -> None:
    typer.echo(json.dumps(payload, sort_keys=True))
    if code:
        raise typer.Exit(code)


def _values_text(values) -> str:
    """``type=Bug, Priority=High``: the values a type or field operation sets."""
    return ", ".join(f"{name}={value}" for name, value in values)


def _value_change_text(change) -> str:
    return f"{change.name} {change.live or '(unset)'} → {change.desired}"


def label_sync_item_row(item) -> dict:
    """One report's sync: the converging diff, drift, swaps, the issue type and field values it
    sets (with their notes) and the record drafted or found."""
    return {
        "report": item.report,
        "url": item.url,
        "add": _sorted(item.add),
        "remove": _sorted(item.remove),
        "drift": _sorted(item.drift),
        "undefined": _sorted(item.undefined),
        "swaps": [{"dimension": dim, "removed": old, "added": new} for dim, old, new in item.swaps],
        "values": [{"name": c.name, "live": c.live, "desired": c.desired} for c in item.values],
        "value_notes": list(item.value_notes),
        "changed": item.changed,
        "drafted": item.drafted,
        "pending": item.pending,
        "superseded": item.superseded,
        "discarded": item.discarded,
        "held": item.held,
        "note": item.note,
        "error": item.error,
    }


def label_sync_row(outcome) -> dict:
    items = outcome.items
    return {
        "dry_run": outcome.dry_run,
        "items": [label_sync_item_row(item) for item in items],
        "changed": [item.report for item in items if item.changed],
        "value_drift": {
            report: [_value_change_text(c) for c in changes]
            for report, changes in outcome.value_drift.items()
        },
        "drafted": list(outcome.drafted),
        "errors": [item.report for item in items if item.error],
    }


def _label_spec_row(spec) -> dict:
    return {
        "name": spec.name,
        "color": spec.color,
        "description": spec.description,
        "dimension": spec.dimension,
        "value": spec.value,
    }


def label_plan_row(plan) -> dict:
    """A ``labels define`` plan: what it would write, what it keeps and what blocks it."""
    return {
        "target": plan.target,
        "digest": plan.digest,
        "short_digest": plan.short_digest,
        "changed": plan.changed,
        "blocked": plan.blocked,
        "create": [_label_spec_row(s) for s in plan.create],
        "update": [_label_spec_row(s) for s in plan.update],
        "delete": list(plan.delete),
        "kept": [
            {"name": k.name, "reason": k.reason, "carried_by": list(k.carried_by)}
            for k in plan.kept
        ],
        "problems": [{"name": p.name, "detail": p.detail} for p in plan.problems],
        "findings": {
            name: [
                {"id": f.id, "kind": f.kind.value, "match": f.match, "overridable": f.overridable}
                for f in found
            ]
            for name, found in sorted(plan.findings.items())
        },
    }


def label_define_row(outcome) -> dict:
    return {
        "plan": label_plan_row(outcome.plan),
        "applied": outcome.applied,
        "grant_ids": list(outcome.grant_ids),
        "audit": outcome.audit,
        "detail": outcome.detail,
    }


def _batch_item_row(item) -> dict:
    return {
        "report": item.report,
        "outbox_id": item.outbox_id,
        "digest": item.digest,
        "short_digest": item.short_digest,
        "url": item.url,
        "add": _sorted(item.add),
        "remove": _sorted(item.remove),
        "kind": item.kind.value,
        "values": [list(pair) for pair in item.values],
        "stale": item.stale,
        "note": item.note,
    }


def label_batch_row(batch) -> dict:
    """The rendered batch an operator approves by its digest."""
    return {
        "digest": batch.digest,
        "short_digest": batch.short_digest,
        "items": [_batch_item_row(item) for item in batch.items],
        "stale": [item.outbox_id for item in batch.stale],
    }


def label_batch_outcome_row(outcome) -> dict:
    return {
        "batch": label_batch_row(outcome.batch),
        "approved": [_outbox_result_row(result) for result in outcome.approved],
        "grant_ids": list(outcome.grant_ids),
        "stale": [_batch_item_row(item) for item in outcome.stale],
        "failed": [{"outbox_id": oid, "error": why} for oid, why in outcome.failed],
    }


def _label_change(add, remove) -> str:
    changes = [f"+{label}" for label in sorted(add)] + [f"-{label}" for label in sorted(remove)]
    return " ".join(changes) or "no change"


def _label_sync_line(item, dry_run: bool = False) -> str:
    if item.error and not (dry_run and item.undefined):
        return f"⚠ {item.report}: {item.error}"
    parts = [_label_change(item.add, item.remove)] if item.labels_changed else []
    parts += [_value_change_text(change) for change in item.values]
    parts = parts or ["converged"]
    if item.undefined:  # a dry run: the diff, and what define must create before a sync drafts
        parts.append(
            f"not defined yet: {', '.join(sorted(item.undefined))} (run `bh work labels define`)"
        )
    if item.drift:
        parts.append(f"drift {', '.join(sorted(item.drift))}")
    parts += list(item.value_notes)
    for what, value in (
        ("drafted", item.drafted),
        ("already drafted as", item.pending),
        ("superseding", item.superseded),
        ("discarded", item.discarded),
    ):
        if value:
            parts.append(f"{what} {value}")
    if item.held:
        parts.append("held by the leak check")
    if item.note:
        parts.append(item.note)
    return f"  {item.report}: {'; '.join(parts)}"


def _result(verb: str, hive: str, as_json: bool, call, payload, lines, failed=False) -> None:
    """Print one labels or triage verb (human or ``--json``); non-zero on a refusal or failure."""
    if as_json:
        body = _envelope(call, verb)
        if not call.code:
            body.update(payload(call.value))
        body["ok"] = not call.code and not failed
        _emit(body, call.code or (1 if failed else 0))
        return
    if call.code:
        _refused(call, verb)
    for line in lines(call.value):
        typer.echo(line, err=line.startswith(("⚠", "✗")))
    if failed:
        raise typer.Exit(1)


def _hive_call(hive: str, run):
    cfg = config.load()
    entry = _import_entry(cfg, hive)
    return cfg, entry, run(cfg, entry, Path(registry.hive_dir(entry)))


def labels_sync(
    report: str = "",
    all_: bool = False,
    include_closed: bool = False,
    dry_run: bool = False,
    prune_unmapped: bool = False,
    as_: str = "",
    hive: str = "",
    as_json: bool = False,
):
    """``bh work labels sync``: converge the bound issues' owned labels by drafting."""
    if report and all_:
        typer.echo("✗ pass a <report> or --all, not both", err=True)
        raise typer.Exit(2)
    if report:
        otel.set_bead(report)
    actor = identity.resolve_actor(as_)
    _cfg, _entry, call = _hive_call(
        hive,
        lambda cfg, entry, directory: intake_services.labels_sync(
            cfg,
            entry,
            directory,
            actor,
            report=report,
            include_closed=include_closed,
            dry_run=dry_run,
            prune_unmapped=prune_unmapped,
            hive=hive,
        ),
    )
    outcome = call.value

    def lines(value) -> list[str]:
        items = value.items
        changed = [i for i in items if i.changed]
        mode = " (dry run: nothing written)" if value.dry_run else ""
        out = [
            f"labels sync{mode}: {len(items)} report(s), {len(changed)} to change, "
            f"{len(value.drafted)} drafted"
        ]
        out += [_label_sync_line(item, value.dry_run) for item in items]
        if value.drafted:
            out.append(
                "  → nothing is sent: review the batch with `bh work labels review`, then an "
                "operator approves it with `bh work labels approve --batch <digest>`"
            )
        return out

    # a dry run only reports a label the repository does not define yet (define it first); any
    # other error, and every refused draft of a real sync, fails the command
    failed = outcome is not None and any(
        not (outcome.dry_run and item.undefined) for item in outcome.errors
    )
    _result("labels sync", hive, as_json, call, label_sync_row, lines, failed)


def labels_define(
    dry_run: bool = False,
    prune: bool = False,
    approve: str = "",
    as_: str = "",
    hive: str = "",
    as_json: bool = False,
):
    """``bh work labels define``: the definitions plan and digest; ``--approve`` applies it."""
    actor = identity.resolve_actor(as_)
    _cfg, _entry, call = _hive_call(
        hive,
        lambda cfg, entry, directory: intake_services.labels_define(
            cfg, entry, directory, actor, approve=approve, prune=prune, dry_run=dry_run, hive=hive
        ),
    )

    def lines(value) -> list[str]:
        plan = value.plan
        out = [f"labels define on {plan.target}: plan {plan.short_digest}"]
        out += [f"  create {s.name} (#{s.color}) {s.description}" for s in plan.create]
        out += [f"  update {s.name} (#{s.color}) {s.description}" for s in plan.update]
        out += [f"  delete {name}" for name in plan.delete]
        out += [f"  keep {k.name}: carried by {', '.join(k.carried_by)}" for k in plan.kept]
        out += [f"⚠ {p.name}: {p.detail}" for p in plan.problems]
        for name, found in sorted(plan.findings.items()):
            out.append(f"⚠ {name}: leak finding {', '.join(f.id for f in found)}")
        if value.grant_ids:
            out.append(f"  grants {', '.join(value.grant_ids)} (single use)")
        out.append(f"  → {value.detail}")
        if not value.applied and plan.changed and not plan.blocked:
            out.append(
                f"  → an operator applies it with `bh work labels define --approve "
                f"{plan.short_digest}`"
            )
        return out

    _result("labels define", hive, as_json, call, label_define_row, lines)


def labels_review(hive: str = "", as_json: bool = False):
    """``bh work labels review``: the batch of drafted label changes and its digest."""
    _cfg, _entry, call = _hive_call(
        hive,
        lambda cfg, entry, directory: intake_services.labels_review(
            cfg, entry, directory, hive=hive
        ),
    )

    def lines(batch) -> list[str]:
        if not batch.items:
            return ["no drafted label change (run `bh work labels sync`)"]
        out = [
            f"label batch {batch.short_digest}: {len(batch.items)} change(s), "
            f"{len(batch.stale)} stale"
        ]
        for item in batch.items:
            stale = f"  [stale: {item.note or 'the labels moved since the draft'}]"
            change = (
                _label_change(item.add, item.remove)
                if not item.values
                else f"{item.kind.value} {_values_text(item.values)}"
            )
            out.append(f"  {item.outbox_id}  {item.report}  {change}{stale if item.stale else ''}")
        out.append(
            f"  → an operator approves it with `bh work labels approve --batch "
            f"{batch.short_digest}` (a stale item is skipped and re-drafted by the next sync)"
        )
        return out

    _result("labels review", hive, as_json, call, lambda b: {"batch": label_batch_row(b)}, lines)


def labels_approve(batch: str, as_: str = "", hive: str = "", as_json: bool = False):
    """``bh work labels approve --batch <digest>``: one single-use grant per issue."""
    actor = identity.resolve_actor(as_)
    _cfg, _entry, call = _hive_call(
        hive,
        lambda cfg, entry, directory: intake_services.labels_approve(
            cfg, entry, directory, batch, actor, hive=hive
        ),
    )

    def lines(outcome) -> list[str]:
        total = len(outcome.batch.items)
        out = [
            f"✓ approved {len(outcome.approved)} of {total} label change(s) in batch "
            f"{outcome.batch.short_digest}"
        ]
        for result in outcome.approved:
            out.append(
                f"  {result.outbox_id} {result.report} {result.kind.value}: grant {result.grant_id}"
            )
        for item in outcome.stale:
            out.append(f"⚠ {item.outbox_id} {item.report}: stale, skipped (re-run labels sync)")
        for outbox_id, why in outcome.failed:
            out.append(f"⚠ {outbox_id}: {why}")
        if outcome.approved:
            out.append(
                "  → nothing is sent: send each with `bh work outbox send <id> --kind labels` "
                "(`--kind type` / `--kind fields` for a type or field change)"
            )
        return out

    failed = bool(call.value is not None and call.value.failed)
    _result("labels approve", hive, as_json, call, label_batch_outcome_row, lines, failed)


# -- review-gated triage -------------------------------------------------------------------------


def _binding_row(binding) -> dict | None:
    if binding is None:
        return None
    return {
        "tracker": binding.tracker,
        "project": binding.project,
        "url": binding.url,
        "identifier": binding.identifier,
    }


def proposal_row(proposal) -> dict | None:
    """A proposal as its stored ``intake.proposal`` document (status, reviews, history)."""
    from .modules.intake import proposal_to_document

    return None if proposal is None else proposal_to_document(proposal)


def _cluster_link_row(link) -> dict:
    return {
        "first": link.first,
        "second": link.second,
        "kind": link.kind.value,
        "score": link.score,
        "detail": link.detail,
    }


def _cluster_row(cluster) -> dict:
    return {
        "key": cluster.key,
        "members": list(cluster.members),
        "links": [_cluster_link_row(link) for link in cluster.links],
    }


def _index_row(entry) -> dict:
    return {
        "report": entry.report,
        "title": entry.title,
        "reference": entry.reference,
        "disposition": entry.disposition,
        "status": entry.status,
        "share_with": entry.share_with,
        "cluster": entry.cluster,
        "batch": entry.batch,
    }


def review_metrics_row(metrics) -> dict:
    return {
        "proposals": metrics.proposals,
        "reviewed": metrics.reviewed,
        "passes": [
            {
                "pass": p.pass_no,
                "reviewed": p.reviewed,
                "confirmed": p.confirmed,
                "corrected": p.corrected,
                "unsure": p.unsure,
                "correction_rate": p.correction_rate,
            }
            for p in metrics.passes
        ],
        "reporter_visible": metrics.reporter_visible,
        "reporter_visible_errors": metrics.reporter_visible_errors,
        "reporter_visible_error_rate": metrics.reporter_visible_error_rate,
    }


def review_packet_row(packet) -> dict:
    """One report's packet; the global index is the run's (not repeated per packet)."""
    return {
        "report": packet.report,
        "title": packet.title,
        "body": packet.body,
        "labels": list(packet.labels),
        "binding": _binding_row(packet.binding),
        "thread": None if packet.thread is None else _thread_row(packet.thread),
        "thread_complete": packet.thread_complete,
        "thread_detail": packet.thread_detail,
        "proposal": proposal_row(packet.proposal),
        "cluster": packet.cluster,
        "cluster_members": list(packet.cluster_members),
        "links": [_cluster_link_row(link) for link in packet.links],
    }


def review_batch_row(batch, run) -> dict:
    """One batch as a self-contained document: its clusters, its packets and the global index."""
    return {
        "number": batch.number,
        "reports": list(batch.reports),
        "clusters": [_cluster_row(c) for c in batch.clusters],
        "packets": [review_packet_row(p) for p in batch.packets],
        "index": [_index_row(entry) for entry in run.index],
    }


def review_run_row(run, *, packets: bool = True) -> dict:
    row = {
        "reports": list(run.reports),
        "index": [_index_row(entry) for entry in run.index],
        "clusters": [_cluster_row(c) for c in run.clusters],
        "ingested": list(run.ingested),
        "metrics": review_metrics_row(run.metrics),
    }
    if packets:
        row["batches"] = [
            {k: v for k, v in review_batch_row(b, run).items() if k != "index"} for b in run.batches
        ]
    else:
        row["batches"] = [
            {"number": b.number, "reports": list(b.reports), "file": _batch_file(b.number)}
            for b in run.batches
        ]
    return row


def _batch_file(number: int) -> str:
    return f"batch-{number:03d}.json"


#: The file ``triage review --out`` writes beside the batches.
REVIEW_INDEX_FILE = "index.json"


def write_review_packets(run, out: Path) -> tuple[Path, ...]:
    """``--out DIR``: one JSON document per batch (``batch-NNN.json``, with the global index) and
    ``index.json`` (the batch list, index, clusters, ingested threads and metrics)."""
    out.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for batch in run.batches:
        path = out / _batch_file(batch.number)
        path.write_text(
            json.dumps(review_batch_row(batch, run), sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        written.append(path)
    index = out / REVIEW_INDEX_FILE
    index.write_text(
        json.dumps(review_run_row(run, packets=False), sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    written.append(index)
    return tuple(written)


def triage_review(
    pending: bool = False,
    batch_size: int = intake_services.DEFAULT_REVIEW_BATCH_SIZE,
    out: str = "",
    as_: str = "",
    hive: str = "",
    as_json: bool = False,
):
    """``bh work triage review --pending``: cluster-aware review packets for the pending set."""
    if not pending:
        typer.echo("✗ pass --pending (review packets are built for the pending set)", err=True)
        raise typer.Exit(2)
    if batch_size < 1:
        typer.echo("✗ --batch-size must be at least 1", err=True)
        raise typer.Exit(2)
    actor = identity.resolve_actor(as_)
    _cfg, _entry, call = _hive_call(
        hive,
        lambda cfg, entry, directory: intake_services.triage_review(
            cfg, entry, directory, actor, batch_size=batch_size, hive=hive
        ),
    )
    written: tuple[Path, ...] = ()
    if not call.code and out:
        written = write_review_packets(call.value, Path(out))

    def payload(run) -> dict:
        row = review_run_row(run, packets=not out)
        if out:
            row["out"] = str(Path(out))
            row["files"] = [str(path) for path in written]
        return row

    def lines(run) -> list[str]:
        result = [
            f"triage review: {len(run.reports)} pending report(s) in {len(run.batches)} "
            f"batch(es), {len(run.clusters)} cluster(s); {len(run.ingested)} thread(s) ingested"
        ]
        for batch in run.batches:
            families = [c.key for c in batch.clusters if len(c.members) > 1]
            extra = f" (families: {', '.join(families)})" if families else ""
            result.append(f"  batch {batch.number}: {', '.join(batch.reports)}{extra}")
        for packet in (p for b in run.batches for p in b.packets):
            if not packet.thread_complete:
                result.append(f"⚠ {packet.report}: partial thread: {packet.thread_detail}")
        metrics = run.metrics
        for p in metrics.passes:
            result.append(
                f"  review pass {p.pass_no}: {p.corrected}/{p.reviewed} corrected "
                f"({p.correction_rate:.0%}), {p.unsure} unsure"
            )
        if written:
            result.append(f"  → wrote {len(written)} file(s) to {out}")
        return result

    _result("triage review", hive, as_json, call, payload, lines)


def _read_document(source: str):
    """The JSON document at ``source`` (a file path, or ``-`` for stdin); exit 2 if unreadable."""
    try:
        if source == "-":
            text = typer.get_text_stream("stdin").read()
        else:
            text = Path(source).read_text(encoding="utf-8")
    except OSError as exc:
        typer.echo(f"✗ cannot read {source}: {exc}", err=True)
        raise typer.Exit(2) from None
    try:
        return json.loads(text)
    except ValueError as exc:
        typer.echo(f"✗ {source} is not a JSON document: {exc}", err=True)
        raise typer.Exit(2) from None


def triage_propose(
    bead: str, source: str, review_of: str = "", as_: str = "", hive: str = "", as_json=False
):
    """``bh work triage propose <report> <file|->`` (``--review-of <pass>``: a verdict)."""
    otel.set_bead(bead)
    pass_no = 0
    if review_of:
        try:
            pass_no = intake_services.review_pass(review_of)
        except ValueError as exc:
            typer.echo(f"✗ {exc}", err=True)
            raise typer.Exit(2) from None
    document = _read_document(source)
    actor = identity.resolve_actor(as_)
    verb = "triage review-of" if pass_no else "triage propose"
    _cfg, _entry, call = _hive_call(
        hive,
        lambda cfg, entry, directory: intake_services.triage_propose(
            cfg,
            entry,
            directory,
            bead,
            document,
            actor,
            review_of=pass_no,
            branch=str(config.integration_branch(cfg, entry)),
            hive=hive,
        ),
    )

    def payload(outcome) -> dict:
        row = {
            "report": outcome.report,
            "proposal": proposal_row(outcome.proposal),
            "status": outcome.proposal.status.value,
        }
        if pass_no:
            row.update({"pass": pass_no, "adopted": outcome.adopted})
        else:
            row["kind"] = outcome.kind.value
        return row

    def lines(outcome) -> list[str]:
        proposal = outcome.proposal
        if pass_no:
            review = proposal.reviews[-1]
            adopted = " (its correction adopted)" if outcome.adopted else ""
            return [
                f"✓ {outcome.report}: review pass {review.pass_no} {review.verdict.value}"
                f"{adopted} → {proposal.status.value}"
            ]
        return [
            f"✓ {outcome.report}: proposal {outcome.kind.value} ({proposal.disposition.value}, "
            f"{proposal.status.value}, seq {proposal.seq})"
        ]

    _result(verb, hive, as_json, call, payload, lines)


def _finding_row(finding) -> dict:
    return {"kind": finding.kind.value, "detail": finding.detail, "fix": finding.fix}


def proposal_check_row(check, item=None) -> dict:
    """One report's checks; with its dry-run plan ``item``, the verification gap too."""
    row = {
        "report": check.report,
        "passed": check.passed,
        "stale": check.stale,
        "findings": [_finding_row(f) for f in check.findings],
        "computed_fixed_in": check.computed_fixed_in,
        "relates": list(check.relates),
    }
    if item is not None:
        row["plan"] = item.status.value
        row["detail"] = item.detail
    return row


def triage_check(pending: bool = False, as_: str = "", hive: str = "", as_json: bool = False):
    """``bh work triage check [--pending]``: every pre-apply check; exit 1 when any fails."""
    actor = identity.resolve_actor(as_)
    _cfg, _entry, call = _hive_call(
        hive,
        lambda cfg, entry, directory: intake_services.triage_check(
            cfg,
            entry,
            directory,
            actor,
            pending=pending,
            branch=str(config.integration_branch(cfg, entry)),
            hive=hive,
        ),
    )

    def items(checks) -> dict:
        return {item.report: item for item in checks.outcome.items}

    def payload(checks) -> dict:
        plans = items(checks)
        return {
            "pending": pending,
            "checks": [proposal_check_row(c, plans.get(c.report)) for c in checks.checks],
            "failed": list(checks.failed),
        }

    def lines(checks) -> list[str]:
        out = [f"triage check: {len(checks.checks)} open proposal(s), {len(checks.failed)} failing"]
        plans = items(checks)
        for check in checks.checks:
            fixed = (
                f" (fix version {check.computed_fixed_in or 'unreleased'})"
                if check.computed_fixed_in is not None
                else ""
            )
            if check.passed:
                item = plans.get(check.report)
                gap = f"; {item.detail}" if item is not None and item.detail else ""
                related = f"; relates to {', '.join(check.relates)}" if check.relates else ""
                out.append(f"  ✓ {check.report}: passed{fixed}{related}{gap}")
                continue
            out.append(f"✗ {check.report}: {len(check.findings)} finding(s){fixed}")
            for finding in check.findings:
                out.append(f"    {finding.kind.value}: {finding.detail} (fix: {finding.fix})")
        return out

    failed = bool(call.value is not None and call.value.failed)
    _result("triage check", hive, as_json, call, payload, lines, failed)


def _reroute_map(pairs) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in pairs or ():
        report, sep, hive = str(pair).partition("=")
        if not sep or not report.strip() or not hive.strip():
            typer.echo(f"✗ --reroute-to takes <report>=<hive>, not {pair!r}", err=True)
            raise typer.Exit(2)
        out[report.strip()] = hive.strip()
    return out


def apply_item_row(item, destinations=None) -> dict:
    """One report in an apply run (applied, planned, blocked, listed or failed)."""
    row = {
        "report": item.report,
        "disposition": item.disposition.value,
        "status": item.status.value,
        "steps": [step.value for step in item.steps],
        "work": item.work,
        "share_with": item.share_with,
        "fixed_in": item.fixed_in,
        "drafted": list(item.drafted),
        "findings": [_finding_row(f) for f in item.findings],
        "detail": item.detail,
        "relates": list(item.relates),
    }
    if "reroute" in row["steps"]:
        row["reroute_to"] = (destinations or {}).get(item.report, "")
    return row


def apply_row(outcome, destinations=None) -> dict:
    return {
        "dry_run": outcome.dry_run,
        "items": [apply_item_row(item, destinations) for item in outcome.items],
        "applied": list(outcome.applied),
        "blocked": list(outcome.blocked),
        "decisions": list(outcome.decisions),
        "failed": [i.report for i in outcome.items if i.status.value == "failed"],
        "drafted": list(outcome.drafted),
        "already_applied": list(outcome.already_applied),
        "checks": [proposal_check_row(c) for c in outcome.checks],
        "labels": None if outcome.labels is None else label_sync_row(outcome.labels),
        "labels_error": outcome.labels_error,
    }


def triage_apply(
    reviewed: bool = False,
    dry_run: bool = False,
    reroute_to=None,
    as_: str = "",
    hive: str = "",
    as_json: bool = False,
):
    """``bh work triage apply --reviewed [--dry-run]``: apply the reviewed proposals."""
    if not reviewed:
        typer.echo(
            "✗ pass --reviewed (only proposals a review stands behind are ever applied)", err=True
        )
        raise typer.Exit(2)
    destinations = _reroute_map(reroute_to)
    actor = identity.resolve_actor(as_)

    def run(cfg, entry, directory):
        from . import report as reporting  # the target hive's filing (the `bh report` path)

        for target in sorted(set(destinations.values())):
            registry.resolve_hive(cfg, target)  # an unknown hive exits here, before any write
        refile = intake_services.reroute_refiler(
            destinations, actor, file_report=reporting.file_report, cfg=cfg
        )
        return intake_services.triage_apply(
            cfg,
            entry,
            directory,
            actor,
            dry_run=dry_run,
            branch=str(config.integration_branch(cfg, entry)),
            refile=refile,
            hive=hive,
        )

    _cfg, _entry, call = _hive_call(hive, run)
    outcome = call.value

    def payload(value) -> dict:
        return apply_row(value, destinations)

    def lines(value) -> list[str]:
        mode = " (dry run: nothing written)" if value.dry_run else ""
        planned = sum(1 for item in value.items if item.status.value == "planned")
        out = [
            f"triage apply{mode}: {len(value.applied)} applied, {planned} planned, "
            f"{len(value.blocked)} blocked, {len(value.decisions)} decision(s)"
        ]
        if value.already_applied:
            out.append(f"  already applied: {', '.join(value.already_applied)}")
        for item in value.items:
            steps = " → ".join(step.value for step in item.steps)
            parts = [item.disposition.value]
            if steps:
                parts.append(steps)
            if item.work:
                parts.append(f"work {item.work}")
            if item.fixed_in is not None:
                parts.append(f"fix version {item.fixed_in or 'unreleased'}")
            if item.drafted:
                parts.append(f"drafted {', '.join(item.drafted)} (never sent)")
            if "reroute" in [s.value for s in item.steps]:
                target = destinations.get(item.report, "")
                parts.append(
                    f"re-filed into {target}"
                    if target
                    else "no --reroute-to destination: it would fail"
                )
            if item.detail:
                parts.append(item.detail)
            mark = "⚠" if item.status.value == "failed" else " "
            out.append(f"{mark} {item.status.value:8} {item.report}: {'; '.join(parts)}")
        if value.labels_error:
            out.append(f"⚠ {value.labels_error}")
        elif value.labels is not None and value.labels.drafted:
            out.append(f"  labels: drafted {', '.join(value.labels.drafted)} (never sent)")
        if value.drafted:
            out.append(
                "  → nothing is sent: an operator reviews, approves and sends with `bh work outbox`"
            )
        return out

    failed = bool(outcome is not None and any(i.status.value == "failed" for i in outcome.items))
    _result("triage apply", hive, as_json, call, payload, lines, failed)
