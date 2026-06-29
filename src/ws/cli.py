"""ws CLI — Typer app wiring the operation groups together.

Surface: bd / git (passthrough + -a/-r routing) · rig · labels · sync · hub · dolt · doctor
· backup · config.
Heavy lifting is delegated to bd / dolt / git / gh / docker; ws encodes the
orchestration, registry/validation logic, and path-derived identity.
"""

from __future__ import annotations

import importlib.metadata
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
config_app = typer.Typer(no_args_is_help=True, help="ws config.")
mcp_app = typer.Typer(no_args_is_help=True, help="Model Context Protocol server (extra: ws[mcp]).")

app.add_typer(rig_app, name="rig", rich_help_panel=WORKSPACE_PANEL)
app.add_typer(labels_app, name="labels", rich_help_panel=WORKSPACE_PANEL)
app.add_typer(wt_app, name="worktree", rich_help_panel=WORKSPACE_PANEL)
app.add_typer(wt_app, name="wt", hidden=True)  # `ws wt` alias (hidden to avoid dup in help)
app.add_typer(work.app, name="work", rich_help_panel=WORKSPACE_PANEL)
app.add_typer(plan.app, name="plan", rich_help_panel=WORKSPACE_PANEL)
app.add_typer(dolt_app, name="dolt", rich_help_panel=ADMIN_PANEL)
app.add_typer(otel_app, name="otel", rich_help_panel=ADMIN_PANEL)
app.add_typer(config_app, name="config", rich_help_panel=ADMIN_PANEL)
app.add_typer(mcp_app, name="mcp", rich_help_panel=ADMIN_PANEL)


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
        otel.init(config.load())
    except Exception:  # best-effort telemetry; never break the CLI on init/config-load failure
        pass
    # Instrument the command-entry seam: register a call_on_close hook that emits a counter +
    # histogram tagged with the invoked subcommand name + outcome (ok/error). Gated on
    # is_active() so the off-path (default: otel disabled) is a single bool read — zero SDK
    # import, zero allocation. The --version eager path exits before this body, so it's untouched.
    if otel.is_active():
        _start = time.monotonic()
        _cmd = ctx.invoked_subcommand or ""

        def _record_invocation() -> None:
            otel.record_cli_invocation(
                _cmd, _outcome_from_exc(sys.exc_info()[1]), time.monotonic() - _start
            )

        ctx.call_on_close(_record_invocation)
    mode = "all" if all_rigs else "rig" if rig else "cwd"
    if mode != "cwd" and ctx.invoked_subcommand not in ("bd", "git"):
        typer.echo("✗ -a/--all and -r/--rig only apply to `ws bd` and `ws git`", err=True)
        raise typer.Exit(1)
    ctx.obj = (mode, rig)


# ---- workspace --------------------------------------------------------------


@app.command(
    "sync",
    rich_help_panel=WORKSPACE_PANEL,
    help="build/refresh the hub: add every registered rig (clone-cache uncloned ones) + sync.",
)
def sync_cmd():
    from . import hub

    hub.sync()


@app.command(
    "hub",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    add_help_option=False,
    rich_help_panel=WORKSPACE_PANEL,
    help="run a bd command against the aggregated hub (cross-rig view), e.g. `ws hub bd ready`.",
)
def hub_cmd(ctx: typer.Context):
    from . import hub

    args = ctx.args
    # allow either `ws hub bd ready` or `ws hub ready`
    if args and args[0] == "bd":
        args = args[1:]
    hub.query(args)


# ---- bd / git (passthrough) -------------------------------------------------


@app.command(
    "bd",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    add_help_option=False,
    rich_help_panel=PASSTHROUGH_PANEL,
    help="Passthrough to bd; `bd create` auto-applies provider/org/repo.",
)
def bd_passthrough(ctx: typer.Context):
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
    force: bool = typer.Option(
        False, "-f", "--force", help="overwrite existing PRIME.md / skills instead of skipping them"
    ),
    kind: str = typer.Option("", help="override: org-native|personal|prototype|fork"),
    prefix: str = typer.Option("", help="override the derived prefix"),
    yes: bool = typer.Option(False, "--yes", help="required to init a fork"),
    dry_run: bool = typer.Option(False, "--dry-run", help="print plan, change nothing"),
):
    from . import rig

    rig.init(
        prime=prime,
        claude=claude,
        skills=skills,
        force=force,
        kind=kind,
        prefix=prefix,
        yes=yes,
        dry_run=dry_run,
    )


@rig_app.command("classify", help="classify a repo (helper).")
def rig_classify(provider: str, org: str, repo: str):
    typer.echo(registry.classify(provider, org, repo))


@rig_app.command("prefix", help="suggest a prefix for a repo (helper).")
def rig_prefix(provider: str, org: str, repo: str, kind: str = typer.Argument("")):
    pref, warns = registry.derive_prefix(provider, org, repo, kind)
    for w in warns:
        typer.echo(w, err=True)
    typer.echo(pref)


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


# ---- mcp ---------------------------------------------------------------------
# Optional FastMCP stdio server (`ws[mcp]` extra). ws.mcp imports fastmcp lazily, so
# wiring this subcommand never drags the optional dep into the main CLI import path.


@mcp_app.command(
    "serve", help="run the ws MCP server over stdio (needs the `mcp` extra: ws[mcp])."
)
def mcp_serve():
    from . import mcp as mcp_mod

    try:
        mcp_mod.serve()
    except mcp_mod.MCPUnavailable as exc:
        typer.echo(f"✗ {exc}", err=True)
        raise typer.Exit(1) from exc


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
