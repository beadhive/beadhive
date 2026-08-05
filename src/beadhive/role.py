"""bh role — seat launcher and TUI statusline.

Two entry points:

* ``launch(role, harness=None)`` — list available seats when role is falsy; otherwise
  validate the role against the bundled agent defs, then exec the seat under a harness
  (``claude`` default, or ``opencode``) with ``BH_ROLE`` exported so ``config.otel_role``
  tags the session correctly. ``harness`` defaults to ``config.harness_name()`` (BH_HARNESS
  env > config) when not passed explicitly (the CLI's ``--harness`` flag). ``claude`` execs
  ``claude --agent bh:<role>`` (scoped to the bh plugin) — a local ``.claude/agents/<role>.md``
  (or ``.opencode/agents/<role>.md``) override switches to the bare ``--agent <role>`` form.
  ``opencode`` execs the bare ``opencode --agent <role>`` (interactive TUI parity; an
  orchestrator driving opencode headlessly calls ``opencode run`` directly, not this launcher).

* ``statusline()`` — read Claude's TUI stdin JSON contract, derive role and hive, and
  print ``⬡ <hive> · <role>``.  NEVER raises: a statusline crash must not disrupt the
  TUI; any error prints a bare ``⬡``.

Test seam: ``run`` is imported at module level so tests can patch ``beadhive.role.run``
without spawning a real ``claude``/``opencode`` process.

**Environment construction (bh-og0q.2).** ``launch()`` does NOT hand the harness a bare copy
of ``os.environ`` — it builds one via :func:`harness_env`. A harness bh launches does not
always inherit a login shell: on a reference Linux host bh itself is started by a systemd
unit whose service environment's ``PATH`` omits the account's user bin dir (e.g.
``~/.local/bin``), where bh's own sibling binaries (``bh``, ``bd``, ``bh-mcp``, ...) live.
The exec'd harness then cannot resolve `bh` by name even though it is present on disk — the
same gap occurs under macOS ``launchd``; it is a launch-context problem, not a platform one.

Two fixes were on the table and they are not equivalent. Fixing the service unit
(``Environment=``/``EnvironmentFile=``) is a legitimate immediate unblock for a *known* host,
but it is an ops change bh does not own and cannot guarantee everywhere. The fix taken here is
the other one: bh constructs the environment it hands to a harness it launches, rather than
blindly inheriting whatever started bh itself. That is owned by bh and works regardless of how
bh was started. Concretely, :func:`harness_env` resolves the directory bh's own console-script
shim was invoked from (``sys.argv[0]``, resolved) and ensures it is on the exec'd ``PATH`` —
that directory is where a process already knows it can find `bh`, and per field evidence is
where colocated sibling tools installed the same way live too. This does not touch preflight
(agent-hitch's, in a different repo) and does not add the directory to any pack's
requirements — the environment was wrong, not the check.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from . import deps as deps_mod  # import-cheap by design (bh-hsus.2/.3) — safe at module level
from .run import run  # noqa: E402 — module-level so tests can patch ws.role.run

# ---------------------------------------------------------------------------
# internal helpers
# ---------------------------------------------------------------------------


def _known_seats() -> list[str]:
    """Sorted list of seat names (stems of *.md files in agents_src).

    Resolves the current roles/RBAC matrix seat set from the bundled agent defs —
    dispatcher / developer / reviewer / merger (Integration), planner / analyst (Planning),
    supervisor / director / custodian / controller (Control). Purely glob-driven, so retiring
    a def (e.g. the folded epic-coordinator[-deep]) or adding one needs no change here."""
    from . import config

    src = config.agents_src()
    return sorted(p.stem for p in src.glob("*.md"))


def _local_agent_override(seat: str) -> bool:
    """True when a local ``.claude/agents/<seat>.md`` or ``.opencode/agents/<seat>.md`` exists
    in the current directory tree.

    A local file outranks the plugin — returning True causes launch() to use the bare
    ``claude --agent <seat>`` form so the override is honoured."""
    return (Path(".claude") / "agents" / f"{seat}.md").is_file() or (
        Path(".opencode") / "agents" / f"{seat}.md"
    ).is_file()


def _plugin_name() -> str:
    """Resolve the configured plugin name, falling back to 'bh' when config is absent."""
    try:
        from . import config

        return config.claude_plugin_name(config.load())
    except Exception:
        return "bh"


def _harness_name() -> str:
    """Resolve the configured harness, falling back to 'claude' when config is absent."""
    try:
        from . import config

        return config.harness_name(config.load())
    except Exception:
        return "claude"


def _resolve_agent_arg(seat: str, plugin: str) -> str:
    """Return the ``--agent`` argument for claude.

    Returns ``plugin:seat`` (scoped) unless a local ``.claude/agents/<seat>.md`` (or
    ``.opencode/agents/<seat>.md``) exists, in which case the bare ``seat`` form is returned
    so local overrides win."""
    if _local_agent_override(seat):
        return seat
    return f"{plugin}:{seat}"


# Known harness names — anything else is rejected by launch() with a non-zero exit. Derived
# from `deps.seat_runners()` (bh-hsus.5), not hand-mirrored: `bh role --harness codex` is now
# rejected because codex genuinely cannot exec a seat (docs/spikes/bh-hsus.2-dependency-table.md
# § Q1), rather than by an unrelated hand-written tuple that happened to agree with that fact.
KNOWN_HARNESSES = tuple(d.name for d in deps_mod.seat_runners())


def _harness_argv(harness: str, seat: str) -> list[str]:
    """Build the exec argv for *seat* under *harness*.

    ``claude`` (default): scoped ``claude --agent <plugin>:<seat>``, or the bare
    ``claude --agent <seat>`` form when a local agent override exists (unchanged behaviour).
    ``opencode``: bare ``opencode --agent <seat>`` — interactive TUI parity; an orchestrator
    driving opencode headlessly calls ``opencode run`` directly, not this launcher."""
    if harness == "opencode":
        return ["opencode", "--agent", seat]
    plugin = _plugin_name()
    return ["claude", "--agent", _resolve_agent_arg(seat, plugin)]


def _bh_bin_dir() -> Path | None:
    """The directory bh's own console-script shim was invoked from, or ``None`` if it can't be
    resolved (e.g. ``python -m beadhive.cli``, or a non-file ``sys.argv[0]``).

    ``sys.argv[0]`` is the path the running process was exec'd with — a systemd/launchd
    ``ExecStart``/``ProgramArguments`` entry names bh by an absolute path even when the
    service environment's ``PATH`` is minimal, so this is stable evidence of a directory that
    can resolve bh, independent of the ambient ``PATH`` (bh-og0q.2)."""
    argv0 = sys.argv[0] if sys.argv else ""
    if not argv0:
        return None
    resolved = Path(argv0).resolve()
    return resolved.parent if resolved.is_file() else None


def harness_env(role: str) -> dict[str, str]:
    """The environment for the harness ``launch()`` exec's: ``os.environ`` plus ``BH_ROLE``,
    with bh's own bin directory (see :func:`_bh_bin_dir`) ensured on ``PATH``.

    bh does not hand the harness a bare copy of whatever launched bh itself — see the module
    docstring for why (bh-og0q.2). When the bin dir can't be resolved, or is already on
    ``PATH``, this is exactly the old inherit-``os.environ`` behavior."""
    env = {**os.environ, "BH_ROLE": role}
    bin_dir = _bh_bin_dir()
    if bin_dir is None:
        return env
    path_dirs = env.get("PATH", "").split(os.pathsep) if env.get("PATH") else []
    if str(bin_dir) not in path_dirs:
        env["PATH"] = os.pathsep.join([str(bin_dir), *path_dirs])
    return env


def _cwd_hive() -> str:
    """Derive hive as ``org/repo`` from cwd via workspace_identity, or return ``—``."""
    try:
        from .identity import workspace_identity

        parts = workspace_identity()
        if parts:
            _provider, org, repo = parts
            return f"{org}/{repo}"
    except Exception:
        pass
    return "—"  # em dash fallback


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------


def launch(role: str, harness: str | None = None) -> None:
    """Launch *role* under a harness, or list available seats when role is falsy.

    Validates *role* against the bundled agent defs.  Unknown seats print a
    friendly error (with the known-seat list) and exit non-zero.  *harness*
    defaults to ``config.harness_name()`` (``BH_HARNESS`` env > per-hive config >
    global config > ``"claude"``) when not passed explicitly — the CLI's
    ``--harness`` flag passes one through. An unknown harness prints a friendly
    error and exits non-zero. On a valid role + harness, execs the seat with
    inherited stdio (interactive hand-over) and propagates the exit code.
    """
    seats = _known_seats()

    if not role:
        print("Available seats:")
        for seat in seats:
            print(f"  {seat}")
        return

    if role not in seats:
        known = ", ".join(seats)
        print(f"✗ unknown role {role!r}. Known seats: {known}", file=sys.stderr)
        raise SystemExit(1)

    harness = harness or _harness_name()
    if harness not in KNOWN_HARNESSES:
        known = ", ".join(KNOWN_HARNESSES)
        print(f"✗ unknown harness {harness!r}. Known harnesses: {known}", file=sys.stderr)
        raise SystemExit(1)

    # A harness the image does not ship must diagnose ITSELF. bh-pc2a.36 stopped baking the
    # proprietary one, so "known harness, absent from PATH" is now an ordinary state — and
    # exec'ing it anyway yields a bare `claude: command not found`, which is true and points
    # nowhere. That is the bh-pc2a.33 failure exactly: a correct message aimed at the wrong fix.
    #
    # bh-hsus.5: this guard used to key off `harness.HARNESSES` — the rows with a bh-known
    # install route — and skip itself whenever `harness` wasn't one of those keys. opencode
    # can run a seat (it's in KNOWN_HARNESSES) but has no install route, so it was NEVER in
    # `HARNESSES`, and an absent opencode fell straight through this guard into `run()`,
    # reproducing the exact bh-pc2a.33 failure one call site over: a bare
    # `opencode: command not found` from the exec itself. The guard must fire for every
    # SEAT-CAPABLE harness — every member of KNOWN_HARNESSES, i.e. `deps.seat_runners()` — not
    # only the strict subset bh also knows how to install. `deps.by_name` is safe unguarded
    # here: `harness` was already checked against KNOWN_HARNESSES above, and every seat runner
    # is a row in `deps.DEPS`.
    from . import harness as harness_mod

    dep = deps_mod.by_name(harness)
    if harness_mod.installed_path(dep) is None:
        print(harness_mod.missing_hint(harness), file=sys.stderr)
        raise SystemExit(1)

    argv = _harness_argv(harness, role)
    env = harness_env(role)
    result = run(argv, check=False, capture=False, env=env)
    raise SystemExit(result.returncode)


def statusline() -> None:
    """Read stdin JSON and print ``⬡ <hive> \xb7 <role>``.

    Role resolution: ``agent.name`` in the JSON → ``BH_ROLE`` env → ``"main"``.
    Hive resolution: ``workspace.repo.{owner,name}`` → cwd-derived ``org/repo`` → ``—``.
    Any exception (bad JSON, import error, etc.) is silently swallowed and a bare
    ``⬡`` is printed so the TUI is never disrupted.
    """
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            raise ValueError("empty stdin")
        data: dict = json.loads(raw)

        seat = (
            ((data.get("agent") or {}).get("name") or "").strip()
            or os.environ.get("BH_ROLE", "").strip()
            or "main"
        )

        repo_block = (data.get("workspace") or {}).get("repo") or {}
        owner = (repo_block.get("owner") or "").strip()
        name = (repo_block.get("name") or "").strip()
        if owner and name:
            hive = f"{owner}/{name}"
        else:
            hive = _cwd_hive()

        print(f"⬡ {hive} \xb7 {seat}")
    except Exception:
        print("⬡")
