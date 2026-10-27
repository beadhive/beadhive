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
