"""``bh hive forward ACTION HIVE_ID`` — the HIDDEN operator verb for the option A forward path.

bh-g7dlo (P-M10), ADR ``docs/design/hive-writer-partitioning-adr.md`` §3 and condition 16.
Documented in ``docs/FORWARD-WRITE-PATH.md``; outside ``bh --help``. Both sides are opt-in per
frame: the primary's actions need ``host.forward.serve.enabled``, the forwarder's need
``host.forward.enabled``.

Primary side (run on the frame whose hive server takes forwarders):

* ``provision --account '<principal>'@'<frame address>'`` — create or regrant one forwarder
  account in the table-scoped shape (password from ``--password-stdin`` or
  ``BH_FORWARD_NEW_PASSWORD`` for a new account), then verify it;
* ``revoke --account …`` — kill its sessions and drop it;
* ``check`` — the globals watchdog list, the read-only ``DOLT_ROOT_PATH`` and grant
  conformance for every account (exit 1 on any finding);
* ``quiesce`` — kill every forwarder session now.

Forwarder side: ``point`` (resolve the placed primary, preflight, record), ``stop``,
``status``.

The procedure is :mod:`beadhive.hive_forward`; this module resolves the hive, this host and
the config, renders, and maps refusals to exit 1.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import typer

from . import hive_forward

__all__ = ["ACTIONS", "run"]

ACTIONS = ("provision", "revoke", "check", "quiesce", "point", "stop", "status")
_PRIMARY = ("provision", "revoke", "check", "quiesce")
NEW_PASSWORD_ENV = "BH_FORWARD_NEW_PASSWORD"


def _fail(message: str) -> None:
    typer.echo(f"✗ {message}", err=True)
    raise typer.Exit(1)


def _emit(payload: dict) -> None:
    typer.echo(json.dumps(payload, indent=2, sort_keys=True, default=str))


def _forward_cfg(cfg) -> dict:
    from .modules.config.contracts import HostForwardConfig

    raw = ((cfg or {}).get("host") or {}).get("forward") or {}
    try:
        return HostForwardConfig.model_validate(dict(raw)).model_dump()
    except ValueError as exc:
        _fail(f"host.forward is invalid: {exc}")
        raise  # unreachable


def _server_node(prefix: str, hive_dir: Path):
    """The hive's server-mode engine (bd's own login on this frame's hive server): the
    :class:`beadhive.hive_forward.Sql` the primary-side actions run through."""
    from . import fence_data

    node = fence_data.node_for(hive_dir)
    if node is None or not isinstance(node.engine, fence_data.BdServerEngine):
        _fail(
            f"{prefix}: no server-mode hive store on this host at {hive_dir} — the primary side "
            "runs on the frame whose hive dolt sql-server takes forwarders"
        )
    return node.engine


def _database(node) -> str:
    rows = node.query("SELECT database() AS d")
    name = str(next(iter(rows[0].values()))) if rows else ""
    if not name:
        _fail("cannot read the hive server's database name")
    return name


def _password(password_stdin: bool) -> str | None:
    if password_stdin:
        return sys.stdin.readline().rstrip("\n") or None
    return os.environ.get(NEW_PASSWORD_ENV) or None


def _placement(cfg, entry) -> tuple[str, int] | None:
    from . import guard

    state = guard.primary_state(cfg=cfg, entry=entry)
    if state is None:
        return None
    lease = state[2]
    return str(lease.host_id), int(lease.epoch)


def _self_frame() -> str:
    from . import host

    try:
        return host.host_id()
    except FileNotFoundError:
        _fail("this host has no frame identity (run bh host init)")
        raise  # unreachable


def run(
    action: str,
    *,
    cfg,
    entry: dict,
    hive_dir: Path,
    account: str = "",
    password_stdin: bool = False,
    as_json: bool = False,
    hq_dir: str = "",
) -> None:
    prefix = str(entry.get("prefix", ""))
    forward = _forward_cfg(cfg)
    serve = forward["serve"]
    if action in _PRIMARY and not serve["enabled"]:
        _fail(
            "this frame does not serve forwarders: set host.forward.serve.enabled: true "
            "(opt-in per frame)"
        )
    if action in ("provision", "revoke"):
        if not account:
            _fail(f"{action} needs --account '<principal>'@'<frame address>'")
        try:
            target = hive_forward.Account.parse(account)
        except ValueError as exc:
            _fail(str(exc))
            return
        node = _server_node(prefix, hive_dir)
        try:
            if action == "revoke":
                dropped = hive_forward.revoke(node, account=target, operators=serve["operators"])
                if as_json:
                    _emit({"account": str(target), "dropped": dropped})
                else:
                    verb = "dropped" if dropped else "did not exist"
                    typer.echo(f"✓ {prefix}: forwarder {target} {verb}")
                return
            report = hive_forward.provision(
                node,
                database=_database(node),
                account=target,
                password=_password(password_stdin),
                operators=serve["operators"],
                require_tls=serve["require_tls"],
            )
        except hive_forward.GrantShapeViolation as exc:
            _fail("\n  ".join([str(exc), *exc.problems]))
            return
        except hive_forward.ForwardError as exc:
            _fail(str(exc))
            return
        if as_json:
            _emit(report.as_dict())
            return
        verb = "created" if report.created else "regranted"
        typer.echo(
            f"✓ {prefix}: forwarder {report.account} {verb} — table-scoped on "
            f"{len(report.tables)} tables, conformant"
        )
        for line in (*report.revoked, *report.granted):
            typer.echo(f"  {line}")
        return
    if action == "check":
        node = _server_node(prefix, hive_dir)
        payload = hive_forward.primary_report(node, database=_database(node), settings=serve)
        if as_json:
            _emit(payload)
        else:
            for line in render_primary(prefix, payload):
                typer.echo(line)
        if payload["findings"]:
            raise typer.Exit(1)
        return
    if action == "quiesce":
        node = _server_node(prefix, hive_dir)
        login = node.login()
        killed = hive_forward.quiesce(node, operators=[*serve["operators"], login])
        if as_json:
            _emit({"killed": [str(s) for s in killed]})
        else:
            typer.echo(f"✓ {prefix}: killed {len(killed)} forwarder session(s)")
        return
    if action == "status":
        marker = hive_forward.read_marker(hive_dir)
        payload = marker.as_dict() if marker else {"prefix": prefix, "state": "not forwarded"}
        if as_json:
            _emit(payload)
        else:
            typer.echo(render_marker(prefix, marker))
        if marker is not None and marker.state != "forwarding":
            raise typer.Exit(1)
        return
    if action == "stop":
        stopped = hive_forward.stop(hive_dir)
        typer.echo(f"✓ {prefix}: " + ("forwarding stopped" if stopped else "was not forwarded"))
        return
    # point
    if not forward["enabled"]:
        _fail("this frame does not forward: set host.forward.enabled: true (opt-in per frame)")
    endpoints = {k: dict(v) for k, v in forward["endpoints"].items()}
    marker = hive_forward.repoint(
        hive_dir,
        _placement(cfg, entry),
        prefix=prefix,
        self_frame=_self_frame(),
        endpoints=endpoints,
        hq_dir=hq_dir,
    )
    if as_json:
        _emit(marker.as_dict() if marker else {"prefix": prefix, "state": "primary"})
    else:
        typer.echo(render_marker(prefix, marker, primary_when_none=True))
    if marker is not None and marker.state != "forwarding":
        raise typer.Exit(1)


def render_marker(prefix: str, marker, *, primary_when_none: bool = False) -> str:
    if marker is None:
        if primary_when_none:
            return f"✓ {prefix}: this frame is the placed primary — it writes main directly"
        return f"• {prefix}: not forwarded"
    if marker.state == "forwarding":
        ep = marker.endpoint
        return (
            f"✓ {prefix}: forwarding to {marker.frame}@{marker.epoch} "
            f"({ep.get('host')}:{ep.get('port')} as {ep.get('user')}, "
            f"tls {ep.get('tls_mode', 'required')})"
        )
    return f"✗ {prefix}: forwarding REFUSED (fails closed) — {marker.reason}"


def render_primary(prefix: str, payload: dict) -> list[str]:
    lines = [f"forward primary {prefix} (database {payload['database']})"]
    for g in payload["globals"]:
        shown = "<unreadable>" if g["actual"] is None else g["actual"]
        lines.append(
            f"  {g['status'].upper():<12} @@GLOBAL.{g['name']} expected={g['expected']} "
            f"actual={shown}"
        )
    root = payload["root"]
    if root["path"]:
        state = "read-only" if not root["problems"] else "NOT read-only"
        lines.append(f"  DOLT_ROOT_PATH {root['path']}: {state}")
    conf = payload["conformance"]
    lines.append(
        "  forwarder grants: conformant"
        if not conf
        else f"  forwarder grants: {len(conf)} finding(s)"
    )
    lines += [f"  ✗ {f}" for f in payload["findings"]]
    return lines
