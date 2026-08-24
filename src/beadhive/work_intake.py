"""Hive-scoped intake command implementations behind the stable ``beadhive.work`` facade."""

from __future__ import annotations

import typer

from . import config, identity, otel, registry, triage


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
):
    otel.set_bead(bead)
    cfg = config.load()
    cwd = registry.hive_dir_for(cfg, hive)
    actor = identity.resolve_actor(as_)
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
