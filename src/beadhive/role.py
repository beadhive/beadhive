"""bh role — seat launcher and TUI statusline.

Two entry points:

* ``launch(role)`` — list available seats when role is falsy; otherwise validate the
  role against the bundled agent defs, then exec ``claude --agent bh:<role>`` (scoped
  to the bh plugin) with ``BH_ROLE`` exported so ``config.otel_role`` tags the session
  correctly.  If a local ``.claude/agents/<role>.md`` file exists, the bare form
  ``claude --agent <role>`` is used instead so local overrides still win.

* ``statusline()`` — read Claude's TUI stdin JSON contract, derive role and hive, and
  print ``⬡ <hive> · <role>``.  NEVER raises: a statusline crash must not disrupt the
  TUI; any error prints a bare ``⬡``.

Test seam: ``run`` is imported at module level so tests can patch ``beadhive.role.run``
without spawning a real ``claude`` process.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

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
    """True when a local .claude/agents/<seat>.md exists in the current directory tree.

    A local file outranks the plugin — returning True causes launch() to use the bare
    ``claude --agent <seat>`` form so the override is honoured."""
    return (Path(".claude") / "agents" / f"{seat}.md").is_file()


def _plugin_name() -> str:
    """Resolve the configured plugin name, falling back to 'bh' when config is absent."""
    try:
        from . import config

        return config.claude_plugin_name(config.load())
    except Exception:
        return "bh"


def _resolve_agent_arg(seat: str, plugin: str) -> str:
    """Return the ``--agent`` argument for claude.

    Returns ``plugin:seat`` (scoped) unless a local ``.claude/agents/<seat>.md`` exists,
    in which case the bare ``seat`` form is returned so local overrides win."""
    if _local_agent_override(seat):
        return seat
    return f"{plugin}:{seat}"


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


def launch(role: str) -> None:
    """Launch claude in *role*, or list available seats when role is falsy.

    Validates *role* against the bundled agent defs.  Unknown seats print a
    friendly error (with the known-seat list) and exit non-zero.  On a valid
    role, execs ``claude --agent <role>`` with inherited stdio (interactive
    hand-over) and propagates claude's exit code.
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

    plugin = _plugin_name()
    agent_arg = _resolve_agent_arg(role, plugin)
    env = {**os.environ, "BH_ROLE": role}
    result = run(["claude", "--agent", agent_arg], check=False, capture=False, env=env)
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
