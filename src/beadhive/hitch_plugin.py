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
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

import typer

from . import config, fleet, plugins, role, run

# bh's own harness vocabulary (mirrors role.KNOWN_HARNESSES) mapped onto hitch's own `up` target
# names — determined empirically: hitch's CLI accepts "claude-code"/"opencode"/"codex", NOT
# "claude" (the ADR's own example command, `bh plugin hitch up claude <profile>`, used bh's
# vocabulary, which is why this module translates rather than passing the bh-side name straight
# through). "codex" only widens this passthrough wrapper's accepted target — it does not touch
# role.KNOWN_HARNESSES / deps_mod.seat_runners(), bh's own internal seat-authority dispatch,
# which bh-a7so.3 found does not (yet) support codex — a separate, unrelated system.
_HITCH_TARGETS: dict[str, str] = {
    "claude": "claude-code",
    "opencode": "opencode",
    "codex": "codex",
}

_LAUNCH_RECEIPT_ENV = "BH_AGENT_LAUNCH_RECEIPT"
_launch_receipt: ContextVar[str | None] = ContextVar("hitch_launch_receipt", default=None)


@contextmanager
def scoped_launch_receipt(payload: str) -> Iterator[None]:
    """Expose one resolved core receipt only to the Hitch launch in this scope.

    ``route`` must retain the legacy ``up(target, profile, cfg)`` call shape, so the receipt
    cannot travel as a new positional or keyword argument.  A context variable supplies that
    compatibility seam without touching ``os.environ``: values are task/thread-local and the
    token reset restores the prior value for every exit, including exceptions and cancellation.
    Direct ``bh plugin hitch up`` callers never enter this scope and remain unmanaged.
    """

    token = _launch_receipt.set(payload)
    try:
        yield
    finally:
        _launch_receipt.reset(token)


def _scoped_launch_env() -> dict[str, str] | None:
    """Build the current managed Hitch child's environment, or preserve legacy inheritance."""

    payload = _launch_receipt.get()
    if payload is None:
        return None
    env = run.child_env()
    env[_LAUNCH_RECEIPT_ENV] = payload
    return env


def _repo_files(repo):
    """(profiles/local.yaml, catalogs/local.yaml) paths under a configured hitch.repo checkout."""
    return repo / "profiles" / "local.yaml", repo / "catalogs" / "local.yaml"


def _hitch_argv(
    cfg,
    hitch_target: str,
    profile: str,
    *,
    command: str,
    repo,
    workspace: str | None = None,
    task: str | None = None,
    detached: bool = False,
    role_: str | None = None,
    explain: bool = False,
) -> list[str]:
    """The real ``hitch up`` invocation argv. Absolute ``--profiles-file``/``--catalog`` paths
    (derived from ``hitch.repo``) so resolution never depends on bh's own cwd; ``--root`` is
    ``hitch_config_dir_root`` — always persistent, independent of ``config.worktrees_ephemeral``
    (ADR Amendment 5). ``workspace``/``task``/``detached``/``role_``/``explain`` are forwarded
    unchanged when set — hitch's own CLI is the sole authority on their validity (e.g. ``-d``
    without ``--task``, ADR 0003), never re-validated here."""
    profiles_file, catalog_file = _repo_files(repo)
    root = config.hitch_config_dir_root(cfg)
    argv = [
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
    if workspace is not None:
        argv += ["--workspace", workspace]
    if task is not None:
        argv += ["--task", task]
    if detached:
        argv += ["-d"]
    if role_ is not None:
        argv += ["--role", role_]
    if explain:
        argv += ["--explain"]
    return argv


def up(
    target: str,
    profile: str,
    cfg=None,
    *,
    workspace: str | None = None,
    task: str | None = None,
    detached: bool = False,
    role_: str | None = None,
    explain: bool = False,
) -> int:
    """``bh plugin hitch up <target> <profile>``'s logic: gate on ``hitch.enabled`` (disabled by
    default — refuses with a clear message, no subprocess spawned), resolve+validate prerequisites
    (known target, hitch on PATH, ``hitch.repo`` configured), then exec the real ``hitch up`` with
    **inherited stdio** (interactive hand-over, mirroring :func:`beadhive.role.launch`) and
    propagate its exit code verbatim — including a preflight failure, so "fails loudly" is
    inherited from hitch's own already-fail-closed implementation rather than re-implemented here.
    ``workspace``/``task``/``detached``/``role_``/``explain`` pass straight through to the real
    ``hitch up`` argv (see :func:`_hitch_argv`) — including ``-d`` without ``--task``, which hitch's
    own CLI refuses with its own ADR-0003 message, not reimplemented here.
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

    argv = _hitch_argv(
        cfg,
        hitch_target,
        profile,
        command=command,
        repo=repo,
        workspace=workspace,
        task=task,
        detached=detached,
        role_=role_,
        explain=explain,
    )
    launch_env = _scoped_launch_env()
    if launch_env is None:
        # Preserve the external/legacy call shape as well as its unmanaged environment.
        result = run.run(argv, check=False, capture=False)
    else:
        result = run.run(argv, check=False, capture=False, env=launch_env)
    return result.returncode


# ---- unified `bh role <seat>` backend selection (bh-6t49w.3) ----------------------------------
# Collapses `bh role <seat>` and `bh plugin hitch up <target> <profile>` into one entry point:
# hitch is picked when enabled AND a seat-aligned profile exists for the resolved harness, else
# bh's own bundled seat defs (role.launch, unchanged) — always stating which backend ran, before
# exec'ing, per the ADR's "fails loudly, never silently" principle. Lives here (not in role.py)
# because role.py is the plain, hitch-unaware default path (see its module docstring + the
# `test_role_module_never_imports_hitch_plugin` structural guard) — this module already imports
# `role` and already owns the profile-matching machinery this reuses.


def _resolve_backend(seat: str, harness: str, cfg) -> tuple[str, str | None, str | None]:
    """Decide native vs hitch for one seat launch under *harness* (bh's own harness vocabulary,
    e.g. "claude"). Returns ``("native", None, None)`` unless hitch is enabled AND *harness*
    maps to a known hitch target AND a seat-aligned profile for *seat* exists in hitch.repo's
    ``profiles/local.yaml`` — in which case ``("hitch", hitch_target, profile)``.

    Reuses :func:`_profile_names` (the same profile-name matching `seat_reports` already
    computes for `bh doctor`'s Seats section) as the existence check — no preflight subprocess
    here, that's :func:`seat_reports`'s job for the (expensive, per-seat) runnability report.
    A config-time "does a profile exist" fact is cheap enough to check on every launch."""
    if not config.hitch_enabled(cfg):
        return "native", None, None
    hitch_target = _HITCH_TARGETS.get(harness)
    if hitch_target is None:
        return "native", None, None
    repo = config.hitch_repo(cfg)
    if repo is None:
        return "native", None, None
    profiles_file, _catalog_file = _repo_files(repo)
    if seat not in _profile_names(profiles_file):
        return "native", None, None
    return "hitch", hitch_target, seat


# ---- headless (`--task` / `-d`) suitability + backend selection (bh-6t49w.6) -----------------


def headless_plan(seat: str, harness: str, cfg) -> tuple[str | None, str]:
    """Would an unattended (`--task` / `-d`) launch of *seat* work on this host, and how?

    The suitability SEAM: pure — config reads and PATH lookups only, no subprocess, no launch —
    so "would this seat be refused, and why" is answerable without starting anything
    (bh-6t49w.7 surfaces exactly this), rather than being a branch buried in the launch path.

    Returns ``(backend, detail)``:

    - ``("baml", detail)`` — a built ``bh-<seat>`` role binary is on PATH. PREFERRED over hitch:
      it already carries the CANCEL ladder and the baked-permission (`--bundle`) contract the
      local-runtime work settled (:func:`beadhive.localloop.seat_argv`), which a ``hitch up``
      hand-over does not.
    - ``("hitch", detail)`` — no such binary, but hitch is enabled with a seat-aligned profile
      for *seat* under this *harness* (the same :func:`_resolve_backend` seam the attached path
      uses — one selection rule, not two).
    - ``(None, reason)`` — refused. Either *seat* is not headless-capable at all
      (:func:`beadhive.localloop.headless_capable`), or neither backend exists; *reason* names
      what is missing, so the refusal is loud rather than a silent fallback to attached.

    ``localloop`` is imported lazily: it is a heavy asyncio module and nothing else in this
    plugin's module-level path needs it.
    """
    unsuitable = headless_unsuitable(seat)
    if unsuitable:
        return None, unsuitable

    binary = f"bh-{seat}"
    if shutil.which(binary) is not None:
        return "baml", f"built role binary {binary} on PATH"

    hitch_backend, hitch_detail = headless_hitch_plan(seat, harness, cfg)
    if hitch_backend == "hitch":
        return hitch_backend, hitch_detail

    return None, (
        f"no headless backend for seat {seat!r}: no built {binary} binary on PATH, and no "
        f"hitch profile named {seat!r} for harness {harness!r} (hitch enabled + `hitch.repo` "
        "configured with a matching entry in profiles/local.yaml). Build the role binary or "
        f"add the profile — or run it attached: `{config.BINARY_ALIAS} role {seat}`."
    )


def headless_unsuitable(seat: str) -> str:
    """Return the shared attached-only refusal, or ``""`` for a headless-capable seat."""

    from . import localloop

    if localloop.headless_capable(seat):
        return ""
    capable = ", ".join(sorted(set(localloop.ROLE_FOR_ACTION.values())))
    return (
        f"{seat!r} is not a headless-capable seat — nothing dispatches an unattended "
        f"{seat} run, so one would never be picked up. Headless-capable seats: {capable}. "
        f"Run it attached instead: `{config.BINARY_ALIAS} role {seat}`."
    )


def headless_hitch_plan(seat: str, harness: str, cfg) -> tuple[str | None, str]:
    """Resolve only direct Hitch for an explicit provider request.

    Unlike :func:`headless_plan`, this seam never consults the provider-unspecified
    ``bh-<seat>`` compatibility alias.  An explicit Claude/Codex/OpenCode request therefore
    cannot accidentally select whatever provider happened to be baked into that old basename.
    """

    if unsuitable := headless_unsuitable(seat):
        return None, unsuitable
    backend, hitch_target, profile = _resolve_backend(seat, harness, cfg)
    if backend == "hitch":
        return "hitch", f"hitch profile {profile!r} (target={hitch_target})"
    return None, (
        f"no direct Hitch backend for seat {seat!r} and harness {harness!r}: hitch must be "
        "enabled with a matching seat-aligned profile"
    )


def _seat_listing_lines(cfg, harness: str, *, full: bool) -> list[str]:
    """One annotated line per known seat for the bare ``bh role`` listing (bh-6t49w.5) —
    which backend(s) this host would actually pick, reusing existing data rather than a new
    capability-detection mechanism:

    - ``native``: always available (`role.launch`'s own path); shown as ``native: ok``.
    - ``hitch``: only shown when :func:`_resolve_backend` would pick it for that seat (the same
      config-time, no-subprocess seam ``bh role <seat>`` itself uses). ``full=True`` folds in
      :func:`seat_reports`'s live per-seat preflight state (ok/reduced/blocked, with detail on
      anything short of ``ok``) — the SAME expensive 7-seat ``hitch profile preflight`` fanout
      `bh doctor --seats` opts into (bh-gqfrm); ``full=False`` (the default here too) shows a
      bare ``hitch: ok`` from the cheap profile-existence check alone, so a bare `bh role` never
      pays that cost unconditionally.
    - ``baml``: a built ``bh-<seat>`` binary on PATH (the settled local-runtime role-binary
      contract, :func:`beadhive.localloop.seat_argv`) — shown as ``baml: built`` when found."""
    reports = {r["seat"]: r for r in seat_reports(cfg)} if full else {}
    lines = []
    for seat in role._known_seats():
        parts = ["native: ok"]
        backend, _hitch_target, _profile = _resolve_backend(seat, harness, cfg)
        if backend == "hitch":
            report = reports.get(seat)
            if report is None:
                parts.append("hitch: ok")
            else:
                detail = f" ({report['detail']})" if report["detail"] else ""
                parts.append(f"hitch: {report['state']}{detail}")
        if shutil.which(f"bh-{seat}") is not None:
            parts.append("baml: built")
        lines.append(f"{seat} — {'; '.join(parts)}")
    return lines


def route(
    seat: str,
    *,
    harness: str | None = None,
    no_hitch: bool = False,
    full_seats: bool = False,
    cfg=None,
    managed_bead: bool | None = None,
    bead: str | None = None,
    available_seats: tuple[str, ...] | None = None,
    current_seat: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    resolved_profile: role.ResolvedAgentLaunchProfile | None = None,
) -> None:
    """``bh role <seat>``'s unified entry point. An unknown (non-empty) seat delegates straight
    to :func:`beadhive.role.launch` unchanged — nothing to pick a backend for. The bare listing
    (no seat) is annotated with each seat's available backend(s) — see
    :func:`_seat_listing_lines`; ``full_seats`` opts into its expensive per-seat hitch preflight
    breakdown. For a known seat, picks hitch when it applies (see :func:`_resolve_backend`),
    forced off by ``--no-hitch`` regardless of what would otherwise apply, and always announces
    which backend is about to run *before* exec'ing.

    Disabled/unconfigured hitch (the default) always resolves to native — same seat, same
    harness, same argv/env `role.launch` always built (Amendment 2's degrade-to-today bar) —
    the only difference from calling `role.launch` directly is this function's own banner line.

    Never raises for an ordinary failure/exit; propagates `role.launch`'s ``SystemExit`` or
    wraps `up`'s non-zero return code the same way `bh plugin hitch up` itself does."""
    if not seat:
        cfg = cfg if cfg is not None else config.load()
        resolved_harness = harness or config.harness_name(cfg)
        typer.echo("Available seats:")
        for line in _seat_listing_lines(cfg, resolved_harness, full=full_seats):
            typer.echo(f"  {line}")
        return

    if seat not in role._known_seats():
        role.launch(seat, harness=harness)
        return

    cfg = cfg if cfg is not None else config.load()
    resolved_harness = (
        resolved_profile.harness
        if resolved_profile is not None
        else harness or config.harness_name(cfg)
    )
    if resolved_profile is None and managed_bead is not None:
        try:
            profile = role.build_launch_profile(
                seat,
                harness=resolved_harness,
                managed_bead=managed_bead,
                bead=bead,
                available_seats=available_seats,
                model=model,
                effort=effort,
            )
            resolved_profile = role.resolve_launch_profile(profile, current_seat=current_seat)
        except ValueError as exc:
            typer.echo(f"✗ invalid agent launch profile: {exc}", err=True)
            raise typer.Exit(1) from None
    if resolved_profile is not None:
        seat = resolved_profile.current_seat
    backend, hitch_target, profile = (
        ("native", None, None) if no_hitch else _resolve_backend(seat, resolved_harness, cfg)
    )

    if backend == "hitch":
        typer.echo(f"→ {seat}: launching via hitch (target={hitch_target}, profile={profile})")
        if resolved_profile is None:
            code = up(resolved_harness, profile, cfg)
        else:
            receipt = role.AgentLaunchReceipt.from_resolved(resolved_profile).model_dump_json()
            # Keep the established attached-Hitch up(target, profile, cfg) call byte-for-byte.
            with scoped_launch_receipt(receipt):
                code = up(resolved_harness, profile, cfg)
        if code != 0:
            raise typer.Exit(code)
        return

    typer.echo(f"→ {seat}: launching via native backend")
    if resolved_profile is None:
        role.launch(seat, harness=harness)
        return
    role.launch(seat, harness=harness, resolved_profile=resolved_profile)


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
    # (bh-ls1ks: 7 seats = 12.7s sequential). SHAPE B (`fleet.fanout`): not a bead-store read
    # at all, so the bulk shape can never apply here. The shape also CAPS this, which the
    # hand-rolled `max_workers=len(seats)` did not — fine at 7 seats, not a shape to keep.
    return fleet.fanout(_one, seats)


def _readiness(cfg, entry, *, full: bool = True) -> tuple[str, str] | None:
    """hive-ready hook: only invoked when hitch is enabled (the generic
    ``hive_ready._plugin_checks`` loop reports "na" for a disabled plugin without calling this —
    an optional integration stays silent when unused). Checks the same prerequisites :func:`up`
    does, live: hitch on PATH, ``hitch.repo`` configured and pointing at a real checkout.

    Once those pass, folds in :func:`seat_reports` (bh-og0q.4) — this is the SAME hook `bh doctor`
    rides (`doctor._data_seats`), not a second bespoke path. ``state`` degrades to ``"warn"`` when
    any seat is blocked (never ``"missing"``: the plugin itself is fine, only a seat lacks a
    capability); with no seat-aligned profiles configured this is byte-identical to the prior
    behavior.

    ``full=False`` (bh-gqfrm) skips :func:`seat_reports` entirely — the 7-way
    ``hitch profile preflight`` fanout is the most expensive thing this hook does (~2.7s of the
    ~8.3s a warm `bh doctor` costs; see docs/BH_DATA_PIPELINE.md §4.1) and `bh hive ready` /
    `bh doctor`'s default report only need "is hitch usable at all" (PATH + repo + catalog
    present), not "which of the 7 seats specifically". Only the prerequisite checks above run;
    the returned detail says explicitly that per-seat detail was skipped and how to get it, so a
    clean report never silently means "and all seats were fine" (bh-gqfrm's acceptance bar)."""
    command = config.hitch_command(cfg)
    if shutil.which(command) is None:
        return ("missing", f"{command!r} not found on PATH")
    repo = config.hitch_repo(cfg)
    if repo is None:
        return ("warn", "hitch.repo not configured")
    profiles_file, catalog_file = _repo_files(repo)
    if not profiles_file.is_file() or not catalog_file.is_file():
        return ("warn", f"{repo} missing profiles/local.yaml or catalogs/local.yaml")

    if not full:
        return (
            "ok",
            f"hitch on PATH; repo {repo}; per-seat checks skipped by default "
            "(pass --seats for per-seat runnability)",
        )

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


@cli.command("up", help="launch <target> (claude|opencode|codex) against <profile>'s hitch config.")
def _up_cmd(
    target: str = typer.Argument(..., help="harness to launch: claude | opencode | codex."),
    profile: str = typer.Argument(..., help="hitch profile name (e.g. dispatcher, developer)."),
    workspace: str = typer.Option(
        None, "--workspace", help="workspace the provider acts on (default: cwd)."
    ),
    task: str = typer.Option(
        None, "--task", help="run headless with this task instead of an interactive session."
    ),
    detached: bool = typer.Option(
        False,
        "-d",
        "--detached",
        help="detach the run; requires --task (refused by hitch without it, see ADR 0003).",
    ),
    role_: str = typer.Option(None, "--role", help="the declared agent to run inside the profile."),
    explain: bool = typer.Option(
        False,
        "--explain",
        "--dry-run",
        help="write and print the redacted launch manifest without starting the provider.",
    ),
) -> None:
    code = up(
        target,
        profile,
        workspace=workspace,
        task=task,
        detached=detached,
        role_=role_,
        explain=explain,
    )
    if code != 0:
        raise typer.Exit(code)


PLUGIN = plugins.Plugin(
    name="hitch",
    cli=cli,
    enabled=lambda cfg, entry: config.hitch_enabled(cfg, entry),
    readiness=_readiness,
)
