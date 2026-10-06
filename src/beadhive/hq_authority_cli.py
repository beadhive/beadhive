"""Explicit operator provisioning, durable observation and expiry renewal."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from typer._click import types as _click_types

from . import (
    config,
    host_heartbeat,
    hq_authority_ceiling,
    hq_authority_enforce,
    hq_authority_expiry,
    hq_control_plane,
    hq_operator_settings,
    hq_sql_inbox_retention,
)


class IntDurationSeconds(_click_types.IntParamType):
    """Integer seconds on the wire; also accepts `36h` / `7d` and converts to whole seconds."""

    name = "integer"

    def convert(self, value, param, ctx):
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        try:
            return int(hq_authority_ceiling.parse_duration(value))
        except ValueError as exc:
            self.fail(str(exc), param, ctx)


def authority_cmd(
    action: str = typer.Argument(
        ...,
        help="install, bind, bind-beadyard, grant, observe, renew, status, check, or prune-inbox",
    ),
    record: Annotated[Path | None, typer.Option("--record")] = None,
    frame: str = typer.Option("", "--frame"),
    public_key: Annotated[Path | None, typer.Option("--public-key")] = None,
    generation: str = typer.Option("", "--generation"),
    expected: str = typer.Option("", "--expected-revision"),
    operator_key: Annotated[Path | None, typer.Option("--operator-key")] = None,
    interpreter: Annotated[Path | None, typer.Option("--interpreter")] = None,
    confirm_server_custody: bool = typer.Option(False, "--confirm-server-custody"),
    server_root: Annotated[Path | None, typer.Option("--server-root")] = None,
    socket_path: Annotated[Path | None, typer.Option("--socket-path")] = None,
    policy_digest: str = typer.Option("", "--policy-digest"),
    role: str = typer.Option("frame", "--role"),
    holder_id: str = typer.Option("", "--holder-id"),
    duration: Annotated[
        int,
        typer.Option(
            "--duration",
            click_type=IntDurationSeconds(),
            help="seconds or e.g. 7d / 36h",
        ),
    ] = 3600,
    client_interpreter: Annotated[Path | None, typer.Option("--client-interpreter")] = None,
    confirm: bool = typer.Option(False, "--confirm"),
    operator_settings: Annotated[
        str | None,
        typer.Option(
            "--operator-settings",
            help="operator settings file (JSON/YAML); overrides $BH_HQ_OPERATOR_SETTINGS",
        ),
    ] = None,
    max_duration: Annotated[
        str | None,
        typer.Option(
            "--max-duration",
            help="renew: authority duration ceiling, seconds or e.g. 7d; overrides "
            "operator settings and $BH_HQ_AUTHORITY_MAX_DURATION",
        ),
    ] = None,
    min_remaining: Annotated[
        str | None,
        typer.Option(
            "--min-remaining",
            help="check: fail when less than this remains, e.g. 6h; overrides "
            "$BH_HQ_AUTHORITY_MIN_REMAINING",
        ),
    ] = None,
) -> None:
    try:
        if (operator_settings or hq_operator_settings.configured()) and action in {
            "install",
            "bind",
        }:
            raise hq_control_plane.ControlPlaneError(
                f"{hq_operator_settings.ENV} / --operator-settings applies to renew, grant, "
                "observe, bind-beadyard, status, check and prune-inbox"
            )
        plane = hq_operator_settings.select_plane(operator_settings)
        if action == "status":
            # Per-host, per-process: BH_HQ_AUTHORITY_ENFORCE=false (UNSUPPORTED, bh-6pqul).
            result = {
                **hq_authority_expiry.authority_status(plane),
                "enforcement": hq_authority_enforce.status(),
            }
        elif action == "check":
            try:
                floor = (
                    hq_authority_expiry.parse_duration(min_remaining, name="--min-remaining")
                    if min_remaining
                    else hq_authority_expiry.min_remaining()
                )
            except ValueError as exc:
                raise hq_control_plane.ControlPlaneError(str(exc)) from None
            healthy, result, message = hq_authority_expiry.check(plane, min_remaining=floor)
            typer.echo(message, err=not healthy)
            typer.echo(json.dumps(result, sort_keys=True))
            if not healthy:
                raise SystemExit(1)
            return
        elif action == "prune-inbox":
            # Signed-mode inbox retention (bh-ce886): a dry run unless --confirm. The margin
            # comes from hq.sql.inbox_retention_s > $BH_HQ_INBOX_RETENTION > 1 h; a CLI flag
            # would change the published hq.authority operation, so there is none yet.
            if not frame:
                raise hq_control_plane.ControlPlaneError("prune-inbox requires --frame")
            if not hasattr(plane, "prune_inbox"):
                raise hq_control_plane.ControlPlaneError("prune-inbox requires a SQL HQ")
            try:
                retention = hq_sql_inbox_retention.resolve_retention(
                    settings=getattr(plane, "inbox_retention_s", None)
                )
            except ValueError as exc:
                raise hq_control_plane.ControlPlaneError(str(exc)) from None
            result = {
                **plane.prune_inbox(frame, retention_s=retention.seconds, dry_run=not confirm),
                "retention_source": retention.source,
            }
        else:
            if not confirm:
                raise hq_control_plane.ControlPlaneError("operator mutation requires --confirm")
            if action == "install":
                if public_key is None:
                    raise hq_control_plane.ControlPlaneError(
                        "install requires operator --public-key"
                    )
                digest = hq_control_plane.install_guard(
                    config.hq_dir(),
                    public_key.read_text(),
                    generation,
                    interpreter=str(interpreter) if interpreter else None,
                    confirm_server_custody=confirm_server_custody,
                )
                result = {"policy_digest": digest}
            elif action == "bind":
                if server_root is None or socket_path is None or not policy_digest:
                    raise hq_control_plane.ControlPlaneError(
                        "bind requires server root, socket and trusted policy digest"
                    )
                anchor = hq_control_plane.bind_broker(
                    config.hq_dir(),
                    server_root,
                    socket_path,
                    policy_digest,
                    client_interpreter=str(client_interpreter) if client_interpreter else None,
                    role=role,
                )
                result = {"authority_anchor": str(anchor), "role": role}
            else:
                if operator_key is None:
                    raise hq_control_plane.ControlPlaneError(
                        "mutation requires separate --operator-key"
                    )
                if action == "grant":
                    if record is None:
                        raise hq_control_plane.ControlPlaneError("grant requires operator --record")
                    data = json.loads(record.read_text())
                    if set(data) != {"authority", "public_key", "desired"}:
                        raise hq_control_plane.ControlPlaneError("unexpected grant fields")
                    sha = plane.grant(
                        host_heartbeat.ObservationAuthority(**data["authority"]),
                        data["public_key"],
                        data["desired"],
                        expected=expected,
                        operator_key=str(operator_key),
                    )
                elif action == "observe":
                    sha = plane.accept_observation(
                        frame,
                        expected=expected,
                        operator_key=str(operator_key),
                        holder_identity=holder_id,
                    )
                elif action == "renew":
                    # Precedence: --max-duration, operator-settings
                    # hq.sql.authority_max_duration_s, $BH_HQ_AUTHORITY_MAX_DURATION, then the
                    # 7 d default (bh-od8ve). Invalid values are refused, never clamped.
                    ceiling = hq_authority_ceiling.resolve_ceiling(
                        cli=max_duration, settings=getattr(plane, "authority_max_duration_s", None)
                    )
                    sha = plane.renew(
                        expected=expected,
                        operator_key=str(operator_key),
                        duration=duration,
                        ceiling=ceiling,
                    )
                elif action == "bind-beadyard":
                    sha = plane.bind_beadyard(expected=expected, operator_key=str(operator_key))
                else:
                    raise hq_control_plane.ControlPlaneError("unknown authority action")
                result = {"revision": sha}
        typer.echo(json.dumps(result, sort_keys=True))
    except (ValueError, OSError, RuntimeError) as exc:
        typer.echo(f"authority refused: {exc}", err=True)
        raise typer.Exit(1) from exc
