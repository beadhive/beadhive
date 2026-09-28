"""Top-level adapter selecting the API-first ``approve`` / ``bounce`` handlers (bh-bwnys.1).

The one composition seam for this cohort: it resolves the shell-owned inputs (config, hive,
actor, operator policy), opens a verified Beads v1.3 session for the hive, supplies the
``beadhive_core`` ports over the named ``bd`` CLI routes (``beadhive-bd-cli``'s
``CliGateOperations`` / ``CliStateOperations``, resolved lazily by name through
:mod:`beadhive.bd_cli`, bh-o3xuf), and renders the
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

from . import bd_cli, host_beads, identity, log, otel, worktree
from .config_consumer_ports import work_settings as config
from .work_logic import opt_str

_CORE_MODULE = "beadhive_core"


def _core() -> Any:
    return importlib.import_module(_CORE_MODULE)


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
        bd_cli.gate_operations(main),
        bd_cli.state_operations(main),
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
