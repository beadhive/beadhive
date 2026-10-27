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
