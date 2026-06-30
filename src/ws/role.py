"""ws role — seat launcher and TUI statusline.

Two entry points:

* ``launch(role)`` — list available seats when role is falsy; otherwise validate the
  role against the bundled agent defs, then exec ``claude --agent <role>`` with
  ``WS_ROLE`` exported so ``config.otel_role`` tags the session correctly.

* ``statusline()`` — read Claude's TUI stdin JSON contract, derive role and rig, and
  print ``⬡ <rig> · <role>``.  NEVER raises: a statusline crash must not disrupt the
  TUI; any error prints a bare ``⬡``.

Test seam: ``run`` is imported at module level so tests can patch ``ws.role.run``
without spawning a real ``claude`` process.
"""

from __future__ import annotations

import json
import os
import sys

from .run import run  # noqa: E402 — module-level so tests can patch ws.role.run

# ---------------------------------------------------------------------------
# internal helpers
# ---------------------------------------------------------------------------


def _known_seats() -> list[str]:
    """Sorted list of seat names (stems of *.md files in agents_src)."""
    from . import config

    src = config.agents_src()
    return sorted(p.stem for p in src.glob("*.md"))


def _cwd_rig() -> str:
    """Derive rig as ``org/repo`` from cwd via workspace_identity, or return ``—``."""
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

    env = {**os.environ, "WS_ROLE": role}
    result = run(["claude", "--agent", role], check=False, capture=False, env=env)
    raise SystemExit(result.returncode)


def statusline() -> None:
    """Read stdin JSON and print ``⬡ <rig> \xb7 <role>``.

    Role resolution: ``agent.name`` in the JSON → ``WS_ROLE`` env → ``"main"``.
    Rig resolution: ``workspace.repo.{owner,name}`` → cwd-derived ``org/repo`` → ``—``.
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
            or os.environ.get("WS_ROLE", "").strip()
            or "main"
        )

        repo_block = (data.get("workspace") or {}).get("repo") or {}
        owner = (repo_block.get("owner") or "").strip()
        name = (repo_block.get("name") or "").strip()
        if owner and name:
            rig = f"{owner}/{name}"
        else:
            rig = _cwd_rig()

        print(f"⬡ {rig} \xb7 {seat}")
    except Exception:
        print("⬡")
