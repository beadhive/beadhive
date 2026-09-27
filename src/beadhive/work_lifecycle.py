"""Top-level adapter selecting the ``beadhive_core`` lifecycle handlers (bh-sy36q.1).

The one composition seam for ``bh work assign`` / ``claim`` / ``resume`` / ``abandon``, the
bead-state half of ``bh work submit`` (the claim-holder admission read and the ``review=pending``
transition), and the claim/release/provision steps `bh work next`'s CLI-compatibility loop
shares. It resolves the shell-owned inputs (config, hive, actor), supplies the core's ports over
the named routes, and renders the core's streamed output. The policy — guards, claim
verification, release-on-failed-provisioning, abandon's re-read — lives in
:mod:`beadhive_core.lifecycle`.

Route selection (same pre-execution rule as :mod:`beadhive.work_queue`, bh-l5sxi.2): the
api-ready ``work.issue.get`` / ``work.issue.update`` rows are served over the hive's one
supervised Beads service (``bh host beads``) when a verified session with
``LIFECYCLE_CAPABILITIES`` can be opened. When it cannot — no service running, an embedded-Dolt
hive that Beads 1.3 cannot serve, a missing capability — the route is selected as the ``bd``
compatibility read/assign BEFORE the first Beads operation of the command, never as a retry after
an API call failed. These verbs are the recovery path for stalled work (``abandon``) and the
first step of every developer loop (``claim``), so they must not fail closed on hives the API
cannot serve. The ``work.lease.*``, ``work.state.*`` and ``work.gate.*`` rows are
``cli-compatibility`` in the matrix and always take their named ``bd`` route here.

Worktree, identity, claim-record and state-sync effects are the supplied capabilities of
:class:`ShellWorkspace`; worktree mechanics go through ``worktree.ensure`` / ``worktree.remove``,
which bind the selected ``worktrees.manager`` (``beadhive-worktrees``, bh-055ot). They are
reached through the ``beadhive.work`` facade at call time so its historical patch seams stay
live. ``beadhive_core`` is resolved lazily by name — ``src/beadhive`` never imports a workspace
package statically (``scripts/check_package_imports.py``).
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any

import typer

from . import bd, host_beads, log, otel
from .config_consumer_ports import work_settings as config
from .work_review import CliGateOperations, CliStateOperations

_CORE_MODULE = "beadhive_core"


def _core() -> Any:
    return importlib.import_module(_CORE_MODULE)


def _work() -> Any:
    """The ``beadhive.work`` facade, resolved at call time (it imports this module)."""
    return importlib.import_module("beadhive.work")


# ---- named CLI-compatibility routes ----------------------------------------------------------


class CliIssues:
    """``work.issue.get`` / ``work.issue.update`` over ``bd`` — selected only when the hive's
    Beads service cannot be used for this command (see the module docstring)."""

    route = "cli-compatibility"

    def __init__(self, main: Path) -> None:
        self._main = main

    def get(self, bead: str) -> dict | None:
        return bd.show(bead, self._main)

    def assign(self, bead: str, assignee: str, *, actor: str, read: Any) -> None:
        result = bd.run(["assign", bead, assignee], self._main, actor=actor)
        if result.returncode != 0:  # bd streamed its own error
            raise _core().WriteFailed(result.returncode)


class CliLeases:
    """``work.lease.acquire`` (``bd update --claim``) / ``work.lease.release`` (reopen +
    unassign) — the renewable claim lease v1.3's ``issues.claim`` does not grant."""

    def __init__(self, main: Path) -> None:
        self._main = main

    def acquire(self, bead: str, *, actor: str) -> None:
        result = bd.run(["update", bead, "--claim"], self._main, actor=actor)
        if result.returncode != 0:
            raise _core().WriteFailed(result.returncode)

    def release(self, bead: str, *, actor: str) -> None:
        args = ["update", bead, "--status", "open", "--assignee", ""]
        result = bd.run(args, self._main, actor=actor)
        if result.returncode != 0:
            raise _core().WriteFailed(result.returncode)


class CliStateReads:
    """``work.state.get`` (``bd state``): '' when unset."""

    def __init__(self, main: Path) -> None:
        self._main = main

    def get_state(self, bead: str, dimension: str) -> str:
        return bd.state(bead, dimension, self._main)


# ---- route selection -------------------------------------------------------------------------


class TelemetryRoutingObserver:
    def selected(self, name: str, kind: str) -> None:
        log.get_logger("beadhive.work").info("route_selected", operation=name, route=kind)


def hive_session(main: Path, entry: Any) -> Any:
    """An unopened session against the hive's one supervised Beads service. Never starts one."""
    try:
        return host_beads.resolve_session(main, _core().LIFECYCLE_CAPABILITIES, entry=entry)
    except host_beads.HiveNotServable as exc:
        raise _core().SessionUnavailable(str(exc)) from exc
    except (KeyError, TypeError) as exc:
        raise _core().SessionUnavailable(
            f"cannot address hive from entry {entry!r}: {exc}"
        ) from exc


#: The session seam: ``(main, entry)`` -> an unopened ``BeadsSession``. Tests substitute a
#: transport-fixture session here; production resolves the hive's supervised service.
session_factory: Callable[[Path, Any], Any] = hive_session


def _unavailable_errors() -> tuple[type[BaseException], ...]:
    client = importlib.import_module("beadhive_beads_client")
    service = importlib.import_module("beadhive_beads_client.service")
    return (
        client.IncompatibleService,
        service.ServiceError,
        _core().SessionUnavailable,
        OSError,
        ValueError,
    )


class SelectedIssues:
    """The ``Issues`` port, its route selected on first use and held for the whole command.

    Selection happens before the first Beads operation that needs it and never changes
    afterwards: once the api-ready route is chosen, a failing call is reported by the core
    (fail closed), never replayed through ``bd``.
    """

    def __init__(self, main: Path, entry: Any, stack: ExitStack) -> None:
        self._main = main
        self._entry = entry
        self._stack = stack
        self._route: Any = None

    def _selected(self) -> Any:
        if self._route is None:
            self._route = self._select()
        return self._route

    def _select(self) -> Any:
        core = _core()
        try:
            session = self._stack.enter_context(session_factory(self._main, self._entry))
        except _unavailable_errors() as exc:
            log.get_logger("beadhive.work").info(
                "lifecycle_route_fallback", operation="work.issue.get", detail=str(exc)
            )
            return CliIssues(self._main)
        return core.SessionIssues(session, observer=TelemetryRoutingObserver())

    @property
    def route(self) -> str:
        return str(self._selected().route)

    def get(self, bead: str) -> Any:
        return self._selected().get(bead)

    def assign(self, bead: str, assignee: str, *, actor: str, read: Any) -> None:
        self._selected().assign(bead, assignee, actor=actor, read=read)


# ---- supplied capabilities -------------------------------------------------------------------


class ShellWorkspace:
    """Worktree, identity, claim-record, state-sync and dispatch-gate capabilities."""

    def __init__(self, cfg: Any, hive: str, main: Path, entry: Any) -> None:
        self._cfg = cfg
        self._hive = hive
        self._main = main
        self._entry = entry

    def refresh(self) -> None:
        _work()._pull_state(self._cfg, self._main)

    def publish(self, actor: str, message: str) -> None:
        _work()._push_state(self._cfg, self._main, actor, message)

    def dispatch_gate(self, issue: Any, bead: str) -> None:
        try:
            _work()._guard_conventions(self._cfg, issue, bead, self._main, action="dispatch")
        except typer.Exit as exc:  # the gate printed its own findings
            raise _core().Refused(exc.exit_code) from exc

    def open_container(self, bead: str) -> None:
        _work()._maybe_open_molecule(self._cfg, self._hive, bead, self._main)

    def provision(self, bead: str, kind: str) -> Any:
        worktree = _work().worktree
        if kind:
            entry, target, branch = worktree.ensure(self._cfg, self._hive, bead, kind=kind)
        else:
            entry, target, branch = worktree.ensure(self._cfg, self._hive, bead)
        return _core().Provisioned(Path(target), str(branch), entry)

    def batch_checkout(self, group: str) -> Any:
        work = _work()
        branch = f"{work.work_group.BATCH_PREFIX}{group}"
        target = work.worktree.locate(self._cfg, self._hive, branch=branch)[2]
        if not target.exists():
            return None
        return _core().Provisioned(Path(target), branch, self._entry)

    def stamp(self, checkout: Any, actor: str) -> None:
        _work()._stamp(self._cfg, checkout.handle or self._entry, checkout.target, actor)

    def record_claim(self, bead: str, actor: str, checkout: Any) -> None:
        _work()._issue_claim(
            self._cfg, checkout.handle or self._entry, bead, actor, checkout.target, self._hive
        )

    def remove(self, bead: str) -> bool:
        worktree = _work().worktree
        target = worktree.locate(self._cfg, self._hive, bead)[2]
        if not target.exists():
            return False
        worktree.remove(self._hive, bead, force=True)
        return True


class TyperOutput:
    def __init__(self, main: Path) -> None:
        self._main = main

    def say(self, text: str, *, error: bool = False) -> None:
        typer.echo(text, err=error)

    def feedback(self, bead: str) -> None:
        # Presentation, not lifecycle state: bd's markdown comment renderer is the operator
        # contract and is not reproducible from typed comment rows.
        bd.run(["comments", bead], self._main)


class TelemetryLifecycleObserver:
    def __init__(self, cfg: Any, entry: Any) -> None:
        self._cfg = cfg
        self._entry = entry

    def transition(self, name: str) -> None:
        otel.count_bead_transition(name)

    def dispatch(self, *, agent: str, bead: str, brief: str) -> Any:
        return otel.record_agent_dispatch(
            agent=agent,
            model=config.otel_genai_model(self._cfg),
            system=config.otel_genai_system(self._cfg, self._entry),
            brief=brief or None,
            attributes={"bh.bead": bead},
        )

    def legacy_seat(self, *, deprecated: str, replacement: str, seat: str) -> None:
        log.get_logger("beadhive.work_guards").warning(
            "legacy_seat_prefix_deprecated",
            deprecated=deprecated,
            replacement=replacement,
            seat=seat,
            reason="seat prefixes renamed per roles/RBAC matrix (coord/->disp/, crew/->dev/)",
        )


@contextmanager
def commands(cfg: Any, hive: str, main: Path, entry: Any) -> Iterator[Any]:
    """One command's ``LifecycleCommands`` with every port bound; closes any session it opened."""
    core = _core()
    main = Path(main)
    with ExitStack() as stack:
        yield core.LifecycleCommands(
            SelectedIssues(main, entry, stack),
            CliLeases(main),
            CliStateOperations(main),
            CliStateReads(main),
            CliGateOperations(main),
            ShellWorkspace(cfg, hive, main, entry),
            TyperOutput(main),
            observer=TelemetryLifecycleObserver(cfg, entry),
            policy=core.LifecyclePolicy(cli_name=config.BINARY_ALIAS),
        )


@contextmanager
def _failing_closed() -> Iterator[None]:
    """Map a core refusal (already rendered) onto the CLI exit code."""
    try:
        yield
    except _core().LifecycleFailed as failure:
        raise typer.Exit(failure.exit_code) from failure


def _actor(cfg: Any, entry: Any, as_: str) -> str:
    work = _work()
    return work.identity.resolve_actor(as_, config.work_identity(cfg, entry)["name"] or "")


def _identity(cfg: Any, entry: Any, actor: str) -> dict:
    prof = config.work_identity(cfg, entry, actor)
    return {
        "mode": prof["mode"],
        "name": actor or prof["name"] or "",
        "email": prof["email"] or "",
        "signing_key": prof["signing_key"] or "",
        "sign": prof["sign"],
    }


def batch_member_procedure(bead: str, group: str) -> str:
    """The error a per-bead verb on a batch member gets (bh-n5z3.7)."""
    return str(_core().batch_member_procedure(bead, group, config.BINARY_ALIAS))


# ---- verbs -----------------------------------------------------------------------------------


def assign(bead: str, to: str, as_: str, hive: str, preview: bool, as_json: bool) -> None:
    work = _work()
    otel.set_bead(bead)
    cfg = config.load()
    if preview:
        work._print_work_preview(cfg, hive, bead, to, op="assign", as_json=as_json)
        return
    work.guard.guard_primary(hive, cfg=cfg, verb="work assign")
    entry, main, _target, _branch = work.worktree.locate(cfg, hive, bead)
    actor = _actor(cfg, entry, as_)
    with _failing_closed(), commands(cfg, hive, main, entry) as lifecycle:
        lifecycle.assign(bead, to, actor)


def claim(
    bead: str, as_: str, group: str, collapse: str, hive: str, preview: bool, as_json: bool
) -> None:
    work = _work()
    cfg = config.load()
    group = work.work_logic.opt_str(group)
    collapse = work.work_logic.opt_str(collapse)
    if preview:
        if collapse or group:
            typer.echo("✗ --preview supports a single <id> only (no --group/--collapse)", err=True)
            raise typer.Exit(1)
        if not bead:
            typer.echo("✗ pass a bead <id>", err=True)
            raise typer.Exit(1)
        entry, _main, _target, _branch = work.worktree.locate(cfg, hive, bead)
        actor = _actor(cfg, entry, as_)
        work._print_work_preview(cfg, hive, bead, actor, op="claim", as_json=as_json)
        return
    work.guard.guard_primary(hive, cfg=cfg, verb="work claim")
    if collapse:
        if bead or group:
            typer.echo("✗ pass either <id>, --group, or --collapse — not more than one", err=True)
            raise typer.Exit(1)
        work.work_group.claim_collapsed(cfg, hive, collapse, as_)
        return
    if group:
        if bead:
            typer.echo("✗ pass either <id> or --group, not both", err=True)
            raise typer.Exit(1)
        work.work_group.claim_group(cfg, hive, group, as_)
        return
    if not bead:
        typer.echo("✗ pass a bead <id> (or --group <ids> for a batch)", err=True)
        raise typer.Exit(1)
    result = work._claim_single_bead(cfg, hive, bead, as_)
    typer.echo(f"✓ claimed {bead} as {result.actor}; worktree {result.worktree}")
    work._print_brief(cfg, result.entry, bead, result.bead)
    if not work.worktree.in_bead_worktree(result.worktree):
        typer.echo(
            "\nWARNING: cwd is not the bead worktree — edits here target the wrong tree.\n"
            f'  → cd "{result.worktree}"  # work happens in the worktree, NOT the main clone',
            err=True,
        )


def claim_single_bead(cfg: Any, hive: str, bead: str, as_: str) -> Any:
    """Claim one bead and return its structured ``ClaimResult`` envelope (no success output)."""
    work = _work()
    otel.set_bead(bead)
    entry, main, _target, _branch = work.worktree.locate(cfg, hive, bead)
    actor = _actor(cfg, entry, as_)
    with _failing_closed(), commands(cfg, hive, main, entry) as lifecycle:
        outcome = lifecycle.claim(bead, actor)
    checkout = outcome.checkout
    entry = checkout.handle or entry
    return work.ClaimResult(
        entry=entry,
        main=Path(main),
        bead=dict(outcome.issue),
        actor=actor,
        disposition=outcome.disposition,
        worktree=Path(checkout.target),
        identity=_identity(cfg, entry, actor),
        branch=str(checkout.branch),
    )


def try_claim(bead: str, actor: str, main: Path) -> bool:
    """`bh work next`'s CLI-loop claim: acquire the lease, then re-verify by re-reading."""
    with commands({}, "", main, None) as lifecycle:
        return bool(lifecycle.try_claim(bead, actor))


def release_claim(main: Path, bead: str, actor: str, detail: str = "") -> None:
    """Release a just-taken claim, recorded under ``dispatch=provisioning_failed``."""
    with commands({}, "", main, None) as lifecycle:
        lifecycle.release(bead, actor, detail=detail)


def provision_claim(cfg: Any, hive: str, main: Path, bead: str, actor: str) -> tuple[str, dict]:
    """Provision/attach a just-won claim's worktree; releases the claim on any failure."""
    work = _work()
    entry = work.worktree.locate(cfg, hive, bead)[0]
    with commands(cfg, hive, main, entry) as lifecycle:
        checkout = lifecycle.provision_claim(bead, actor)
    return (str(checkout.target), _identity(cfg, checkout.handle or entry, actor))


def resume(bead: str, as_: str, hive: str) -> None:
    work = _work()
    otel.set_bead(bead)
    cfg = config.load()
    entry, main, _target, _branch = work.worktree.locate(cfg, hive, bead)
    actor = _actor(cfg, entry, as_)
    with _failing_closed(), commands(cfg, hive, main, entry) as lifecycle:
        lifecycle.resume(bead, actor)


def abandon(bead: str, hive: str, rm: bool) -> None:
    work = _work()
    otel.set_bead(bead)
    cfg = config.load()
    entry, main, _target, _branch = work.worktree.locate(cfg, hive, bead)
    actor = _actor(cfg, entry, "")
    with _failing_closed(), commands(cfg, hive, main, entry) as lifecycle:
        lifecycle.abandon(bead, actor, remove=rm)


# ---- submit's bead state ---------------------------------------------------------------------


def admit_submission(cfg: Any, hive: str, entry: Any, main: Path, bead: str, actor: str) -> dict:
    """The one pre-mutation bead read submit's policy checks share, refused unless ``actor``
    holds the claim."""
    with _failing_closed(), commands(cfg, hive, main, entry) as lifecycle:
        return dict(lifecycle.admit_submission(bead, actor))


def mark_submitted(
    cfg: Any, hive: str, entry: Any, main: Path, bead: str, sha: str, actor: str
) -> None:
    """``review=pending`` once the review gate is open."""
    with _failing_closed(), commands(cfg, hive, main, entry) as lifecycle:
        lifecycle.mark_submitted(bead, sha, actor)
