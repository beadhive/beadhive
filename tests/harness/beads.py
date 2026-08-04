"""Real `bd` (embedded Dolt) seam for the harness — no fake to maintain.

Thin wrappers around the bd binary, always scoped to a repo via `-C`. `bd` is the real
process so deps/ready/gate/merge-slot/dolt-push-pull are exercised for real.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from beadhive.run import run

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


def seed_minimal_store(repo: Path, prefix: str, *, timeout: float = 60):
    """A REAL, minimal, zero-footprint bd store at `repo/.beads` — for fixtures that need to
    simulate "this hive is already onboarded" (bh-areg.7's review, round 4): `_act_bd_init`'s
    idempotent skip keys off `hive.store_opens()`, an ACTUAL open test, not merely `.beads/`
    existing — a bare `mkdir(".beads")` is exactly the WRECKAGE state that review's own
    finding is about, not a legitimate simulation of "already initialized".

    Embedded mode (no `--shared-server`) specifically for speed and to stay independent of
    this suite's shared-server isolation: fixture seeding has no reason to need a real dolt
    sql-server. `--setup-exclude` keeps it zero-footprint (no commit, no tracked
    `.beads/config.yaml`) so it drops into an existing-folder fixture without dirtying the
    tree callers may already be asserting about; the stray tracked root `.gitignore`
    `--setup-exclude` itself still writes is discarded outright (not relocated into
    `.git/info/exclude` the way a real onboard's own zero-footprint path does) — callers here
    only need a working store, not a byte-exact simulation of that relocation, which
    `test_onboard_dag.py`'s own dedicated tests already cover."""
    run(
        [
            "bd",
            "init",
            "--prefix",
            prefix,
            "--setup-exclude",
            "--non-interactive",
            "--skip-agents",
            "--skip-hooks",
        ],
        cwd=str(repo),
        check=True,
        capture=True,
        timeout=timeout,
    )
    (repo / ".gitignore").unlink(missing_ok=True)


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
