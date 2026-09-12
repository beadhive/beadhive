"""Thin Typer projection over command-specific Herdr application contracts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import wraps
from pathlib import Path

import typer

from beadhive.kernel.plugins import CapabilitySelection, DiscoveryResult, bind_application_port

from . import application_runtime
from . import application_services as application
from .agent_adapter import (
    HERDR_AGENT_SESSION,
    HERDR_AGENT_SESSION_KEY,
    HerdrAgentAdapter,
    HerdrAgentSessionPort,
    herdr_agent_provider_binding,
)
from .application_contracts import (
    AddRequest,
    AddUseCase,
    AttachRequest,
    AttachUseCase,
    DispatchRequest,
    DispatchUseCase,
    HerdrCommandResult,
    IntegrateRequest,
    IntegrateUseCase,
    LaunchRequest,
    LaunchUseCase,
    PsRequest,
    PsUseCase,
    ReapRequest,
    ReapUseCase,
    SpawnRequest,
    SpawnUseCase,
    StatusRequest,
    StatusUseCase,
    WatchRequest,
    WatchUseCase,
)
from .production_runtime import HerdrProductionResolver

CliOutcome = HerdrCommandResult


@dataclass(frozen=True, slots=True, init=False)
class HerdrCliApplication:
    """Statically composed application use cases sharing one bound provider port."""

    agent_session: HerdrAgentSessionPort
    launch: LaunchUseCase
    add: AddUseCase
    status: StatusUseCase
    ps: PsUseCase
    integrate: IntegrateUseCase
    attach: AttachUseCase
    reap: ReapUseCase
    spawn: SpawnUseCase
    dispatch: DispatchUseCase
    watch: WatchUseCase

    def __init__(self, agent_session: HerdrAgentSessionPort) -> None:
        object.__setattr__(self, "agent_session", agent_session)
        object.__setattr__(self, "launch", application.LaunchApplication(agent_session))
        object.__setattr__(self, "add", application.AddApplication(agent_session))
        object.__setattr__(self, "status", application.StatusApplication(agent_session))
        object.__setattr__(self, "ps", application.PsApplication(agent_session))
        object.__setattr__(self, "integrate", application.IntegrateApplication(agent_session))
        object.__setattr__(self, "attach", application.AttachApplication(agent_session))
        object.__setattr__(self, "reap", application.ReapApplication(agent_session))
        object.__setattr__(self, "spawn", application.SpawnApplication(agent_session))
        object.__setattr__(self, "dispatch", application.DispatchApplication(agent_session))
        object.__setattr__(self, "watch", application.WatchApplication(agent_session))


def compose_application() -> HerdrCliApplication:
    """Compose the built-in ``agent.session@1`` provider without discovery effects."""

    adapter = HerdrAgentAdapter(HerdrProductionResolver(application.production_runtime_for_receipt))
    discovery = DiscoveryResult((), (CapabilitySelection(HERDR_AGENT_SESSION, "herdr"),), ())
    port = bind_application_port(
        HERDR_AGENT_SESSION_KEY,
        discovery,
        (herdr_agent_provider_binding(adapter),),
    )
    return HerdrCliApplication(port)


APPLICATION = compose_application()
cli = typer.Typer(no_args_is_help=True, help="herdr terminal/agent-pane integration.")
_neutral_session_scoped = application.session_scoped


def session_scoped(fn):
    """Translate neutral session-selection failures for legacy Typer view commands."""

    scoped = _neutral_session_scoped(fn)

    @wraps(fn)
    def presented(*args, **kwargs):
        try:
            return scoped(*args, **kwargs)
        except application_runtime.StopApplication as exc:
            error = exc.error
            if error.parameter is not None:
                raise typer.BadParameter(error.detail, param_hint=error.parameter) from exc
            raise typer.Exit(error.exit_code) from exc

    return presented


def _render(outcome: HerdrCommandResult) -> None:
    for notice in outcome.notices:
        typer.echo(
            notice.detail,
            nl=notice.newline,
            err=notice.severity == "error",
        )
    if outcome.payload is not None:
        typer.echo(json.dumps(outcome.payload, indent=2, default=str))
    if outcome.error is None:
        return
    if outcome.error.parameter is not None:
        raise typer.BadParameter(
            outcome.error.detail,
            param_hint=outcome.error.parameter,
        )
    if outcome.error.cause is not None:
        raise outcome.error.cause
    if outcome.error.detail and not outcome.notices and outcome.payload is None:
        typer.echo(outcome.error.detail, err=True)
    raise typer.Exit(outcome.error.exit_code)


@cli.command(
    "launch",
    help=(
        "bh plugin herdr launch nvhack-lvxi --json\n\n"
        "Claim one exact bead and start or reuse its warm Herdr coding agent. The one-argument "
        "path discovers the hive and kind. All options are overrides: --hive disambiguates, "
        "--kind selects an installed integration, --session selects an exact named session "
        "or the current in-pane session, --as selects the developer identity, "
        "--adopt-expired uses only non-forced host adoption, --direction defaults right, "
        "--no-focus is the safe focus default, and --json is the agent contract. Herdr never "
        "creates or removes a worktree, never installs an integration, and never seizes an "
        "active foreign host lease."
    ),
)
def _launch(
    bead: str | None = typer.Argument(
        None, metavar="BEAD_ID", help="exact Beads issue ID (omitted for planner_session)"
    ),
    hive: str = typer.Option(
        "", "--hive", help="explicit registered hive when exact bead lookup is ambiguous"
    ),
    kind: str | None = typer.Option(
        None, "--kind", help="Herdr kind; overrides per-hive/global herdr.kind defaults"
    ),
    session: str | None = typer.Option(None, "--session", help=application._SESSION_OPTION_HELP),
    as_: str = typer.Option(
        "", "--as", help="developer identity; otherwise use normal bh work claim precedence"
    ),
    adopt_expired: bool = typer.Option(
        False,
        "--adopt-expired",
        help="non-forcibly adopt only an expired or released host lease before claiming",
    ),
    direction: str = typer.Option(
        "right", "--direction", help="new pane direction: right (default) or down"
    ),
    focus: bool = typer.Option(
        False, "--focus/--no-focus", help="focus the new pane (safe default: --no-focus)"
    ),
    as_json: bool = typer.Option(
        False, "--json", help="emit only the versioned launch result on stdout"
    ),
    profile_json: str | None = typer.Option(
        None,
        "--profile-json",
        help="versioned HerdrAgentLaunchProfile JSON; exact targeting is mandatory when set",
    ),
    recover_after_pane: str | None = typer.Option(
        None,
        "--recover-after-pane",
        help=(
            "after authoritative Herdr Agent loss, relaunch a fresh fenced generation "
            "after this exact pane; requires --profile-json"
        ),
    ),
    session_checkout: Path | None = typer.Option(  # noqa: B008
        None,
        "--session-checkout",
        help="explicit existing git checkout for a beadless planner_session profile",
    ),
) -> None:
    _render(
        APPLICATION.launch.execute(
            LaunchRequest(
                bead,
                hive,
                kind,
                session,
                as_,
                adopt_expired,
                direction,
                focus,
                as_json,
                profile_json,
                recover_after_pane,
                session_checkout,
            )
        )
    )


@cli.command(
    "add",
    help="explicitly link or install the external Beadhive package in Herdr's user registry.",
)
def _add(
    local: Path | None = typer.Option(  # noqa: B008
        None, "--local", help="validated local beadhive/herdr-plugin checkout to link"
    ),
    managed_ref: str = typer.Option(
        "",
        "--managed-ref",
        help="auditable Git ref of the known beadhive/herdr-plugin repository to install",
    ),
    yes: bool = typer.Option(
        False, "--yes", help="consent to execute the managed package's installation/build"
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="validate and print the plan only"),
    as_json: bool = typer.Option(False, "--json", help="emit a versioned lifecycle receipt"),
    operation_id: str = typer.Option("", "--operation-id", help="caller correlation ID"),
) -> None:
    _render(
        APPLICATION.add.execute(AddRequest(local, managed_ref, yes, dry_run, as_json, operation_id))
    )


@cli.command("status", help="show herdr server health and installed agent integrations.")
def _status(
    session: str | None = typer.Option(None, "--session", help=application._SESSION_OPTION_HELP),
    as_json: bool = typer.Option(False, "--json", help="emit a versioned lifecycle receipt"),
    operation_id: str = typer.Option("", "--operation-id", help="caller correlation ID"),
) -> None:
    _render(APPLICATION.status.execute(StatusRequest(session, as_json, operation_id)))


@cli.command("ps", help="list live herdr agents and their bh identity.")
def _ps(
    session: str | None = typer.Option(None, "--session", help=application._SESSION_OPTION_HELP),
    as_json: bool = typer.Option(
        False, "--json", help="emit a versioned lifecycle receipt with the live roster"
    ),
    operation_id: str = typer.Option("", "--operation-id", help="caller correlation ID"),
) -> None:
    _render(APPLICATION.ps.execute(PsRequest(session, as_json, operation_id)))


@cli.command("integrate", help="install herdr lifecycle hooks for one agent kind.")
def _integrate(kind: str = typer.Argument(..., metavar="KIND")) -> None:
    _render(APPLICATION.integrate.execute(IntegrateRequest(kind)))


@cli.command("attach", help="print the command for a human to attach to an agent pane.")
def _attach(
    target: str = typer.Argument(..., metavar="TARGET"),
    session: str | None = typer.Option(None, "--session", help=application._SESSION_OPTION_HELP),
    as_json: bool = typer.Option(False, "--json", help="emit a versioned lifecycle receipt"),
    operation_id: str = typer.Option("", "--operation-id", help="caller correlation ID"),
) -> None:
    _render(APPLICATION.attach.execute(AttachRequest(target, session, as_json, operation_id)))


@cli.command("reap", help="close a pane previously created by bh plugin herdr spawn.")
def _reap(
    target: str = typer.Argument(..., metavar="TARGET"),
    session: str | None = typer.Option(None, "--session", help=application._SESSION_OPTION_HELP),
    pane: str = typer.Option(
        "", "--pane", help="exact pane locator from the spawn receipt (enables terminal cleanup)"
    ),
    as_json: bool = typer.Option(False, "--json", help="emit a versioned lifecycle receipt"),
    operation_id: str = typer.Option("", "--operation-id", help="caller correlation ID"),
    generation: int | None = typer.Option(
        None, "--generation", min=1, help="exact managed launch generation fence"
    ),
    launch_spec_digest: str = typer.Option(
        "", "--launch-spec-digest", help="exact sha256 launch specification fence"
    ),
) -> None:
    _render(
        APPLICATION.reap.execute(
            ReapRequest(
                target,
                session,
                pane,
                as_json,
                operation_id,
                generation,
                launch_spec_digest,
            )
        )
    )


@cli.command("spawn", help="start a warm, steerable agent pane in an existing bh worktree.")
def _spawn(
    hive: str = typer.Option(..., "--hive", help="managed hive identifier"),
    bead: str = typer.Option(..., "--bead", help="already-claimed bead identifier"),
    kind: str = typer.Option(..., "--kind", help="Herdr agent kind, e.g. claude or codex"),
    session: str | None = typer.Option(None, "--session", help=application._SESSION_OPTION_HELP),
    as_json: bool = typer.Option(False, "--json", help="emit a versioned lifecycle receipt"),
    operation_id: str = typer.Option("", "--operation-id", help="caller correlation ID"),
) -> None:
    _render(
        APPLICATION.spawn.execute(SpawnRequest(hive, bead, kind, session, as_json, operation_id))
    )


@cli.command("dispatch", help="send a prompt and verify it reached the agent pane.")
def _dispatch(
    target: str = typer.Argument(..., metavar="TARGET", help="herdr agent target"),
    prompt: str | None = typer.Argument(
        None, metavar="[PROMPT]", help="legacy positional prompt; prefer --stdin or --prompt-file"
    ),
    from_stdin: bool = typer.Option(
        False, "--stdin", help="read prompt from stdin and keep it out of process arguments"
    ),
    prompt_file: str = typer.Option(
        "", "--prompt-file", help="read prompt from this file and keep it out of process arguments"
    ),
    session: str | None = typer.Option(None, "--session", help=application._SESSION_OPTION_HELP),
    as_json: bool = typer.Option(False, "--json", help="emit a versioned lifecycle receipt"),
    operation_id: str = typer.Option("", "--operation-id", help="caller correlation ID"),
) -> None:
    _render(
        APPLICATION.dispatch.execute(
            DispatchRequest(
                target,
                prompt,
                from_stdin,
                prompt_file,
                session,
                as_json,
                operation_id,
            )
        )
    )


@cli.command("watch", help="wait for an agent to become blocked or finish.")
def _watch(
    target: str = typer.Argument(..., metavar="TARGET"),
    session: str | None = typer.Option(None, "--session", help=application._SESSION_OPTION_HELP),
    timeout: float | None = typer.Option(
        None,
        "--timeout",
        min=0.0,
        help="seconds to wait before giving up (herdr's millisecond timeout is derived from it)",
    ),
    as_json: bool = typer.Option(False, "--json", help="emit a versioned lifecycle receipt"),
    operation_id: str = typer.Option("", "--operation-id", help="caller correlation ID"),
) -> None:
    _render(
        APPLICATION.watch.execute(WatchRequest(target, session, timeout, as_json, operation_id))
    )
