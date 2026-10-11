"""``bh work close``: close a bead, optionally waiting for its state to reach the remote.

bh-midl0, trust-profiles ADR section 5. A close is a local bead write; with ``hive.sync``
auto-push on, the host daemon publishes it on its next flush. Three ways the state leaves:

* ``close`` listed in ``hive.sync.durable_verbs``: push synchronously, as ``submit`` does.
* ``--await-sync``: ask the daemon's sync loop for a flush now and wait (up to
  ``--await-timeout``) for the push that includes this close — a successful push that STARTED
  after the close committed. A hive without auto-push has no flush to wait for, so the close
  pushes synchronously instead.
* otherwise: nothing more; the next flush (or durable verb) carries it.

A failed or timed-out wait exits 1 with the last recorded error; the close itself stands.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import typer

from . import bd, bd_cli, hive_sync_settings, hive_sync_state, identity, otel, registry
from .config_consumer_ports import work_settings as config

DEFAULT_AWAIT_TIMEOUT_S = 600


def _push(cfg, hive_dir, actor: str, message: str) -> tuple[bool, str]:
    from . import engine

    res = engine.get_engine(cfg).push_state(hive_dir, actor=actor, message=message)
    return res.returncode == 0, bd.err_line(res)


def close(
    bead: str,
    reason: str = "",
    await_sync: bool = False,
    await_timeout: int = DEFAULT_AWAIT_TIMEOUT_S,
    as_: str = "",
    hive: str = "",
    *,
    push: Callable[..., tuple[bool, str]] = _push,
    wait: Callable[[float], Any] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    now: Callable[[], float] = time.time,
    state_path=None,
):
    otel.set_bead(bead)
    cfg = config.load()
    hive_dir = registry.hive_dir_for(cfg, hive)
    entry = registry.entry_for_dir(cfg, hive_dir) or {}
    prefix = str(entry.get("prefix") or hive or "")
    actor = identity.resolve_actor(as_)
    res = bd_cli.routes(hive_dir).issue_close(
        bead, reason=reason or "closed", actor=actor, capture=True
    )
    if res.returncode:
        typer.echo(f"✗ bd close failed: {bd.err_line(res)}", err=True)
        raise typer.Exit(res.returncode)
    typer.echo(f"✓ closed {bead}")
    settings = hive_sync_settings.for_entry(entry, cfg)
    if hive_sync_settings.durable("close", settings) or (await_sync and not settings.push_enabled):
        why = "durable verb" if hive_sync_settings.durable("close", settings) else "no auto-push"
        ok, error = push(cfg, hive_dir, actor, f"close {bead}")
        if not ok:
            typer.echo(f"⚠ state push failed ({why}): {error}", err=True)
            if await_sync:
                raise typer.Exit(1)
            return
        typer.echo(f"✓ state pushed ({why})")
        return
    if not await_sync:
        return
    requested = hive_sync_state.request_flush(prefix, now=now, path=state_path)
    typer.echo(f"… waiting up to {await_timeout}s for the hive.sync flush of {prefix}")
    outcome = hive_sync_state.await_flush(
        prefix,
        requested,
        timeout_s=float(await_timeout),
        wait=wait,
        clock=clock,
        path=state_path,
    )
    if outcome is None:
        record = hive_sync_state.recorded(prefix, path=state_path)
        skipped = record.push is not None and record.push.status == hive_sync_state.Status.SKIPPED
        if record.last_error:
            detail = f" (last error: {record.last_error})"
        elif skipped:
            detail = f" (the daemon does not push this hive here: {record.push.error})"
        else:
            detail = ""
        typer.echo(
            f"✗ no hive.sync flush of {prefix} within {await_timeout}s{detail}; {bead} is closed "
            "locally and the next flush publishes it (is the host daemon running?)",
            err=True,
        )
        raise typer.Exit(1)
    target = outcome.target or "the remote"
    typer.echo(f"✓ synced: {bead}'s close is on {target} ({outcome.status})")
