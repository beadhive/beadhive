"""The ``bh`` root callback and process boundary, shared by every command-tree composition.

Both the full command tree (:mod:`beadhive.cli`) and the import-lazy intake fast path
(:mod:`beadhive.cli_fastpath`, bh-lwz8a) mount :func:`_root` as their root callback and run
through :func:`run`, so a verb behaves the same whichever tree carried it: the per-invocation
config and identity memos, the best-effort migrations and warnings, telemetry, the setup gate and
the hive-routing check. Keep this module's imports light: it is on the fast path.
"""

from __future__ import annotations

import importlib.metadata
import sys
import time

import typer

from . import config, home_migration, identity, log, otel, plugins

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


def _is_help_invocation(ctx: typer.Context) -> bool:
    """True for a pure `--help`/`-h` pass or shell completion (NOT `--dry-run`): nothing on
    that path may load runtime/HQ configuration (bh-o01m6)."""
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


def _warn_retired_orca_worktrees_best_effort(ctx: typer.Context) -> None:
    """Say out loud, once per invocation, that ``orca.worktrees`` no longer does anything
    (bh-055ot.1): Orca's worktree delegation is retired and the configured
    ``worktrees.manager`` owns worktree mechanics, so a hive still setting the flag must not
    believe Orca is creating its worktrees. Same placement rule and same `--help`/completion
    exemption as the schema-staleness nudge above."""
    if _is_help_or_completion_invocation(ctx):
        return
    try:
        config.warn_retired_orca_worktrees_if_needed()
    except Exception:
        pass


def _announce_authority_enforcement(ctx: typer.Context) -> None:
    """``BH_HQ_AUTHORITY_ENFORCE`` (UNSUPPORTED, dev-only, bh-6pqul): print the disabled banner
    once on stderr for every command while it is ``false``, and refuse an invalid value outright
    rather than guessing. ``--help``/completion stay usable so the operator can still read help
    while fixing the variable."""
    from . import hq_authority_enforce

    try:
        hq_authority_enforce.emit_banner()
    except hq_authority_enforce.AuthorityEnforcementError as exc:
        if _is_help_or_completion_invocation(ctx):
            return
        typer.echo(f"✗ {exc}", err=True)
        raise typer.Exit(2) from exc


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
        # Per-worktree endpoint overlay: prefer `.bh/observability/otel.env`; a flat
        # `.bh/otel.env` remains a bounded read-only compatibility input.
        # load it into os.environ BEFORE init so config.otel_endpoint / config.observaloop_profile
        # pick up the hive profile's endpoint + name. The common path is a single file read with no
        # beadhive.observaloop import (only the self-heal branch touches observaloop);
        # best-effort, so it never blocks startup. observaloop_env imports config + worktree
        # only — not observaloop.
        from . import observaloop_env

        observaloop_env.load_worktree_env(_cfg)
        otel.init(_cfg)
        plugins.configure_semantic_telemetry(otel.current_semantic_telemetry())
    except Exception:  # best-effort telemetry; never break the CLI on init/config-load failure
        pass


def _instrument_command_entry(ctx: typer.Context, *, command_name: str | None = None) -> None:
    """Instrument the command-entry seam: register a call_on_close hook that emits a counter +
    histogram tagged with the invoked subcommand name + outcome (ok/error). Gated on
    is_active() so the off-path (default: otel disabled) is a single bool read — zero SDK
    import, zero allocation. The --version eager path exits before this body, so it's untouched."""
    if not otel.is_active():
        return
    _start = time.monotonic()
    _cmd = command_name if command_name is not None else ctx.invoked_subcommand or ""
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


#: Verbs that run for the life of a service. They must not freeze ``GIT_WORKSPACE`` for the
#: process lifetime, so they never opt into ``identity.process_config_memo`` (bh-olnhf) or
#: ``config.process_load_memo`` (bh-pc9rf). ``cli_entrypoint`` keeps its own copy (it runs
#: before this module is imported).
_LONG_LIVED_VERBS = frozenset({"host", "mcp"})


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
    if ctx.invoked_subcommand not in _LONG_LIVED_VERBS:
        # One-shot verb: resolve the effective config once for every child it spawns instead of
        # re-qualifying the SQL fleet snapshot per `bd`/`git` launch (bh-olnhf). The serving
        # verbs stay on the uncached path; see identity._ProcessConfigMemo.
        ctx.with_resource(identity.process_config_memo())
        # ...and every other config read of the verb resolves it once too (bh-pc9rf): one
        # `outbox show` used to re-qualify the SQL snapshot 7 times. Same exclusion, same reason.
        ctx.with_resource(config.process_load_memo())
    if ctx.invoked_subcommand == "bd":
        # A bead read/write needs only hive-local config: in trusted mode a transient HQ
        # config outage may degrade to the bounded-age cached verified snapshot (bh-kjxe9).
        ctx.with_resource(config.hive_local_config_reads())
    _announce_authority_enforcement(ctx)
    # `--help` / completion is informational: it must never read runtime config — which may
    # sit behind HQ SQL that is down during partial degradation (bh-o01m6) — so the config
    # migrations and the config-loading telemetry init are skipped for it.
    help_only = _is_help_invocation(ctx)
    if not help_only:
        _migrate_home_best_effort()
        _migrate_hive_keys_best_effort()
    _warn_stale_schema_version_best_effort(ctx)
    _warn_missing_fleet_config_best_effort(ctx)
    _warn_literal_violations_best_effort(ctx)
    _warn_retired_orca_worktrees_best_effort(ctx)
    # ``host daemon serve`` owns a daemon-scoped provider for its entire outer lifespan.  Defer
    # generic CLI telemetry through the nested host/daemon callbacks so that supported entrypoint
    # does not consume the process-global SDK first.  Every other command still initializes the
    # ordinary short-lived CLI provider before its handler runs.
    if ctx.invoked_subcommand != "host" and not help_only:
        _init_telemetry_best_effort()
        _instrument_command_entry(ctx)
    # Same informational-only exemption as the schema-staleness nudge above (bh-sn9q): a
    # subcommand's `--help`/`-h` or shell-completion must never be blocked by the setup gate
    # (it would otherwise swallow the help text entirely on a fresh, ungated install).
    if not _is_help_or_completion_invocation(ctx):
        _enforce_setup_gate(ctx)
    mode = _resolve_hive_routing_mode(ctx, all_hives, hive)
    ctx.obj = (mode, hive)


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
    if command in ("worktree", "wt") and any(
        "SqlConfigError" in {c.__name__ for c in type(e).__mro__} for e in (exc, *leaves)
    ):
        # Partial degradation (bh-o01m6): point at the path that works without HQ.
        typer.echo(
            "  ↳ HQ config is unavailable — reclaim worktree space without it: "
            f"`{config.BINARY_ALIAS} worktree local-reclaim` (docs/WORKTREES.md)",
            err=True,
        )


def run(app: typer.Typer) -> None:
    """Run *app* as the ``bh`` process: the in-data fence resolver first, then the command, with
    a genuine unhandled error observed and surfaced as one stderr line and exit 1."""
    from . import fence_data

    fence_data.register()  # the product in-data fence resolver, before any guard path (bh-uz46l)
    try:
        app()
    except SystemExit:
        raise  # control-flow exit (typer.Exit codes, validation failures) — preserve verbatim
    except Exception as exc:  # genuine unhandled error: observe + clean surface + non-zero exit
        _handle_cli_error(exc)
        raise SystemExit(1) from exc
