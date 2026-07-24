"""ws CLI — Typer app wiring the operation groups together.

Surface: bd / git (passthrough + -a/-r routing) · hive · labels · sync · hub · dolt · doctor
· backup · config · setup.
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
from . import (
    config,
    config_schema,
    dolt,
    home_migration,
    log,
    otel,
    plan,
    plugins,
    registry,
    release,
    toolchain,
    validate,
    work,
)
from .run import run

app = typer.Typer(no_args_is_help=True, help="Workspace CLI.")

# Help panels — the 6-panel scheme reflecting the plane model (see
# docs/design/cli-mcp-naming-conventions-adr.md §5a), ordered by lifecycle.
PLANNING_PANEL = "Planning plane"
INTEGRATION_PANEL = "Integration plane"
HIVE_PANEL = "Hive"
FLEET_PANEL = "Fleet / HQ"
ADMIN_PANEL = "Admin / infra"
PASSTHROUGH_PANEL = "Passthrough"

hive_app = typer.Typer(no_args_is_help=True, help="Onboard repos as beads hives.")
label_app = typer.Typer(no_args_is_help=True, help="Registry: validate / sync / docs.")
wt_app = typer.Typer(no_args_is_help=True, help="Managed worktrees.")
dolt_app = typer.Typer(no_args_is_help=True, help="Optional Dolt SQL server.")
otel_app = typer.Typer(no_args_is_help=True, help="Local LGTM stack (grafana/otel-lgtm).")
plugin_app = typer.Typer(no_args_is_help=True, help="External-tool integrations (orca, ...).")
config_app = typer.Typer(no_args_is_help=True, help=f"{config.BINARY_ALIAS} config.")
mcp_app = typer.Typer(
    no_args_is_help=True,
    help=(
        f"Model Context Protocol server (fastmcp is a core dependency of "
        f"{config.BINARY_ALIAS}).\n\n"
        "Register with Claude Code at user scope (run once):\n\n"
        f"  claude mcp add {config.BINARY_ALIAS} --scope user -- "
        f"{config.BINARY_ALIAS} mcp serve\n\n"
        f"Or use the convenience verb: {config.BINARY_ALIAS} mcp install"
    ),
)
hq_app = typer.Typer(
    no_args_is_help=True, help="Factory HQ: the durable central store (kind=hq singleton)."
)
setup_app = typer.Typer(no_args_is_help=True, help="Post-install dependency check + cached gate.")
contrib_app = typer.Typer(
    no_args_is_help=True,
    help="Contribution plane: the contributor seat's outbound editor (upstream issues).",
)
contrib_profile_app = typer.Typer(
    no_args_is_help=True,
    help="Contribution dossier: build/show an external upstream's contribution profile (go/no-go).",
)

app.add_typer(setup_app, name="setup", rich_help_panel=ADMIN_PANEL)
app.add_typer(contrib_app, name="contrib", rich_help_panel=INTEGRATION_PANEL)
app.add_typer(hive_app, name="hive", rich_help_panel=HIVE_PANEL)
app.add_typer(hq_app, name="hq", rich_help_panel=FLEET_PANEL)
app.add_typer(label_app, name="label", rich_help_panel=HIVE_PANEL)
app.add_typer(toolchain.app, name="toolchain", rich_help_panel=HIVE_PANEL)
app.add_typer(wt_app, name="worktree", rich_help_panel=INTEGRATION_PANEL)
app.add_typer(wt_app, name="wt", hidden=True)  # `bh wt` alias (hidden to avoid dup in help)
app.add_typer(work.app, name="work", rich_help_panel=INTEGRATION_PANEL)
app.add_typer(plan.app, name="plan", rich_help_panel=PLANNING_PANEL)
app.add_typer(release.app, name="release", rich_help_panel=INTEGRATION_PANEL)
app.add_typer(dolt_app, name="dolt", hidden=True)  # deprecation-track: off all panels
app.add_typer(otel_app, name="otel", hidden=True)  # deprecation-track: off all panels
app.add_typer(plugin_app, name="plugin", rich_help_panel=ADMIN_PANEL)
app.add_typer(config_app, name="config", rich_help_panel=ADMIN_PANEL)
app.add_typer(mcp_app, name="mcp", rich_help_panel=ADMIN_PANEL)
hive_app.add_typer(contrib_profile_app, name="contrib-profile")

# Mount each registered plugin's own Typer sub-app: `bh plugin <name> …` (e.g.
# `bh plugin orca sync`). Generic — new integrations appear here just by joining the registry.
for _plugin in plugins.registry():
    plugin_app.add_typer(_plugin.cli, name=_plugin.name)

# Module-level singleton for the repeatable `--plugin` option — an inline `list[str]` default
# would trip ruff B008 (mutable-literal in a default call); shared by hive init + hive onboard.
_PLUGIN_OPT = typer.Option(
    [],
    "--plugin",
    help="enable a plugin integration for this hive (repeatable), e.g. --plugin orca. "
    "Runs the plugin's onboard hook regardless of its config flag.",
)

# Shared hive-id positional for the contribution-plane verbs (module singleton — same idiom as
# _PLUGIN_OPT; a typer.Argument default cannot be re-inlined per-command without B008).
_CONTRIB_HIVE_ARG = typer.Argument(
    ..., metavar="HIVE", help="external hive (prefix / triplet / org-repo)"
)


# ---- help / shell-completion detection ----------------------------------------


def _is_help_or_completion_invocation(ctx: typer.Context) -> bool:
    """True when this invocation is purely informational — a `--help`/`-h` pass or
    shell-completion — and must never trigger a gate or a diagnostic side effect.

    ``ctx.resilient_parsing`` is Click's own signal that it's generating shell completions
    (set while it walks the command tree without executing anything). `--help`/`-h` doesn't
    set it: for `bh <cmd> --help`, Click invokes this group callback FIRST (to resolve the
    subcommand), then the subcommand's own eager `--help` option short-circuits before that
    subcommand's body runs — so by the time `--help` exits, this group callback has already
    fired. Detected the same way `_handle_cli_error` extracts the invoked verb: scanning raw
    ``sys.argv`` (cli.py:1831 precedent).
    """
    if ctx.resilient_parsing:
        return True
    return any(arg in ("--help", "-h") for arg in sys.argv[1:])


# ---- setup gate ---------------------------------------------------------------

# Subcommands exempt from the setup-complete gate.  The gate guards every OTHER
# verb: a fresh install that has never run `ws setup check` must still be able
# to bootstrap (config init), diagnose itself (doctor), or run setup check itself.
# Top-level --version/--help never reach the gate (eager callback + typer exit before
# body); a subcommand's `--help`/`-h` and shell-completion DO reach this callback (the
# subcommand's own eager --help short-circuits only after this group callback runs), so
# _root skips the call entirely for those via _is_help_or_completion_invocation.
_SETUP_GATE_ALLOW: frozenset[str] = frozenset({"setup", "config", "doctor"})


def _enforce_setup_gate(ctx: typer.Context) -> None:
    """Gate every verb not in _SETUP_GATE_ALLOW behind a passing setup cache.

    Bypass entirely when:
    - ``BH_SKIP_SETUP_CHECK`` (or the deprecated ``WS_SKIP_SETUP_CHECK``) is truthy (debug
      escape hatch)
    - the invoked subcommand is in the allow-list or is None (no subcommand)
    - the setup cache exists with ``setup == true``

    Denied verbs surface a clear "run bh setup check" message on stderr and exit 1.
    """
    if config.skip_setup_check():
        return
    subcmd = ctx.invoked_subcommand
    if subcmd is None or subcmd in _SETUP_GATE_ALLOW:
        return
    from . import setup as setup_mod  # lazy: avoids import at module load

    if not setup_mod.is_setup_complete():
        typer.echo(
            f"✗ `{config.BINARY_ALIAS} {subcmd}` requires setup — "
            f"run `{config.BINARY_ALIAS} setup check` first.\n"
            "  Skip with BH_SKIP_SETUP_CHECK=1 (debug bypass).",
            err=True,
        )
        raise typer.Exit(1)


# ---- root: global hive-routing flags -----------------------------------------


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
        typer.echo(importlib.metadata.version("beadhive"))
        raise typer.Exit()


@app.callback()
def _root(
    ctx: typer.Context,
    all_hives: bool = typer.Option(
        False, "-a", "--all", help="route the passthrough across ALL registered hives"
    ),
    hive: str = typer.Option(
        None, "--hive", help="route the passthrough to one hive (see hive_match)"
    ),
    version: bool = typer.Option(
        False, "--version", "-V", callback=_version, is_eager=True, help="show version and exit"
    ),
):
    """Workspace beads CLI. -a/-r route `bd`/`git` across hives (need git_workspace)."""
    # One-time ~/.ws -> ~/.beadhive migration: deliberately placed here, not
    # inside config.home(), so a plain config read/import (tests, MCP tools, library callers)
    # never has the side effect of moving real state on disk — only an actual `bh <command>`
    # invocation does. Best-effort: a migration failure must never block the CLI.
    try:
        home_migration.migrate_home_if_needed()
    except Exception:
        pass
    # One-time otel.rig/git_workspace.rig_match -> otel.hive/git_workspace.hive_match config-key
    # migration (bh-41rh hard cutover): same placement rule as the home-dir migration above.
    try:
        config.migrate_hive_keys_if_needed()
    except Exception:
        pass
    # Lightest schema_version staleness nudge (bh-5cgm.3): NOT a migration — never rewrites
    # the config, just warns once when it predates the current schema. Same placement rule
    # as the migrations above: a real CLI invocation only, never a bare load()/getter. Skipped
    # entirely for `--help`/`-h` and shell-completion (bh-sn9q): those are informational-only
    # passes and must never emit diagnostic noise, even to stderr.
    if not _is_help_or_completion_invocation(ctx):
        try:
            config.warn_stale_schema_version_if_needed()
        except Exception:
            pass
    # Eager telemetry init: this callback runs before every subcommand, so it's the one place
    # that activates OTel for a real `ws` command path (otherwise is_active() is forever False
    # and every emitter is inert). It's cheap + safe when off: init() no-ops fast on the default
    # (otel.enabled false) and never imports opentelemetry on that path. Telemetry is best-effort
    # and must never block the CLI — a missing/unreadable config (e.g. before `bh config init`)
    # degrades to telemetry-off rather than erroring. The eager `--version` path exits before
    # this body, so it stays untouched.
    try:
        _cfg = config.load()
        # Per-worktree endpoint overlay: if cwd is a managed worktree with a `.bh/otel.env` cache,
        # load it into os.environ BEFORE init so config.otel_endpoint / config.observaloop_profile
        # pick up the hive profile's endpoint + name. The common path is a single file read with no
        # beadhive.observaloop import (only the self-heal branch touches observaloop);
        # best-effort, so it never blocks startup. observaloop_env imports config + worktree
        # only — not observaloop.
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
        _cli_span_cm = otel.span(f"bh.cli {_cmd}", {"bh.cli.command": _cmd})
        _cli_span = _cli_span_cm.__enter__()

        def _record_invocation() -> None:
            exc = sys.exc_info()[1]
            outcome = _outcome_from_exc(exc)
            _cli_span.set_attribute("bh.cli.outcome", outcome)
            # Pass exc only for real errors — clean-exit control flow (Exit(0), SystemExit(0))
            # must not mark the span ERROR.
            if outcome == "error" and exc is not None:
                _cli_span_cm.__exit__(type(exc), exc, exc.__traceback__)
            else:
                _cli_span_cm.__exit__(None, None, None)
            otel.record_cli_invocation(_cmd, outcome, time.monotonic() - _start)

        ctx.call_on_close(_record_invocation)
    # Same informational-only exemption as the schema-staleness nudge above (bh-sn9q): a
    # subcommand's `--help`/`-h` or shell-completion must never be blocked by the setup gate
    # (it would otherwise swallow the help text entirely on a fresh, ungated install).
    if not _is_help_or_completion_invocation(ctx):
        _enforce_setup_gate(ctx)
    mode = "all" if all_hives else "hive" if hive else "cwd"
    if mode != "cwd" and ctx.invoked_subcommand not in ("bd", "git"):
        typer.echo(
            f"✗ -a/--all and --hive only apply to `{config.BINARY_ALIAS} bd` "
            f"and `{config.BINARY_ALIAS} git`",
            err=True,
        )
        raise typer.Exit(1)
    ctx.obj = (mode, hive)


# ---- workspace --------------------------------------------------------------


@app.command(
    "role",
    rich_help_panel=FLEET_PANEL,
    help=f"launch claude in a seat role (e.g. `{config.BINARY_ALIAS} role developer`); "
    "no arg → list seats.",
)
def role_cmd(
    name: str = typer.Argument("", help="seat role to launch (e.g. developer, dispatcher)"),
    harness: str = typer.Option(
        "", "--harness", help="harness to exec (claude|opencode); overrides config."
    ),
):
    from . import role as role_mod

    role_mod.launch(name, harness=harness or None)


@app.command("statusline", hidden=True, help="print role/hive statusline from stdin JSON (TUI).")
def statusline_cmd():
    from . import role as role_mod

    role_mod.statusline()


@app.command(
    "sync",
    rich_help_panel=FLEET_PANEL,
    help="build/refresh the hub: add every registered hive (clone-cache uncloned ones) + sync.",
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
    rich_help_panel=FLEET_PANEL,
    help=f"[DEPRECATED] use `{config.BINARY_ALIAS} hq` instead. "
    "Query the aggregated hub (cross-hive view).",
)
def hub_cmd(ctx: typer.Context):
    typer.echo(
        f"⚠ `{config.BINARY_ALIAS} hub` is deprecated — use `{config.BINARY_ALIAS} hq` instead.",
        err=True,
    )
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
    help="fleet-wide untriaged-intake inbox: superintendent's cross-hive view (hub.intake).",
)
def hq_intake_cmd(ctx: typer.Context):
    from . import hub

    hub.intake(ctx.args)


@hq_app.command(
    "bd",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    add_help_option=False,
    help="run a bd command against the HQ aggregate (cross-hive view), "
    f"e.g. `{config.BINARY_ALIAS} hq bd ready`.",
)
def hq_bd_cmd(ctx: typer.Context):
    from . import hub

    hub.query(ctx.args)


@app.command(
    "report",
    rich_help_panel=FLEET_PANEL,
    help="file a bug/feature/chore into a hive we own; lands as untriaged intake for triage.",
)
def report_cmd(
    hive: str = typer.Argument(
        ..., metavar="HIVE", help="target hive (prefix / triplet / org-repo)"
    ),
    title: str = typer.Argument(..., metavar="TITLE", help="report title"),
    report_type: str = typer.Option(
        "bug", "--type", "-t", metavar="TYPE", help="report type: bug | feature | chore"
    ),
    as_actor: str = typer.Option(
        "", "--as", metavar="ACTOR", help="reporting seat/human (stamped as bd --actor)"
    ),
    description: str = typer.Option(
        "", "--description", "-m", help="report body/description (or piped via stdin)"
    ),
):
    from . import report as report_mod
    from .identity import resolve_actor

    actor = resolve_actor(as_actor)
    if not description and not sys.stdin.isatty():
        description = sys.stdin.read()
    code, error, new_id = report_mod.file_report(
        hive, title, report_type, actor, description=description
    )
    if error:
        typer.echo(f"✗ {error}", err=True)
        raise typer.Exit(code)
    typer.echo(f"✓ filed {new_id} into '{hive}' as intake ({report_type}) — reported by {actor}")
    # Dedup on ENTRY: surface likely dupes so a colliding feature request is caught before it
    # buries the queue (the triage side runs the same `bd find-duplicates` pass). Best-effort.
    for pair in report_mod.entry_dupes(hive, new_id):
        other = (
            pair.get("issue_b_id") if pair.get("issue_a_id") == new_id else pair.get("issue_a_id")
        )
        typer.echo(f"  ⚠ likely duplicate of {other} — triage may reject/reroute this")


@app.command(
    "report-target",
    rich_help_panel=FLEET_PANEL,
    help=f"emit {config.BINARY_ALIAS}'s own report-channel descriptor "
    f"(where to file {config.BINARY_ALIAS} issues).",
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
    rich_help_panel=FLEET_PANEL,
    help=(
        "fire-and-forget escalation to HQ: name a tool problem, hand it up, and never block."
        f" Offers to run '{config.BINARY_ALIAS} hq init' when no HQ exists yet."
    ),
)
def escalate_cmd(
    title: str = typer.Argument(..., metavar="TITLE", help="short description of the problem"),
    tool: str = typer.Option(
        "", "--tool", metavar="TOOL", help="name of the tool or verb that triggered the escalation"
    ),
    as_seat: str = typer.Option(
        "",
        "--as",
        metavar="SEAT",
        help="raiser's seat/crew (e.g. crew/dev1); defaults to $BH_DEV",
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


# ---- contribution plane: dossier + outbound editor --------------------------


@contrib_profile_app.command(
    "build",
    help="build/refresh the contribution dossier for an external upstream and store it "
    "(four layers → explicit go/no-go + authorship strategy).",
)
def contrib_profile_build(
    hive: str = _CONTRIB_HIVE_ARG,
):
    from . import contributor

    dossier = contributor.build_dossier(hive)
    contributor.store_dossier(dossier)
    typer.echo(contributor.render_dossier(dossier))


@contrib_profile_app.command(
    "show",
    help="render the stored contribution dossier for an external upstream (build it if absent).",
)
def contrib_profile_show(
    hive: str = _CONTRIB_HIVE_ARG,
    as_json: bool = typer.Option(False, "--json", help="emit the dossier as JSON"),
):
    import json
    from dataclasses import asdict

    from . import config as config_mod
    from . import contributor, registry

    entry = registry.resolve_hive(config_mod.load(), hive)
    dossier = contributor.load_dossier(registry.hive_key(entry))
    if dossier is None:
        typer.echo(
            f"✗ no stored dossier for '{hive}' — run "
            f"`{config.BINARY_ALIAS} hive contrib-profile build {hive}`",
            err=True,
        )
        raise typer.Exit(1)
    if as_json:
        typer.echo(json.dumps(asdict(dossier)))
        return
    stale = contributor.is_stale(dossier)
    typer.echo(contributor.render_dossier(dossier))
    if stale:
        typer.echo(
            f"\n⚠ dossier is stale — refresh with "
            f"`{config.BINARY_ALIAS} hive contrib-profile build {hive}`"
        )


@contrib_app.command(
    "outbound",
    help="the contributor's outbound editor: list the external hive's outbound:pending queue and "
    "the bd find-duplicates pairs touching it (aggregate related items before publish).",
)
def contrib_outbound(
    hive: str = _CONTRIB_HIVE_ARG,
    as_json: bool = typer.Option(False, "--json", help="emit {rows, dupes} as JSON"),
):
    import json

    from . import config as config_mod
    from . import contributor, registry, report

    cfg = config_mod.load()
    entry = registry.resolve_hive(cfg, hive)
    target, _pushed = report._target(cfg, entry)
    if target is None:
        typer.echo(
            f"✗ external hive '{hive}' is not cloned and has no remote beads data to read", err=True
        )
        raise typer.Exit(1)
    payload = contributor.outbound_queue(target)
    rows, dupes = payload["rows"], payload["dupes"]
    if as_json:
        typer.echo(json.dumps(payload))
        return
    if not rows:
        typer.echo(f"✓ no outbound:pending candidates for '{hive}' — the queue is clear")
        return
    typer.echo(f"outbound:pending for '{hive}': {len(rows)}")
    for r in rows:
        note = _dupe_note(dupes, r.get("id"))
        typer.echo(f"  {r.get('id')}  [{r.get('issue_type', '?')}]  {r.get('title', '')}{note}")
    typer.echo(
        "  curate → open the human publication gate, then "
        f"`{config.BINARY_ALIAS} contrib publish {hive} <id>` (after a human resolves the gate)"
    )


def _dupe_note(pairs, bead_id) -> str:
    """A ' ⚠ likely dup of <ids>' suffix for a bead the dedupe pass flags ('' when none)."""
    others = []
    for p in pairs:
        if p.get("issue_a_id") == bead_id:
            others.append(p.get("issue_b_id"))
        elif p.get("issue_b_id") == bead_id:
            others.append(p.get("issue_a_id"))
    others = [o for o in others if o]
    return f"  ⚠ likely dup of {', '.join(others)}" if others else ""


@contrib_app.command(
    "publish",
    help="file ONE curated outbound bead upstream via the gated single-item path — refuses a "
    "non-contributor seat, a dirty/multi-item push, or an ungated push; flips to publish=approved.",
)
def contrib_publish(
    hive: str = _CONTRIB_HIVE_ARG,
    bead: str = typer.Argument(..., metavar="BEAD", help="the outbound:pending bead to file"),
    external_ref: str = typer.Option(
        "", "--external-ref", metavar="GH_REF", help="the filed issue ref to stamp (e.g. gh-42)"
    ),
    as_seat: str = typer.Option(
        "", "--as", metavar="SEAT", help="contributor seat (contrib/<name>); defaults to $BH_DEV"
    ),
):
    from . import config as config_mod
    from . import contributor, registry, report
    from .identity import resolve_actor

    cfg = config_mod.load()
    entry = registry.resolve_hive(cfg, hive)
    target, _pushed = report._target(cfg, entry)
    if target is None:
        typer.echo(
            f"✗ external hive '{hive}' is not cloned and has no remote beads data to read", err=True
        )
        raise typer.Exit(1)
    actor = resolve_actor(as_seat)
    code, error, message = contributor.publish(target, bead, actor, external_ref=external_ref)
    if error:
        typer.echo(f"✗ {error}", err=True)
        raise typer.Exit(code)
    typer.echo(message)


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
            f"✗ `{config.BINARY_ALIAS} bd` passthrough is disabled "
            "(default off; passthrough.bd_enabled).\n"
            f"  Read beads with `{config.BINARY_ALIAS} work ready|issue|list`; "
            f"file plans with `{config.BINARY_ALIAS} plan file`;\n"
            f"  drive beads with `{config.BINARY_ALIAS} work`. "
            "Set BH_BD_PASS_ENABLED=1 (or BH_DEBUG=1) to override.",
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
    help="Passthrough to git (incl. git workspace). "
    f"`{config.BINARY_ALIAS} git workspace --help` → git-workspace.",
)
def git_passthrough(ctx: typer.Context):
    if not config.git_pass_enabled():
        otel.count_passthrough("git", allowed=False)
        typer.echo(
            f"✗ `{config.BINARY_ALIAS} git` passthrough is disabled "
            "(passthrough.git_enabled=false).\n"
            "  Set BH_GIT_PASS_ENABLED=1 (or BH_DEBUG=1) to override.",
            err=True,
        )
        raise typer.Exit(1)
    otel.count_passthrough("git", allowed=True)
    from . import git as git_mod

    mode, target = ctx.obj or ("cwd", None)
    git_mod.passthrough(mode, target, ctx.args)


# ---- hive --------------------------------------------------------------------


@hive_app.command("init")
def hive_init(
    furnish: bool = typer.Option(
        None,
        "--furnish/--no-furnish",
        help="declare tracked in-repo AGF furniture (scaffolding committed to history) — an "
        "ownership-gated, per-hive opt-in; default is zero-footprint (nothing tracked, "
        "nothing committed). --claude/--agents/--skills imply --furnish.",
    ),
    claude: bool = typer.Option(
        False,
        "--claude",
        help="install .claude/ settings: shared settings.json (SessionStart hook + "
        "bd-remember deny) + a host-local settings.local.json sandbox grant for this "
        "hive's worktree subtree",
    ),
    skills: bool = typer.Option(
        False,
        "--skills",
        help="copy bundled role skills into ./skills; with --claude also symlink .claude/skills",
    ),
    observaloop: bool = typer.Option(
        False,
        "--observaloop",
        help="stand up this hive's observaloop profile (ensure+up) and apply the "
        f"{config.BINARY_ALIAS} Grafana telemetry dashboard; best-effort — warns + continues "
        "when observaloop/docker/the visualizer is absent or otel is off",
    ),
    agents: bool = typer.Option(
        False,
        "--agents",
        help="install an AGENTS.md AGF hint stanza (points harnesses at `bh hive ready`); "
        "with --claude the same stanza is added to CLAUDE.md. Non-destructive "
        "(managed marked block); -f refreshes an existing block",
    ),
    opencode: bool = typer.Option(
        False,
        "--opencode",
        help="furnish for OpenCode: opencode.json (bh MCP server + permission rules "
        "auto-allowing read-only bd/bh + bh-mcp calls), translated seat agent defs under "
        ".opencode/agents/, a global skills install (~/.config/opencode/skills/), the "
        "bd-steer plugin under .opencode/plugins/ (steers raw `bd` to `bh bd`), and the "
        "AGENTS.md AGF hint stanza",
    ),
    force: bool = typer.Option(
        False,
        "-f",
        "--force",
        help="re-register an already-configured hive (re-classify kind; the registered "
        "prefix is preserved) and overwrite existing skills instead of "
        "preserving/skipping them",
    ),
    kind: str = typer.Option("", help="override: org-native|personal|prototype|fork|external"),
    prefix: str = typer.Option("", help="override the derived prefix"),
    yes: bool = typer.Option(
        False,
        "--yes",
        help="required to init a fork or to change a registered prefix (orphans bead IDs)",
    ),
    plugin: list[str] = _PLUGIN_OPT,
    dry_run: bool = typer.Option(False, "--dry-run", help="print plan, change nothing"),
    skip_check: str = typer.Option(
        "",
        "--skip-check",
        help="comma-separated preflight check id(s) to downgrade from failure to warning "
        "(overridable checks only, e.g. dirty-tree,on-default-branch); ids show under --dry-run",
    ),
):
    from . import config, hive

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
                "skills — drop --skills (or set claude.source: copy in ~/.beadhive/config.yaml to "
                "use the legacy copy path).",
                err=True,
            )
            raise typer.Exit(1)

    hive.init(
        furnish=furnish,
        claude=claude,
        skills=skills,
        observaloop=observaloop,
        agents=agents,
        opencode=opencode,
        plugins=plugin,
        force=force,
        kind=kind,
        prefix=prefix,
        yes=yes,
        dry_run=dry_run,
        skip_check=skip_check,
    )


@hive_app.command("add", help="register a hive from a provider/org/repo triplet (no cwd/bd init).")
def hive_add(
    hive_id: str = typer.Argument(..., metavar="PROVIDER/ORG/REPO"),
    prefix: str = typer.Option("", help="override the derived prefix"),
    kind: str = typer.Option("", help="org-native|personal|prototype|fork|external"),
    upstream: str = typer.Option("", help="upstream org/repo (for forks)"),
):
    from . import hive

    hive.add(hive_id, prefix=prefix, kind=kind, upstream=upstream)


@hive_app.command("rm", help="unregister a hive by id (registry-only; leaves .beads/repo intact).")
def hive_rm(hive_id: str = typer.Argument(..., metavar="HIVE_ID")):
    from . import hive

    hive.rm(hive_id)


@hive_app.command(
    "retire",
    help="guarded teardown of a hive: assess → (backup|consent) → worktree teardown → "
    "unregister → soft-archive the clone. Refuses to lose unbacked work without --backup or "
    "--confirm. --dry-run previews the full plan with zero mutation; --purge hard-deletes the "
    "clone instead of archiving it (still gated).",
)
def hive_retire(
    hive_id: str = typer.Argument(..., metavar="HIVE_ID"),
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

    retire.retire_hive(hive_id, dry_run=dry_run, backup=backup, confirm=confirm, purge=purge)


@hive_app.command(
    "sync-remote",
    help="guarded fleet-wide push+verify before switching physical hosts: scan every registered "
    "hive (git + dolt-ref-aware), report clean/dirty/unpushed-git/unpushed-dolt/blocked, and push "
    "what's safe. Refuses to push over a dirty working tree; --dry-run reports only, with zero "
    "mutation. Exits non-zero and lists offending hives if any hive can't be safely synced.",
)
def hive_sync_remote(
    all_hives: bool = typer.Option(
        False, "--all", help="required today (single-hive targeting is a future extension)"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="print the per-hive plan and change nothing (default-safe)"
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        help="for hives classified unpushed-dolt (embedded engine, dolt_status 'unknown'), "
        "also print a bounded list of recently-updated beads (bd list --updated-after, last "
        "24h) as approximate context — not a precise unpushed diff. Default output unchanged.",
    ),
):
    from . import sync_remote

    if not all_hives:
        typer.echo("✗ pass --all (sync-remote targets the whole fleet)", err=True)
        raise typer.Exit(1)

    plan = sync_remote.sync_remote(dry_run=dry_run, verbose=verbose)
    if plan.offending:
        raise typer.Exit(1)


@hive_app.command(
    "onboard",
    help="onboard a hive end-to-end: clone it down (if --clone-url and absent), run hive init in "
    "the target, then sync the hub. Works for an already-local folder or a remote repo.",
)
def hive_onboard(
    hive_id: str = typer.Argument(..., metavar="PROVIDER/ORG/REPO"),
    clone_url: str = typer.Option(
        "", "--clone-url", help="clone URL — used only when the target dir is absent"
    ),
    furnish: bool = typer.Option(
        None,
        "--furnish/--no-furnish",
        help="declare tracked in-repo AGF furniture (see `hive init`); default zero-footprint",
    ),
    claude: bool = typer.Option(
        False, "--claude", help="install .claude/ settings + sandbox grant (see `hive init`)"
    ),
    skills: bool = typer.Option(
        False, "--skills", help="copy bundled role skills into ./skills (see `hive init`)"
    ),
    observaloop: bool = typer.Option(
        False, "--observaloop", help="stand up this hive's observaloop profile (see `hive init`)"
    ),
    agents: bool = typer.Option(
        False, "--agents", help="install an AGENTS.md AGF hint stanza (see `hive init`)"
    ),
    opencode: bool = typer.Option(
        False, "--opencode", help="furnish for OpenCode (see `hive init`)"
    ),
    force: bool = typer.Option(
        False, "-f", "--force", help="re-register an already-configured hive (see `hive init`)"
    ),
    kind: str = typer.Option("", help="override: org-native|personal|prototype|fork|external"),
    prefix: str = typer.Option("", help="override the derived prefix"),
    yes: bool = typer.Option(
        False,
        "--yes",
        help="required to init a fork or to change a registered prefix (orphans bead IDs)",
    ),
    plugin: list[str] = _PLUGIN_OPT,
    dry_run: bool = typer.Option(
        False, "--dry-run", help="print the preflight plan (every check id) and change nothing"
    ),
    skip_check: str = typer.Option(
        "",
        "--skip-check",
        help="comma-separated preflight check id(s) to downgrade from failure to warning "
        "(overridable checks only, e.g. dirty-tree,on-default-branch); ids show under --dry-run",
    ),
):
    from . import config, hive

    # Same plugin-mode --claude --skills guard as hive init.
    if claude and skills:
        try:
            cfg = config.load()
        except Exception:
            cfg = {}
        if config.claude_source(cfg) == "plugin":
            typer.echo(
                "✗ --claude --skills conflict: in plugin mode the agf plugin already vends "
                "skills — drop --skills (or set claude.source: copy in ~/.beadhive/config.yaml to "
                "use the legacy copy path).",
                err=True,
            )
            raise typer.Exit(1)

    hive.onboard(
        hive_id,
        clone_url=clone_url,
        furnish=furnish,
        claude=claude,
        skills=skills,
        observaloop=observaloop,
        agents=agents,
        opencode=opencode,
        plugins=plugin,
        force=force,
        kind=kind,
        prefix=prefix,
        yes=yes,
        dry_run=dry_run,
        skip_check=skip_check,
    )


@hive_app.command(
    "list", help="list registered hives; --available lists discoverable repos not yet registered."
)
def hive_list(
    available: bool = typer.Option(
        False,
        "--available",
        help="list discoverable-but-unregistered repos (diffs git-workspace's tracked repos "
        "from workspace-lock.toml against the registry — zero API calls)",
    ),
):
    from . import hive

    hive.ls(show_available=available)


@hive_app.command(
    "status",
    help="fleet health: prefix collisions, required-org violations, unregistered candidates, "
    "and the registered-hive table (--hive narrows to one hive).",
)
def hive_status(
    hive_id: str = typer.Option(
        "", "--hive", help="narrow the hive table to one hive (default: all)"
    ),
    as_json: bool = typer.Option(False, "--json", help="emit the status payload as JSON"),
):
    from . import hive

    hive.status(hive_id=hive_id, as_json=as_json)


@hive_app.command(
    "migrate",
    help="upgrade already-onboarded managed repos onto the current bh command name: rewrite "
    "AGENTS.md/CLAUDE.md AGF hint + marker, .claude/settings.json hooks, .claude/agents/, "
    "legacy .beads/PRIME.md, and bundled skills/. Idempotent; --dry-run shows the diff and "
    "changes nothing.",
)
def hive_migrate(
    hive_id: str = typer.Argument("", help="hive id to migrate (default: every registered hive)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="show the diff, change nothing"),
):
    from . import hive_migrate as hive_migrate_mod

    hive_migrate_mod.migrate(dry_run=dry_run, hive_id=hive_id)


@hive_app.command(
    "repair",
    help="reconcile a hive's registry prefix against its beads-DB issue prefix: detect both, "
    "preview the change against --prefix, migrate the DB (bd rename-prefix), update the "
    "registry in place, then verify. Idempotent; --yes required to mutate, --dry-run to preview.",
)
def hive_repair_cmd(
    prefix: str = typer.Option(
        ..., "--prefix", help="target canonical prefix (no trailing hyphen)"
    ),
    hive: str = typer.Option("", "--hive", help="target hive (default: cwd's hive)"),
    yes: bool = typer.Option(
        False, "--yes", help="required to apply a prefix change (orphans no bead IDs — bd "
        "rename-prefix rewrites every issue's id in place, but any prefix cached elsewhere goes "
        "stale); no prompt so this stays agent-drivable"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="print the detect/preview and change nothing"
    ),
):
    from . import hive_repair

    hive_repair.repair(hive=hive, prefix=prefix, yes=yes, dry_run=dry_run)


@hive_app.command("ready", help="check whether this repo is set up for AGF (read-only).")
def hive_ready(
    verbose: bool = typer.Option(
        False, "-v", "--verbose", help="show the per-line-item breakdown (required + optional)"
    ),
):
    from . import hive_ready as ready

    ready.run_check(verbose)


@hive_app.command("context", hidden=True)
def hive_context(
    hook_json: bool = typer.Option(
        False,
        "--hook-json",
        help="wrap the context in the SessionStart hook JSON envelope (Claude Code)",
    ),
):
    """Registry-driven AGF steering payload for session hooks (read-only, local, no network).

    Inside a registered hive: prints the AGF steering text (the hint-stanza body + this hive's
    prefix/kind/footprint), or with --hook-json the SessionStart hook envelope. Outside a hive
    or in an unregistered repo: prints nothing and exits 0 — a hook consumer must never break
    a session start, so ANY failure here is silent success."""
    import json as _json

    from . import hive

    try:
        payload = hive.agf_context()
    except Exception:  # noqa: BLE001 - hook safety: never break a session start
        raise typer.Exit(0) from None
    if payload is None:
        raise typer.Exit(0)
    if hook_json:
        typer.echo(
            _json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "SessionStart",
                        "additionalContext": payload["text"],
                    }
                }
            )
        )
    else:
        typer.echo(payload["text"])


@hive_app.command(
    "survey",
    help="fleet table for onboarding triage: one row per on-disk repo (read-only).",
)
def hive_survey(
    available: bool = typer.Option(
        False,
        "--available",
        help="show only unregistered candidate repos "
        f"(those not yet `{config.BINARY_ALIAS} hive add`ed)",
    ),
    as_json: bool = typer.Option(
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

    survey_mod.survey(available=available, json_out=as_json, sort=sort)


@hive_app.command("classify", help="classify a repo (helper).")
def hive_classify(provider: str, org: str, repo: str):
    typer.echo(registry.classify(provider, org, repo))


@hive_app.command("prefix", help="suggest a prefix for a repo (helper).")
def hive_prefix(provider: str, org: str, repo: str, kind: str = typer.Argument("")):
    # No KIND → classify and resolve it the way onboard does, so the helper reports the
    # prefix onboard will actually register instead of the bare-if-unique fallback (bh-skbo).
    resolved, _upstream = registry.resolve_kind(
        registry.classify(provider, org, repo) if not kind else "", kind
    )
    pref, warns = registry.derive_prefix(provider, org, repo, resolved)
    for w in warns:
        typer.echo(w, err=True)
    typer.echo(pref)


@hive_app.command(
    "enable",
    help="set <feature>.enabled = true on the hive's managed_repos entry (default: cwd's hive).",
)
def hive_enable(
    feature: str = typer.Argument(..., help="feature name, e.g. observaloop"),
    hive_id: str = typer.Argument("", help="hive id (default: cwd's hive)"),
):
    from . import worktree as wt_mod

    cfg = config.load()
    entry = wt_mod._resolve_entry(cfg, hive_id)
    res = config.set_hive_feature_flag(entry, feature, True)
    _echo_problems(res["problems"])
    if not res["ok"]:
        raise typer.Exit(1)
    prefix = str(entry.get("prefix", hive_id))
    config.save(cfg)
    typer.echo(f"✓ {prefix}: {feature}.enabled = true")


@hive_app.command(
    "disable",
    help="set <feature>.enabled = false on the hive's managed_repos entry (default: cwd's hive).",
)
def hive_disable(
    feature: str = typer.Argument(..., help="feature name, e.g. observaloop"),
    hive_id: str = typer.Argument("", help="hive id (default: cwd's hive)"),
):
    from . import worktree as wt_mod

    cfg = config.load()
    entry = wt_mod._resolve_entry(cfg, hive_id)
    res = config.set_hive_feature_flag(entry, feature, False)
    _echo_problems(res["problems"])
    if not res["ok"]:
        raise typer.Exit(1)
    prefix = str(entry.get("prefix", hive_id))
    config.save(cfg)
    typer.echo(f"✓ {prefix}: {feature}.enabled = false")


# ---- hive archive ------------------------------------------------------------

archive_app = typer.Typer(
    no_args_is_help=True,
    help="Inspect and reclaim the soft-archive graveyard "
    f"({config.BINARY_ALIAS} hive retire destinations).",
)
hive_app.add_typer(archive_app, name="archive")


@archive_app.command("list", help="list archived repos with age and size.")
def archive_list(
    as_json: bool = typer.Option(False, "--json", help="emit machine-readable JSON"),
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

    if as_json:
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
        False, "--all-ages", help="remove every archived repo regardless of age"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="preview what would be removed, mutating nothing"
    ),
):
    """Docker-``system-prune``-style reclamation of the archive graveyard.

    By default, removes archived repos whose age >= ``--older-than`` (defaulting to
    ``archive.window_days``, itself defaulting to 30 days). ``--all-ages`` removes every archived
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
# --hive/--bead/--branch are command-local: the global -a/-r routing flags apply only
# to the `bd`/`git` passthrough, not here.


@wt_app.command("add", help="create a managed worktree (off the hive's HEAD) + run init ops.")
def wt_add(
    hive: str = typer.Option("", "--hive", help="target hive (default: cwd's hive)"),
    bead: str = typer.Option("", "--bead", help="branch bead/<id>, leaf <id>"),
    branch: str = typer.Option("", "--branch", help="literal branch name (leaf = last segment)"),
    dry_run: bool = typer.Option(
        False, "--dry-run", "--preview", help="print plan, change nothing"
    ),
    as_json: bool = typer.Option(
        False, "--json", help="emit the preview (or created result) as machine-readable JSON"
    ),
):
    from . import worktree

    worktree.add(hive=hive, bead=bead, branch=branch, dry_run=dry_run, as_json=as_json)


@wt_app.command(
    "list", help=f"list {config.BINARY_ALIAS}-managed worktrees (prefix / branch / path)."
)
def wt_list():
    from . import worktree

    worktree.list_cmd()


@wt_app.command("path", help="print the absolute path of a managed worktree (for scripts).")
def wt_path(
    ref: str = typer.Argument("", help="bead id, branch, or leaf"),
    bead: str = typer.Option("", "--bead", help="resolve by bead id"),
    hive: str = typer.Option("", "--hive", help="target hive (default: cwd's hive)"),
):
    from . import worktree

    target = bead or ref
    if not target:
        typer.echo("✗ give a <ref> or --bead <id>", err=True)
        raise typer.Exit(1)
    worktree.path_of(hive, target)


@wt_app.command("init", help="re-run init ops on an existing managed worktree.")
def wt_init(path: str):
    from . import worktree

    worktree.init_existing(path)


@wt_app.command("rm", help="remove one managed worktree.")
def wt_rm(
    ref: str = typer.Argument("", help="bead id, branch, or leaf"),
    bead: str = typer.Option("", "--bead", help="resolve by bead id"),
    hive: str = typer.Option("", "--hive", help="target hive (default: cwd's hive)"),
    force: bool = typer.Option(False, "-f", "--force", help="remove even if dirty"),
    as_json: bool = typer.Option(False, "--json", help="emit {op, hive, path, removed} as JSON"),
):
    from . import worktree

    target = bead or ref
    if not target:
        typer.echo("✗ give a <ref> or --bead <id>", err=True)
        raise typer.Exit(1)
    worktree.remove(hive, target, force=force, as_json=as_json)


@wt_app.command(
    "status",
    help=(
        "show per-worktree classification (SAFE / ACTIVE / DIRTY / …) for one hive or all hives."
        " Repopulates fresh metadata before classifying — the pre-flight never uses stale data."
    ),
)
def wt_status(
    hive: str = typer.Option("", "--hive", help="target hive (default: cwd's hive or all hives)"),
    as_json: bool = typer.Option(False, "--json", help="emit JSON array of WtStatus records"),
):
    from . import worktree

    worktree.status_cmd(hive=hive, as_json=as_json)


@wt_app.command("prune", help="remove ALL managed worktrees (or one hive's) + prune admin files.")
def wt_prune(hive: str = typer.Option("", "--hive", help="limit to one hive")):
    from . import worktree

    worktree.prune(hive=hive)


@wt_app.command(
    "mark-landed",
    help=(
        "operator escape hatch: assert an out-of-band landing — stamp close_reason 'merged' "
        "on the bead so `prune` reaps its seat/branch. Prefer `work land` when a PR exists."
    ),
)
def wt_mark_landed(
    ref: str = typer.Argument(..., help="bead id or wt/bead/<type>/<id> branch"),
    hive: str = typer.Option("", "--hive", help="target hive (default: cwd's hive)"),
):
    from . import worktree

    worktree.mark_landed(hive, ref)


# ---- labels (registry) ------------------------------------------------------


@label_app.command("validate", help="lint the hive/workspace DB against the registry.")
def labels_validate(
    enforce: bool = typer.Option(False, "--enforce", help="fail on any violation (default)"),
    advisory: bool = typer.Option(False, "--advisory", help="report only, always exit 0"),
):
    mode = "advisory" if advisory and not enforce else "enforce"
    validate.validate(mode)


@label_app.command("sync", help="reconcile registry vs git-workspace.")
def labels_sync():
    registry.repos_sync()


@label_app.command("report", help="usage report per dimension.")
def labels_report():
    registry.report()


@label_app.command("allowed", help="print the allowed label set.")
def labels_allowed():
    registry.allowed()


@label_app.command("docs", help="regenerate ~/.beadhive/labels.md from config.")
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


# ---- config -----------------------------------------------------------------


@config_app.command("path", help="print the resolved config path.")
def config_path_cmd():
    typer.echo(config.config_path())


@config_app.command("show", help="pretty-print the resolved config (the doctor overview + extras).")
def config_show():
    from . import doctor

    doctor.show()


@config_app.command("init", help="scaffold ~/.beadhive from bundled templates.")
def config_init(
    force: bool = typer.Option(False, "-f", "--force", help="overwrite existing files"),
):
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


@config_app.command(
    "schema",
    help="dump every known config key (dotted path, type, default, description).",
)
def config_schema_cmd(as_json: bool = typer.Option(False, "--json", help="machine payload")):
    fields = config_schema.iter_schema_fields()
    if as_json:
        import json as json_mod

        rows = [
            {"path": f.path, "type": f.type, "default": f.default, "description": f.description}
            for f in fields
        ]
        typer.echo(json_mod.dumps(rows, indent=2))
        return
    path_width = max(len(f.path) for f in fields)
    type_width = max(len(f.type) for f in fields)
    default_width = max(len(f.default) for f in fields)
    for f in fields:
        row = f"{f.path:<{path_width}}  {f.type:<{type_width}}  {f.default:<{default_width}}"
        typer.echo(f"{row}  {f.description}" if f.description else row)


@config_app.command("validate", help="validate the resolved config against the schema.")
def config_validate(
    fix: bool = typer.Option(
        False,
        "--fix",
        help="print a paste-ready prompt for a coding agent to update a stale config "
        "to the current schema (no auto-write).",
    ),
):
    """Run the schema validator over the resolved config: print problems + the ws→bh rename
    table, exit 1 on any error (a wrong-type value or an unknown/renamed key), else 0. When the
    config is stale (missing/old schema_version or a renamed key), append a paste-ready
    agentic-update offer. `--fix` prints just that prompt. A missing config file prints
    `bh config init` guidance rather than a traceback."""
    from . import config_validate as cv

    try:
        cfg = config.load()
    except FileNotFoundError:
        typer.echo(
            f"no config found — scaffold it with `{config.BINARY_ALIAS} config init`.", err=True
        )
        raise typer.Exit(1) from None

    if fix:
        prompt = cv.agentic_update_prompt(cfg)
        if prompt is None:
            typer.echo(f"✓ config is already at schema v{cv.SCHEMA_VERSION} — nothing to fix.")
            return
        typer.echo(prompt)
        return

    problems = cv.validate_config(cfg)
    if not problems:
        typer.echo(f"✓ config is valid (schema v{cv.SCHEMA_VERSION}).")
        return

    _echo_problems(problems)
    if cv.renamed_keys_present(cfg):
        typer.echo("\nws → bh renames:", err=True)
        for line in cv.renamed_key_table():
            typer.echo(line, err=True)

    offer = cv.agentic_update_prompt(cfg)
    if offer is not None:
        typer.echo(
            f"\n─ stale config — paste this to a coding agent to update it "
            f"(or run `{config.BINARY_ALIAS} config validate --fix`): ─",
            err=True,
        )
        typer.echo(offer, err=True)

    raise typer.Exit(1 if config._has_errors(problems) else 0)


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


@config_app.command(
    "get",
    help=f"read a dotted config key (e.g. `{config.BINARY_ALIAS} config get otel.enabled`).",
)
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


@config_app.command(
    "unset",
    help=f"delete a dotted config key (e.g. `{config.BINARY_ALIAS} config unset otel`).",
)
def config_unset(key: str = typer.Argument(..., help="dotted.key path into the config")):
    res = config.unset_value(key)
    if not res["ok"]:
        _echo_problems(res["problems"])
        raise typer.Exit(1)
    typer.echo(f"✓ unset {key}")


# ---- mcp ---------------------------------------------------------------------
# FastMCP stdio server (fastmcp is a core dependency of ws). ws.mcp imports fastmcp lazily, so
# wiring this subcommand never drags it into the main CLI import path.

#: The name used to register the server with Claude Code (the `<name>` arg passed
#: to `claude mcp add`). Kept as a constant so tests and the help text never drift.
MCP_SERVER_NAME = config.BINARY_ALIAS

#: The Claude Code MCP scope applied by default when running `bh mcp install`.
MCP_DEFAULT_SCOPE = "user"


def _build_claude_mcp_add_cmd(scope: str = MCP_DEFAULT_SCOPE) -> list[str]:
    """Return the argv list for `claude mcp add bh --scope <scope> -- bh mcp serve`.

    Pure (no I/O, no side effects): the install command calls this once and passes the
    result to subprocess so tests can assert the exact command without spawning a process.
    """
    return [
        "claude",
        "mcp",
        "add",
        MCP_SERVER_NAME,
        "--scope",
        scope,
        "--",
        config.BINARY_ALIAS,
        "mcp",
        "serve",
    ]


@mcp_app.command(
    "serve",
    help=f"run the {config.BINARY_ALIAS} MCP server over stdio "
    f"(fastmcp is a core dependency of {config.BINARY_ALIAS}).",
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
        f"Wire the {config.BINARY_ALIAS} MCP server into Claude Code "
        "(runs once, persists across hives).\n\n"
        f"Shells out to: claude mcp add {config.BINARY_ALIAS} --scope user "
        f"-- {config.BINARY_ALIAS} mcp serve\n\n"
        f"After registration, every Claude Code session sees the {config.BINARY_ALIAS} "
        "control-plane tools:\n"
        "hive_onboard, hive_add, config_set, hive_status, hive_list, plan_check."
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
    typer.echo(f"✓ {config.BINARY_ALIAS} MCP server registered with Claude Code (scope={scope}).")


# ---- setup ------------------------------------------------------------------


@setup_app.command("check", help=f"probe post-{config.BINARY_ALIAS} deps and cache the result.")
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
