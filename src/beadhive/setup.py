"""ws setup — post-installation dependency gate.

Probes for the tools ws delegates to (git-workspace, gh, bd, dolt, colima),
records the result in ~/.ws/setup-state.json on success, and surfaces the gate
check that ``_root`` in cli.py uses to guard every verb except
setup / config / doctor / --version / --help.

Cache schema (``~/.ws/setup-state.json``)::

    {
      "setup": true,
      "checked_at": "<iso8601>",
      "os": "<Darwin|Linux|…>",
      "backend": "<dolt|jsonl>",
      "tools": {
        "<name>": {"found": <bool>, "version": "<str | null>"}
      }
    }

The OS + backend tag lets later OS/backend variants extend the probe table
without changing the gate contract.
"""

from __future__ import annotations

import json
import platform
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer

from . import config

# ---- probe table ---------------------------------------------------------------

# Each entry: (name, which_binary, version_cmd)
# ``which_binary`` is the basename looked up via ``shutil.which``.
# ``version_cmd`` is the argv list used to get a version string (best-effort).
PROBE_TABLE: list[tuple[str, str, list[str]]] = [
    ("git-workspace", "git-workspace", ["git", "workspace", "--version"]),
    ("gh", "gh", ["gh", "--version"]),
    ("bd", "bd", ["bd", "--version"]),
    ("dolt", "dolt", ["dolt", "version"]),
    ("colima", "colima", ["colima", "--version"]),
]


def probe_one(name: str, which_binary: str, version_cmd: list[str]) -> dict[str, Any]:
    """Probe a single tool: check presence via ``shutil.which``, then fetch version.

    Returns ``{"found": bool, "version": str | None}``.

    Presence is determined by ``shutil.which(which_binary)`` — a missing binary
    immediately returns ``found=False``.  When found, ``version_cmd`` is run to
    get the first line of stdout/stderr; a failure there still returns ``found=True``
    with ``version=None``.

    Probe helpers are intentionally importable from this module so doctor.py can
    reuse them without duplicating the subprocess logic.
    """
    if shutil.which(which_binary) is None:
        return {"found": False, "version": None}

    try:
        result = subprocess.run(
            version_cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        out = (result.stdout or result.stderr or "").strip()
        version = out.splitlines()[0] if out else None
        return {"found": True, "version": version}
    except (OSError, subprocess.TimeoutExpired):
        return {"found": True, "version": None}


def probe_tools() -> dict[str, dict[str, Any]]:
    """Run every entry in PROBE_TABLE and return results keyed by tool name.

    Importable by doctor.py or any other module that needs to surface tool
    availability without reimplementing the probe logic.
    """
    return {name: probe_one(name, which_bin, vcmd) for name, which_bin, vcmd in PROBE_TABLE}


# ---- cache I/O -----------------------------------------------------------------


def setup_state_path() -> Path:
    """Path to the setup cache file (``~/.ws/setup-state.json``)."""
    return config.home() / "setup-state.json"


def _backend_tag(cfg: dict | None = None) -> str:
    """Derive the backend tag from config: ``dolt`` or ``jsonl``."""
    try:
        c = cfg if cfg is not None else config.load()
        backend = config.dolt_cfg(c).get("backend", "jsonl")
        return str(backend) if backend else "jsonl"
    except Exception:
        return "jsonl"


def read_cache() -> dict[str, Any] | None:
    """Read and parse the setup cache. Returns ``None`` when absent or unreadable."""
    p = setup_state_path()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def _write_cache(tools: dict[str, dict[str, Any]], success: bool) -> None:
    """Write the setup-state cache, creating ``~/.ws/`` if needed."""
    state: dict[str, Any] = {
        "setup": success,
        "checked_at": datetime.now(tz=UTC).isoformat(),
        "os": platform.system(),
        "backend": _backend_tag(),
        "tools": tools,
    }
    p = setup_state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2))


# ---- gate helper ---------------------------------------------------------------


def is_setup_complete() -> bool:
    """Return True iff the cache exists and ``setup == true``.

    Used by the ``_root`` gate in cli.py — must be cheap (one file read).
    Returns ``False`` on any read/parse error to keep the gate conservative.
    """
    cache = read_cache()
    return bool(cache and cache.get("setup") is True)


# ---- command implementations ---------------------------------------------------


def run_check() -> None:
    """Implement ``ws setup check``: probe all deps and cache the result.

    Exits 1 when one or more required deps are missing.  Re-running refreshes
    the cache even if it was previously passing.
    """
    typer.echo("Checking post-ws dependencies…")
    tools = probe_tools()

    all_found = True
    for name, result in tools.items():
        status = "✓" if result["found"] else "✗"
        version_note = f"  ({result['version']})" if result["version"] else ""
        typer.echo(f"  {status} {name}{version_note}")
        if not result["found"]:
            all_found = False

    _write_cache(tools, success=all_found)

    if all_found:
        typer.echo("✓ setup complete — cache updated.")
    else:
        missing = [n for n, r in tools.items() if not r["found"]]
        typer.echo(
            f"✗ missing: {', '.join(missing)}\n"
            "  Install the missing tools and re-run `ws setup check`.",
            err=True,
        )
        raise typer.Exit(1)


def run_show() -> None:
    """Implement ``ws setup show``: report cached status without re-probing."""
    cache = read_cache()
    if cache is None:
        typer.echo(
            "setup: not checked yet — run `ws setup check` to probe dependencies.",
            err=True,
        )
        raise typer.Exit(1)

    status = "complete" if cache.get("setup") else "incomplete"
    typer.echo(f"setup: {status}")
    typer.echo(f"  checked_at: {cache.get('checked_at', '(unknown)')}")
    typer.echo(f"  os:         {cache.get('os', '(unknown)')}")
    typer.echo(f"  backend:    {cache.get('backend', '(unknown)')}")
    typer.echo("  tools:")
    for name, result in (cache.get("tools") or {}).items():
        mark = "✓" if result.get("found") else "✗"
        ver = result.get("version") or "(version unknown)"
        typer.echo(f"    {mark} {name}: {ver if result.get('found') else 'not found'}")
