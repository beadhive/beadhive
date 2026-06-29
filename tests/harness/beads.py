"""Real `bd` (embedded Dolt) seam for the harness — no fake to maintain.

Thin wrappers around the bd binary, always scoped to a repo via `-C`. `bd` is the real
process so deps/ready/gate/merge-slot/dolt-push-pull are exercised for real.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from ws.run import run

skip_if_no_bd = pytest.mark.skipif(shutil.which("bd") is None, reason="bd not installed")


# Dolt ops route through a shared dolt sql-server; cap them so a contended/wedged server
# surfaces as a fast, clear test failure instead of an indefinite hang.
_DOLT_TIMEOUT = 120


def bd(*args, cwd: Path, check=True, capture=False, actor: str = "", timeout=None):
    cmd = ["bd", "-C", str(cwd)]
    if actor:
        cmd += ["--actor", actor]
    cmd += [str(a) for a in args]
    return run(cmd, check=check, capture=capture, timeout=timeout)


def bd_json(*args, cwd: Path):
    res = bd(*args, "--json", cwd=cwd, check=False, capture=True)
    if res.returncode != 0:
        return None
    try:
        return json.loads(res.stdout or "null")
    except json.JSONDecodeError:
        return None


def init_embedded(repo: Path, prefix: str):
    """Initialize an embedded-Dolt bd database in `repo`. `init` runs with cwd (not -C):
    -C requires an existing project, but init is what creates it."""
    run(["bd", "init", "--prefix", prefix, "--quiet"], cwd=str(repo), check=True, capture=True)


def add_file_remote(repo: Path, remote_dir: Path, name: str = "origin"):
    """Wire a filesystem dolt remote (file://) for serverless push/pull."""
    url = f"file://{remote_dir}"
    bd("dolt", "remote", "add", name, url, cwd=repo, capture=True, timeout=_DOLT_TIMEOUT)


def push(repo: Path, name: str = "origin"):
    bd("dolt", "push", "--remote", name, cwd=repo, capture=True, timeout=_DOLT_TIMEOUT)


def pull(repo: Path, name: str = "origin"):
    bd("dolt", "pull", "--remote", name, cwd=repo, check=False, capture=True, timeout=_DOLT_TIMEOUT)


def create(repo: Path, title: str, *, type_="task", priority=2) -> str:
    """Create a bead, return its id (quick-capture emits only the id)."""
    res = bd("q", title, cwd=repo, capture=True)
    return (res.stdout or "").strip().splitlines()[-1].strip()


def dep_add(repo: Path, child: str, parent: str):
    """`child` depends on `parent` (parent blocks child)."""
    bd("dep", "add", child, parent, cwd=repo, capture=True)


def ready_ids(repo: Path) -> list[str]:
    data = bd_json("ready", "--limit", "0", cwd=repo) or []
    return [i.get("id") for i in data if i.get("id")]


def status(repo: Path, bead: str) -> dict:
    data = bd_json("show", bead, cwd=repo)
    if isinstance(data, list):
        data = data[0] if data else {}
    return data or {}


def resolve_gates(repo: Path, bead: str):
    """Approve: resolve any open gate blocking `bead` (the gate names it in its description)."""
    for g in bd_json("gate", "list", cwd=repo) or []:
        if g.get("status") == "open" and bead in (g.get("description") or ""):
            bd("gate", "resolve", g["id"], cwd=repo, check=False, capture=True)


def close(repo: Path, bead: str, *, actor: str = "", reason: str = "merged"):
    bd("close", bead, "--reason", reason, cwd=repo, actor=actor, capture=True)
