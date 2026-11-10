"""Import-lazy command tree for the external-report verbs (bh-lwz8a).

``bh work outbox <verb>``, ``bh work thread <verb>`` and ``bh work labels <verb>`` run once per
operator step, and a single ``outbox send`` used to spend about 2.5 s importing the whole command
tree (doctor, host, backfill, plan, ...) before any work. For those leaves the entry point mounts
this tree instead: the same root callback and process boundary (:mod:`beadhive.cli_root`) and the
same catalog-projected leaves (:mod:`beadhive.work_intake_cli`, the handlers ``bh work`` itself
projects), and nothing else. Help, shell completion and every other verb, including these groups'
own ``--help``, keep going through :mod:`beadhive.cli`.
"""

from __future__ import annotations

from collections.abc import Sequence

import typer

#: The nested ``work`` groups this tree carries.
GROUPS = frozenset({"outbox", "thread", "labels"})


def selects(argv: Sequence[str]) -> bool:
    """Whether *argv* (without the program name) is cheaply recognisable as one of the groups'
    leaves: ``work <group> <verb> ...`` with no root option in front. The verb itself is checked
    against the catalog by :func:`app_for`."""
    return (
        len(argv) >= 3 and argv[0] == "work" and argv[1] in GROUPS and not argv[2].startswith("-")
    )


def _operations():
    from .kernel.operations import operations

    return tuple(
        spec
        for spec in operations()
        if (cli := spec.surfaces.get("cli")) is not None
        and len(parts := cli["path"].split()) == 3
        and parts[0] == "work"
        and parts[1] in GROUPS
    )


def leaf_paths() -> frozenset[str]:
    """Every ``work <group> <verb>`` path this tree serves, from the operation catalog."""
    return frozenset(spec.surfaces["cli"]["path"] for spec in _operations())


def build_app() -> typer.Typer:
    """The root Typer app holding only ``work outbox``, ``work thread`` and ``work labels``."""
    from . import cli_root, work_intake_cli
    from .cli_projection import project_cli_group

    app = typer.Typer(no_args_is_help=True, help="Workspace CLI.")
    app.callback()(cli_root._root)
    work_app = typer.Typer(
        no_args_is_help=True, help="Drive a bead assigned→merged (integration plane)."
    )
    project_cli_group(
        work_app,
        "work",
        work_intake_cli.CLI_HANDLERS,
        operation_specs=_operations(),
        group_help=work_intake_cli.GROUP_HELP,
    )
    app.add_typer(work_app, name="work")
    return app


def app_for(argv: Sequence[str]) -> typer.Typer | None:
    """The fast-path app when *argv* selects one of its leaves, else ``None`` (full tree)."""
    if not selects(argv) or " ".join(argv[:3]) not in leaf_paths():
        return None
    return build_app()
