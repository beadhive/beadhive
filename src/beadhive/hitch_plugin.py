"""hitch_plugin.py — the agent-hitch launch integration (bh-og0q.5), an OPTIONAL plugin.

docs/design/managed-harness-config-adr.md, Amendment 2: agent-hitch is exposed ONLY through
this plugin seam (mirrors gitworkspace_plugin.py — a thin ``bh plugin hitch …`` sub-app over an
external binary invoked by subprocess, never imported as a Python package). It is disabled by
default and shares no data/state with any other plugin — enabling or disabling it, or hitch
being absent from PATH or crashing on invoke, has **zero** effect on bh's existing default
launch path (``bh role <seat>``, in :mod:`beadhive.role`), which never references this module.

**Binding mechanism — determined empirically, not assumed (ADR Amendment 1's open question).**
Reading agent-hitch's own ``hitch up`` implementation (``_up_claude_code`` /
``profile_build_claude_config_dir.py``) settles it: the Config Directory built for ``claude-code``
is a full standalone ``$CLAUDE_CONFIG_DIR`` tree (``skills/``, ``commands/``, ``agents/``,
``hooks/``, ``settings.json`` merged from every pack), and ``hitch up`` execs ``claude`` with
only ``CLAUDE_CONFIG_DIR`` pointed at it — no ``claude plugin marketplace add`` /
``plugin install`` step, confirmed by the tool's own generated README. This module adds nothing
on top: it shells out to the real ``hitch up`` unchanged, so the operator's ``~/.claude`` is
never referenced by the launched process (verified: neither ``_up_claude_code`` nor the
config-dir builder read or write it) and it is never read by this module either.

**wt_create is deliberately NOT used for provisioning (bh-og0q.5's explicit decision).** The
bead's design note evaluates it as the seam ("a seat's config directory is the same shape of
per-seat resource as its worktree, and wt_create already fires at exactly the moment a seat is
provisioned") — considered and rejected, for three reasons:

1. **Contract mismatch.** ``wt_create`` delegates the *git worktree creation subprocess itself*
   — it must return the created worktree ``Path`` to "win" (skip native ``git worktree add``) or
   ``None`` to fall through. hitch never creates a git worktree; it would always return ``None``,
   making the hook a confusing place to hang an unrelated side effect (build a Config Directory)
   that the hook's own contract doesn't model.
2. **Wrong failure mode.** ``worktree._consult_wt_create`` treats any non-``typer.Exit`` exception
   from a hook as best-effort — warn, then fall through to native worktree creation. Wiring a
   hitch build in there would inherit that silent-degrade behavior, directly contradicting this
   bead's own acceptance bar: "when hitch is ENABLED and preflight fails, the launch fails
   loudly rather than silently falling back." Loud failure is a *launch-time* property; folding it
   into worktree provisioning would either violate it (if best-effort) or blow up an unrelated
   ``bh work claim`` for every seat in a hitch-enabled hive (if hard-failing).
3. **Scope creep + duplicate drift vector.** Building at every worktree ``wt_create`` would put
   hitch back on the critical path of every worktree provision, even for beads never launched via
   ``bh plugin hitch up`` — exactly the coupling Amendment 2 retracts as a cost. It would also add
   a second build codepath alongside ``hitch up``'s own "build if absent, reuse if present" —
   two places that can now disagree about whether a Config Directory is stale.

So the build/launch happens **only** inside the explicit ``bh plugin hitch up`` verb, matching
hitch's own already-implemented "build if absent, launch" idiom (Amendment 1) and the bead's own
launch-verb spec — no earlier, no implicit.

**Ephemeral by default (bh-og0q.5's acceptance bar).** :func:`beadhive.config.hitch_config_dir_root`
mirrors :func:`beadhive.config.worktrees_root` exactly: ephemeral (default, matching
``worktrees.ephemeral``) ⇒ an OS-temp root sharing its seat's disposable, no-sandbox-grant
lifecycle; persistent ⇒ ``hitch.root`` (or ``~/.beadhive/hitch``). Whether a given (profile,
target) Config Directory is rebuilt within that root is hitch's own "build if absent" call, not
reimplemented here.
"""

from __future__ import annotations

import shutil

import typer

from . import config, plugins, run

# bh's own harness vocabulary (mirrors role.KNOWN_HARNESSES) mapped onto hitch's own `up` target
# names — determined empirically: hitch's CLI accepts "claude-code"/"opencode", NOT "claude"
# (the ADR's own example command, `bh plugin hitch up claude <profile>`, used bh's vocabulary,
# which is why this module translates rather than passing the bh-side name straight through).
_HITCH_TARGETS: dict[str, str] = {"claude": "claude-code", "opencode": "opencode"}


def _repo_files(repo):
    """(profiles/local.yaml, catalogs/local.yaml) paths under a configured hitch.repo checkout."""
    return repo / "profiles" / "local.yaml", repo / "catalogs" / "local.yaml"


def _hitch_argv(cfg, hitch_target: str, profile: str, *, command: str, repo) -> list[str]:
    """The real ``hitch up`` invocation argv. Absolute ``--profiles-file``/``--catalog`` paths
    (derived from ``hitch.repo``) so resolution never depends on bh's own cwd; ``--root`` is
    ``hitch_config_dir_root`` (ephemeral/persistent per ``config.worktrees_ephemeral``)."""
    profiles_file, catalog_file = _repo_files(repo)
    root = config.hitch_config_dir_root(cfg)
    return [
        command,
        "up",
        hitch_target,
        profile,
        "--profiles-file",
        str(profiles_file),
        "--catalog",
        str(catalog_file),
        "--root",
        str(root),
    ]


def up(target: str, profile: str, cfg=None) -> int:
    """``bh plugin hitch up <target> <profile>``'s logic: gate on ``hitch.enabled`` (disabled by
    default — refuses with a clear message, no subprocess spawned), resolve+validate prerequisites
    (known target, hitch on PATH, ``hitch.repo`` configured), then exec the real ``hitch up`` with
    **inherited stdio** (interactive hand-over, mirroring :func:`beadhive.role.launch`) and
    propagate its exit code verbatim — including a preflight failure, so "fails loudly" is
    inherited from hitch's own already-fail-closed implementation rather than re-implemented here.
    Returns the process exit code (0 on success); never raises for an ordinary failure."""
    cfg = cfg if cfg is not None else config.load()

    if not config.hitch_enabled(cfg):
        typer.echo(
            "✗ hitch integration disabled — set `hitch.enabled: true` in config to use it "
            "(see docs/design/managed-harness-config-adr.md, Amendment 2)",
            err=True,
        )
        return 1

    hitch_target = _HITCH_TARGETS.get(target)
    if hitch_target is None:
        known = ", ".join(sorted(_HITCH_TARGETS))
        typer.echo(f"✗ unknown target {target!r}. Known targets: {known}", err=True)
        return 1

    command = config.hitch_command(cfg)
    if shutil.which(command) is None:
        typer.echo(
            f"✗ hitch not found on PATH (looked for {command!r}) — install agent-hitch and "
            "retry (see docs/design/managed-harness-config-adr.md)",
            err=True,
        )
        return 1

    repo = config.hitch_repo(cfg)
    if repo is None:
        typer.echo(
            "✗ hitch.repo not configured — set it to the agent-hitch checkout providing "
            "profiles/local.yaml + catalogs/local.yaml + packs/",
            err=True,
        )
        return 1

    argv = _hitch_argv(cfg, hitch_target, profile, command=command, repo=repo)
    result = run.run(argv, check=False, capture=False)
    return result.returncode


def _readiness(cfg, entry) -> tuple[str, str] | None:
    """hive-ready hook: only invoked when hitch is enabled (the generic
    ``hive_ready._plugin_checks`` loop reports "na" for a disabled plugin without calling this —
    an optional integration stays silent when unused). Checks the same prerequisites :func:`up`
    does, live: hitch on PATH, ``hitch.repo`` configured and pointing at a real checkout."""
    command = config.hitch_command(cfg)
    if shutil.which(command) is None:
        return ("missing", f"{command!r} not found on PATH")
    repo = config.hitch_repo(cfg)
    if repo is None:
        return ("warn", "hitch.repo not configured")
    profiles_file, catalog_file = _repo_files(repo)
    if not profiles_file.is_file() or not catalog_file.is_file():
        return ("warn", f"{repo} missing profiles/local.yaml or catalogs/local.yaml")
    return ("ok", f"hitch on PATH; repo {repo}")


cli = typer.Typer(no_args_is_help=True, help="agent-hitch launch integration (optional).")


@cli.command("up", help="launch <target> (claude|opencode) against <profile>'s hitch config.")
def _up_cmd(
    target: str = typer.Argument(..., help="harness to launch: claude | opencode."),
    profile: str = typer.Argument(..., help="hitch profile name (e.g. dispatcher, developer)."),
) -> None:
    code = up(target, profile)
    if code != 0:
        raise typer.Exit(code)


PLUGIN = plugins.Plugin(
    name="hitch",
    cli=cli,
    enabled=lambda cfg, entry: config.hitch_enabled(cfg, entry),
    readiness=_readiness,
)
