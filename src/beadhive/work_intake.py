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
    render_disposition(*triage.accept(cwd, bead, actor, issue_type=issue_type, priority=priority))


def reject(bead: str, reason: str, as_: str = "", hive: str = ""):
    otel.set_bead(bead)
    cfg = config.load()
    cwd = registry.hive_dir_for(cfg, hive)
    actor = identity.resolve_actor(as_)
    render_disposition(*triage.reject(cwd, bead, actor, reason=reason))


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
    render_disposition(*triage.promote(cwd, bead, actor))


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
        typer.echo(json.dumps(payload, sort_keys=True))
        if result.code:
            raise typer.Exit(result.code)
        return
    if result.code:
        code = f" ({result.reason})" if result.reason else ""
        typer.echo(f"✗ link refused{code}: {result.error}", err=True)
        raise typer.Exit(result.code)
    if disposition is not None:
        made = "created and tracks" if disposition.created_work else "tracks"
        typer.echo(f"✓ accepted {report}: it {made} {disposition.work} (delivery:pending)")
        typer.echo(
            f"  → {report} stays held (it is a report, not work); {disposition.work} is work"
        )
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
    lines: list[str] = []
    for item in outcome.items:
        parts = []
        if item.delivery:
            parts.append(f"delivery:{item.delivery}")
        if item.drafted:
            parts.append(f"drafted record {item.drafted} (nothing is sent)")
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


def _outbox_payload(call, verb: str) -> dict:
    value = call.value
    payload: dict = {"ok": call.code == 0, "verb": verb, "reason": call.reason, "error": call.error}
    if value is None:
        return payload
    if hasattr(value, "renders"):  # a RenderedRecord
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


def render_outbox(call, verb: str, as_json: bool) -> None:
    """Print one ``bh work outbox <verb>`` result (human or ``--json``); non-zero on a refusal."""
    if as_json:
        typer.echo(json.dumps(_outbox_payload(call, verb), sort_keys=True))
        if call.code:
            raise typer.Exit(call.code)
        return
    value = call.value
    done = value if isinstance(value, tuple) else ((value,) if value is not None else ())
    if hasattr(value, "renders"):
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
