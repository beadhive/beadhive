"""Top-level adapter selecting the API-first ``approve`` / ``bounce`` handlers (bh-bwnys.1).

The one composition seam for this cohort: it resolves the shell-owned inputs (config, hive,
actor, operator policy), opens a verified Beads v1.3 session for the hive, supplies the
``beadhive_core`` ports over the existing named ``bd`` CLI compatibility routes, and renders the
core's operator notices. It never starts ``bd serve``: the session comes from the hive's one
supervised service (``bh host beads``, :mod:`beadhive.host_beads`). ``beadhive_core`` is
resolved lazily by name — ``src/beadhive`` never imports a workspace package statically
(``scripts/check_package_imports.py``).
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import typer

from . import bd, host_beads, identity, log, otel, worktree
from .config_consumer_ports import work_settings as config
from .work_logic import opt_str

_CORE_MODULE = "beadhive_core"


def _core() -> Any:
    return importlib.import_module(_CORE_MODULE)


class CliGateOperations:
    """``GateOperations`` over the named ``work.gate.lookup`` / ``work.gate.resolve`` routes.

    Lookup is the same ``bd gate list --limit 0 --all`` read the review-gate selector has always
    used (``--limit 0`` defeats bd's 50-row window, bh-pwi2), narrowed with the anchored
    ``bd.names_bead`` match (bh-1vvdp). Unlike that selector, a failed read raises instead of
    reading as "no gates", so approve and bounce fail closed on it.
    """

    def __init__(self, main: Path) -> None:
        self._main = main

    def gates_for(self, bead: str) -> list[Any]:
        core = _core()
        rows = bd.json(["gate", "list", "--limit", "0", "--all"], self._main)
        if not isinstance(rows, list):
            raise core.GateLookupFailed("`bd gate list` failed or returned no JSON list")
        return [
            core.Gate(
                id=str(row.get("id") or ""),
                status=str(row.get("status") or ""),
                description=str(row.get("description") or ""),
                reason=str(row.get("reason") or ""),
                await_type=str(row.get("await_type") or ""),
            )
            for row in rows
            if isinstance(row, dict) and bd.names_bead(row.get("description"), bead)
        ]

    def resolve(self, gate_id: str, *, reason: str, actor: str) -> None:
        result = bd.run(["gate", "resolve", gate_id, "--reason", reason], self._main, actor=actor)
        if result.returncode != 0:
            raise _core().GateResolveFailed(gate_id, result.returncode, bd.err_line(result))


class CliStateOperations:
    """``StateOperations`` over the named ``work.state.update`` route (``bd set-state``)."""

    def __init__(self, main: Path) -> None:
        self._main = main

    def set_state(self, bead: str, dimension: str, value: str, *, reason: str, actor: str) -> None:
        args = ["set-state", bead, f"{dimension}={value}", "--reason", reason]
        result = bd.run(args, self._main, actor=actor)
        if result.returncode != 0:
            raise _core().StateUpdateFailed(result.returncode, bd.err_line(result))


class TelemetryReviewObserver:
    """Map core review notifications onto the existing OTel counters and structured log."""

    def transition(self, name: str, attributes: Mapping[str, str]) -> None:
        otel.count_bead_transition(name, dict(attributes))

    def self_review_advised(self, *, bead: str, actor: str, author: str, policy: str) -> None:
        log.get_logger("beadhive.work").warning(
            "reviewer_cross_seat_self_review",
            bead=bead,
            actor=actor,
            author=author,
            policy=policy,
            reason="approver authored the bead (rubber-stamp risk); advise warns, hard "
            "(default) blocks",
        )


def hive_session(main: Path, entry: Any) -> Any:
    """An unopened session against the hive's one supervised Beads service (``bh host beads``).

    Never starts ``bd serve``: an absent or stale service raises the client's
    ``ServiceUnavailable`` (whose message names ``bh host beads start --hive <hive>``), which
    the core reports fail-closed; a hive that cannot be served at all is mapped likewise.
    """
    try:
        return host_beads.resolve_session(main, _core().REVIEW_CAPABILITIES, entry=entry)
    except host_beads.HiveNotServable as exc:
        raise _core().SessionUnavailable(str(exc)) from exc


#: The session seam: ``(main, entry)`` -> an unopened ``BeadsSession``. Tests substitute a
#: transport-fixture session here; production resolves the hive's supervised service.
session_factory: Callable[[Path, Any], Any] = hive_session


def review_commands(main: Path, cfg: Any, entry: Any) -> Any:
    core = _core()
    policy = core.ReviewPolicy(
        cli_name=config.BINARY_ALIAS,
        self_review=config.dispatch_reviewer_cross_seat(cfg, entry),
    )
    return core.ReviewCommands(
        lambda: session_factory(main, entry),
        CliGateOperations(main),
        CliStateOperations(main),
        observer=TelemetryReviewObserver(),
        policy=policy,
    )


def _render(notices: Any) -> None:
    for notice in notices:
        typer.echo(notice.text, err=notice.error)


def _dispatch(bead: str, as_: str, hive: str, command: Callable[[Any, str], Any]) -> None:
    otel.set_bead(bead)
    cfg = config.load()
    entry, main, _target, _branch = worktree.locate(cfg, hive, bead)
    actor = identity.resolve_actor(as_, config.work_identity(cfg, entry)["name"] or "")
    try:
        outcome = command(review_commands(main, cfg, entry), actor)
    except _core().ReviewFailed as failure:
        _render(failure.notices)
        raise typer.Exit(failure.exit_code) from failure
    _render(outcome.notices)


def approve(bead: str, as_: str, hive: str) -> None:
    _dispatch(bead, as_, hive, lambda commands, actor: commands.approve(bead, actor))


def bounce(bead: str, message: str, as_: str, hive: str) -> None:
    reason = opt_str(message).strip()
    _dispatch(bead, as_, hive, lambda commands, actor: commands.bounce(bead, actor, reason))
