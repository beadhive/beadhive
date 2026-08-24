"""ws CLI — Typer app wiring the operation groups together.

Surface: bd / git (passthrough + -a/-r routing) · hive · labels · sync · hub · dolt · doctor
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
from typer.core import TyperGroup

from . import (
    alerts,
    checkpoint,
    complexity_backfill,
    config,
    config_schema,
    dep_cli,
    dolt,
    gitworkspace_plugin,
    home_migration,
    host_cli,
    jsonout,
    log,
    otel,
    plan,
    plugins,
    registry,
    release,
    stream_cli,
    toolchain,
    validate,
    work,
)
from . import bd as bd_mod
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
alerts_app = typer.Typer(no_args_is_help=True, help="Active agent-steering alerts.")
hq_app = typer.Typer(
    no_args_is_help=True, help="Factory HQ: the durable central store (kind=hq singleton)."
)
setup_app = typer.Typer(no_args_is_help=True, help="Post-install dependency check + cached gate.")
harness_app = typer.Typer(
    no_args_is_help=True,
    help="Aliases onto `bh dep`, filtered to agent harnesses (bh-hsus.6).",
)
contrib_app = typer.Typer(
    no_args_is_help=True,
    help="Contribution plane: the contributor seat's outbound editor (upstream issues).",
)
contrib_profile_app = typer.Typer(
    no_args_is_help=True,
    help="Contribution dossier: build/show an external upstream's contribution profile (go/no-go).",
)

app.add_typer(setup_app, name="setup", rich_help_panel=ADMIN_PANEL)
app.add_typer(dep_cli.app, name="dep", rich_help_panel=ADMIN_PANEL)
# `harness` is now a FILTER over `bh dep`, not a noun of its own — kept because bh-q160.3's
# acceptance and the documented adoption sequences name it (bh-hsus.6). Hidden from the panels so
# the help lists one surface, not two.
app.add_typer(harness_app, name="harness", hidden=True)
app.add_typer(contrib_app, name="contrib", rich_help_panel=INTEGRATION_PANEL)
app.add_typer(hive_app, name="hive", rich_help_panel=HIVE_PANEL)
app.add_typer(hq_app, name="hq", rich_help_panel=FLEET_PANEL)
app.add_typer(host_cli.app, name="host", rich_help_panel=FLEET_PANEL)
app.add_typer(label_app, name="label", rich_help_panel=HIVE_PANEL)
app.add_typer(toolchain.app, name="toolchain", rich_help_panel=HIVE_PANEL)
app.add_typer(wt_app, name="worktree", rich_help_panel=INTEGRATION_PANEL)
app.add_typer(wt_app, name="wt", hidden=True)  # `bh wt` alias (hidden to avoid dup in help)
app.add_typer(work.app, name="work", rich_help_panel=INTEGRATION_PANEL)
app.add_typer(plan.app, name="plan", rich_help_panel=PLANNING_PANEL)
app.add_typer(release.app, name="release", rich_help_panel=INTEGRATION_PANEL)
app.add_typer(checkpoint.app, name="checkpoint", rich_help_panel=INTEGRATION_PANEL)
app.add_typer(alerts_app, name="alerts", rich_help_panel=FLEET_PANEL)
app.command(
    "backfill-complexity",
    rich_help_panel=HIVE_PANEL,
    help="preview/apply a hash-gated full-corpus complexity-label migration",
)(complexity_backfill.command)
app.command(
    "stream",
    rich_help_panel=FLEET_PANEL,
    help="stream backend-neutral bead state as snapshot-first NDJSON frames",
)(stream_cli.command)
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

# git-workspace is a required dep (deps.py, required=ALWAYS), not an optional plugin — it has
# no `enabled` flag to loop over, so it is not in plugins.registry() (bh-hsus.4). It is however
# the one dep with a real `bh plugin`-shaped surface, so `bh plugin git-workspace groups`
# mounts here explicitly instead — same `bh plugin <name>` mount point, one dep-owned exception.
plugin_app.add_typer(gitworkspace_plugin.cli, name="git-workspace")

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
    """True when this invocation is purely informational — a `--help`/`-h` pass,
    shell-completion, or a `--dry-run` — and must never trigger a gate or a diagnostic
    side effect.

    ``--dry-run`` belongs here for exactly the reason `--help` does, and bh-1kzc is the bug
    that proved it: on a fresh host `bh host provision --dry-run` was REFUSED by the setup
    gate, even though the plan it prints mutates nothing. The only route to that preview was
    ``BH_SKIP_SETUP_CHECK=1`` — a bypass the error message itself labels debug-only. A
    zero-mutation preview is the safest thing a new operator can run and the first thing they
    reach for; gating it teaches them to use a debug escape hatch as routine.

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
    return any(arg in ("--help", "-h", "--dry-run") for arg in sys.argv[1:])


# ---- setup gate ---------------------------------------------------------------

# Subcommands exempt from the setup-complete gate.  The gate guards every OTHER
# verb: a fresh install that has never run `ws setup check` must still be able
# to bootstrap (config init), diagnose itself (doctor), or run setup check itself.
# Top-level --version/--help never reach the gate (eager callback + typer exit before
# body); a subcommand's `--help`/`-h` and shell-completion DO reach this callback (the
# subcommand's own eager --help short-circuits only after this group callback runs), so
# _root skips the call entirely for those via _is_help_or_completion_invocation.
#
# `harness` is exempt for the same bootstrap reason (bh-pc2a.36): the image deliberately does not
# ship the proprietary harness, so installing one is part of GETTING set up, not something to do
# after. Gating the only verb that fixes "no harness" behind a check the user has not run yet puts
# a step in front of the exact flow this is meant to smooth — and `harness list` is a pure read.
# `dep` inherits that exemption because it is now where those verbs live (bh-hsus.6): `bh dep
# install` fixes "no harness", `bh dep auth` fixes "no credential", and `bh dep list|show` are
# pure reads that diagnose a host the gate would otherwise refuse to let anyone inspect.
_SETUP_GATE_ALLOW: frozenset[str] = frozenset({"setup", "config", "doctor", "harness", "dep"})

# Individual verbs that BOOTSTRAP THEMSELVES, exempt even though their group is gated (bh-1kzc).
# `host provision` runs `bh setup check` as its own first step (host_provision.PLAN[0]), so
# gating it behind that same check is a deadlock: the verb that performs the check cannot run
# until the check has been performed. Scoped to the verb, NOT the group — `host list`,
# `host retire` and the rest are ordinary verbs and stay gated.
_SETUP_GATE_ALLOW_VERBS: dict[str, frozenset[str]] = {"host": frozenset({"provision"})}


def _enforce_setup_gate(ctx: typer.Context) -> None:
    """Gate every verb not in _SETUP_GATE_ALLOW behind a passing setup cache.

    Bypass entirely when:
    - ``BH_SKIP_SETUP_CHECK`` (or the deprecated ``WS_SKIP_SETUP_CHECK``) is truthy (debug
      escape hatch)
    - the invoked subcommand is in the allow-list or is None (no subcommand)
    - the setup cache exists with ``setup == true``

    Denied verbs surface a clear "run bh setup check" message on stderr and exit 1.

    That message is ALSO the first-run pointer at ``bh setup guide``
    (:data:`beadhive.setup_guide.POST_INSTALL_POINTER`, whose comment records the other two
    channels saying the same sentence). Deliberately ONE hint, not two: this gate is the
    only thing many users see after installing by a route that never showed them
    ``INSTALL.md``, and "run the check" alone tells them what is wrong without telling them
    what to do about it. Extend this string; do not add a second nudge beside it.
    """
    if config.skip_setup_check():
        return
    subcmd = ctx.invoked_subcommand
    if subcmd is None or subcmd in _SETUP_GATE_ALLOW:
        return
    # Self-bootstrapping verbs inside a gated group. `ctx` only knows the top-level group here
    # (the root callback runs before Click resolves the chain), so the verb comes from raw
    # ``sys.argv`` — the same precedent _is_help_or_completion_invocation sets.
    bootstrapping = _SETUP_GATE_ALLOW_VERBS.get(subcmd, frozenset())
    if bootstrapping and any(arg in bootstrapping for arg in sys.argv[1:]):
        return
    from . import setup as setup_mod  # lazy: avoids import at module load

    if not setup_mod.is_setup_complete():
        from . import setup_guide  # lazy, and only on the failing path

        typer.echo(
            f"✗ `{config.BINARY_ALIAS} {subcmd}` requires setup — "
            f"run `{config.BINARY_ALIAS} setup check` first.\n"
            f"  {setup_guide.POST_INSTALL_POINTER}\n"
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


def _migrate_home_best_effort() -> None:
    """One-time ~/.ws -> ~/.beadhive migration: deliberately placed here, not
    inside config.home(), so a plain config read/import (tests, MCP tools, library callers)
    never has the side effect of moving real state on disk — only an actual `bh <command>`
    invocation does. Best-effort: a migration failure must never block the CLI."""
    try:
        home_migration.migrate_home_if_needed()
    except Exception:
        pass


def _migrate_hive_keys_best_effort() -> None:
    """One-time otel.rig/git_workspace.rig_match -> otel.hive/git_workspace.hive_match
    config-key migration (bh-41rh hard cutover): same placement rule as the home-dir
    migration above."""
    try:
        config.migrate_hive_keys_if_needed()
    except Exception:
        pass


def _warn_stale_schema_version_best_effort(ctx: typer.Context) -> None:
    """Lightest schema_version staleness nudge (bh-5cgm.3): NOT a migration — never rewrites
    the config, just warns once when it predates the current schema. Same placement rule
    as the migrations above: a real CLI invocation only, never a bare load()/getter. Skipped
    entirely for `--help`/`-h` and shell-completion (bh-sn9q): those are informational-only
    passes and must never emit diagnostic noise, even to stderr."""
    if _is_help_or_completion_invocation(ctx):
        return
    try:
        config.warn_stale_schema_version_if_needed()
    except Exception:
        pass


def _warn_missing_fleet_config_best_effort(ctx: typer.Context) -> None:
    """Nudge when this host has an HQ store but no `fleet.yaml` in it (bh-e0y8.5): `config.load()`
    degrades to host-only config, which is worth saying out loud once. Same placement rule and
    same `--help`/completion exemption as the schema-staleness nudge above."""
    if _is_help_or_completion_invocation(ctx):
        return
    try:
        config.warn_missing_fleet_config_if_needed()
    except Exception:
        pass


def _warn_literal_violations_best_effort(ctx: typer.Context) -> None:
    """Nudge once per invocation when a persisted value sits outside its schema Literal's
    declared range (bh-aidze) — e.g. `dolt.backend: shared-server`, which used to load, persist,
    and render back silently as if it were in effect. Same placement rule and same
    `--help`/completion exemption as the schema-staleness nudge above."""
    if _is_help_or_completion_invocation(ctx):
        return
    try:
        config.warn_literal_violations_if_needed()
    except Exception:
        pass


def _init_telemetry_best_effort() -> None:
    """Eager telemetry init: this callback runs before every subcommand, so it's the one place
    that activates OTel for a real `ws` command path (otherwise is_active() is forever False
    and every emitter is inert). It's cheap + safe when off: init() no-ops fast on the default
    (otel.enabled false) and never imports opentelemetry on that path. Telemetry is best-effort
    and must never block the CLI — a missing/unreadable config (e.g. before `bh config init`)
    degrades to telemetry-off rather than erroring. The eager `--version` path exits before
    this body, so it stays untouched."""
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


def _instrument_command_entry(ctx: typer.Context) -> None:
    """Instrument the command-entry seam: register a call_on_close hook that emits a counter +
    histogram tagged with the invoked subcommand name + outcome (ok/error). Gated on
    is_active() so the off-path (default: otel disabled) is a single bool read — zero SDK
    import, zero allocation. The --version eager path exits before this body, so it's untouched."""
    if not otel.is_active():
        return
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


def _resolve_hive_routing_mode(ctx: typer.Context, all_hives: bool, hive: str) -> str:
    """Compute the -a/--hive routing mode and reject it on any verb but `bd`/`git`.

    Returns ``"all"`` / ``"hive"`` / ``"cwd"``; exits 1 with a usage message when a routing
    flag is passed ahead of a non-passthrough subcommand.
    """
    mode = "all" if all_hives else "hive" if hive else "cwd"
    if mode != "cwd" and ctx.invoked_subcommand not in ("bd", "git"):
        typer.echo(
            f"✗ -a/--all and --hive only apply to `{config.BINARY_ALIAS} bd` "
            f"and `{config.BINARY_ALIAS} git`",
            err=True,
        )
        raise typer.Exit(1)
    return mode


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
    _migrate_home_best_effort()
    _migrate_hive_keys_best_effort()
    _warn_stale_schema_version_best_effort(ctx)
    _warn_missing_fleet_config_best_effort(ctx)
    _warn_literal_violations_best_effort(ctx)
    _init_telemetry_best_effort()
    _instrument_command_entry(ctx)
    # Same informational-only exemption as the schema-staleness nudge above (bh-sn9q): a
    # subcommand's `--help`/`-h` or shell-completion must never be blocked by the setup gate
    # (it would otherwise swallow the help text entirely on a fresh, ungated install).
    if not _is_help_or_completion_invocation(ctx):
        _enforce_setup_gate(ctx)
    mode = _resolve_hive_routing_mode(ctx, all_hives, hive)
    ctx.obj = (mode, hive)


# ---- workspace --------------------------------------------------------------


def _role_bead_hive_prefix(bead: str) -> str:
    """The leading ``<prefix>-`` token of a bead id (e.g. ``"bh"`` from ``"bh-6t49w.4"``) — not
    a new hive-id format, just the bit of an id that ``registry._hive_matches``' ``by_prefix``
    already accepts as a bare hive_id (flexible/prefix mode)."""
    return bead.split("-", 1)[0] if "-" in bead else bead


def _apply_role_workspace(bead: str, hive: str) -> None:
    """``bh role <seat> [--bead <id>] [--hive <hive>]``'s workspace resolution (bh-6t49w.4):
    changes bh's own cwd to the resolved workspace BEFORE ``hitch_plugin.route`` execs a seat,
    so the launched process (native or hitch — both inherit bh's cwd) lands there for either
    backend. Composes bh's EXISTING pieces as an explicit two-step sequence, not a new resolver
    or a ``wt_create`` hook (see ``hitch_plugin``'s module docstring for why that hook was
    rejected for this exact use case):

    - ``registry.resolve_hive`` — the flexible bead-prefix / triplet / org-repo / bare-repo
      resolver ``bh work claim --hive`` already uses — for hive resolution.
    - ``bh work claim`` + ``worktree.locate`` for bead attachment.

    Lives here (not in ``hitch_plugin``/``role``) because both of those sit on the static import
    path FROM ``publish_export`` (via ``plugins``/``bd``) TO the cross-hive aggregates
    (``hub``/``hq``, reached through ``work``) that path must never reach — see
    ``docs/design/publish-boundary-adr.md`` and ``tests/test_publish_boundary.py``. ``cli.py`` is
    never imported by anything on that path, so resolving+claiming here (where ``work`` and
    ``registry`` are already module-level imports) can freely use ``bh work claim`` without
    widening that boundary.

    - neither given -> no-op (unchanged default: launch from cwd).
    - ``--hive`` only -> chdir to that hive's root, no bead.
    - ``--bead`` only -> the hive is resolved from the bead id's own leading ``<prefix>-`` token,
      through the SAME resolver.
    - both given and they name the same hive -> claim/attach the bead's worktree, chdir there.
    - both given and they disagree -> refuse loudly, naming both (``registry.resolve_hive``'s
      own not-found/ambiguous errors are inherited unchanged for either alone)."""
    if not bead and not hive:
        return

    cfg = config.load()
    hive_entry = registry.resolve_hive(cfg, hive) if hive else None
    if not bead:
        os.chdir(registry.hive_dir(hive_entry))
        return

    bead_hive_id = _role_bead_hive_prefix(bead)
    bead_entry = registry.resolve_hive(cfg, bead_hive_id)
    if hive_entry is not None and registry.hive_key(bead_entry) != registry.hive_key(hive_entry):
        typer.echo(
            f"✗ --bead {bead!r} belongs to hive '{registry.hive_key(bead_entry)}', which "
            f"disagrees with --hive {hive!r} ('{registry.hive_key(hive_entry)}')",
            err=True,
        )
        raise typer.Exit(1)

    hive_id = hive or bead_hive_id
    from . import worktree

    work.claim(bead=bead, as_="", group="", collapse="", hive=hive_id)
    _entry, _main, target, _branch = worktree.locate(cfg, hive_id, bead=bead)
    os.chdir(target)


def _role_instructions(seat: str, bead: str, task: str) -> str:
    """The unattended run's brief. Deliberately a POINTER, not a restated task (bh-6t49w.6):
    the same shape `LocalLoop._default_instructions` already writes — carry only what the
    contract needs and let the seat read the bead's own brief through `bh work brief`, which is
    the live spec. A restatement here would be a second, immediately-stale copy of it.

    ``--task`` is therefore OPTIONAL when ``--bead`` is given; when present it is appended as an
    extra section, so it adds to (or overrides, by being the more specific instruction) the
    pointer rather than replacing the bead as the source of truth."""
    lines = [f"# {seat} — {bead or 'ad-hoc run'}", ""]
    if bead:
        lines += [
            f"Bead: {bead}",
            "",
            f"Read this bead's own brief — `{config.BINARY_ALIAS} work brief {bead}` — it is the",
            f"spec, and nothing is restated here. Drive it through `{config.BINARY_ALIAS} work`",
            "per your seat prompt. Commit after every step: the branch is the checkpoint, and a",
            "restart re-dispatches a fresh turn against this same worktree rather than resuming a",
            "dead session.",
        ]
    if task:
        lines += ["", "## Task", "", task]
    return "\n".join(lines) + "\n"


def _role_dispatch_dir() -> Path:
    """Scratch for headless `bh role` runs — instructions + detached logs. Under bh's own home,
    NOT the worktree: a detached seat outlives this process, and dropping files into the bead's
    checkout would show up as dirty tree in the very branch the seat is about to commit."""
    d = config.home() / "dispatch"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _role_headless(
    seat: str, harness: str, task: str, detached: bool, bead: str, hive: str, no_hitch: bool
):
    """`bh role <seat> --task/-d`'s launch (bh-6t49w.6). Suitability is decided FIRST, before
    any workspace resolution, so an attached-only seat refuses immediately instead of claiming
    a bead's worktree on the way to a launch that was never going to happen."""
    import subprocess
    import uuid

    from . import hitch_plugin, localloop
    from .run import child_env

    cfg = config.load()
    resolved_harness = harness or config.harness_name(cfg)
    backend, detail = hitch_plugin.headless_plan(seat, resolved_harness, cfg)
    if backend == "hitch" and no_hitch:
        backend, detail = None, f"--no-hitch, and the only headless backend here is {detail}"
    if backend is None:
        typer.echo(f"✗ {detail}", err=True)
        raise typer.Exit(1)
    if not bead and not task:
        typer.echo(
            "✗ a headless run needs something to do — pass --bead <id> (the seat reads its "
            "brief) and/or --task '<what to do>'",
            err=True,
        )
        raise typer.Exit(1)

    _apply_role_workspace(bead, hive)
    instructions = _role_instructions(seat, bead, task)
    typer.echo(f"→ {seat}: headless via {backend} — {detail}", err=True)

    if backend == "hitch":
        # hitch has no instructions-file flag; the SAME pointer travels as its --task string.
        code = hitch_plugin.up(
            resolved_harness,
            seat,
            cfg,
            workspace=os.getcwd(),
            task=instructions,
            detached=detached,
            role_=seat,
        )
        if code != 0:
            raise typer.Exit(code)
        return

    entry = registry.entry_for_dir(cfg, Path.cwd())
    stem = bead or seat
    path = _role_dispatch_dir() / f"{stem}.role-{seat}.md"
    path.write_text(instructions, encoding="utf-8")
    argv = list(
        localloop.seat_argv(
            config.dispatch_seat_command(cfg, entry),
            seat,
            workspace=os.getcwd(),
            bead=bead,
            instructions=str(path),
            session_id=str(uuid.uuid4()),
            bundle=config.dispatch_seat_bundle(cfg, entry),
        )
    )
    if not detached:
        raise typer.Exit(run(argv, check=False, capture=False).returncode)

    log_path = _role_dispatch_dir() / f"{stem}.role-{seat}.log"
    with log_path.open("ab") as sink:
        proc = subprocess.Popen(  # noqa: S603 — argv is built, never shell-interpreted
            argv,
            cwd=os.getcwd(),
            env=child_env(),
            stdin=subprocess.DEVNULL,
            stdout=sink,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    typer.echo(f"→ {seat}: detached pid {proc.pid}, log {log_path}", err=True)


def _role_explain(seat: str, harness: str, no_hitch: bool, bead: str) -> None:
    """``bh role <seat> --explain``'s read-only preview (bh-6t49w.7): the resolved headless
    backend + suitability mode without launching anything or claiming ``--bead``'s worktree —
    mirrors hitch's own ``--explain``/``--dry-run`` contract (see ``hitch_plugin._up_cmd``).
    ``mode``/``backend``/``detail`` all come straight from ``hitch_plugin.headless_plan`` — the
    same pure, no-subprocess seam ``_role_headless`` decides suitability from before it commits
    to anything — so there is no second predicate to keep in sync with it or with `bh work
    schedule`'s own `mode` field. ``--bead`` is accepted for context only (echoed in the line);
    resolving/claiming its worktree would be a write, which `--explain` must never do.

    Deliberately does not re-validate ``seat`` against ``role._known_seats()`` (the bundled
    agent-def glob): ``headless_capable`` is checked against `ROLE_FOR_ACTION` — a closed,
    hardcoded table independent of which agent defs happen to be installed on this host — the
    exact same seam `_role_headless` already trusts, so an unknown/unsuitable seat still gets a
    loud, correct answer here instead of a spurious "not installed" refusal."""
    if not seat:
        typer.echo("✗ --explain needs a seat (e.g. `bh role developer --explain`)", err=True)
        raise typer.Exit(1)
    from . import hitch_plugin, localloop

    cfg = config.load()
    resolved_harness = harness or config.harness_name(cfg)
    mode = "headless-safe" if localloop.headless_capable(seat) else "attached-required"
    backend, detail = hitch_plugin.headless_plan(seat, resolved_harness, cfg)
    if backend == "hitch" and no_hitch:
        backend, detail = None, f"--no-hitch, and the only headless backend here is {detail}"
    bead_note = f" (bead {bead})" if bead else ""
    typer.echo(f"{seat}{bead_note}: mode={mode} backend={backend or 'none'} — {detail}")


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
    no_hitch: bool = typer.Option(
        False,
        "--no-hitch",
        help="force the native backend even when hitch (if enabled) would otherwise apply.",
    ),
    seats: bool = typer.Option(
        False,
        "--seats",
        help=(
            "with no seat given, run the full per-seat `hitch profile preflight` check "
            "(~2.7s, 7 seats) in the listing; the default listing only checks which backend "
            "would be picked, cheaply (bh-gqfrm)"
        ),
    ),
    bead: str = typer.Option(
        "",
        "--bead",
        help="claim/attach this bead's worktree and launch with it as the workspace "
        "(hive defaults to the bead's own leading prefix).",
    ),
    hive: str = typer.Option(
        "", "--hive", help="launch at this hive's root, no bead (see hive_match)."
    ),
    task: str = typer.Option(
        "",
        "--task",
        help="run unattended with this task. OPTIONAL alongside --bead — the seat is pointed "
        "at the bead's own brief; --task only adds to or overrides that.",
    ),
    detached: bool = typer.Option(
        False, "-d", "--detached", help="detach the unattended run (implies headless)."
    ),
    explain: bool = typer.Option(
        False,
        "--explain",
        "--dry-run",
        help="print the resolved headless backend + suitability mode for this seat and exit; "
        "no launch, no --bead worktree claim (mirrors hitch's own --explain/--dry-run).",
    ),
):
    from . import hitch_plugin

    if explain:
        _role_explain(name, harness, no_hitch, bead)
        return

    if task or detached:
        _role_headless(name, harness, task, detached, bead, hive, no_hitch)
        return

    _apply_role_workspace(bead, hive)
    hitch_plugin.route(name, harness=harness or None, no_hitch=no_hitch, full_seats=seats)


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
    rich_help_panel=FLEET_PANEL,
    help="query the hub — this host's derived cross-hive aggregate. "
    f"`{config.BINARY_ALIAS} hub bd ready` for work anywhere; "
    f"`{config.BINARY_ALIAS} hub intake` for the fleet-wide untriaged inbox. "
    f"(`{config.BINARY_ALIAS} hq bd` is HQ's OWN store, a different thing — see docs/HQ.md.)",
)
def hub_cmd(ctx: typer.Context):
    # UN-DEPRECATED by bh-89wxf.2. It was an alias for `bh hq` back when both names resolved to
    # one store; they are two stores with two jobs now, and this is the cross-hive one.
    from . import hub

    args = ctx.args
    # allow either `ws hub bd ready` or `ws hub ready`
    if args and args[0] == "bd":
        args = args[1:]
    # `ws hub intake` → the superintendent's fleet-wide untriaged-intake inbox (a filtered read).
    if args and args[0] == "intake":
        hub.intake(args[1:])
        return
    hub.query(args, label="hub")


@hq_app.command(
    "init",
    help="stand up the Factory HQ store (kind=hq singleton), move aggregation onto it, and "
    "(idempotently) scaffold its distributable layout + wire/push the configured hq.remote. "
    "Re-running once the remote is wired is a clean no-op. --create makes the remote (private, "
    "empty) when it does not exist yet. --dry-run previews the pre-push backup plan with zero "
    "mutation.",
)
def hq_init(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="preview the pre-push backup plan; no writes"
    ),
    auto: bool = typer.Option(
        False, "--auto", help="take the derived hq.remote without prompting (CI/headless)"
    ),
    create: bool = typer.Option(
        False,
        "--create",
        help="create hq.remote as a private, empty repo when it does not exist",
    ),
):
    from . import hq

    hq.init(dry_run=dry_run, auto=auto, create=create)


@hq_app.command(
    "push",
    help="publish HQ to its wired remote: the git half (fleet.yaml/workspace.toml/hosts/) and "
    "the Dolt half (HQ's own hq-prefixed beads), reporting what moved on each. Idempotent — "
    "'nothing to push' when there's nothing new. The repeatable counterpart to `hq init`'s "
    "one-shot first push. It does NOT refresh any aggregate: that is the hub's, it is derived "
    "and per-host, and `bh sync` owns it.",
)
def hq_push(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="preview what would be pushed; no writes"
    ),
):
    from . import hq

    hq.push(dry_run=dry_run)


@hq_app.command(
    "prune-aggregate",
    help="MIGRATION (bh-89wxf.2): delete the hive-derived beads a pre-split HQ accumulated, so "
    "HQ's Dolt database carries only its own hq-prefixed beads. Safe — every one of them is a "
    "derived copy that lives in its own hive; rebuild the cross-hive view with `bh sync`.",
)
def hq_prune_aggregate(
    dry_run: bool = typer.Option(False, "--dry-run", help="list what would be deleted; no writes"),
    confirm: bool = typer.Option(False, "--confirm", help="actually delete them"),
):
    from . import hq

    hq.prune_aggregate(dry_run=dry_run, confirm=confirm)


@hq_app.command(
    "status",
    help="read-only ahead/behind report for HQ against its wired remote, for BOTH the git half "
    "(main) and the Dolt half (bead state).",
)
def hq_status():
    from . import hq

    hq.status()


@hq_app.command(
    "clone",
    help="bootstrap a host with no local HQ: clone main + hydrate bead state from the "
    "configured hq.remote, so `hq bd ready` works afterward. Refuses if the local HQ already "
    "exists.",
)
def hq_clone(
    auto: bool = typer.Option(
        False, "--auto", help="take the derived hq.remote without prompting (CI/headless)"
    ),
):
    from . import hq

    hq.clone(auto=auto)


@hq_app.command(
    "restore",
    help="restore HQ from a pre-push backup: --list shows what exists; --level tar replaces "
    "the Dolt store, --level jsonl upserts the portable export (works with no readable "
    "store). --dry-run previews; a real restore needs --confirm.",
)
def hq_restore_cmd(
    list_only: bool = typer.Option(False, "--list", help="list available backups and exit"),
    from_dir: str = typer.Option(
        "", "--from", help="restore from this backup directory (default: newest)"
    ),
    level: str = typer.Option(
        "auto", "--level", help="auto | tar | jsonl (auto prefers tar, falls back to jsonl)"
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="preview the plan; no writes"),
    confirm: bool = typer.Option(
        False, "--confirm", help="proceed with a real restore, overwriting live HQ data"
    ),
):
    from pathlib import Path as _Path

    from . import config, hq_restore

    cfg = config.load()
    sets = hq_restore.list_backups(cfg)
    if list_only:
        hq_restore.echo_backups(sets)
        return
    if not sets:
        hq_restore.echo_backups(sets)
        raise typer.Exit(1)
    if from_dir:
        wanted = _Path(from_dir).expanduser()
        chosen = next((s for s in sets if s.directory == wanted or s.label == from_dir), None)
        if chosen is None:
            typer.echo(f"✗ no backup at {from_dir} — try --list", err=True)
            raise typer.Exit(1)
    else:
        chosen = sets[0]
    typer.echo(f"hq restore: {chosen.directory}")
    out = hq_restore.restore(cfg, chosen, level=level, dry_run=dry_run, confirm=confirm)
    hq_restore.echo_result(out)
    if not out.ok:
        raise typer.Exit(1)


@hq_app.command(
    "intake",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    add_help_option=False,
    help="HQ's OWN untriaged inbox (escalations filed by `bh escalate`). For the fleet-wide "
    f"cross-hive view use `{config.BINARY_ALIAS} hub intake` — bh-89wxf.2 stopped one verb "
    "carrying both scopes.",
)
def hq_intake_cmd(ctx: typer.Context):
    from . import hq

    hq.intake(ctx.args)


@hq_app.command(
    "bd",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    add_help_option=False,
    help="run a bd command against HQ's OWN store (authoritative hq-prefixed beads), "
    f"e.g. `{config.BINARY_ALIAS} hq bd ready`. The cross-hive view is "
    f"`{config.BINARY_ALIAS} hub bd`.",
)
def hq_bd_cmd(ctx: typer.Context):
    from . import hq

    hq.query(ctx.args)


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
    help="Passthrough to bd; `bd create` auto-applies provider/org/repo "
    "(`create --json <path>|-` takes a whole bead as one document — no prose through the shell).",
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

# ponytail: hive_init (13 params) and hive_onboard (15) are Repowise primitive-obsession
# findings, deferred rather than collapsed into a params dataclass. Each parameter is a
# distinct Typer CLI flag with its own --help text; Typer binds flags 1:1 to function
# parameters, so a dataclass wrapper wouldn't shrink the flag surface — it would add an
# indirection layer (build the dataclass from the individual Typer-bound args, then unpack it
# again to call hive.init/hive.onboard) for zero reduction in what the user types or what the
# signature exposes. The real duplication between the two commands — the plugin-mode
# --claude/--skills guard — IS extracted below, which is the actual mechanical win available
# here without restructuring the Typer wiring itself.


def _reject_claude_skills_conflict_in_plugin_mode(claude: bool, skills: bool) -> None:
    """`hive init`/`hive onboard` shared guard: in plugin mode, --skills is incompatible with
    --claude (the plugin already vends skills, so a separate local copy is redundant). Exits 1
    with a clear message on the conflict; a no-op otherwise."""
    if not (claude and skills):
        return
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


def _reject_global_without_claude_or_codex(global_grant: bool, claude: bool, codex: bool) -> None:
    """`hive init`/`hive onboard` shared guard: --global is a modifier on --claude/--codex, not
    a standalone flag — bare `--global` with neither would silently do nothing."""
    if global_grant and not (claude or codex):
        typer.echo(
            "✗ --global needs --claude and/or --codex — it's a modifier on those grants, not "
            "a standalone flag.",
            err=True,
        )
        raise typer.Exit(1)


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
    codex: bool = typer.Option(
        False,
        "--codex",
        help="write a project-local, git-excluded Codex sandbox grant (.codex/config.toml, "
        "[sandbox_workspace_write].writable_roots) covering this hive's own worktree "
        "subtree — the Codex-native twin of --claude's settings.local.json grant",
    ),
    global_grant: bool = typer.Option(
        False,
        "--global",
        help="modifier on --claude/--codex: grant the WHOLE shared worktrees_root() (every "
        "hive's worktrees, not just this one) in the harness's GLOBAL config instead of this "
        "hive's own subtree — ~/.claude/settings.json for --claude, ~/.codex/config.toml for "
        "--codex. One-time, coarser-grained, opt-in; broader blast radius than the per-hive "
        "grant, which stays the default. Requires --claude and/or --codex.",
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
    from . import hive

    _reject_claude_skills_conflict_in_plugin_mode(claude, skills)
    _reject_global_without_claude_or_codex(global_grant, claude, codex)

    hive.init(
        furnish=furnish,
        claude=claude,
        skills=skills,
        observaloop=observaloop,
        agents=agents,
        opencode=opencode,
        codex=codex,
        global_grant=global_grant,
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


@hive_app.command(
    "rm",
    help="FLEET-WIDE: unregister a hive by id (registry-only; leaves .beads/repo intact). "
    "managed_repos is shared fleet truth, so this drops the hive for every host, not just "
    "this one — for a host-local drop that keeps the hive registered, see `bh hive reclaim`. "
    "Requires --confirm; --dry-run previews with zero mutation.",
)
def hive_rm(
    hive_id: str = typer.Argument(..., metavar="HIVE_ID"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="print what would be unregistered and change nothing"
    ),
    confirm: bool = typer.Option(
        False, "--confirm", help="proceed with the FLEET-WIDE unregister (every host loses it)"
    ),
):
    from . import hive

    hive.rm(hive_id, dry_run=dry_run, confirm=confirm)


@hive_app.command(
    "retire",
    help="FLEET-WIDE: guarded teardown of a hive: assess → (backup|consent) → worktree "
    "teardown → soft-archive the clone → unregister. The unregister step drops managed_repos "
    "fleet-wide (every host loses this hive), even though the clone/worktree teardown only "
    "affects this host — for a host-local-only drop that leaves the hive registered for the "
    "fleet, use `bh hive reclaim` instead. Refuses to lose unbacked work without --backup or "
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
    "reclaim",
    help="HOST-LOCAL: guarded teardown of a hive's clone/worktrees on THIS host only — "
    "identical assess → (backup|consent) → worktree teardown → soft-archive the clone as "
    "`bh hive retire`, but never unregisters: managed_repos (and every other host's copy) is "
    "left untouched, so the hive stays registered for the fleet. Use this when only this "
    "host no longer wants a local copy. Refuses to lose unbacked work without --backup or "
    "--confirm. --dry-run previews the full plan with zero mutation; --purge hard-deletes the "
    "clone instead of archiving it (still gated).",
)
def hive_reclaim(
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

    retire.reclaim_hive(hive_id, dry_run=dry_run, backup=backup, confirm=confirm, purge=purge)


# ---- hive sync: remotes (bd dolt push/pull) + peers (bd federation sync) -----------------
#
# bh-ummb9: one sync verb, two subcommands, because bd's two mechanisms are exactly inverted
# in capability (remotes: push/pull, no bidirectional-in-one-call; federation: bidirectional
# `sync`, no single-direction verb) — see docs/design/hive-sync-unification-molecule.yaml for
# the full rationale.
#
# COMPOSITION NOTE (bh-ummb9.1, checked before building anything else): a plain
# `typer.Typer(invoke_without_command=True)` + callback does NOT compose with a positional
# `[HIVE]...` argument on that callback — Click's Group parses its own declared params
# (greedily, `nargs=-1`) before it ever looks for a subcommand name, so `bh hive sync remotes`
# gets swallowed whole as `hive=["remotes"]` instead of dispatching to the `remotes` command
# (verified interactively: with the positional argument present, `sync remotes --all` prints
# the *default* callback's output, never `remotes`'s). Flag-only groups don't hit this — only
# the plural positional argument does, which the "no subcommand = remotes, HIVE... allowed
# bare" shape requires. `DefaultGroup` below is the documented fallback made to work in-tree
# rather than the uglier bare-forwarding one: it strips its own positional Arguments out of
# `self.params` for exactly one parse when the first token names a real subcommand
# (`remotes`/`peers`), so Click's normal dispatch takes over; any other first token (a hive
# name, `--all`, nothing) falls through to the callback exactly as before. Known limitation
# inherited from `nargs=-1` generally (put flags before positional hives:
# `sync --dry-run myhive`, not `sync myhive --dry-run` — the latter also swallows `--dry-run`
# as a second hive name, same as any other Click command with a trailing variadic argument).
class DefaultGroup(TyperGroup):
    def parse_args(self, ctx: typer.Context, args: list[str]) -> list[str]:
        if args and args[0] in self.commands:
            saved = self.params
            self.params = [p for p in saved if getattr(p, "param_type_name", "") != "argument"]
            try:
                return super().parse_args(ctx, args)
            finally:
                self.params = saved
        return super().parse_args(ctx, args)


sync_app = typer.Typer(
    invoke_without_command=True,
    cls=DefaultGroup,
    help="bead-state sync: `remotes` (bd dolt push/pull) or `peers` (bd federation sync). "
    "No subcommand = remotes, the common case.",
)


def _hive_args_ok(hive: list[str], all_hives: bool) -> bool:
    """Exactly one of positional HIVE(s) or --all — same "pick a target" rule both verbs
    have always enforced, extended to the now-plural HIVE... form."""
    return bool(hive) != all_hives


def _pull_push_flags(pull: bool, push: bool) -> tuple[bool, bool]:
    """`--pull`/`--push` are mutually exclusive; absence of both means both (pull-then-push —
    the actual pull-first sequencing is bh-ummb9.2's job, this only routes the flags)."""
    if pull and push:
        typer.echo("✗ --pull and --push are mutually exclusive", err=True)
        raise typer.Exit(1)
    if not pull and not push:
        return True, True
    return pull, push


def _run_sync_remotes(
    hive: list[str],
    all_hives: bool,
    remote: str | None,
    pull: bool,
    push: bool,
    dry_run: bool,
    force: bool,
    verbose: bool = False,
) -> None:
    from . import sync_remote

    if not _hive_args_ok(hive, all_hives):
        typer.echo("✗ pass one or more HIVE, or --all", err=True)
        raise typer.Exit(1)
    do_pull, do_push = _pull_push_flags(pull, push)

    plan = sync_remote.sync_remote(
        dry_run=dry_run,
        verbose=verbose,
        hive_ids=list(hive) if hive else None,
        pull=do_pull,
        push=do_push,
        remote=remote or "",
        force=force,
    )
    if plan.offending:
        raise typer.Exit(1)


def _run_sync_peers(
    hive: list[str],
    all_hives: bool,
    peer: str | None,
    strategy: str | None,
    dry_run: bool,
) -> None:
    from . import hive_sync

    if not _hive_args_ok(hive, all_hives):
        typer.echo("✗ pass one or more HIVE, or --all", err=True)
        raise typer.Exit(1)
    if strategy and strategy not in hive_sync.STRATEGIES:
        typer.echo(f"✗ --strategy must be ours|theirs (got {strategy!r})", err=True)
        raise typer.Exit(1)

    offending = hive_sync.hive_sync(
        hive_ids=list(hive) if hive else None, peer=peer, strategy=strategy, dry_run=dry_run
    )
    if offending:
        raise typer.Exit(1)


_HIVE_ARG = typer.Argument(
    None, metavar="[HIVE]...", help="one or more registered hives (prefix / triplet / org/repo)"
)
_ALL_OPT = typer.Option(
    False,
    "--all",
    help="target every registered hive (HQ excluded; remote-only hives are reported and skipped)",
)
_DRY_RUN_OPT = typer.Option(False, "--dry-run", help="print the plan and change nothing")


@sync_app.callback(invoke_without_command=True)
def sync_default(
    ctx: typer.Context,
    hive: list[str] = _HIVE_ARG,
    all_hives: bool = _ALL_OPT,
    remote: str = typer.Option(
        None, "--remote", help="target a named dolt remote instead of every configured one"
    ),
    pull: bool = typer.Option(False, "--pull", help="pull only (skip push)"),
    push: bool = typer.Option(False, "--push", help="push only (skip pull)"),
    dry_run: bool = _DRY_RUN_OPT,
    force: bool = typer.Option(False, "--force", help="bd dolt push --force"),
):
    """No subcommand = `remotes`, the common case: pull then push every targeted hive's dolt
    remote(s)."""
    if ctx.invoked_subcommand is not None:
        return
    _run_sync_remotes(hive, all_hives, remote, pull, push, dry_run, force)


@sync_app.command(
    "remotes",
    help="bd dolt push/pull with a hive's dolt remote(s). Default (no --pull/--push): pull "
    "then push. --dry-run reports only, zero mutation.",
)
def sync_remotes_cmd(
    hive: list[str] = _HIVE_ARG,
    all_hives: bool = _ALL_OPT,
    remote: str = typer.Option(
        None, "--remote", help="target a named dolt remote instead of every configured one"
    ),
    pull: bool = typer.Option(False, "--pull", help="pull only (skip push)"),
    push: bool = typer.Option(False, "--push", help="push only (skip pull)"),
    dry_run: bool = _DRY_RUN_OPT,
    force: bool = typer.Option(False, "--force", help="bd dolt push --force"),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        help="for hives classified unpushed-dolt (embedded engine, dolt_status 'unknown'), "
        "also print a bounded list of recently-updated beads as approximate context.",
    ),
):
    _run_sync_remotes(hive, all_hives, remote, pull, push, dry_run, force, verbose)


@sync_app.command(
    "peers",
    help="bidirectional bead-state sync with a hive's federation peer(s) (bd federation sync): "
    "pull + push authoritative dolt state in one step, pausing on conflicts (re-run with "
    "--strategy ours|theirs). No --pull/--push — bd exposes no single-direction peer verb.",
)
def sync_peers_cmd(
    hive: list[str] = _HIVE_ARG,
    all_hives: bool = _ALL_OPT,
    peer: str = typer.Option(None, "--peer", help="target a named federation peer, or 'all'"),
    strategy: str = typer.Option(
        None,
        "--strategy",
        help="conflict resolution: ours|theirs (omit → pause and report conflicted tables)",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="read-only: render the federation status table, sync nothing"
    ),
):
    _run_sync_peers(hive, all_hives, peer, strategy, dry_run)


hive_app.add_typer(sync_app, name="sync", rich_help_panel=HIVE_PANEL)


@hive_app.command(
    "sync-remote",
    hidden=True,
    help="DEPRECATED — alias for `bh hive sync remotes --push`. Kept working, never removed "
    "without notice.",
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
    typer.echo(
        "⚠ `hive sync-remote` is deprecated — use `hive sync remotes --push` instead", err=True
    )
    if not all_hives:
        typer.echo("✗ pass --all (sync-remote targets the whole fleet)", err=True)
        raise typer.Exit(1)
    _run_sync_remotes([], all_hives, None, False, True, dry_run, False, verbose)


@hive_app.command(
    "onboard",
    help="onboard a hive end-to-end: clone it down (if --clone-url and absent), run hive init in "
    "the target, then sync this hive into the hub (fleet-wide aggregation deferred to the "
    "background by default — see --hub-sync). Works for an already-local folder or a remote repo.",
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
    codex: bool = typer.Option(
        False, "--codex", help="write a project-local Codex sandbox grant (see `hive init`)"
    ),
    global_grant: bool = typer.Option(
        False,
        "--global",
        help="modifier on --claude/--codex: grant the whole shared worktrees_root() in the "
        "harness's GLOBAL config instead of this hive's own subtree (see `hive init`)",
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
    hub_sync: bool = typer.Option(
        None,
        "--hub-sync/--no-hub-sync",
        help="fleet-wide hub aggregation after onboarding this hive (bh-d5jhc.1): default runs "
        "it in the background (best-effort, never blocks — this hive's own export still lands "
        "synchronously); --hub-sync waits for the full fleet-wide sync to complete; --no-hub-sync "
        "skips the hub entirely",
    ),
):
    from . import hive

    _reject_claude_skills_conflict_in_plugin_mode(claude, skills)
    _reject_global_without_claude_or_codex(global_grant, claude, codex)

    hive.onboard(
        hive_id,
        clone_url=clone_url,
        furnish=furnish,
        claude=claude,
        skills=skills,
        observaloop=observaloop,
        agents=agents,
        opencode=opencode,
        codex=codex,
        global_grant=global_grant,
        plugins=plugin,
        force=force,
        kind=kind,
        prefix=prefix,
        yes=yes,
        dry_run=dry_run,
        skip_check=skip_check,
        hub_sync=hub_sync,
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
    "migrate-storage",
    help="move a hive off bd's legacy embedded Dolt engine onto the fleet's shared-server mode: "
    "per hive, back up (verified) -> migrate -> verify -> report; fleet-wide (no HIVE_ID), "
    "resumable and per-hive isolated, Factory HQ migrated last. NOT `hive migrate` (that's the "
    "ws->bh rename) — this is a Dolt storage-mode move. Idempotent; --dry-run reports sizes and "
    "target paths and changes nothing; a real run needs --confirm.",
)
def hive_migrate_storage(
    hive_id: str = typer.Argument(
        "", help="hive id to migrate (default: every registered hive, HQ last)"
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="preview sizes/targets, change nothing"),
    confirm: bool = typer.Option(
        False, "--confirm", help="required to apply a real (non-dry-run) migration"
    ),
    keep_pre_migrate: bool = typer.Option(
        False,
        "--keep-pre-migrate",
        help="keep the moved-aside embedded store for an in-place rollback instead of removing "
        "it once verification passes (the verified backup set under $BH_HOME/backups/migrate/ "
        "is kept either way)",
    ),
):
    from . import storage_migrate

    storage_migrate.migrate(
        hive_id, dry_run=dry_run, confirm=confirm, keep_pre_migrate=keep_pre_migrate
    )


@hive_app.command(
    "repair",
    help="reconcile ONE piece of hive/host config drift — pass exactly one of --prefix / "
    "--node-id / --role / --server-database: (--prefix) detect the registry prefix vs. the "
    "beads-DB issue prefix, migrate the DB (bd rename-prefix), update the registry in place; "
    "(--node-id) set this HOST's node_id (~/.config/bd/config.yaml) from bh's own host "
    "identity; (--role) set the hive's beads.role (git config) from its registry kind; "
    "(--server-database) record the shared-server database name a server-mode hive already "
    "resolves, so it stops being re-derived. Idempotent; --yes required to mutate, --dry-run "
    "to preview.",
)
def hive_repair_cmd(
    prefix: str = typer.Option("", "--prefix", help="target canonical prefix (no trailing hyphen)"),
    node_id: bool = typer.Option(
        False, "--node-id", help="set this host's bd node_id from bh's host identity"
    ),
    role: bool = typer.Option(
        False, "--role", help="set the hive's beads.role from its registered kind"
    ),
    server_database: bool = typer.Option(
        False,
        "--server-database",
        help="record dolt_server_database from the name this server-mode hive already resolves "
        "(no-op for an embedded hive)",
    ),
    hive: str = typer.Option("", "--hive", help="target hive (default: cwd's hive)"),
    yes: bool = typer.Option(
        False,
        "--yes",
        help="required to apply a change (a prefix change orphans no bead IDs — bd "
        "rename-prefix rewrites every issue's id in place, but any prefix cached elsewhere goes "
        "stale); no prompt so this stays agent-drivable",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="print the detect/preview and change nothing"
    ),
):
    from . import hive_repair

    hive_repair.repair(
        hive=hive,
        prefix=prefix,
        node_id=node_id,
        role=role,
        server_database=server_database,
        yes=yes,
        dry_run=dry_run,
    )


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


@hive_app.command("check-push-fence", hidden=True)
def hive_check_push_fence(
    hive_dir: str = typer.Option(
        ..., "--hive-dir", help="the hive directory the hook script baked in at install time"
    ),
):
    """The pre-push fence hook's actual decision (bh-ytbb.12) — shelled out to from the
    `pre-push` git hook `prepush.install_for_hive` furnishes, never called directly by an
    operator. Reads `stdin` for git's own protocol only inasmuch as the hook script already
    filtered on it (a refs/dolt/data push); this command's own job is solely
    `prepush.check_fence`'s local-only primary/not-primary decision.

    Kept (hidden) for the hook scripts already installed in the wild — including transport-repo
    copies in other hives — which call it by name. `hive hook pre-push` is the entrypoint new
    callers should use; both reuse the same :func:`prepush.check_fence`."""
    from . import prepush

    ok, detail = prepush.check_fence(Path(hive_dir))
    if ok:
        raise typer.Exit(0)
    typer.echo(detail, err=True)
    raise typer.Exit(1)


# ---- hive hook: git-hook entrypoints for an external dispatcher (bh-smcj) -----

hive_hook_app = typer.Typer(
    no_args_is_help=True,
    help="git-hook entrypoints — call these from your own dispatcher (lefthook, a plain "
    f".git/hooks file, anything). {config.BINARY_ALIAS} does not install hook files.",
)
hive_app.add_typer(hive_hook_app, name="hook")


@hive_hook_app.command(
    "install",
    help="OPT-IN: install the pre-push fence shim into the repos git actually pushes "
    "refs/dolt/data from. Not run by `hive init`/onboard — the fence is a fast-fail "
    "convenience, not the enforcement.",
)
def hive_hook_install(
    hive_id: str = typer.Argument(
        None, metavar="[HIVE_ID]", help="hive to install for (default: the hive owning cwd)"
    ),
):
    """Install the `pre-push` fence shim for one hive — explicitly, never as a side effect
    (bh-smcj, docs/design/hooks-as-functionality-adr.md).

    Onboarding used to do this for every hive automatically. It no longer does, for two
    reasons. First, bh installing hook files behind your back is what that ADR forbids —
    it fights whatever dispatcher you actually use, and loses silently (`_write_hook` leaves a
    foreign `pre-push` alone and reports `"skipped (custom hook present)"`, which nobody
    reads). Second, and decisively: this hook was never the enforcement. It is a LOCAL,
    fast-fail refusal in front of the atomic `--force-with-lease` epoch fence
    (:mod:`beadhive.host_fence`), which rejects a stale-epoch push regardless of hooks and
    regardless of `--no-verify`. Defaulting it off costs an early, legible error — not safety.

    It stays available because the location that matters cannot be reached any other way: with
    bd's embedded engine, `bd dolt push` runs `git push` from a HIDDEN bare repo nested under
    `.beads/embeddeddolt/`, created lazily at a content-hash path. No dispatcher will ever be
    installed there, so this verb is the only way to fence that path early. The shim it writes
    holds no logic — it execs `bh hive hook pre-push <hive>`.

    Re-run it after the first `bd dolt push` on a fresh hive: the transport repo does not exist
    until then, so an earlier run has nothing to install into (and says so by omission)."""
    from . import prepush

    cfg = config.load()
    hive = hive_id or ""
    entry = registry.resolve_hive(cfg, hive) if hive else registry.current_hive(cfg)
    if entry is None:
        typer.echo("✗ cwd belongs to no managed hive — pass a HIVE_ID or run inside one.", err=True)
        raise typer.Exit(1)

    statuses = prepush.install_for_hive(registry.hive_dir(entry), str(entry["prefix"]))
    if not statuses:
        typer.echo(
            "• nothing to install into yet — no hooks dir found. With bd's embedded engine the "
            "transport repo appears on the first `bd dolt push`; re-run this after that."
        )
        return
    for line in statuses:
        typer.echo(f"✓ {line}")


@hive_hook_app.command(
    "pre-push",
    help="git pre-push: refuse a refs/dolt/data push when this host is not primary. Reads "
    "git's ref list on stdin; exit 0 allows, non-zero refuses.",
)
def hive_hook_pre_push(
    hive_id: str = typer.Argument(
        None, metavar="[HIVE_ID]", help="hive to fence (default: the hive owning cwd)"
    ),
):
    """The whole `pre-push` hook contract in one verb (bh-smcj,
    docs/design/hooks-as-functionality-adr.md) — stdin protocol, ref filter, and exit
    semantics together, so a dispatcher never has to know which refs matter. It pipes git's
    stdin in and honors the exit code; that is the entire integration.

    This replaces generating a shell script. `prepush.hook_script` built the same filter as a
    string and installed it, which meant any second dispatcher had to transcribe the
    `refs/dolt/data` check into a copy free to drift — and a copy that gets it wrong fails
    silently in both directions (fence every ordinary push, or never fence at all).

    The hive is resolved at RUN time (`registry.hive_dir_for`: the argument, else the hive
    owning cwd), not baked in at install time the way the generated script's absolute
    `hive_dir` was — so moving or re-cloning a hive cannot leave a hook pointing at a path
    that no longer exists.

    Fails OPEN for "nothing to fence" (no `refs/dolt/data` in the push, cwd is no managed hive,
    the multi-host model was never adopted) and CLOSED only for a real not-primary verdict —
    the same `prepush.check_fence` predicate `bh work`'s write verbs use."""
    from . import host_fence, prepush

    # git's pre-push protocol, one line per ref: "<local_ref> <sha> <remote_ref> <sha>".
    # isatty guards a by-hand invocation with no pipe, which would otherwise block on read().
    lines = [] if sys.stdin.isatty() else [ln for ln in sys.stdin.read().splitlines() if ln.strip()]
    if not lines and not sys.stdin.isatty():
        # A real push always sends at least one line. Empty almost always means the dispatcher
        # did not forward stdin (lefthook's `use_stdin: true`), which would silently disable
        # the fence -- say so rather than allow quietly.
        typer.echo(
            f"⚠ {config.BINARY_ALIAS} hive hook pre-push got no ref list on stdin — "
            f"the fence cannot run. Does your hook forward stdin?",
            err=True,
        )
    if not any(ln.split()[:1] == [host_fence.DATA_REF] for ln in lines):
        raise typer.Exit(0)

    ok, detail = prepush.check_fence(registry.hive_dir_for(config.load(), hive_id or ""))
    if ok:
        raise typer.Exit(0)
    typer.echo(detail, err=True)
    raise typer.Exit(1)


@hive_hook_app.command(
    "push-main",
    help="git pre-push (integration branch): exit 0 ONLY when a fresh green `push-main` "
    "verdict already exists for REV's exact tree. Every other outcome — miss, stale, "
    "invalid, error — is non-zero and MEANS RUN THE FULL GATE.",
)
def hive_hook_push_main(
    rev: str = typer.Argument(
        ..., metavar="REV", help="the sha being pushed (git's `local_sha` for the main ref)"
    ),
    gate: str = typer.Option(
        "",
        "--gate",
        metavar="CMD",
        help="REQUIRED: the command the caller runs on a miss; `work.validate.push-main` must "
        "resolve to exactly this or the lookup refuses (a phase naming a weaker command is not "
        "a verdict about this gate)",
    ),
    hive_id: str = typer.Option(
        "", "--hive", metavar="HIVE_ID", help="hive to consult (default: the hive owning cwd)"
    ),
):
    """The attested-green lookup for the outermost, most expensive gate (bh-ku9n9.5,
    `docs/design/attested-green-adr.md`) — the whole hook contract in a verb, so the hook file
    stays one line and cannot drift from bh's own notion of the gate (bh-smcj,
    `docs/design/hooks-as-functionality-adr.md`).

    Exit 0 says one thing only: a real, confirming run already exercised the exact tree this
    push would land, under the exact command this gate would otherwise run, recently enough to
    trust. **Non-zero is not an error — it is the normal answer**, and it means the caller runs
    the full gate inline exactly as it did before this verb existed. A miss, a stale entry, a
    red or malformed record, an unconfigured `work.validate.push-main`, a phase that names a
    different command, no hive, no clone, an unresolvable rev, a corrupt config, an exception of
    any kind: all non-zero. **No path here treats a missing or unreadable attestation as a
    pass** — the worst case of consulting it is the behaviour you already had.

    WHAT THIS DOES NOT SOLVE: the miss path still runs the full ~371s gate inside the push,
    holding a connection git opened before the hook started, so the SSH keepalive from bh-53o8f
    (`just push` / `scripts/push-main.sh`) remains required. This makes that path rarer, not
    safe to run bare."""
    from . import prepush

    # `--gate` is required, not merely conventional (bh-ku9n9.19, item 8): an EMPTY gate_cmd
    # makes `push_main_cmd` skip its command-equality check entirely (it stays permissive there
    # for `release.py`'s own optional `--gate`), so a hive configuring `push-main: "true"` could
    # earn an exit 0 from a caller that forgot to name its own command. Refusing here removes
    # that path rather than relying on the only in-repo caller (`scripts/main-push-gate.sh`)
    # always passing one.
    if not gate:
        typer.echo("✗ --gate is required — name the command this gate runs on a miss", err=True)
        raise typer.Exit(1)

    ok, detail = prepush.check_push_main(rev, hive_id=hive_id, gate_cmd=gate)
    typer.echo(detail, err=not ok)
    raise typer.Exit(0 if ok else 1)


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

    cfg = config.load_host()  # read-modify-write: save() must only persist host-owned content
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

    cfg = config.load_host()  # read-modify-write: save() must only persist host-owned content
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


def _echo_prune_plan(adir: str, all_repos: bool, days: float, dry_run: bool) -> None:
    """Announce the prune plan (repos affected + threshold) before mutating anything."""
    tag = "DRY-RUN " if dry_run else ""
    if all_repos:
        typer.echo(f"{tag}prune: removing ALL archived repos under {adir}")
    else:
        typer.echo(f"{tag}prune: removing repos archived more than {days:.0f}d ago under {adir}")


def _report_prune_result(result, adir: str, cfg, dry_run: bool) -> None:
    """Print per-repo removal lines + the reclaimed/would-reclaim total; on a real (non-dry-run)
    prune, also invalidate the now-purged repos' metadata cache entries."""
    from . import archive as archive_mod
    from .safety import format_bytes

    if not result.removed:
        typer.echo("  nothing to prune")
        return

    for triplet in result.removed:
        verb = "would remove" if dry_run else "removed"
        typer.echo(f"  {verb}: {triplet}")

    if dry_run:
        total = sum(
            r.size_bytes for r in archive_mod.list_archived(adir) if r.triplet in result.removed
        )
        typer.echo(f"\n  Would reclaim {format_bytes(total)} across {len(result.removed)} repo(s)")
    else:
        from . import metadata

        for triplet in result.removed:  # drop any lingering entry for a now-purged repo
            metadata.invalidate(cfg, triplet, reload=False)
        n = len(result.removed)
        typer.echo(f"\nReclaimed {format_bytes(result.reclaimed_bytes)} across {n} repo(s)")


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

    cfg = config.load()
    adir = config.archive_dir(cfg)

    if older_than:
        days = _parse_older_than(older_than)
    else:
        days = float(config.archive_window_days(cfg))

    _echo_prune_plan(adir, all_repos, days, dry_run)

    result = archive_mod.prune_archived(
        adir, older_than_days=days, remove_all=all_repos, dry_run=dry_run
    )

    _report_prune_result(result, adir, cfg, dry_run)


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
    # host.yaml is identity, not template output: minted exactly once and never rewritten,
    # not even by --force (see beadhive.host module docstring) — config.scaffold_home()
    # never re-mints it regardless of `force`.
    for dst, wrote in config.scaffold_home(force=force):
        typer.echo(f"wrote {dst}" if wrote else f"skip {dst} (exists)")

    typer.echo(f"✓ edit {config.config_path()} and copy .env.example → .env")


@config_app.command(
    "split",
    help="one-time migration: split an existing flat config.yaml into fleet.yaml + a "
    "reduced host config.yaml, per the fleet/host partition. Idempotent; backs up the "
    "original to config.yaml.bak first; --dry-run previews the split with zero mutation.",
)
def config_split(
    dry_run: bool = typer.Option(False, "--dry-run", help="preview the split; no writes"),
):
    from . import config_split_migration

    config_split_migration.split_flat_config(dry_run=dry_run)


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


def _load_config_or_exit():
    """Load the resolved config; exit 1 with `config init` guidance instead of a traceback
    when no config file exists yet."""
    try:
        return config.load()
    except FileNotFoundError:
        typer.echo(
            f"no config found — scaffold it with `{config.BINARY_ALIAS} config init`.", err=True
        )
        raise typer.Exit(1) from None


def _print_fix_prompt(cv, cfg) -> None:
    """`--fix`: print the paste-ready agentic-update prompt, or a no-op confirmation when the
    config is already current."""
    prompt = cv.agentic_update_prompt(cfg)
    if prompt is None:
        typer.echo(f"✓ config is already at schema v{cv.SCHEMA_VERSION} — nothing to fix.")
        return
    typer.echo(prompt)


def _report_validation_problems(cv, cfg, problems) -> None:
    """Print validation problems + the ws→bh rename table + a paste-ready agentic-update offer
    for a stale config, then exit 1 on any error-level problem (0 when only warnings)."""
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

    cfg = _load_config_or_exit()

    if fix:
        _print_fix_prompt(cv, cfg)
        return

    problems = cv.validate_config(cfg)
    if not problems:
        typer.echo(f"✓ config is valid (schema v{cv.SCHEMA_VERSION}).")
        return

    _report_validation_problems(cv, cfg, problems)


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


#: --scope values `config get/set/unset` accept — mirrors `config.SCOPE_FLEET`/`SCOPE_HOST`.
_SCOPE_HELP = "fleet|host — read/write the named layer instead of the merged/default view"


def _resolve_scope(scope: str) -> str | None:
    """Validate a `--scope` option: "" (unset) passes through as None; anything other than
    `fleet`/`host` exits 1 with a clear message instead of silently misrouting the read/write."""
    if not scope:
        return None
    if scope not in (config.SCOPE_FLEET, config.SCOPE_HOST):
        typer.echo(
            f"✗ --scope must be '{config.SCOPE_FLEET}' or '{config.SCOPE_HOST}', got {scope!r}",
            err=True,
        )
        raise typer.Exit(1)
    return scope


@config_app.command(
    "get",
    help=f"read a dotted config key (e.g. `{config.BINARY_ALIAS} config get otel.enabled`).",
)
def config_get(
    key: str = typer.Argument(..., help="dotted.key path into the config"),
    scope: str = typer.Option("", "--scope", help=_SCOPE_HELP + " (default: merged view)"),
):
    res = config.get_value(key, scope=_resolve_scope(scope))
    if not res["ok"]:
        _echo_problems(res["problems"])
        raise typer.Exit(1)
    _echo_value(res["value"])


@config_app.command("set", help="set a dotted config key (bool/int coercion; --json for maps).")
def config_set(
    key: str = typer.Argument(..., help="dotted.key path into the config"),
    value: str = typer.Argument(..., help="value (true|false→bool, integer→int, else string)"),
    as_json: bool = typer.Option(False, "--json", help="parse value as JSON (lists/maps/literals)"),
    scope: str = typer.Option("", "--scope", help=_SCOPE_HELP + " (default: host)"),
):
    res = config.set_value(key, value, as_json=as_json, scope=_resolve_scope(scope))
    _echo_problems(res["problems"])
    if not res["ok"]:
        raise typer.Exit(1)
    typer.echo(f"✓ {key} = {res['new']!r}")


@config_app.command(
    "unset",
    help=f"delete a dotted config key (e.g. `{config.BINARY_ALIAS} config unset otel`).",
)
def config_unset(
    key: str = typer.Argument(..., help="dotted.key path into the config"),
    scope: str = typer.Option("", "--scope", help=_SCOPE_HELP + " (default: host)"),
):
    res = config.unset_value(key, scope=_resolve_scope(scope))
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

    # ponytail: blocking subprocess.run is intentional here, not a hot-path deferral candidate
    # — `mcp install` is a one-shot interactive admin verb a human runs once per machine, and the
    # command's own success/failure message (below) depends on `claude mcp add`'s exit code, so
    # the call has to be synchronous regardless.
    result = subprocess.run(cmd, check=False)  # noqa: S603
    if result.returncode != 0:
        typer.echo(f"✗ 'claude mcp add' exited {result.returncode}", err=True)
        raise typer.Exit(result.returncode)
    typer.echo(f"✓ {config.BINARY_ALIAS} MCP server registered with Claude Code (scope={scope}).")


# ---- setup ------------------------------------------------------------------


@setup_app.command("check", help=f"probe post-{config.BINARY_ALIAS} deps and cache the result.")
def setup_check(
    as_json: bool = typer.Option(
        False, "--json", help="emit the structured, schema-versioned check result as JSON"
    ),
):
    """`--json` (bh-0olv9.2) emits `setup.check_payload`: per-tool presence, version,
    satisfied/unsatisfied and the per-tool REMEDY, plus the advisories — on stdout with nothing
    interleaved. Same probe, same cache write, same exit code as the text render, because the
    text render is this same object echoed rather than a second assembly of it."""
    from . import setup as setup_mod

    setup_mod.run_check(as_json=as_json)


@setup_app.command("show", help="report cached setup status without re-probing.")
def setup_show():
    from . import setup as setup_mod

    setup_mod.run_show()


@setup_app.command(
    "guide",
    help="export the bundled setup Guide to ~/.beadhive/guides/setup/, then hand it to your "
    "harness — or walk it here with --wizard.",
)
def setup_guide_cmd(
    wizard: bool = typer.Option(
        False, "--wizard", help="walk the exported steps interactively in this terminal"
    ),
    handoff: bool = typer.Option(
        False, "--handoff", help="export + print the walk instruction only; never prompt"
    ),
    force: bool = typer.Option(
        False, "-f", "--force", help="overwrite exported files that differ from the bundled copy"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="report what would be exported, change nothing"
    ),
):
    """WHY THIS LIVES UNDER `setup` (bh-0olv9.6) — the same call `setup toolchain` made
    (bh-vmdq.7): `setup` already owns the probe that REPORTS the gap (`setup check`), so it owns
    the thing that closes it. `bh dep` is a table surface over individual tools; a guided walk is
    not a dep row.

    Export is idempotent and never silently clobbers: a file you edited is left alone and named
    in the report, with `--force` as the way to take the bundled copy instead.

    The default hands off rather than guessing at your harness — see `setup_guide`'s module
    docstring for why Guide-awareness is not probed. `--wizard` forces the CLI fallback, whose
    step list is DERIVED from the exported `steps/` files, never hardcoded here.
    """
    from . import setup_guide as guide_mod

    if wizard and handoff:
        typer.echo("✗ --wizard and --handoff are opposite branches — pass at most one.", err=True)
        raise typer.Exit(2)
    guide_mod.run_guide(wizard_mode=wizard, handoff_mode=handoff, force=force, dry_run=dry_run)


@setup_app.command(
    "toolchain",
    help="install the pinned toolchain (bd, dolt, gh, git-workspace) via nix — no checkout needed.",
)
def setup_toolchain(
    dry_run: bool = typer.Option(False, "--dry-run", help="print the command, change nothing"),
):
    """WHY THIS LIVES UNDER `setup` AND NOT `dep` (bh-vmdq.7).

    It IS a real choice between two commands — `bh dep` ships in this same release with
    list/show/install/auth. The decision rests on what each one owns:

    `bh dep` is PER-DEP and deliberately does not install infra. All four infra rows
    (git-workspace, gh, bd, dolt) carry no install route on purpose, because there is no
    universal per-tool command for them and putting that branch inside the table is what ADR
    Decision 5 forbids — the bh-tccp bug family (five instances) came from exactly that
    knowledge leaking into per-row prose. Giving those rows an install route here would be that
    change by another name.

    The toolchain is not four installs; it is ONE nix invocation that places all four at once,
    pinned together by flake.lock. That is a statement about machine readiness, which is
    `setup`'s concern — and `setup check` is already the verb that reports the gap, so remedy
    belongs beside the thing that names it.

    THIS DOES NOT MAKE `bh` AN INSTALLER OF INFRA DEPS. It shells out to nix and nothing else:
    no per-tool install commands, no platform branch, no `install.cmd` rows for the four infra
    deps. ADR Decision 5's no-platform-branching property is untouched — the nix profile stays
    the mechanism and `bh` only invokes it. That distinction is the whole reason the dependency
    table is not the place for this.

    NIX ITSELF IS NOT INSTALLED HERE, and cannot be: it needs root (and on macOS creates an APFS
    volume), which is a human step on any machine with hardware-token sudo or a corporate policy
    against daemon installs. This refuses with a pointer rather than pretending otherwise.
    """
    import shutil

    from . import setup as setup_mod

    if shutil.which("nix") is None:
        typer.echo("nix not found — it is a prerequisite, not something bh installs.")
        typer.echo("  Installing nix needs root; see INSTALL.md's managed path.")
        raise typer.Exit(1)

    cmd = setup_mod.toolchain_install_cmd()
    if dry_run:
        typer.echo(" ".join(cmd))
        return
    rc = setup_mod.install_toolchain(cmd)
    raise typer.Exit(rc)


# ---- `bh harness …` — thin aliases onto `bh dep` (bh-hsus.6) -----------------
#
# "harness" is a FILTER over the dep table (`kind == "harness"`), not a noun of its own: the verb
# had to probe `gh`, which runs no seat and is not a harness. These three survive because
# bh-q160.3's acceptance and the documented adoption sequences name them, and each one is a
# CALL into `dep_cli` rather than a second implementation — there is no path by which the alias
# and the canonical verb can drift apart.


@harness_app.command("list", help="alias: `bh dep list --kind harness`.")
def harness_list():
    dep_cli.ls(kind="harness", missing=False)


@harness_app.command("auth", help="alias: `bh dep auth [<name>] [--check]`.")
def harness_auth(
    name: str = typer.Argument("", help="probe one row only (gh|claude|codex)"),
    check: bool = typer.Option(
        False, "--check", help="exit non-zero when the host is not usable (CI/headless gate)."
    ),
):
    dep_cli.auth(name=name, check=check)


@harness_app.command("install", help="alias: `bh dep install <name>`.")
def harness_install(
    name: str = typer.Argument(
        ..., help="harness to bootstrap (claude; codex names its own remedy)"
    ),
    version: str = typer.Option(
        "",
        "--version",
        help=(
            "install target (stable|latest|X.Y.Z); defaults to $BH_CLAUDE_CODE_VERSION if set. "
            "Pins ONLY this initial bootstrap — once claude is on PATH it owns its own version "
            "(`claude install <target>` / `claude update`, background auto-update on by default), "
            "and this flag has no further effect. It is never consulted for an already-installed "
            "harness, so it cannot fight that auto-update."
        ),
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="skip the proprietary-licence confirmation (for headless runs)."
    ),
):
    dep_cli.install(name=name, version=version, yes=yes)


# ---- top-level --------------------------------------------------------------


@app.command(
    "doctor",
    rich_help_panel=ADMIN_PANEL,
    help="status + diagnostics: providers, orgs, repo counts, warnings.",
)
def doctor_cmd(
    as_json: bool = typer.Option(
        False, "--json", help="emit the structured, schema-versioned diagnostics as JSON"
    ),
    verbose: bool = typer.Option(
        False, "-v", "--verbose", help="also print the per-section timings breakdown (bh-8nnh7)"
    ),
    seats: bool = typer.Option(
        False,
        "--seats",
        help=(
            "run the full per-seat `hitch profile preflight` check (~2.7s, 7 seats); the "
            "default report only checks that hitch itself is usable and says so (bh-gqfrm)"
        ),
    ),
):
    """`--json` (bh-0olv9.2) emits `doctor.doctor_payload` — the same section-keyed object the
    text render is built from, and the same one the `beadhive://doctor` MCP resource serves.
    `--verbose` prints the per-section wall-clock timings under the text report; the JSON
    payload always carries them regardless of this flag. `--seats` opts into the full 7-seat
    hitch preflight fanout (bh-gqfrm) — the default Seats section skips it and says so."""
    from . import doctor

    doctor.doctor(as_json=as_json, verbose=verbose, seats=seats)


@alerts_app.command("show", help="render active agent-steering alerts.")
def alerts_show(
    as_json: bool = typer.Option(False, "--json", help="emit the normalized alert list as JSON"),
):
    """Print active alerts, or an explicit clean result when there are none."""
    rows = alerts.active()
    if as_json:
        jsonout.emit(rows)
        return
    if not rows:
        typer.echo("✓ no active alerts")
        return
    for row in rows:
        typer.echo(f"[{row['severity']}] {row['code']}: {row['message']}")
        typer.echo(f"  Remediation: {row['remediation']}")


# ---- backup (bh-cmqp.2, bh-5009a) --------------------------------------------
# Four roots, one boundary contract: docs/design/backup-retention-boundary-adr.md.
# `export`/`usage`/`reclaim`/`migrate-layout` — a real Typer group (not a bare command with
# subcommands bolted on): a positional `dest` argument and named subcommands are ambiguous
# together in Click's parser (confirmed empirically before choosing this shape over it —
# `bh backup usage` would silently export to a directory literally named "usage").
backup_app = typer.Typer(
    no_args_is_help=True,
    help="Backup roots: HQ pre-push snapshots, bd's own per-hive Dolt backup, the JSONL "
    "interchange mirror, and migrate-storage's pre-migration sets — see "
    "docs/design/backup-retention-boundary-adr.md for the boundary + retention policy "
    "behind each.",
)
app.add_typer(backup_app, name="backup", rich_help_panel=ADMIN_PANEL)


@backup_app.command("export", help="export issues to a JSONL mirror (was the bare `bh backup`).")
def backup_export(
    dest: str = typer.Argument(
        None,
        help="export destination (default: a fixed per-hive path under "
        "$BH_HOME/backups/mirrors/, independent of cwd — see `bh backup usage`)",
    ),
):
    """Ad hoc interchange snapshot — overwrites `issues.jsonl` in place each run (no history
    retained by design; pass an explicit `dest` for a series of dated copies, at which point
    keeping/pruning them is on you). NOT a restore source for `bh hq restore` or `bd backup
    restore` — see the ADR for why this is deliberately separate from those."""
    from . import backup as backup_mod

    out_dir = Path(dest) if dest else backup_mod.mirror_root()
    out_dir.mkdir(parents=True, exist_ok=True)
    run(["bd", "export", "-o", f"{out_dir}/issues.jsonl", "--all"])
    typer.echo(f"exported → {out_dir}/issues.jsonl")


@backup_app.command("usage", help="disk usage + retention policy across every backup root.")
def backup_usage_cmd(
    as_json: bool = typer.Option(False, "--json", help="emit machine-readable JSON"),
):
    import json as json_mod

    from . import backup as backup_mod
    from .safety import format_bytes

    cfg = config.load()
    entries = backup_mod.usage_report(cfg)
    warning = backup_mod.total_warning(entries, cfg)

    if as_json:
        out = {
            "roots": [
                {
                    "root": e.root,
                    "label": e.label,
                    "path": str(e.path),
                    "size_bytes": e.size_bytes,
                    "detail": e.detail,
                }
                for e in entries
            ],
            "total_bytes": sum(e.size_bytes for e in entries),
            "warning": warning,
        }
        typer.echo(json_mod.dumps(out, indent=2))
        return

    typer.echo("Backup usage:")
    col_w = max(len(e.label) for e in entries)
    for e in entries:
        typer.echo(f"  {e.label:<{col_w}}  {format_bytes(e.size_bytes):>10}  {e.path}")
        typer.echo(f"  {'':<{col_w}}  {'':>10}  {e.detail}")
    total = sum(e.size_bytes for e in entries)
    typer.echo(f"\n  total: {format_bytes(total)} across {len(entries)} root(s)")
    if warning:
        typer.echo(f"  ⚠ {warning}")


@backup_app.command(
    "migrate-layout",
    help="one-time relocation of pre-bh-5009a backup artifacts into $BH_HOME/backups/"
    "{hq,mirrors,migrate}/, and heal any hive left with a dangling/mis-pointed bd backup "
    "registration (bh-ypfnu). Reads work either way — this just stops `usage` reporting a "
    "legacy row. --dry-run previews (default), --confirm applies.",
)
def backup_migrate_layout_cmd(
    dry_run: bool = typer.Option(False, "--dry-run", help="preview the moves; no writes"),
    confirm: bool = typer.Option(False, "--confirm", help="required to actually relocate"),
):
    from . import backup as backup_mod
    from .identity import resolve_actor
    from .safety import format_bytes

    cfg = config.load()
    preview = dry_run or not confirm
    result = backup_mod.migrate_layout(cfg, dry_run=preview, actor=resolve_actor("", ""))

    if not result.moves:
        typer.echo("nothing to relocate — every backup artifact is already in the current layout")
        return
    for m in result.moves:
        mark = "✗" if m.error else ("○" if preview else "✓")
        typer.echo(f"  {mark} [{m.kind}] {m.src} -> {m.dest}  ({format_bytes(m.size_bytes)})")
        if m.error:
            typer.echo(f"      {m.error}")
        elif m.how:
            typer.echo(f"      {m.how}")
    for note in result.notes:
        typer.echo(f"  note: {note}")
    verb = "would relocate" if preview else "relocated"
    typer.echo(f"\n  {verb} {len(result.moves)} set(s), {format_bytes(result.moved_bytes)}")
    if preview:
        typer.echo("  (preview — pass --confirm to apply)")
    elif not result.ok:
        raise typer.Exit(1)


@backup_app.command(
    "reclaim",
    help="apply each root's retention policy: --dry-run previews, --root narrows, --confirm "
    "is required to actually rotate the hive root.",
)
def backup_reclaim_cmd(
    root: str = typer.Option("all", "--root", help="hq | hive | migrate | all"),
    hive_id: str = typer.Option(
        "", "--hive", help="hive for the hive root's rotate (default: cwd's hive)"
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="preview the plan; no writes"),
    confirm: bool = typer.Option(
        False,
        "--confirm",
        help="proceed with a real hive-root rotate (bd's own backup), or with removing a kept "
        "in-repo pre-migrate store",
    ),
    force: bool = typer.Option(
        False, "--force", help="rotate the hive root even under backup.hive_cap_mb"
    ),
):
    from . import backup as backup_mod
    from .safety import format_bytes

    if root not in ("hq", "hive", "migrate", "all"):
        typer.echo(f"✗ --root must be hq | hive | migrate | all, got {root!r}", err=True)
        raise typer.Exit(1)
    cfg = config.load()

    if root in ("hq", "all"):
        result = backup_mod.prune_hq_backups(cfg, dry_run=dry_run)
        verb = "would prune" if dry_run else "pruned"
        if result.removed:
            typer.echo(
                f"hq: {verb} {len(result.removed)} old set(s) "
                f"({format_bytes(result.reclaimed_bytes)}): {', '.join(result.removed)}"
            )
        else:
            typer.echo("hq: nothing to prune")

    if root in ("hive", "all"):
        hive_dir = registry.hive_dir_for(cfg, hive_id)
        rotate = backup_mod.rotate_hive_backup(
            hive_dir, cfg, dry_run=dry_run, confirm=confirm, force=force
        )
        for line in rotate.actions:
            typer.echo(f"hive ({hive_dir.name}): {line}")
        if rotate.rotated_to is not None:
            prune = backup_mod.prune_hive_rotated(hive_dir, cfg, dry_run=dry_run)
            if prune.removed:
                verb = "would prune" if dry_run else "pruned"
                typer.echo(
                    f"hive: {verb} {len(prune.removed)} old generation(s) "
                    f"({format_bytes(prune.reclaimed_bytes)})"
                )
        if not rotate.ok and not dry_run:
            raise typer.Exit(1)

    if root in ("migrate", "all"):
        prune = backup_mod.prune_migrate_backups(cfg=cfg, dry_run=dry_run)
        verb = "would prune" if dry_run else "pruned"
        if prune.removed:
            typer.echo(
                f"migrate: {verb} {len(prune.removed)} old set(s) "
                f"({format_bytes(prune.reclaimed_bytes)}): {', '.join(prune.removed)}"
            )
        else:
            typer.echo("migrate: nothing to prune")

        # The in-repo half. Gated behind --confirm even though the sets above are not: this
        # deletes a directory inside the operator's own working tree, which is a different kind
        # of blast radius from pruning bh's own artifact root.
        stores = backup_mod.pre_migrate_stores(cfg)
        if stores:
            preview = dry_run or not confirm
            removal = backup_mod.prune_pre_migrate_stores(cfg, dry_run=preview)
            verb = "would remove" if preview else "removed"
            typer.echo(
                f"migrate: {verb} {len(removal.removed)} in-repo pre-migrate store(s) "
                f"({format_bytes(removal.reclaimed_bytes)})"
            )
            for path in removal.removed:
                typer.echo(f"    {path}")
            if preview:
                typer.echo("    (pass --confirm to remove them)")


def _exception_group_leaves(exc: BaseException) -> list[BaseException]:
    """Every LEAF exception inside *exc*, recursing through nested groups.

    An `ExceptionGroup` stringifies to "unhandled errors in a TaskGroup (1 sub-exception)" —
    which names neither the failing operation nor the error. `bh work loop` runs its passes in a
    TaskGroup, so EVERY failure inside one reached the operator with the only useful information
    already discarded, and `BH_DEBUG=1` printed the same two lines (bh-x2yy0). A host missing the
    `ps` binary presented exactly that way; the cause was found only by installing procps and
    noticing the symptom stop.

    Returns `[exc]` for an ordinary exception, so callers have one shape to handle.
    """
    if not isinstance(exc, BaseExceptionGroup):
        return [exc]
    leaves: list[BaseException] = []
    for sub in exc.exceptions:
        leaves.extend(_exception_group_leaves(sub))
    return leaves


def _handle_cli_error(exc: Exception) -> None:
    """Boundary handler for an unhandled exception escaping a CLI command.

    Observes the failure across all three telemetry signals — a structlog ``cli_command_error``
    line (always, even otel-off), the active span (record_exception + ERROR status, no-op when
    off), and the ``ws.errors`` counter (no-op when off) — then surfaces a concise stderr line
    instead of a bare traceback. The non-zero exit is the caller's ``SystemExit(1)``.

    An ``ExceptionGroup`` is reported by its LEAVES as well as by the group (bh-x2yy0): the
    group line alone carries no cause, and the group is what a TaskGroup — which is how the
    local dispatch tier runs every pass — always raises. Both go to stderr and to the structured
    record, so neither a human nor a log reader has to guess.

    Only *genuine* unhandled exceptions reach here: control-flow exits (``typer.Exit`` codes,
    validation failures → ``SystemExit``) are re-raised untouched in ``main`` and never observed
    as errors. The dqw.2 invocation counter has already tagged this path outcome=error via
    ``call_on_close`` inside ``app()``, so ``count_error`` is additive, not a double-count."""
    command = next((arg for arg in sys.argv[1:] if not arg.startswith("-")), "")
    leaves = _exception_group_leaves(exc)
    causes = [{"error_type": type(e).__name__, "error": str(e)} for e in leaves]
    log.get_logger(__name__).error(
        "cli_command_error",
        command=command,
        error_type=type(exc).__name__,
        error=str(exc),
        # Absent for an ordinary exception, where the two fields above already say everything.
        **({"causes": causes} if isinstance(exc, BaseExceptionGroup) else {}),
    )
    otel.record_exception(exc)
    otel.count_error("cli", type(exc).__name__)
    typer.echo(f"✗ {type(exc).__name__}: {exc}", err=True)
    if isinstance(exc, BaseExceptionGroup):
        for leaf in leaves:
            typer.echo(f"  ↳ {type(leaf).__name__}: {leaf}", err=True)


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
