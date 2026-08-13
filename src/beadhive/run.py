"""Thin subprocess helpers — ws delegates all heavy lifting to other binaries.

One OpenTelemetry span wraps the subprocess at this single seam, so every bd/git/dolt call ws
makes is traced when otel is on. The span is gated on ``otel.is_active()``: when telemetry is off
(the default) ``run`` is the original ``subprocess.run`` under a zero-cost ``nullcontext`` — no
span name or attributes are even built. Tests fake subprocesses by patching the per-module ``run``
they import (``ws.work.run`` etc.), which replaces this whole function, so fakes bypass the span
entirely and keep working unchanged.

**bh CONSTRUCTS the child environment; it never just inherits one (bh-9qor).** :func:`child_env`
is that launcher and :func:`run` routes every call through it, so a value bh has already resolved
reaches the process bh spawns. The failure this closes was measured on beadhive-factory
(2026-08-05): ``host_provision`` ran ``git workspace update`` with no ``env=``, ``GIT_WORKSPACE``
was unset in the invoking shell, and one variable bh resolves itself
(:func:`beadhive.identity.workspace_root`) took out three of ten provisioning steps.
``role.harness_env`` (bh-og0q.2) made the same argument for one call site — it is now a CALLER of
this launcher rather than a parallel implementation of it.

**PRECEDENCE IS THE CONTRACT.** The launcher fills GAPS. An operator-set value always wins; a
variable already carrying a non-blank value is never rewritten. Getting this backwards would make
bh silently ignore a deliberately-set token.

**THE SET IS DELIBERATELY SHORT** — the two variables a real failure demanded, and no speculative
third. ``GITHUB_TOKEN`` is opt-in per call (``github_token=True``) because deriving it costs
subprocesses and because a secret has no business in the environment of every ``git``/``bd`` call
bh makes.

INVENTORY OF EVERY SUBPROCESS PATH IN THE TREE, and why each is or is not routed through here
(bh-9qor's acceptance — a launcher half the code bypasses is worse than none, because it looks
like a guarantee):

* ``run`` / ``out`` / ``ok`` (this module) — ROUTED. The seam nearly everything shells out
  through, including ``bd`` (``bd._run``), ``git``, ``dolt`` and ``git workspace``.
* ``setup.probe_one`` (setup.py) — NOT routed, deliberately. It is stage 1 of dependency
  DETECTION: presence is decided by ``shutil.which()`` against bh's OWN environment, and running
  the version command under a different (constructed) environment would let ``found`` and
  ``version`` disagree about which PATH answered. A detector must observe the environment, not
  improve it.
* ``safety.py`` (``_run``, ``_bd_dolt_mode``, ``_bd_has_dolt_remote``, ``_non_hive_dirty_paths``,
  ``_gh_authenticated``, ``gh repo create``) — NOT routed, deliberately. Every one passes
  ``_CLEAN_ENV``, which SCRUBS ``GIT_*`` so a hook-exported ``GIT_DIR``/``GIT_WORK_TREE`` cannot
  override ``git -C``. Routing them here would put ``GIT_WORKSPACE`` back into an environment
  whose whole point is that ``GIT_*`` was removed.
* ``worktree._pid_start`` / ``_pid_starts`` — NOT routed, deliberately (already documented at the
  call site): pure local ``ps`` probes that tests faking ``worktree.run`` must not intercept.
* ``config._gh_login`` — NOT routed: a bounded ``gh api user`` identity probe that reads gh's own
  stored credential; no bh-resolved value applies.
* ``cli.py``'s ``claude mcp add`` — NOT routed: a one-shot interactive admin verb with inherited
  stdio, where inheriting the human's shell verbatim is the intended behaviour.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess

from . import otel


def _tool(cmd) -> str:
    """Basename of the invoked binary (argv[0]) — the span's ``ws.subprocess.tool`` attribute."""
    if isinstance(cmd, str):
        toks = cmd.split()
        first = toks[0] if toks else ""
    else:
        first = str(cmd[0]) if cmd else ""
    return os.path.basename(first) if first else "subprocess"


def _safe_op(cmd) -> str:
    """A low-cardinality, non-secret span name: the tool plus its first subcommand (e.g.
    ``git merge``, ``bd gate``), stopping at the first flag. Anything after a flag may be a secret
    (e.g. a dolt ``--password`` value) and positional args (bead ids, paths) are high-cardinality,
    so only the two leading verb tokens land in the name — IDs belong in attributes, not here."""
    toks = cmd.split() if isinstance(cmd, str) else [str(t) for t in (cmd or [])]
    parts: list[str] = []
    for tok in toks:
        if tok.startswith("-"):
            break
        parts.append(os.path.basename(tok) if not parts else tok)
        if len(parts) >= 2:
            break
    return " ".join(parts) if parts else "subprocess"


def _span(cmd):
    """The subprocess span, or a zero-cost ``nullcontext`` when otel is off — gated so the default
    path builds no span name/attributes and stays byte-for-byte the un-instrumented original."""
    if not otel.is_active():
        return contextlib.nullcontext()
    return otel.span(_safe_op(cmd), {"bh.subprocess.tool": _tool(cmd)})


# ---- the child environment bh CONSTRUCTS (bh-9qor) ---------------------------------------

#: The variable git-workspace's providers declare (`env_var = "GITHUB_TOKEN"` in every
#: `workspace*.toml` block HQ ships). gh's own read order lives on the dep row and is read from
#: there; this names the one bh WRITES.
GITHUB_TOKEN_VAR = "GITHUB_TOKEN"

#: `gh auth token` is local (it reads an already-minted credential) but still a subprocess, so it
#: is bounded like every other probe that could wedge a headless host.
TOKEN_TIMEOUT = 15.0


def _set(env: dict[str, str], name: str, value: str) -> None:
    """Gap-fill only: never overwrite a name already carrying a non-blank value.

    Blank is treated as unset on purpose — ``GIT_WORKSPACE=`` is an empty shell variable, not an
    operator asking for the empty path."""
    if not env.get(name, "").strip() and value:
        env[name] = value


def _fill_git_workspace(env: dict[str, str]) -> None:
    """``GIT_WORKSPACE`` from :func:`beadhive.identity.workspace_root` — the resolver bh already
    consults everywhere else, and the one whose absence in the child skipped three provisioning
    steps on beadhive-factory. Not a secret; no hygiene machinery applies."""
    from .identity import workspace_root  # lazy: identity imports this module

    _set(env, "GIT_WORKSPACE", workspace_root())


def _fill_github_token(env: dict[str, str]) -> None:
    """``GITHUB_TOKEN`` for a child that authenticates to GitHub over HTTPS, derived FRESH from
    ``gh auth token`` and never persisted.

    Order, and it is all precedence: an operator-set ``GITHUB_TOKEN`` is left alone; failing that
    a set ``GH_TOKEN`` is MIRRORED (still the operator's value, under the name the providers
    declare); only with neither set does bh derive one, and only when ``deps.satisfied(gh)``.

    A token derived here lives in one dict and one child process. It reaches no log, no OTEL
    attribute and no error message — every failure branch below names the STATE, never a value.
    """
    from . import credentials, deps, log  # lazy: both import this module

    gh = deps.by_name("gh")
    if env.get(GITHUB_TOKEN_VAR, "").strip():
        return  # operator intent — the launcher fills gaps, it never overrides
    for var in gh.auth.env_vars:  # gh's own read order, from the dep row
        if inherited := env.get(var, "").strip():
            env[GITHUB_TOKEN_VAR] = inherited
            return

    report = credentials.probe(gh)
    if not deps.satisfied(gh, authenticated=report.authenticated):
        log.get_logger(__name__).warning(
            "github_token_underivable",
            installed=report.installed,
            authenticated=report.authenticated,
            remedy=report.remedy or gh.auth.remedy,
            consequence=gh.auth.consequence,
        )
        return

    res = run(["gh", "auth", "token"], check=False, capture=True, timeout=TOKEN_TIMEOUT)
    token = (res.stdout or "").strip()
    if res.returncode != 0 or not token:
        # Never echo stdout/stderr here: on the success path it IS the token.
        log.get_logger(__name__).warning(
            "github_token_underivable",
            installed=True,
            authenticated=True,
            remedy=gh.auth.remedy,
            consequence=gh.auth.consequence,
        )
        return
    env[GITHUB_TOKEN_VAR] = token


def child_env(base=None, *, github_token: bool = False) -> dict[str, str]:
    """The environment bh hands a child it spawns: *base* (default ``os.environ``) plus every
    value bh has resolved that the child would otherwise not see.

    *base* is read FRESH each call and never cached — a module-level snapshot would be taken
    before any per-invocation override could apply (the same hazard ``hub._bd_ni_env`` documents).

    ``github_token=True`` additionally derives ``GITHUB_TOKEN`` for children that authenticate to
    GitHub over HTTPS (``git workspace update``'s provider queries). Off by default: a secret
    belongs in the environment of the call that needs it, not of every subprocess bh makes.
    """
    env = dict(os.environ if base is None else base)
    _fill_git_workspace(env)
    if github_token:
        _fill_github_token(env)
    return env


#: Exit code a `check=False` caller sees when the binary itself is not on PATH — the shell's own
#: command-not-found convention. Always accompanied by a `bh_missing_binary` tag on the result;
#: check the tag, never this number alone, since a child may legitimately exit 127 too.
MISSING_BINARY_EXIT = 127


def missing_binary(res) -> str:
    """The binary name when `res` came back because the executable was absent, else ''.

    The tag, not the returncode, is the discriminator. `bd.json` returns None for BOTH "bd exited
    non-zero" and "bd is not installed", and `None` already means "no such bead" to its callers —
    so without this, hiding bd from PATH turned `bh work brief <id>` into `✗ no such bead: <id>`,
    a confident and false answer about the operator's data."""
    return str(getattr(res, "bh_missing_binary", "") or "")


def run(
    cmd,
    *,
    check=True,
    capture=False,
    env=None,
    cwd=None,
    text_input=None,
    timeout=None,
    github_token=False,
):
    """Run a command. Returns CompletedProcess. capture=True grabs stdout/stderr as text.
    timeout (seconds) raises subprocess.TimeoutExpired so a wedged child can't block forever.

    The child's environment is CONSTRUCTED (:func:`child_env`), never inherited raw — an explicit
    ``env=`` is the base it gap-fills, not a bypass. ``github_token=True`` additionally supplies a
    freshly-derived ``GITHUB_TOKEN`` to a child that needs one.

    A binary that is not on PATH comes back to a ``check=False`` caller as exit 127 (the shell's
    own command-not-found code), NOT as a raised ``FileNotFoundError``. Those callers are written
    against a returncode — ``bd.json`` even documents "returns None on error" — so the raise
    escaped as an unhandled crash from anything that merely READ through bd: `bh doctor` died with
    a traceback on precisely the broken seat it exists to diagnose (bh-7m2h9). A ``check=True``
    caller asked for an exception, so it still gets one, unchanged."""
    with _span(cmd):
        try:
            return subprocess.run(
                cmd,
                check=check,
                text=True,
                env=child_env(env, github_token=github_token),
                cwd=cwd,
                input=text_input,
                timeout=timeout,
                stdout=subprocess.PIPE if capture else None,
                stderr=subprocess.PIPE if capture else None,
            )
        except FileNotFoundError:
            if check:
                raise
            binary = cmd[0] if cmd else "?"
            res = subprocess.CompletedProcess(
                cmd,
                MISSING_BINARY_EXIT,
                "" if capture else None,
                f"{binary}: command not found" if capture else None,
            )
            # Tag the result so a caller can tell THIS 127 from a 127 the child chose to exit
            # with. Without the tag the two are indistinguishable, and `bd.json`'s None then
            # means both "no such bead" and "bd is not installed" — which is how a missing
            # binary became `✗ no such bead: <id>`, the exact false finding bh-7m2h9 exists to
            # remove. Callers read it via `run.missing_binary(res)`.
            res.bh_missing_binary = binary
            return res


_INDEX_LOCK_RETRIES = 5
_INDEX_LOCK_SLEEP = 0.2  # seconds


def retry_on_index_lock(run_fn, cmd, *, retries=_INDEX_LOCK_RETRIES, sleep=_INDEX_LOCK_SLEEP, **kw):
    """Run a git command via ``run_fn``, retrying briefly on transient ``.git/index.lock``
    contention (bh-i6o7). A ``git commit`` moments earlier — bd's scaffolding commit, a worktree
    merge, or a test-harness commit — can spawn a detached ``git maintenance run --auto`` that
    transiently holds the index; a mutation racing it fails with ``index.lock`` in stderr. Retry
    rather than fail the otherwise-green op. ``run_fn`` is passed in (not ``run``) so each caller
    keeps its own subprocess seam — the per-module ``run`` symbol tests fake. Detection needs
    captured stderr; an uncaptured call (``stderr is None``) simply runs once, as before."""
    import time

    res = run_fn(cmd, **kw)
    for _ in range(retries - 1):
        if res.returncode == 0 or "index.lock" not in (getattr(res, "stderr", "") or ""):
            return res
        time.sleep(sleep)
        res = run_fn(cmd, **kw)
    return res


def ps_argv(fields: str) -> list[str]:
    """The argv for a full-process-table `ps`, with the flags that make it READABLE (bh-jwwls).

    `ps -e -o <fields>`, plus the two things every caller here has independently forgotten:

    * ``-ww`` — `ps` truncates each command line to ``$COLUMNS`` **even when its output is a
      pipe**. Every scanner in this tree keys on tokens near the END of a long argv, so the cut
      silently removes the match rather than the noise, and the scan returns a plausible EMPTY
      result instead of an error.
    * ``=``-suffixed fields — suppresses the header, so line 0 is data and no caller has to
      remember to skip it. A header-skipping parser over headerless output drops a real row.

    FOUR SITES LOST THIS FLAG, which is why it is a function and not a comment: `world.py`
    (bh-7wp2y), `dolt_health.py` and `localloop.py` (bh-jwwls), and `demo_local_loop.py`. The
    knowledge existed in prose in this repo and failed to transfer twice; prose does not get
    imported. Not for `ps -p <pid>` lookups (`worktree.py`) — those name their pids and print
    short columns, so there is nothing to truncate.
    """
    return ["ps", "-eww", "-o", fields]


# ---- a child that cannot outlive its caller (bh-toitp) -----------------------------------
#
# MEASURED 2026-08-07 on beadhive-factory: 31 live `bd -C ~/.beadhive/hq show <~50 ids> --json`
# processes, 9.6 GB RSS, oldest 2h12m, all `ppid=1`, none stuck in the kernel — every one older
# than ten minutes exited cleanly on a plain SIGTERM. So nothing was ever signalling them.
#
# TWO INDEPENDENT WAYS THAT HAPPENS, and fixing one alone leaves the leak:
#
#   1. The call never finishes and the caller never bounds it. `subprocess.run(timeout=)` closes
#      this — but only for the DIRECT child, and only while the caller is alive to enforce it.
#   2. The CALLER is killed and the child is reparented to init. This is the measured shape
#      (`ppid=1`): a consumer's own `execFile(..., {timeout: 10_000})` signalled `bh` and left the
#      `bd` grandchild running — which is exactly the 10s spacing seen in the observed waves. No
#      timeout on bh's side can help there; bh is already dead. The CHILD has to be told.
#
# `PR_SET_PDEATHSIG` is the kernel telling it: the child gets SIGTERM the moment its parent dies,
# however it died — including SIGKILL, which nothing in userspace can trap. Linux-only; elsewhere
# this degrades to the timeout alone, which is stated here rather than quietly assumed.

_PR_SET_PDEATHSIG = 1


def _die_with_parent() -> None:
    """`preexec_fn`: put the child in its own process group AND arm PDEATHSIG.

    Both, because they cover different halves. The new session is what lets a timeout reap the
    child's whole SUBTREE (`bd` starts work of its own; signalling only `bd` leaves that behind).
    PDEATHSIG covers the caller being killed. Best-effort by construction — on a platform with no
    `prctl` the child simply starts as it always did, and the timeout still applies.
    """
    with contextlib.suppress(Exception):
        os.setsid()
    with contextlib.suppress(Exception):
        import ctypes

        ctypes.CDLL("libc.so.6", use_errno=True).prctl(_PR_SET_PDEATHSIG, signal.SIGTERM)


def _reap_group(proc) -> None:
    """SIGTERM the child's process GROUP, then SIGKILL whatever is still there.

    The group, not the pid: `bd` starts work of its own. SIGTERM first because the incident's own
    recovery probe reaped 27 of these with SIGTERM alone and zero SIGKILLs — they are reapable,
    nobody was signalling them — and because bd flushes pending batch commits on SIGTERM.
    """
    for sig, wait in ((signal.SIGTERM, 5.0), (signal.SIGKILL, 5.0)):
        with contextlib.suppress(Exception):
            os.killpg(os.getpgid(proc.pid), sig)
        try:
            proc.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue


class ChildTimeout(RuntimeError):
    """A bounded child exceeded its timeout and was terminated (bh-toitp).

    An EXCEPTION rather than a non-zero CompletedProcess on purpose: this is the one outcome that
    must not be mistakable for an answer. The measured consequence was a cross-hive hydration
    that "silently did not happen", taking an escalation with it — the report that incident was
    carrying was very likely never filed."""


def bounded(cmd, *, timeout: float, label: str = "", capture=False, env=None, cwd=None):
    """Run `cmd` under a hard wall-clock bound, in its own process group, armed to die with bh.

    On expiry the whole child group is reaped and :class:`ChildTimeout` is raised NAMING the
    command and the bound — bh-toitp's first acceptance criterion ("terminated and reported as a
    failure naming the hive and the verb — not left running and not silently dropped").

    Otherwise it matches :func:`run`: the environment is CONSTRUCTED the same way, and a missing
    binary still comes back as exit 127 rather than a raised FileNotFoundError.
    """
    with _span(cmd):
        try:
            proc = subprocess.Popen(
                cmd,
                text=True,
                env=child_env(env),
                cwd=cwd,
                stdout=subprocess.PIPE if capture else None,
                stderr=subprocess.PIPE if capture else None,
                preexec_fn=_die_with_parent,
            )
        except FileNotFoundError:
            binary = cmd[0] if cmd else "?"
            res = subprocess.CompletedProcess(
                cmd,
                MISSING_BINARY_EXIT,
                "" if capture else None,
                f"{binary}: command not found" if capture else None,
            )
            res.bh_missing_binary = binary
            return res
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _reap_group(proc)
            raise ChildTimeout(
                f"{label or _safe_op(cmd)} exceeded {timeout:g}s and was TERMINATED "
                f"(pid {proc.pid}; its whole process group was reaped)"
            ) from None
        return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


def out(cmd, **kw):
    """Run and return stdout. Raises on non-zero unless check=False is passed."""
    return run(cmd, capture=True, **kw).stdout


def ok(cmd, **kw):
    """True iff the command exits 0 (output suppressed)."""
    return run(cmd, check=False, capture=True, **kw).returncode == 0
