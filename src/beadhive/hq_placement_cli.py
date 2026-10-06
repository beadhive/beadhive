"""``bh hq placement`` — the HIDDEN operator/director placement verb (bh-16347.5).

Documented only in ``docs/design/hq-placement-runbook.md``, like ``bh hive fence``: it is
outside ``bh --help`` and the CLI reference while 0.23.0 is dormant. Every action goes through
:mod:`beadhive.hq_placement_ops` over the bh-a94qw library, bound by the director credential
(``hq.sql.placement_writer``) in the operator settings file named by ``--operator-settings`` or
``$BH_HQ_OPERATOR_SETTINGS``. Mutating actions are dry runs unless ``--confirm``.
"""

from __future__ import annotations

import json
import os
from typing import Annotated

import typer

from . import hq_authority_cli, hq_operator_settings, hq_placement_ops
from .hq_sql_placement import DEFAULT_TENURE_S

__all__ = ["placement_cmd"]


def placement_cmd(
    action: str = typer.Argument(..., help="show, seed, place, release, or check"),
    prefix: str = typer.Argument(
        "", metavar="[PREFIX]", help="the hive prefix (every action except check)"
    ),
    frame: str = typer.Option("", "--frame", help="place: the frame to place on the hive"),
    expected: str = typer.Option(
        "", "--expected-revision", help="place/release: the row's current revision (the CAS)"
    ),
    epoch: int | None = typer.Option(
        None,
        "--epoch",
        help="seed: the hive's current refs/bh/epoch (1 when never fenced); place: the placed "
        "epoch (default: row + 1; must increase)",
    ),
    tenure: Annotated[
        int,
        typer.Option(
            "--tenure",
            click_type=hq_authority_cli.IntDurationSeconds(),
            help="place: lease tenure, seconds or e.g. 12h; at most 24h (expiry is only a hint)",
        ),
    ] = int(DEFAULT_TENURE_S),
    confirm: bool = typer.Option(
        False, "--confirm", help="place/release: write (default: dry run)"
    ),
    operator_settings: Annotated[
        str | None,
        typer.Option(
            "--operator-settings",
            help="operator settings file (JSON/YAML) carrying hq.sql.placement_writer; "
            "overrides $BH_HQ_OPERATOR_SETTINGS",
        ),
    ] = None,
) -> None:
    """HIDDEN — see docs/design/hq-placement-runbook.md.

    show PREFIX: read the hive's placement row.

    seed PREFIX --epoch N: render the one-time provisioning INSERT for a never-placed hive.

    place PREFIX --frame F --expected-revision R [--epoch N] [--confirm]: placement CAS.

    release PREFIX --expected-revision R [--confirm]: release to a tombstone at the same epoch.

    check: grant and trigger conformance for the director and every frame account."""
    try:
        if action not in hq_placement_ops.ACTIONS:
            raise ValueError(
                f"unknown placement action {action!r} "
                f"(expected one of {', '.join(hq_placement_ops.ACTIONS)})"
            )
        if action != "check" and not prefix:
            raise ValueError(f"{action} requires a hive PREFIX")
        if action == "check" and prefix:
            raise ValueError("check takes no PREFIX: it covers every account")
        if frame and action != "place":
            raise ValueError("--frame applies to place only")
        if confirm and action not in ("place", "release"):
            raise ValueError("--confirm applies to place and release only")
        if epoch is not None and action not in ("seed", "place"):
            raise ValueError("--epoch applies to seed and place only")
        path = operator_settings or os.environ.get(hq_operator_settings.ENV) or None
        if action == "seed":
            director = hq_operator_settings.placement_director(path) if path else None
            result = hq_placement_ops.seed(prefix, epoch=epoch, director=director)
        elif action == "check":
            if not path:
                raise ValueError(
                    f"check needs the operator settings file ({hq_operator_settings.ENV} "
                    "or --operator-settings)"
                )
            settings = hq_operator_settings.load_placement_settings(path)
            result = hq_placement_ops.check(settings)
            typer.echo(json.dumps(result, sort_keys=True))
            if not result["conformant"]:
                raise SystemExit(1)
            return
        else:
            director = hq_operator_settings.placement_director(path)
            if action == "show":
                result = hq_placement_ops.show(director, prefix)
            elif action == "place":
                result = hq_placement_ops.place(
                    director,
                    prefix,
                    frame=frame,
                    expected=expected,
                    epoch=epoch,
                    tenure_s=float(tenure),
                    confirm=confirm,
                )
            else:
                result = hq_placement_ops.release(
                    director, prefix, expected=expected, confirm=confirm
                )
        typer.echo(json.dumps(result, sort_keys=True))
    except (ValueError, OSError, RuntimeError) as exc:
        typer.echo(f"placement refused: {exc}", err=True)
        raise typer.Exit(1) from exc
