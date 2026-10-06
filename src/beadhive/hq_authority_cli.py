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
        ..., help="install, bind, bind-beadyard, grant, observe, renew, status, or check"
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
) -> None:
    try:
        if hq_operator_settings.configured() and action in {"install", "bind"}:
            raise hq_control_plane.ControlPlaneError(
                f"{hq_operator_settings.ENV} applies to renew, grant, observe, bind-beadyard, "
                "status and check"
            )
        plane = hq_operator_settings.select_plane()
        if action == "status":
            # Per-host, per-process: BH_HQ_AUTHORITY_ENFORCE=false (UNSUPPORTED, bh-6pqul).
            result = {
                **hq_authority_expiry.authority_status(plane),
                "enforcement": hq_authority_enforce.status(),
            }
        elif action == "check":
            try:
                floor = hq_authority_expiry.min_remaining()
            except ValueError as exc:
                raise hq_control_plane.ControlPlaneError(str(exc)) from None
            healthy, result, message = hq_authority_expiry.check(plane, min_remaining=floor)
            typer.echo(message, err=not healthy)
            typer.echo(json.dumps(result, sort_keys=True))
            if not healthy:
                raise SystemExit(1)
            return
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
                    # Precedence: operator-settings hq.sql.authority_max_duration_s, then
                    # $BH_HQ_AUTHORITY_MAX_DURATION, then the 7 d default (bh-od8ve).
                    ceiling = hq_authority_ceiling.resolve_ceiling(
                        settings=getattr(plane, "authority_max_duration_s", None)
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
