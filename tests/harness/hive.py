"""Rig builder: a real main clone under $GIT_WORKSPACE with embedded bd + a filesystem
git remote and a filesystem dolt remote, registered in the ws config.

Identity + signing are written **repo-local** so ws-driven git ops (which scrub
GIT_CONFIG_GLOBAL) still see them; the supervised modality inherits this human identity.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from beadhive import config

from . import beads
from .world import World, git

_DEFAULT_WORK = {"validate_cmd": "true", "review_gate": "human", "integration_branch": "main"}


@dataclass
class Hive:
    world: World
    org: str
    repo: str
    prefix: str
    main: Path
    git_remote: Path  # bare git repo (branch push target)
    dolt_remote: Path  # file:// dolt remote dir (bead-state target)


def make_hive(
    world: World,
    *,
    org="myorg",
    repo="myrepo",
    prefix="mr",
    work=None,
    chdir=True,
    with_remotes=False,
) -> Hive:
    """Build a rig. `with_remotes=True` also wires a bare git remote + a file:// dolt remote
    and publishes to them — needed ONLY by the remote-sandbox modality. The matrix modalities
    work entirely in linked worktrees, so they skip remotes (avoids a per-rig `bd dolt`
    round-trip through the shared dolt sql-server, which is what stalled the suite)."""
    main = world.ws_root / "github" / org / repo
    main.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=main)

    # repo-local human identity + ssh signing + allowed-signers (inherited by supervised worktrees)
    h = world.human
    for k, v in {
        "user.name": h.name,
        "user.email": h.email,
        "gpg.format": "ssh",
        "user.signingkey": str(h.key),
        "commit.gpgsign": "true",
        "gpg.ssh.allowedSignersFile": str(world.allowed),
    }.items():
        git("config", k, v, cwd=main)

    (main / "README.md").write_text("# hive\n")
    # bd init/bootstrap drop .beads/ + AGENTS.md etc. into the working dir; ignore them like a
    # real rig so a full-clone developer's `git add -A` never commits beads internals.
    (main / ".gitignore").write_text(".beads/\nAGENTS.md\nCLAUDE.md\n.codex/\n")
    git("add", "-A", cwd=main)
    git("commit", "-qm", "chore: init", cwd=main)

    beads.init_embedded(main, prefix)

    git_remote = world.remotes / f"{prefix}.git"
    dolt_remote = world.remotes / f"{prefix}-dolt"
    if with_remotes:
        # -b main so the bare remote's HEAD is a valid default branch; otherwise a clone of a
        # multi-branch remote with HEAD→(nonexistent) master checks out nothing → orphans.
        git("init", "-q", "--bare", "-b", "main", str(git_remote), cwd=world.remotes)
        git("remote", "add", "origin", str(git_remote), cwd=main)
        git("push", "-q", "origin", "main", cwd=main)
        beads.add_file_remote(main, dolt_remote)
        beads.push(main)

    entry = {
        "provider": "github",
        "org": org,
        "repo": repo,
        "prefix": prefix,
        "kind": "personal",
        "work": {**_DEFAULT_WORK, **(work or {})},
    }
    cfg = config.load()
    cfg.setdefault("managed_repos", []).append(entry)
    config.save(cfg)

    if chdir:
        world.chdir(main)
    return Hive(world, org, repo, prefix, main, git_remote, dolt_remote)
