"""Shared type, seat, and molecule guards for the ``bh work`` facade.

This module owns the read-only predicates and guard rendering used by several lifecycle verbs.
``beadhive.work`` re-exports the historical names so callers and monkeypatch-based tests keep a
stable compatibility surface while the implementation remains independently testable.
"""

from __future__ import annotations

import typer

from . import config, log

DISP_PREFIX = "disp/"
DEV_PREFIX = "dev/"
DIRECTOR_PREFIX = "dir/"
LEGACY_SEAT_PREFIXES = {
    "coord/": ("dispatcher", DISP_PREFIX),
    "crew/": ("developer", DEV_PREFIX),
}
KNOWN_SEAT_PREFIXES = frozenset(
    {
        "super/",
        "dir/",
        "cust/",
        "ctrl/",
        "plan/",
        "analyst/",
        "disp/",
        "dev/",
        "rev/",
        "merge/",
        "warden/",
        "verify/",
        "release/",
        "contrib/",
        "ops/",
        "coord/",
        "crew/",
    }
)


def first(data, *keys):
    """Return the first present, truthy value among ``keys``."""
    return next((data[k] for k in keys if data.get(k)), None)


def is_epic(data) -> bool:
    return str((data or {}).get("issue_type") or "") == "epic"


def kind_of(data) -> str:
    return "epic" if is_epic(data) else "issue"


def seat_of(name: str) -> str:
    if name.startswith(DISP_PREFIX):
        return "dispatcher"
    if name.startswith(DEV_PREFIX):
        return "developer"
    for legacy, (seat, replacement) in LEGACY_SEAT_PREFIXES.items():
        if name.startswith(legacy):
            log.get_logger(__name__).warning(
                "legacy_seat_prefix_deprecated",
                deprecated=legacy,
                replacement=replacement,
                seat=seat,
                reason="seat prefixes renamed per roles/RBAC matrix (coord/->disp/, crew/->dev/)",
            )
            return seat
    return ""


def guard_seat(data, name, bead, *, verb):
    want = "dispatcher" if is_epic(data) else "developer"
    if seat_of(name) in ("", want):
        return
    kind = "epic" if is_epic(data) else "issue"
    pfx = DISP_PREFIX if want == "dispatcher" else DEV_PREFIX
    typer.echo(
        f"✗ {bead} is an {kind} — it may only be {verb} a {want} ({pfx}<name>), not {name!r}",
        err=True,
    )
    raise typer.Exit(1)


def is_orchestrator(name: str) -> bool:
    if name.startswith(DISP_PREFIX) or name.startswith(DIRECTOR_PREFIX):
        return True
    return any(
        name.startswith(prefix) and seat == "dispatcher"
        for prefix, (seat, _) in LEGACY_SEAT_PREFIXES.items()
    )


def names_a_seat(name: str) -> bool:
    return any(name.startswith(prefix) for prefix in KNOWN_SEAT_PREFIXES)


def guard_orchestrator(actor, bead):
    if is_orchestrator(actor) or not names_a_seat(actor):
        return
    typer.echo(
        f"✗ {bead}: `{config.BINARY_ALIAS} work assign` is orchestrator-only — "
        "only a dispatcher (disp/<name>) or "
        f"director (dir/<name>) may assign work, not {actor!r}.",
        err=True,
    )
    raise typer.Exit(1)


def epic_of(data, bead) -> str:
    if is_epic(data):
        return bead
    parent = str((data or {}).get("parent") or "").strip()
    if parent:
        return parent
    stem, sep, _ = bead.rpartition(".")
    return stem if sep else ""


def guard_conventions(cfg, data, bead, main, *, action):
    from . import plan

    epic = epic_of(data, bead)
    if epic:
        plan.enforce_epic_conventions(epic, cfg, main, action=action)


def print_brief(cfg, entry, bead, data):
    if not data:
        typer.echo(f"✗ no such bead: {bead}", err=True)
        raise typer.Exit(1)
    typer.echo(f"# {data.get('id', bead)}  {data.get('title', '')}")
    description = first(data, "description")
    if description:
        typer.echo(f"\n## Requirements / goals\n{description}")
    acceptance = first(data, "acceptance_criteria", "acceptance")
    if acceptance:
        typer.echo(f"\n## Acceptance\n{acceptance}")
    design = first(data, "design")
    if design:
        typer.echo(f"\n## Design\n{design}")
    typer.echo(f"\n## Validate with\n{config.validate_cmd(cfg, entry)}")
