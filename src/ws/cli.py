"""ws CLI — Typer app wiring the operation groups together.

Surface: bd / git (passthrough + -a/-r routing) · rig · labels · sync · hub · dolt · doctor
· backup · config · setup.
Heavy lifting is delegated to bd / dolt / git / gh / docker; ws encodes the
orchestration, registry/validation logic, and path-derived identity.
"""

from __future__ import annotations

import importlib.metadata
import os
import shutil
import sys
import time
from pathlib import Path

import typer

from . import bd as bd_mod
from . import config, dolt, log, otel, plan, registry, validate, work
from .run import run

app = typer.Typer(no_args_is_help=True, help="Workspace CLI.")

# Help panels. Passthrough honors the global -a/--all and -r/--rig flags; the others split
# into operating on rigs in the workspace vs administering ws itself.
PASSTHROUGH_PANEL = "Passthrough"
WORKSPACE_PANEL = "Workspace"
ADMIN_PANEL = "Admin"

rig_app = typer.Typer(no_args_is_help=True, help="Onboard repos as beads rigs.")
labels_app = typer.Typer(no_args_is_help=True, help="Registry: validate / sync / docs.")
wt_app = typer.Typer(no_args_is_help=True, help="Managed worktrees.")
dolt_app = typer.Typer(no_args_is_help=True, help="Optional Dolt SQL server.")
otel_app = typer.Typer(no_args_is_help=True, help="Local LGTM stack (grafana/otel-lgtm).")
observaloop_app = typer.Typer(
    no_args_is_help=True, help="observaloop telemetry routing profile (rig-scoped)."
)
config_app = typer.Typer(no_args_is_help=True, help="ws config.")
mcp_app = typer.Typer(
    no_args_is_help=True,
    help=(
        "Model Context Protocol server (fastmcp is a core dependency of ws).\n\n"
        "Register with Claude Code at user scope (run once):\n\n"
        "  claude mcp add ws --scope user -- ws mcp serve\n\n"
        "Or use the convenience verb: ws mcp install"
    ),
)
hq_app = typer.Typer(
    no_args_is_help=True, help="Factory HQ: the durable central store (kind=hq singleton)."
)
setup_app = typer.Typer(
    no_args_is_help=True, help="Post-install dependency check + cached gate."
)

app.add_typer(setup_app, name="setup", rich_help_panel=ADMIN_PANEL)
app.add_typer(rig_app, name="rig", rich_help_panel=WORKSPACE_PANEL)
app.add_typer(hq_app, name="hq", rich_help_panel=WORKSPACE_PANEL)
app.add_typer(labels_app, name="labels", rich_help_panel=WORKSPACE_PANEL)
app.add_typer(wt_app, name="worktree", rich_help_panel=WORKSPACE_PANEL)
app.add_typer(wt_app, name="wt", hidden=True)  # `ws wt` alias (hidden to avoid dup in help)
app.add_typer(work.app, name="work", rich_help_panel=WORKSPACE_PANEL)
app.add_typer(plan.app, name="plan", rich_help_panel=WORKSPACE_PANEL)
app.add_typer(dolt_app, name="dolt", rich_help_panel=ADMIN_PANEL)
app.add_typer(otel_app, name="otel", rich_help_panel=ADMIN_PANEL)
app.add_typer(observaloop_app, name="observaloop", rich_help_panel=ADMIN_PANEL)
app.add_typer(config_app, name="config", rich_help_panel=ADMIN_PANEL)
app.add_typer(mcp_app, name="mcp", rich_help_panel=ADMIN_PANEL)


# ---- setup gate ---------------------------------------------------------------

# Subcommands exempt from the setup-complete gate.  The gate guards every OTHER
# verb: a fresh install that has never run `ws setup check` must still be able
# to bootstrap (config init), diagnose itself (doctor), or run setup check itself.
# --version and --help never reach the gate (eager callback + typer exit before body).
_SETUP_GATE_ALLOW: frozenset[str] = frozenset({"setup", "config", "doctor"})


def _enforce_setup_gate(ctx: typer.Context) -> None:
    """Gate every verb not in _SETUP_GATE_ALLOW behind a passing setup cache.

    Bypass entirely when:
    - ``WS_SKIP_SETUP_CHECK=1`` is set (debug escape hatch)
    - the invoked subcommand is in the allow-list or is None (no subcommand)
    - the setup cache exists with ``setup == true``

    Denied verbs surface a clear "run ws setup check" message on stderr and exit 1.
    """
    if os.environ.get("WS_SKIP_SETUP_CHECK") == "1":
        return
    subcmd = ctx.invoked_subcommand
    if subcmd is None or subcmd in _SETUP_GATE_ALLOW:
        return
    from . import setup as setup_mod  # lazy: avoids import at module load

    if not setup_mod.is_setup_complete():
        typer.echo(
            f"✗ `ws {subcmd}` requires setup — run `ws setup check` first.\n"
            "  Skip with WS_SKIP_SETUP_CHECK=1 (debug bypass).",
            err=True,
        )
        raise typer.Exit(1)


# ---- root: global rig-routing flags -----------------------------------------


def _outcome_from_exc(exc: BaseException | None) -> str:
    """Map the active ``sys.exc_info()[1]`` inside ``ctx.call_on_close`` to ``ok`` or ``error``.

    Click fires ``call_on_close`` while the exit exception is still active in ``sys.exc_info()``,
    so we can inspect it to determine the command outcome without interfering with Click's own
    handling.  Three distinct cases arise:

    - ``None``: ``standalone_mode=False`` success path (e.g. Typer CliRunner) — the ``with``
      block exits normally, no exception is active → ``ok``.
    - ``Exit`` (``typer.Exit`` / ``click.Exit``): carries an ``exit_code`` attribute →
      ``ok`` if ``exit_code == 0``, else ``error``.
    - ``SystemExit``: direct ``sys.exit()`` call → ``ok`` if ``code in (0, None)``, else ``error``.
    - ``Abort`` or any other exception → ``error``.
    """
    if exc is None:
        return "ok"
    if isinstance(exc, SystemExit):
        return "error" if exc.code not in (0, None) else "ok"
    exit_code = getattr(exc, "exit_code", None)
    if exit_code is not None:
        return "error" if exit_code != 0 else "ok"
    return "error"


def _version(value: bool):
    if value:
        typer.echo(importlib.metadata.version("ws"))
        raise typer.Exit()


@app.callback()
def _root(
    ctx: typer.Context,
    all_rigs: bool = typer.Option(
        False, "-a", "--all", help="route the passthrough across ALL registered rigs"
    ),
    rig: str = typer.Option(
        None, "-r", "--rig", help="route the passthrough to one rig (see rig_match)"
    ),
    version: bool = typer.Option(
        False, "--version", "-V", callback=_version, is_eager=True, help="show version and exit"
    ),
):
    """Workspace beads CLI. -a/-r route `bd`/`git` across rigs (need git_workspace)."""
    # Eager telemetry init: this callback runs before every subcommand, so it's the one place
    # that activates OTel for a real `ws` command path (otherwise is_active() is forever False
    # and every emitter is inert). It's cheap + safe when off: init() no-ops fast on the default
    # (otel.enabled false) and never imports opentelemetry on that path. Telemetry is best-effort
    # and must never block the CLI — a missing/unreadable config (e.g. before `ws config init`)
    # degrades to telemetry-off rather than erroring. The eager `--version` path exits before
    # this body, so it stays untouched.
    try:
        _cfg = config.load()
        # Per-worktree endpoint overlay: if cwd is a managed worktree with a `.ws/otel.env` cache,
        # load it into os.environ BEFORE init so config.otel_endpoint / config.observaloop_profile
        # pick up the rig profile's endpoint + name. The common path is a single file read with no
        # ws.observaloop import (only the self-heal branch touches observaloop); best-effort, so it
        # never blocks startup. observaloop_env imports config + worktree only — not observaloop.
        from . import observaloop_env

        observaloop_env.load_worktree_env(_cfg)
        otel.init(_cfg)
    except Exception:  # best-effort telemetry; never break the CLI on init/config-load failure
        pass
    # Instrument the command-entry seam: register a call_on_close hook that emits a counter +
    # histogram tagged with the invoked subcommand name + outcome (ok/error). Gated on
    # is_active() so the off-path (default: otel disabled) is a single bool read — zero SDK
    # import, zero allocation. The --version eager path exits before this body, so it's untouched.
    if otel.is_active():
        _start = time.monotonic()
        _cmd = ctx.invoked_subcommand or ""
        # Open a root ws.cli {command} span so all child spans (trace_verb + subprocess) nest
        # under it. The context manager is entered here (making the span current) and exited in
        # call_on_close after the subcommand completes. otel.span() delegates to get_tracer(),
        # which is already gated on _initialized, so no opentelemetry import on the off-path.
        _cli_span_cm = otel.span(f"ws.cli {_cmd}", {"ws.cli.command": _cmd})
        _cli_span = _cli_span_cm.__enter__()

        def _record_invocation() -> None:
            exc = sys.exc_info()[1]
            outcome = _outcome_from_exc(exc)
            _cli_span.set_attribute("ws.cli.outcome", outcome)
            # Pass exc only for real errors — clean-exit control flow (Exit(0), SystemExit(0))
            # must not mark the span ERROR.
            if outcome == "error" and exc is not None:
                _cli_span_cm.__exit__(type(exc), exc, exc.__traceback__)
            else:
                _cli_span_cm.__exit__(None, None, None)
            otel.record_cli_invocation(_cmd, outcome, time.monotonic() - _start)

        ctx.call_on_close(_record_invocation)
    _enforce_setup_gate(ctx)
    mode = "all" if all_rigs else "rig" if rig else "cwd"
    if mode != "cwd" and ctx.invoked_subcommand not in ("bd", "git"):
        typer.echo("✗ -a/--all and -r/--rig only apply to `ws bd` and `ws git`", err=True)
        raise typer.Exit(1)
    ctx.obj = (mode, rig)


# ---- workspace --------------------------------------------------------------


@app.command(
    "role",
    rich_help_panel=WORKSPACE_PANEL,
    help="launch claude in a seat role (e.g. `ws role developer`); no arg → list seats.",
)
def role_cmd(
    name: str = typer.Argument("", help="seat role to launch (e.g. developer, dispatcher)"),
):
    from . import role as role_mod

    role_mod.launch(name)


@app.command("statusline", hidden=True, help="print role/rig statusline from stdin JSON (TUI).")
def statusline_cmd():
    from . import role as role_mod

    role_mod.statusline()


@app.command(
    "sync",
    rich_help_panel=WORKSPACE_PANEL,
    help="build/refresh the hub: add every registered rig (clone-cache uncloned ones) + sync.",
)
def sync_cmd():
    from . import hub

    if hub.sync():  # genuine add/sync failures propagate as a non-zero exit
        raise typer.Exit(1)


@app.command(
    "hub",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    add_help_option=False,
    hidden=True,  # deprecated: use `ws hq` instead
    rich_help_panel=WORKSPACE_PANEL,
    help="[DEPRECATED] use `ws hq` instead. Query the aggregated hub (cross-rig view).",
)
def hub_cmd(ctx: typer.Context):
    typer.echo("⚠ `ws hub` is deprecated — use `ws hq` instead.", err=True)
    from . import hub

    args = ctx.args
    # allow either `ws hub bd ready` or `ws hub ready`
    if args and args[0] == "bd":
        args = args[1:]
    # `ws hub intake` → the superintendent's fleet-wide untriaged-intake inbox (a filtered read).
    if args and args[0] == "intake":
        hub.intake(args[1:])
        return
    hub.query(args)


@hq_app.command(
    "init",
    help="stand up the Factory HQ store (kind=hq singleton) and move aggregation onto it.",
)
def hq_init():
    from . import hq

    hq.init()


@hq_app.command(
    "intake",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    add_help_option=False,
    help="fleet-wide untriaged-intake inbox: superintendent's cross-rig view (hub.intake).",
)
def hq_intake_cmd(ctx: typer.Context):
    from . import hub

    hub.intake(ctx.args)


@hq_app.command(
    "bd",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    add_help_option=False,
    help="run a bd command against the HQ aggregate (cross-rig view), e.g. `ws hq bd ready`.",
)
def hq_bd_cmd(ctx: typer.Context):
    from . import hub

    hub.query(ctx.args)


@app.command(
    "report",
    rich_help_panel=WORKSPACE_PANEL,
    help="file a bug/feature/chore into a rig we own; lands as untriaged intake for triage.",
)
def report_cmd(
    rig: str = typer.Argument(..., metavar="RIG", help="target rig (prefix / triplet / org-repo)"),
    title: str = typer.Argument(..., metavar="TITLE", help="report title"),
    report_type: str = typer.Option(
        "bug", "--type", "-t", metavar="TYPE", help="report type: bug | feature | chore"
    ),
    as_actor: str = typer.Option(
        "", "--as", metavar="ACTOR", help="reporting seat/human (stamped as bd --actor)"
    ),
):
    from . import report as report_mod
    from .identity import resolve_actor

    actor = resolve_actor(as_actor)
    code, error, new_id = report_mod.file_report(rig, title, report_type, actor)
    if error:
        typer.echo(f"✗ {error}", err=True)
        raise typer.Exit(code)
    typer.echo(f"✓ filed {new_id} into '{rig}' as intake ({report_type}) — reported by {actor}")
    # Dedup on ENTRY: surface likely dupes so a colliding feature request is caught before it
    # buries the queue (the triage side runs the same `bd find-duplicates` pass). Best-effort.
    for pair in report_mod.entry_dupes(rig, new_id):
        other = (
            pair.get("issue_b_id") if pair.get("issue_a_id") == new_id else pair.get("issue_a_id")
        )
        typer.echo(f"  ⚠ likely duplicate of {other} — triage may reject/reroute this")


@app.command(
    "report-target",
    rich_help_panel=WORKSPACE_PANEL,
    help="emit ws's own report-channel descriptor (where to file ws issues).",
)
def report_target_cmd(
    as_json: bool = typer.Option(
        False, "--json", help="emit a machine-readable JSON discovery document"
    ),
):
    from . import report_target as rt_mod

    raise typer.Exit(rt_mod.emit(as_json=as_json))


@app.command(
    "escalate",
    rich_help_panel=WORKSPACE_PANEL,
    help=(
        "fire-and-forget escalation to HQ: name a tool problem, hand it up, and never block."
        " Requires 'ws hq init' first."
    ),
)
def escalate_cmd(
    title: str = typer.Argument(..., metavar="TITLE", help="short description of the problem"),
    tool: str = typer.Option(
        "", "--tool", metavar="TOOL", help="name of the tool or verb that triggered the escalation"
    ),
    as_seat: str = typer.Option(
        "", "--as", metavar="SEAT",
        help="raiser's seat/crew (e.g. crew/dev1); defaults to $WS_CREW",
    ),
):
    from . import escalate as escalate_mod
    from .identity import resolve_actor

    seat = resolve_actor(as_seat)
    code, error, new_id = escalate_mod.file_escalation(title, tool=tool, seat=seat)
    if error:
        typer.echo(f"✗ {error}", err=True)
        raise typer.Exit(code)
    tool_note = f" [tool: {tool}]" if tool else ""
    typer.echo(f"✓ escalated {new_id} to HQ as intake:untriaged{tool_note} — raised by {seat}")


# ---- bd / git (passthrough) -------------------------------------------------


@app.command(
    "bd",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    add_help_option=False,
    rich_help_panel=PASSTHROUGH_PANEL,
    help="Passthrough to bd; `bd create` auto-applies provider/org/repo.",
)
def bd_passthrough(ctx: typer.Context):
    if not config.bd_pass_enabled():
        otel.count_passthrough("bd", allowed=False)
        typer.echo(
            "✗ `ws bd` passthrough is disabled (default off; passthrough.bd_enabled).\n"
            "  Read beads with `ws work ready|issue|list`; file plans with `ws plan file`;\n"
            "  drive beads with `ws work`. Set WS_BD_PASS_ENABLED=1 (or WS_DEBUG=1) to override.",
            err=True,
        )
        raise typer.Exit(1)
    otel.count_passthrough("bd", allowed=True)
    mode, target = ctx.obj or ("cwd", None)
    bd_mod.passthrough(mode, target, ctx.args)


@app.command(
    "git",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    add_help_option=False,
    rich_help_panel=PASSTHROUGH_PANEL,
    help="Passthrough to git (incl. git workspace). `ws git workspace --help` → git-workspace.",
)
def git_passthrough(ctx: typer.Context):
    if not config.git_pass_enabled():
        otel.count_passthrough("git", allowed=False)
        typer.echo(
            "✗ `ws git` passthrough is disabled (passthrough.git_enabled=false).\n"
            "  Set WS_GIT_PASS_ENABLED=1 (or WS_DEBUG=1) to override.",
            err=True,
        )
        raise typer.Exit(1)
    otel.count_passthrough("git", allowed=True)
    from . import git as git_mod

    mode, target = ctx.obj or ("cwd", None)
    git_mod.passthrough(mode, target, ctx.args)


# ---- rig --------------------------------------------------------------------


@rig_app.command("init")
def rig_init(
    prime: bool = typer.Option(False, "--prime", help="install .beads/PRIME.md (issue workflow)"),
    claude: bool = typer.Option(
        False,
        "--claude",
        help="install .claude/ settings: shared settings.json (SessionStart hook + "
        "bd-remember deny) + a host-local settings.local.json sandbox grant for this "
        "rig's worktree subtree",
    ),
    skills: bool = typer.Option(
        False,
        "--skills",
        help="copy bundled role skills into ./skills; with --claude also symlink .claude/skills",
    ),
    observaloop: bool = typer.Option(
        False,
        "--observaloop",
        help="stand up this rig's observaloop profile (ensure+up) and apply the ws Grafana "
        "telemetry dashboard; best-effort — warns + continues when observaloop/docker/the "
        "visualizer is absent or otel is off",
    ),
    agents: bool = typer.Option(
        False,
        "--agents",
        help="install an AGENTS.md AGF hint stanza (points harnesses at `ws rig ready` + "
        ".beads/PRIME.md); with --claude the same stanza is added to CLAUDE.md. Non-destructive "
        "(managed marked block); -f refreshes an existing block",
    ),
    force: bool = typer.Option(
        False,
        "-f",
        "--force",
        help="re-register an already-configured rig (re-classify kind; the registered "
        "prefix is preserved) and overwrite existing PRIME.md / skills instead of "
        "preserving/skipping them",
    ),
    kind: str = typer.Option("", help="override: org-native|personal|prototype|fork"),
    prefix: str = typer.Option("", help="override the derived prefix"),
    yes: bool = typer.Option(
        False, "--yes",
        help="required to init a fork or to change a registered prefix (orphans bead IDs)",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="print plan, change nothing"),
    skip_check: str = typer.Option(
        "", "--skip-check",
        help="comma-separated preflight check id(s) to downgrade from failure to warning "
        "(overridable checks only, e.g. dirty-tree,on-default-branch); ids show under --dry-run",
    ),
):
    from . import config, rig

    # In plugin mode, --skills is incompatible with --claude: the plugin vends skills, so a
    # separate local copy is redundant.  Reject the combination early with a clear message.
    if claude and skills:
        try:
            cfg = config.load()
        except Exception:
            cfg = {}
        if config.claude_source(cfg) == "plugin":
            typer.echo(
                "✗ --claude --skills conflict: in plugin mode the agf plugin already vends "
                "skills — drop --skills (or set claude.source: copy in ~/.ws/config.yaml to "
                "use the legacy copy path).",
                err=True,
            )
            raise typer.Exit(1)

    rig.init(
        prime=prime,
        claude=claude,
        skills=skills,
        observaloop=observaloop,
        agents=agents,
        force=force,
        kind=kind,
        prefix=prefix,
        yes=yes,
        dry_run=dry_run,
        skip_check=skip_check,
    )


@rig_app.command("add", help="register a rig from a provider/org/repo triplet (no cwd/bd init).")
def rig_add(
    rig_id: str = typer.Argument(..., metavar="PROVIDER/ORG/REPO"),
    prefix: str = typer.Option("", help="override the derived prefix"),
    kind: str = typer.Option("", help="org-native|personal|prototype|fork"),
    upstream: str = typer.Option("", help="upstream org/repo (for forks)"),
):
    from . import rig

    rig.add(rig_id, prefix=prefix, kind=kind, upstream=upstream)


@rig_app.command("rm", help="unregister a rig by id (registry-only; leaves .beads/repo intact).")
def rig_rm(rig_id: str = typer.Argument(..., metavar="RIG_ID")):
    from . import rig

    rig.rm(rig_id)


@rig_app.command(
    "retire",
    help="guarded teardown of a rig: assess → (backup|consent) → worktree teardown → "
    "unregister → soft-archive the clone. Refuses to lose unbacked work without --backup or "
    "--confirm. --dry-run previews the full plan with zero mutation; --purge hard-deletes the "
    "clone instead of archiving it (still gated).",
)
def rig_retire(
    rig_id: str = typer.Argument(..., metavar="RIG_ID"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="print the full plan and change nothing (default-safe)"
    ),
    backup: bool = typer.Option(
        False, "--backup", help="snapshot unpushed/dirty work to durable wip branches first"
    ),
    confirm: bool = typer.Option(
        False, "--confirm", help="proceed past the safety gate, explicitly accepting data loss"
    ),
    purge: bool = typer.Option(
        False, "--purge", help="hard-delete the clone instead of soft-archiving it (still gated)"
    ),
):
    from . import retire

    retire.retire_rig(rig_id, dry_run=dry_run, backup=backup, confirm=confirm, purge=purge)


@rig_app.command(
    "onboard",
    help="onboard a rig end-to-end: clone it down (if --clone-url and absent), run rig init in "
    "the target, then sync the hub. Works for an already-local folder or a remote repo.",
)
def rig_onboard(
    rig_id: str = typer.Argument(..., metavar="PROVIDER/ORG/REPO"),
    clone_url: str = typer.Option(
        "", "--clone-url", help="clone URL — used only when the target dir is absent"
    ),
    prime: bool = typer.Option(False, "--prime", help="install .beads/PRIME.md (issue workflow)"),
    claude: bool = typer.Option(
        False, "--claude", help="install .claude/ settings + sandbox grant (see `rig init`)"
    ),
    skills: bool = typer.Option(
        False, "--skills", help="copy bundled role skills into ./skills (see `rig init`)"
    ),
    observaloop: bool = typer.Option(
        False, "--observaloop", help="stand up this rig's observaloop profile (see `rig init`)"
    ),
    agents: bool = typer.Option(
        False, "--agents", help="install an AGENTS.md AGF hint stanza (see `rig init`)"
    ),
    force: bool = typer.Option(
        False, "-f", "--force", help="re-register an already-configured rig (see `rig init`)"
    ),
    kind: str = typer.Option("", help="override: org-native|personal|prototype|fork"),
    prefix: str = typer.Option("", help="override the derived prefix"),
    yes: bool = typer.Option(
        False, "--yes",
        help="required to init a fork or to change a registered prefix (orphans bead IDs)",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="print the preflight plan (every check id) and change nothing"
    ),
    skip_check: str = typer.Option(
        "", "--skip-check",
        help="comma-separated preflight check id(s) to downgrade from failure to warning "
        "(overridable checks only, e.g. dirty-tree,on-default-branch); ids show under --dry-run",
    ),
):
    from . import config, rig

    # Same plugin-mode --claude --skills guard as rig init.
    if claude and skills:
        try:
            cfg = config.load()
        except Exception:
            cfg = {}
        if config.claude_source(cfg) == "plugin":
            typer.echo(
                "✗ --claude --skills conflict: in plugin mode the agf plugin already vends "
                "skills — drop --skills (or set claude.source: copy in ~/.ws/config.yaml to "
                "use the legacy copy path).",
                err=True,
            )
            raise typer.Exit(1)

    rig.onboard(
        rig_id,
        clone_url=clone_url,
        prime=prime,
        claude=claude,
        skills=skills,
        observaloop=observaloop,
        agents=agents,
        force=force,
        kind=kind,
        prefix=prefix,
        yes=yes,
        dry_run=dry_run,
        skip_check=skip_check,
    )


@rig_app.command(
    "ls", help="list registered rigs; --available lists discoverable repos not yet registered."
)
def rig_ls(
    available: bool = typer.Option(
        False,
        "--available",
        help="list discoverable-but-unregistered repos (diffs git-workspace's tracked repos "
        "from workspace-lock.toml against the registry — zero API calls)",
    ),
):
    from . import rig

    rig.ls(show_available=available)


@rig_app.command("ready", help="check whether this repo is set up for AGF (read-only).")
def rig_ready(
    verbose: bool = typer.Option(
        False, "-v", "--verbose", help="show the per-line-item breakdown (required + optional)"
    ),
):
    from . import rig_ready as ready

    ready.run_check(verbose)


@rig_app.command(
    "survey",
    help="fleet table for onboarding triage: one row per on-disk repo (read-only).",
)
def rig_survey(
    available: bool = typer.Option(
        False,
        "--available",
        help="show only unregistered candidate repos (those not yet `ws rig add`ed)",
    ),
    json_out: bool = typer.Option(
        False,
        "--json",
        help="emit machine-readable JSON (one object per repo)",
    ),
    sort: str = typer.Option(
        "",
        "--sort",
        help="sort rows by: disk | age | difficulty",
        show_default=False,
    ),
):
    from . import survey as survey_mod

    survey_mod.survey(available=available, json_out=json_out, sort=sort)


@rig_app.command("classify", help="classify a repo (helper).")
def rig_classify(provider: str, org: str, repo: str):
    typer.echo(registry.classify(provider, org, repo))


@rig_app.command("prefix", help="suggest a prefix for a repo (helper).")
def rig_prefix(provider: str, org: str, repo: str, kind: str = typer.Argument("")):
    pref, warns = registry.derive_prefix(provider, org, repo, kind)
    for w in warns:
        typer.echo(w, err=True)
    typer.echo(pref)


@rig_app.command(
    "enable",
    help="set <feature>.enabled = true on the rig's managed_repos entry (default: cwd's rig).",
)
def rig_enable(
    feature: str = typer.Argument(..., help="feature name, e.g. observaloop"),
    rig_id: str = typer.Argument("", help="rig id (default: cwd's rig)"),
):
    from . import worktree as wt_mod

    cfg = config.load()
    entry = wt_mod._resolve_entry(cfg, rig_id)
    res = config.set_rig_feature_flag(entry, feature, True)
    _echo_problems(res["problems"])
    if not res["ok"]:
        raise typer.Exit(1)
    prefix = str(entry.get("prefix", rig_id))
    config.save(cfg)
    typer.echo(f"✓ {prefix}: {feature}.enabled = true")


@rig_app.command(
    "disable",
    help="set <feature>.enabled = false on the rig's managed_repos entry (default: cwd's rig).",
)
def rig_disable(
    feature: str = typer.Argument(..., help="feature name, e.g. observaloop"),
    rig_id: str = typer.Argument("", help="rig id (default: cwd's rig)"),
):
    from . import worktree as wt_mod

    cfg = config.load()
    entry = wt_mod._resolve_entry(cfg, rig_id)
    res = config.set_rig_feature_flag(entry, feature, False)
    _echo_problems(res["problems"])
    if not res["ok"]:
        raise typer.Exit(1)
    prefix = str(entry.get("prefix", rig_id))
    config.save(cfg)
    typer.echo(f"✓ {prefix}: {feature}.enabled = false")


# ---- rig archive ------------------------------------------------------------

archive_app = typer.Typer(
    no_args_is_help=True,
    help="Inspect and reclaim the soft-archive graveyard (ws rig retire destinations).",
)
rig_app.add_typer(archive_app, name="archive")


@archive_app.command("ls", help="list archived repos with age and size.")
def archive_ls(
    json_out: bool = typer.Option(False, "--json", help="emit machine-readable JSON"),
):
    """List every ``<provider>/<org>/<repo>`` clone under ``archive.dir``.

    Shows age (days since archived, based on dir mtime) and size for each entry, plus a
    total. ``--json`` emits one object per repo with typed fields (age_days, size_bytes).
    """
    import json as json_mod

    from . import archive as archive_mod
    from .safety import format_bytes

    adir = config.archive_dir()
    repos = archive_mod.list_archived(adir)

    if json_out:
        out = [
            {
                "triplet": r.triplet,
                "age_days": r.age_days,
                "size_bytes": r.size_bytes,
            }
            for r in repos
        ]
        typer.echo(json_mod.dumps(out, indent=2))
        return

    if not repos:
        typer.echo(f"archive: {adir} (empty)")
        return

    total_bytes = sum(r.size_bytes for r in repos)
    col_w = max(len(r.triplet) for r in repos)
    typer.echo(f"archive: {adir}")
    typer.echo(f"  {'REPO':<{col_w}}  {'AGE':>8}  SIZE")
    for r in repos:
        age_label = f"{r.age_days:.0f}d"
        typer.echo(f"  {r.triplet:<{col_w}}  {age_label:>8}  {format_bytes(r.size_bytes)}")
    typer.echo(f"\n  total: {format_bytes(total_bytes)} across {len(repos)} repo(s)")


def _parse_older_than(value: str) -> float:
    """Parse an ``--older-than`` value like ``30d`` or ``30`` into a float (days)."""
    v = str(value).strip()
    if v.endswith("d"):
        v = v[:-1]
    try:
        return float(v)
    except ValueError as exc:
        raise typer.BadParameter(f"expected N or Nd (e.g. 30 or 30d), got {value!r}") from exc


@archive_app.command("prune", help="remove archived repos older than a threshold.")
def archive_prune(
    older_than: str = typer.Option(
        "",
        "--older-than",
        help="remove repos archived more than N days ago (e.g. 30 or 30d); "
        "default: archive.window_days from config",
    ),
    all_repos: bool = typer.Option(
        False, "--all", help="remove every archived repo regardless of age"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="preview what would be removed, mutating nothing"
    ),
):
    """Docker-``system-prune``-style reclamation of the archive graveyard.

    By default, removes archived repos whose age >= ``--older-than`` (defaulting to
    ``archive.window_days``, itself defaulting to 30 days). ``--all`` removes every archived
    repo. ``--dry-run`` previews the plan without mutating anything.

    Reports total bytes reclaimed (e.g. ``Reclaimed 1.2 GB across 3 repos``).
    """
    from . import archive as archive_mod
    from .safety import format_bytes

    cfg = config.load()
    adir = config.archive_dir(cfg)

    if older_than:
        days = _parse_older_than(older_than)
    else:
        days = float(config.archive_window_days(cfg))

    tag = "DRY-RUN " if dry_run else ""
    if all_repos:
        typer.echo(f"{tag}prune: removing ALL archived repos under {adir}")
    else:
        typer.echo(f"{tag}prune: removing repos archived more than {days:.0f}d ago under {adir}")

    result = archive_mod.prune_archived(
        adir, older_than_days=days, remove_all=all_repos, dry_run=dry_run
    )

    if not result.removed:
        typer.echo("  nothing to prune")
        return

    for triplet in result.removed:
        verb = "would remove" if dry_run else "removed"
        typer.echo(f"  {verb}: {triplet}")

    if dry_run:
        from .safety import format_bytes as _fb
        total = sum(
            r.size_bytes for r in archive_mod.list_archived(adir) if r.triplet in result.removed
        )
        typer.echo(f"\n  Would reclaim {_fb(total)} across {len(result.removed)} repo(s)")
    else:
        from . import metadata
        for triplet in result.removed:  # drop any lingering entry for a now-purged repo
            metadata.invalidate(cfg, triplet, reload=False)
        n = len(result.removed)
        typer.echo(f"\nReclaimed {format_bytes(result.reclaimed_bytes)} across {n} repo(s)")


# ---- worktree ---------------------------------------------------------------
# `ws worktree …` (short form: `ws wt`, registered as a hidden alias above).
# --rig/--bead/--branch are command-local: the global -a/-r routing flags apply only
# to the `bd`/`git` passthrough, not here.


@wt_app.command("add", help="create a managed worktree (off the rig's HEAD) + run init ops.")
def wt_add(
    rig: str = typer.Option("", "--rig", "-r", help="target rig (default: cwd's rig)"),
    bead: str = typer.Option("", "--bead", help="branch bead/<id>, leaf <id>"),
    branch: str = typer.Option("", "--branch", help="literal branch name (leaf = last segment)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="print plan, change nothing"),
):
    from . import worktree

    worktree.add(rig=rig, bead=bead, branch=branch, dry_run=dry_run)


@wt_app.command("list", help="list ws-managed worktrees (prefix / branch / path).")
def wt_list():
    from . import worktree

    worktree.list_cmd()


@wt_app.command("path", help="print the absolute path of a managed worktree (for scripts).")
def wt_path(
    ref: str = typer.Argument("", help="bead id, branch, or leaf"),
    bead: str = typer.Option("", "--bead", help="resolve by bead id"),
    rig: str = typer.Option("", "--rig", "-r", help="target rig (default: cwd's rig)"),
):
    from . import worktree

    target = bead or ref
    if not target:
        typer.echo("✗ give a <ref> or --bead <id>", err=True)
        raise typer.Exit(1)
    worktree.path_of(rig, target)


@wt_app.command("init", help="re-run init ops on an existing managed worktree.")
def wt_init(path: str):
    from . import worktree

    worktree.init_existing(path)


@wt_app.command("rm", help="remove one managed worktree.")
def wt_rm(
    ref: str = typer.Argument("", help="bead id, branch, or leaf"),
    bead: str = typer.Option("", "--bead", help="resolve by bead id"),
    rig: str = typer.Option("", "--rig", "-r", help="target rig (default: cwd's rig)"),
    force: bool = typer.Option(False, "--force", help="remove even if dirty"),
):
    from . import worktree

    target = bead or ref
    if not target:
        typer.echo("✗ give a <ref> or --bead <id>", err=True)
        raise typer.Exit(1)
    worktree.remove(rig, target, force=force)


@wt_app.command(
    "status",
    help=(
        "show per-worktree classification (SAFE / ACTIVE / DIRTY / …) for one rig or all rigs."
        " Repopulates fresh metadata before classifying — the pre-flight never uses stale data."
    ),
)
def wt_status(
    rig: str = typer.Option("", "--rig", "-r", help="target rig (default: cwd's rig or all rigs)"),
    as_json: bool = typer.Option(False, "--json", help="emit JSON array of WtStatus records"),
):
    from . import worktree

    worktree.status_cmd(rig=rig, as_json=as_json)


@wt_app.command("prune", help="remove ALL managed worktrees (or one rig's) + prune admin files.")
def wt_prune(rig: str = typer.Option("", "--rig", "-r", help="limit to one rig")):
    from . import worktree

    worktree.prune(rig=rig)


# ---- labels (registry) ------------------------------------------------------


@labels_app.command("validate", help="lint the rig/workspace DB against the registry.")
def labels_validate(
    enforce: bool = typer.Option(False, "--enforce", help="fail on any violation (default)"),
    advisory: bool = typer.Option(False, "--advisory", help="report only, always exit 0"),
):
    mode = "advisory" if advisory and not enforce else "enforce"
    validate.validate(mode)


@labels_app.command("sync", help="reconcile registry vs git-workspace.")
def labels_sync():
    registry.repos_sync()


@labels_app.command("report", help="usage report per dimension.")
def labels_report():
    registry.report()


@labels_app.command("allowed", help="print the allowed label set.")
def labels_allowed():
    registry.allowed()


@labels_app.command("docs", help="regenerate ~/.ws/labels.md from config.")
def labels_docs():
    registry.docs()


# ---- dolt -------------------------------------------------------------------


@dolt_app.command("up", help="start the container backend + compose + provision.")
def dolt_up():
    dolt.up()


@dolt_app.command("provision", help="wait for the app user + grant privileges.")
def dolt_provision():
    dolt.provision()


@dolt_app.command("down")
def dolt_down():
    dolt.down()


@dolt_app.command("logs")
def dolt_logs():
    dolt.logs()


@dolt_app.command("ps")
def dolt_ps():
    dolt.ps()


@dolt_app.command("sql")
def dolt_sql():
    dolt.sql()


# ---- otel -------------------------------------------------------------------


@otel_app.command("up", help="start grafana/otel-lgtm (Grafana + Collector + Loki/Tempo/Mimir).")
def otel_up():
    from . import otel_lgtm

    otel_lgtm.up()


@otel_app.command("down", help="stop the otel-lgtm stack.")
def otel_down():
    from . import otel_lgtm

    otel_lgtm.down()


@otel_app.command("logs", help="stream otel-lgtm container logs.")
def otel_logs():
    from . import otel_lgtm

    otel_lgtm.logs()


@otel_app.command("ps", help="show otel-lgtm service status.")
def otel_ps():
    from . import otel_lgtm

    otel_lgtm.ps()


@otel_app.command("enable", help="set otel.enabled = true in config.")
def otel_enable():
    res = config.set_value("otel.enabled", "true")
    _echo_problems(res["problems"])
    if not res["ok"]:
        raise typer.Exit(1)
    typer.echo("✓ otel.enabled = true")


@otel_app.command("disable", help="set otel.enabled = false in config.")
def otel_disable():
    res = config.set_value("otel.enabled", "false")
    _echo_problems(res["problems"])
    if not res["ok"]:
        raise typer.Exit(1)
    typer.echo("✓ otel.enabled = false")


@otel_app.command("endpoint", help="set otel.endpoint <url> in config.")
def otel_endpoint_cmd(
    url: str = typer.Argument(..., help="OTLP collector endpoint URL"),
):
    res = config.set_value("otel.endpoint", url)
    _echo_problems(res["problems"])
    if not res["ok"]:
        raise typer.Exit(1)
    typer.echo(f"✓ otel.endpoint = {url!r}")


# ---- observaloop ------------------------------------------------------------
# Mode 1: one shared profile per rig, provisioned by `ws worktree add` / `ws rig init
# --observaloop` and torn down explicitly by `ws observaloop down` when the rig is retired.
# Individual worktree removal / `ws worktree prune` NEVER tears down the rig profile.


def _observaloop_profile_name(cfg, entry) -> str:
    """Derive + return the current rig's observaloop profile name, or '' when underivable."""
    return config.observaloop_profile_name(cfg, entry)


@observaloop_app.command("status", help="show the current rig's observaloop profile status.")
def observaloop_status():
    """Report observaloop enabled/available state, the rig profile name, its up/down state, and
    the OTLP endpoint.  Read-only; best-effort — never raises, clear message when disabled or
    unavailable."""
    from . import observaloop as obs_mod
    from . import worktree

    cfg = config.load()
    entry = worktree._resolve_entry(cfg, "")
    name = _observaloop_profile_name(cfg, entry)
    if not name:
        typer.echo("✗ could not derive observaloop profile name for rig", err=True)
        raise typer.Exit(1)
    enabled = config.observaloop_enabled(cfg, entry)
    if not enabled:
        typer.echo(
            f"observaloop: enabled=no  profile={name}\n"
            "  → set observaloop.enabled=true and otel.enabled=true in config"
        )
        return
    available = obs_mod.is_available(cfg)
    if not available:
        typer.echo(
            f"observaloop: enabled=yes  available=no  profile={name}\n"
            "  → install the observaloop plugin or set observaloop.command in config"
        )
        return
    status = obs_mod.profile_status(name, cfg)
    endpoint = obs_mod.endpoint_for(name, config.otel_protocol(cfg), cfg)
    if endpoint:
        state = "up"
    elif status is not None:
        state = "down"
    else:
        state = "unknown"
    typer.echo("observaloop: enabled=yes  available=yes")
    typer.echo(f"profile:     {name}")
    typer.echo(f"state:       {state}")
    typer.echo(f"endpoint:    {endpoint or '(none)'}")


@observaloop_app.command("down", help="tear down the current rig's observaloop profile.")
def observaloop_down():
    """Tear down the shared rig observaloop profile (Mode 1 explicit retire).  Best-effort —
    never raises, clear message when disabled or unavailable."""
    from . import observaloop as obs_mod
    from . import worktree

    cfg = config.load()
    entry = worktree._resolve_entry(cfg, "")
    name = _observaloop_profile_name(cfg, entry)
    if not name:
        typer.echo("✗ could not derive observaloop profile name for rig", err=True)
        raise typer.Exit(1)
    enabled = config.observaloop_enabled(cfg, entry)
    if not enabled:
        typer.echo(f"observaloop: disabled — nothing to tear down (profile: {name})")
        return
    available = obs_mod.is_available(cfg)
    if not available:
        typer.echo(f"observaloop: unavailable — nothing to tear down (profile: {name})")
        return
    result = obs_mod.down(name, cfg)
    if result is None:
        typer.echo(
            f"⚠ could not stop profile '{name}' (adapter returned no data)", err=True
        )
    else:
        typer.echo(f"✓ profile '{name}' stopped")


# ---- config -----------------------------------------------------------------


@config_app.command("path", help="print the resolved config path.")
def config_path_cmd():
    typer.echo(config.config_path())


@config_app.command("show", help="pretty-print the resolved config (the doctor overview + extras).")
def config_show():
    from . import doctor

    doctor.show()


@config_app.command("init", help="scaffold ~/.ws from bundled templates.")
def config_init(force: bool = typer.Option(False, "--force", help="overwrite existing files")):
    config.home().mkdir(parents=True, exist_ok=True)
    pairs = [
        (config.template("config.example.yaml"), config.config_path()),
        (config.template("docker-compose.yml"), config.compose_file()),
        (config.template("docker-compose.otel.yml"), config.otel_compose_file()),
        (config.template("env.example"), config.home() / ".env.example"),
    ]
    for src, dst in pairs:
        if dst.exists() and not force:
            typer.echo(f"skip {dst} (exists)")
            continue
        shutil.copy(src, dst)
        typer.echo(f"wrote {dst}")
    typer.echo(f"✓ edit {config.config_path()} and copy .env.example → .env")


def _echo_value(value) -> None:
    """Print a config value for `config get`: bools as true/false, scalars verbatim, lists/maps
    as compact JSON so the output round-trips back through `config set --json`."""
    if isinstance(value, bool):
        typer.echo("true" if value else "false")
    elif isinstance(value, (str, int, float)):
        typer.echo(str(value))
    else:
        import json

        typer.echo(json.dumps(value, default=str))


def _echo_problems(problems) -> None:
    """Surface validation problems on stderr — `error` (rejects) and `warning` (proceeds)."""
    for p in problems:
        mark = "✗" if p["level"] == "error" else "⚠"
        typer.echo(f"{mark} {p['message']}", err=True)


@config_app.command("get", help="read a dotted config key (e.g. `ws config get otel.enabled`).")
def config_get(key: str = typer.Argument(..., help="dotted.key path into the config")):
    res = config.get_value(key)
    if not res["ok"]:
        _echo_problems(res["problems"])
        raise typer.Exit(1)
    _echo_value(res["value"])


@config_app.command("set", help="set a dotted config key (bool/int coercion; --json for maps).")
def config_set(
    key: str = typer.Argument(..., help="dotted.key path into the config"),
    value: str = typer.Argument(..., help="value (true|false→bool, integer→int, else string)"),
    as_json: bool = typer.Option(False, "--json", help="parse value as JSON (lists/maps/literals)"),
):
    res = config.set_value(key, value, as_json=as_json)
    _echo_problems(res["problems"])
    if not res["ok"]:
        raise typer.Exit(1)
    typer.echo(f"✓ {key} = {res['new']!r}")


@config_app.command("unset", help="delete a dotted config key (e.g. `ws config unset otel`).")
def config_unset(key: str = typer.Argument(..., help="dotted.key path into the config")):
    res = config.unset_value(key)
    if not res["ok"]:
        _echo_problems(res["problems"])
        raise typer.Exit(1)
    typer.echo(f"✓ unset {key}")


# ---- mcp ---------------------------------------------------------------------
# FastMCP stdio server (fastmcp is a core dependency of ws). ws.mcp imports fastmcp lazily, so
# wiring this subcommand never drags it into the main CLI import path.

#: The name used to register the ws server with Claude Code (the `<name>` arg passed
#: to `claude mcp add`). Kept as a constant so tests and the help text never drift.
MCP_SERVER_NAME = "ws"

#: The Claude Code MCP scope applied by default when running `ws mcp install`.
MCP_DEFAULT_SCOPE = "user"


def _build_claude_mcp_add_cmd(scope: str = MCP_DEFAULT_SCOPE) -> list[str]:
    """Return the argv list for `claude mcp add ws --scope <scope> -- ws mcp serve`.

    Pure (no I/O, no side effects): the install command calls this once and passes the
    result to subprocess so tests can assert the exact command without spawning a process.
    """
    return ["claude", "mcp", "add", MCP_SERVER_NAME, "--scope", scope, "--", "ws", "mcp", "serve"]


@mcp_app.command(
    "serve", help="run the ws MCP server over stdio (fastmcp is a core dependency of ws)."
)
def mcp_serve():
    from . import mcp as mcp_mod

    try:
        mcp_mod.serve()
    except mcp_mod.MCPUnavailable as exc:
        typer.echo(f"✗ {exc}", err=True)
        raise typer.Exit(1) from exc


@mcp_app.command(
    "install",
    help=(
        "Wire the ws MCP server into Claude Code (runs once, persists across rigs).\n\n"
        "Shells out to: claude mcp add ws --scope user -- ws mcp serve\n\n"
        "After registration, every Claude Code session sees the ws control-plane tools:\n"
        "rig_onboard, rig_add, config_set, rigs_status, rigs_available, plan_check."
    ),
)
def mcp_install(
    scope: str = typer.Option(
        MCP_DEFAULT_SCOPE,
        help="Claude Code MCP scope. Use 'user' (default) for all projects, 'local' for CWD only.",
    ),
):
    """Register the ws MCP server with Claude Code at the given scope.

    Equivalent to running manually:

        claude mcp add ws --scope user -- ws mcp serve

    Exits with an error and prints the manual command when the `claude` binary is not on PATH.
    """
    import subprocess

    claude_bin = shutil.which("claude")
    cmd = _build_claude_mcp_add_cmd(scope)

    if claude_bin is None:
        manual = " ".join(cmd)
        typer.echo(
            "✗ 'claude' binary not found on PATH — install Claude Code first.\n"
            f"  Once installed, run manually:\n\n    {manual}",
            err=True,
        )
        raise typer.Exit(1)

    result = subprocess.run(cmd, check=False)  # noqa: S603
    if result.returncode != 0:
        typer.echo(f"✗ 'claude mcp add' exited {result.returncode}", err=True)
        raise typer.Exit(result.returncode)
    typer.echo(f"✓ ws MCP server registered with Claude Code (scope={scope}).")


# ---- setup ------------------------------------------------------------------


@setup_app.command("check", help="probe post-ws deps and cache the result.")
def setup_check():
    from . import setup as setup_mod

    setup_mod.run_check()


@setup_app.command("show", help="report cached setup status without re-probing.")
def setup_show():
    from . import setup as setup_mod

    setup_mod.run_show()


# ---- top-level --------------------------------------------------------------


@app.command(
    "doctor",
    rich_help_panel=ADMIN_PANEL,
    help="status + diagnostics: providers, orgs, repo counts, warnings.",
)
def doctor_cmd():
    from . import doctor

    doctor.doctor()


@app.command("backup", rich_help_panel=ADMIN_PANEL, help="export issues to a JSONL mirror.")
def backup(dest: str = typer.Argument("./backup")):
    Path(dest).mkdir(parents=True, exist_ok=True)
    run(["bd", "export", "-o", f"{dest}/issues.jsonl", "--all"])
    typer.echo(f"exported → {dest}/issues.jsonl")


def _handle_cli_error(exc: Exception) -> None:
    """Boundary handler for an unhandled exception escaping a CLI command.

    Observes the failure across all three telemetry signals — a structlog ``cli_command_error``
    line (always, even otel-off), the active span (record_exception + ERROR status, no-op when
    off), and the ``ws.errors`` counter (no-op when off) — then surfaces a concise stderr line
    instead of a bare traceback. The non-zero exit is the caller's ``SystemExit(1)``.

    Only *genuine* unhandled exceptions reach here: control-flow exits (``typer.Exit`` codes,
    validation failures → ``SystemExit``) are re-raised untouched in ``main`` and never observed
    as errors. The dqw.2 invocation counter has already tagged this path outcome=error via
    ``call_on_close`` inside ``app()``, so ``count_error`` is additive, not a double-count."""
    command = next((arg for arg in sys.argv[1:] if not arg.startswith("-")), "")
    log.get_logger(__name__).error(
        "cli_command_error", command=command, error_type=type(exc).__name__, error=str(exc)
    )
    otel.record_exception(exc)
    otel.count_error("cli", type(exc).__name__)
    typer.echo(f"✗ {type(exc).__name__}: {exc}", err=True)


def main():
    try:
        app()
    except SystemExit:
        raise  # control-flow exit (typer.Exit codes, validation failures) — preserve verbatim
    except Exception as exc:  # genuine unhandled error: observe + clean surface + non-zero exit
        _handle_cli_error(exc)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
