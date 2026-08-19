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

**Seat-runnability reporting (bh-og0q.4) rides `_readiness`, the same `Plugin.readiness` hook
`bh hive ready` already consumes — no bespoke `bh doctor` code path.** :func:`seat_reports`
delegates entirely to ``hitch profile preflight``: for every bh seat (:func:`beadhive.role.
_known_seats`) that also has a matching "seat-aligned" profile in the configured repo's
``profiles/local.yaml``, it shells out to preflight and classifies the result by reading only
that command's own ``[fail]``/``[info]`` line markers and exit code — never re-deriving *why* a
profile passes or fails, which would create a second source of truth that can disagree with the
emitter it describes. Three states fall out of that read: ``"blocked"`` (a missing binary or
unsupported OS — a hard blocker, exit != 0; the ``[fail]`` line already names the binary, so
that's what's surfaced verbatim), ``"reduced"`` (exit 0 but the target drops a declared family —
the ADR's own ``target 'claude-code' does not support family 'instructions'`` example; the seat
runs, with less), and ``"ok"`` (fully runnable). :func:`_readiness` folds this into its existing
single ``(state, detail)`` return once the tool+repo prerequisites it already checks are met —
unchanged when they are not, so existing hive-ready behavior for those cases is untouched.

**Silent when disabled, per bh-og0q.4's acceptance bar.** Neither :func:`seat_reports` nor the
extended :func:`_readiness` are invoked at all unless a caller has already gated on
``config.hitch_enabled`` — :func:`_readiness` is only ever reached that way (`hive_ready.
_plugin_checks` short-circuits to "na" for a disabled plugin before calling it; ``bh doctor``'s
new Seats section, `doctor._data_seats`, gates the same way). An optional integration that
complains when unused is not optional.

**Unauthenticated Config Directories are deliberately OUT OF SCOPE here, not silently omitted.**
The epic notes (bh-og0q, approval of bh-og0q.5) float this as a candidate fourth state — distinct
from "cannot run this seat" — worth recording the reasoning either way. It is not added:

1. *Wrong layer.* Preflight (this bead's sole check, per its own acceptance bar) evaluates a
   profile+target **before** any Config Directory exists — it has no way to observe auth state,
   which lives in ``.claude.json`` **inside a built** directory (Amendment 5). Most seats a fresh
   host is asked "can you run this" about have never been built at all.
2. *Second source of truth, again.* Detecting it would mean bh reading Claude Code's own
   ``.claude.json`` shape directly — exactly the kind of parallel capability-detection mechanism
   this bead's design section already argues against for the runnability question itself.
3. *Different kind of fact.* An unauthenticated directory still fully **can** run the seat
   (binaries present, OS supported) — it needs a one-time login, not a capability it lacks.
   Folding it into seat-runnability would blur the hard-blocker/reduced-capability distinction
   this bead exists to draw. It stays a follow-on concern (the epic notes name two candidate
   homes, neither of which is this bead) — not fixed by inaction, but by a scoping decision made
   explicitly here.

**Persistent by default, decoupled from worktree ephemerality (ADR Amendment 5; bh-og0q.8).**
:func:`beadhive.config.hitch_config_dir_root` does **not** mirror
:func:`beadhive.config.worktrees_root` — a Config Directory holds Claude Code's OAuth session
(``.claude.json``), which nothing regenerates, unlike a worktree's git-reconstructible content,
so the two do not share
``worktrees.ephemeral``. It always resolves to ``hitch.root`` (default ``~/.beadhive/hitch``);
there is no ``hitch.ephemeral`` knob, since persistent is the only correct value for state a
one-time login populates. (bh-og0q.5 originally wired this to ``worktrees.ephemeral``, which was
correct under the ADR's then-current Decision 4; Amendment 5 retracted that decision on evidence
bh-og0q.5 itself produced.) Whether a given (profile, target) Config Directory is rebuilt within
that root is hitch's own "build if absent" call, not reimplemented here — pruning stale emitted
content on rebuild is tracked separately (bh-add2.2), out of scope here.
"""

from __future__ import annotations

import shutil
from concurrent.futures import ThreadPoolExecutor

import typer

from . import config, plugins, role, run

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
    ``hitch_config_dir_root`` — always persistent, independent of ``config.worktrees_ephemeral``
    (ADR Amendment 5)."""
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


# ---- seat-runnability reporting (bh-og0q.4) ---------------------------------------------------
# "Which seats can THIS host run" — a reporting surface over hitch's own preflight, not a second
# capability-detection mechanism. See the module docstring for the full design rationale
# (including why an unauthenticated Config Directory is deliberately not a fourth state here).

_REDUCED_CAPABILITY_MARKER = "does not support family"

# Preflight-state -> human label, used only by _readiness's rendered detail.
_SEAT_LABEL = {"ok": "runnable", "reduced": "runs, reduced capability", "blocked": "cannot run"}


def _profile_names(profiles_file) -> set[str]:
    """Profile names declared in a hitch ``profiles/local.yaml``'s top-level ``profiles:``
    mapping, or an empty set if the file is missing/unreadable/malformed. Read-only
    introspection only — this never judges whether a profile is usable; ``hitch profile
    preflight`` is the sole authority for that (see :func:`seat_reports`)."""
    from ruamel.yaml import YAML

    try:
        data = YAML(typ="safe").load(profiles_file.read_text())
    except Exception:  # noqa: BLE001 — a malformed/unreadable catalog degrades to "no seats"
        return set()
    profiles = (data or {}).get("profiles") if isinstance(data, dict) else None
    return set(profiles.keys()) if isinstance(profiles, dict) else set()


def _classify_preflight(returncode: int, stdout: str) -> tuple[str, str]:
    """Classify one profile's ``hitch profile preflight`` result. Never re-derives WHY a
    profile passes or fails — only reads the report's own ``[fail]``/``[info]`` line markers
    (``hitch``'s own ``_print_preflight_report`` format) and exit code.

    - exit != 0  -> ``"blocked"`` (hard blocker: missing binary, unsupported OS, ...); detail
      is the ``[fail]`` line(s) verbatim, so a missing binary is named because preflight's own
      message already names it.
    - exit == 0 with >=1 ``[info] ... does not support family ...`` line -> ``"reduced"``
      (the ADR's own example: the seat runs, with a declared family dropped).
    - otherwise  -> ``"ok"`` (fully runnable; empty detail — nothing more to say)."""
    lines = stdout.splitlines()
    if returncode != 0:
        fails = [ln.strip()[len("[fail] ") :] for ln in lines if ln.strip().startswith("[fail]")]
        return "blocked", "; ".join(fails)
    reduced = [
        ln.strip()[len("[info] ") :]
        for ln in lines
        if ln.strip().startswith("[info]") and _REDUCED_CAPABILITY_MARKER in ln
    ]
    return ("reduced", "; ".join(reduced)) if reduced else ("ok", "")


def seat_reports(cfg) -> list[dict]:
    """Per-seat runnability for THIS host, delegating entirely to ``hitch profile preflight``
    (bh-og0q.4) — one entry per bh seat (:func:`beadhive.role._known_seats`) that also has a
    matching hitch profile in the configured repo's ``profiles/local.yaml`` ("seat-aligned
    profiles: the name matches a beadhive seat", per that file's own comment). A seat with no
    matching profile is silently skipped — nothing to check, not a blocker.

    Returns ``[]`` when the tool/repo prerequisites :func:`_readiness` already checks are not
    met, or no seat-aligned profile exists, or the configured harness has no known hitch target.
    Does **not** itself gate on ``config.hitch_enabled`` — every other helper in this module
    leaves that to its caller, and both of this function's callers (:func:`_readiness`,
    ``doctor._data_seats``) already do, matching bh-og0q.4's "silent when disabled" bar.

    Each report is ``{"seat": name, "state": "ok"|"reduced"|"blocked", "detail": str}``."""
    command = config.hitch_command(cfg)
    if shutil.which(command) is None:
        return []
    repo = config.hitch_repo(cfg)
    if repo is None:
        return []
    profiles_file, catalog_file = _repo_files(repo)
    if not profiles_file.is_file() or not catalog_file.is_file():
        return []

    seats = sorted(_profile_names(profiles_file) & set(role._known_seats()))
    if not seats:
        return []

    hitch_target = _HITCH_TARGETS.get(config.harness_name(cfg))
    if hitch_target is None:
        return []

    def _one(seat: str) -> dict:
        argv = [
            command,
            "profile",
            "preflight",
            seat,
            "--profiles",
            str(profiles_file),
            "--catalog",
            str(catalog_file),
            "--target",
            hitch_target,
        ]
        try:
            result = run.run(argv, check=False, capture=True)
        except Exception as exc:  # noqa: BLE001 — one seat's spawn failing is that seat's report
            return {"seat": seat, "state": "blocked", "detail": f"{command} preflight: {exc}"}
        state, detail = _classify_preflight(result.returncode, result.stdout or "")
        return {"seat": seat, "state": state, "detail": detail}

    # Preflights are independent and read-only, and each costs ~1.8s of external process
    # (bh-ls1ks: 7 seats = 12.7s sequential). Run them in a pool and keep `seats`' sorted
    # order by consuming `map`'s results positionally, so the report stays deterministic.
    with ThreadPoolExecutor(max_workers=len(seats)) as pool:
        return list(pool.map(_one, seats))


def _readiness(cfg, entry) -> tuple[str, str] | None:
    """hive-ready hook: only invoked when hitch is enabled (the generic
    ``hive_ready._plugin_checks`` loop reports "na" for a disabled plugin without calling this —
    an optional integration stays silent when unused). Checks the same prerequisites :func:`up`
    does, live: hitch on PATH, ``hitch.repo`` configured and pointing at a real checkout.

    Once those pass, folds in :func:`seat_reports` (bh-og0q.4) — this is the SAME hook `bh doctor`
    rides (`doctor._data_seats`), not a second bespoke path. ``state`` degrades to ``"warn"`` when
    any seat is blocked (never ``"missing"``: the plugin itself is fine, only a seat lacks a
    capability); with no seat-aligned profiles configured this is byte-identical to the prior
    behavior."""
    command = config.hitch_command(cfg)
    if shutil.which(command) is None:
        return ("missing", f"{command!r} not found on PATH")
    repo = config.hitch_repo(cfg)
    if repo is None:
        return ("warn", "hitch.repo not configured")
    profiles_file, catalog_file = _repo_files(repo)
    if not profiles_file.is_file() or not catalog_file.is_file():
        return ("warn", f"{repo} missing profiles/local.yaml or catalogs/local.yaml")

    seats = seat_reports(cfg)
    if not seats:
        return ("ok", f"hitch on PATH; repo {repo}")

    lines = [
        f"{s['seat']}: {_SEAT_LABEL[s['state']]}" + (f" — {s['detail']}" if s["detail"] else "")
        for s in seats
    ]
    detail = f"hitch on PATH; repo {repo}; seats -\n  " + "\n  ".join(lines)
    state = "warn" if any(s["state"] == "blocked" for s in seats) else "ok"
    return (state, detail)


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
